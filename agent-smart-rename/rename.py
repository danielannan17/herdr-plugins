#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "pydantic>=2"]
# ///
"""Name one herdr agent session from what it is currently doing.

Invoked by herdr as an event hook (pane.agent_detected, pane.agent_status_changed,
pane.exited) and as plugin actions. Always exits 0: a naming plugin must never
break the multiplexer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

# What the sidebar shows: an imperative English phrase on one 26-column row,
# written to the pane label, which takes arbitrary text.
MAX_LABEL_CHARS = 26
MIN_LABEL_WORDS = 2
MAX_LABEL_WORDS = 5

# What the CLI targets: herdr enforces ^[a-z][a-z0-9_-]{0,31}$ on agent names,
# so the same phrase is slugified into an alias you can `herdr agent send` to.
MAX_NAME_CHARS = 32
RENAME_FLOOR_SECONDS = 60
STALE_LOCK_SECONDS = 120

# A long session drifts onto new work while its title goes stale.
RENAME_TOKEN_STEP = 100_000
# Claude Code compacts a long conversation; a context that fell this far below
# the naming baseline has been compacted even when the transcript marker has
# scrolled out of the tail.
COMPACTION_MIN_DROP = 50_000
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
TRANSCRIPT_TAIL_BYTES = 400_000
# Reading for messages and titles stops at the message counts below or at this many bytes.
TRANSCRIPT_SCAN_CAP_BYTES = 8_000_000
TRANSCRIPT_CHUNK_BYTES = 256_000
USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

EVIDENCE_CHAR_CAP = 6000
FIRST_MESSAGE_COUNT = 5
LATEST_MESSAGE_COUNT = 15
USER_MESSAGE_CHAR_CAP = 200
LOG_BYTE_CAP = 64_000

AGENT_LABELS = {
    "claude", "codex", "cursor", "copilot", "devin", "droid", "kilo", "kimi",
    "pi", "omp", "hermes", "qodercli", "mastracode", "aider", "goose", "amp",
}

# User records Claude Code writes that the user did not type: slash-command and
# `!` shell echoes, reminders, background-task notices.
NON_TYPED_MESSAGE = re.compile(
    r"<(command-name|command-message|local-command-stdout|local-command-stderr|local-command-caveat"
    r"|bash-input|bash-stdout|bash-stderr|task-notification|system-reminder)\b",
    re.I,
)

# Spinner glyphs, elapsed timers and token counters churn constantly; a title
# that differs only in these has not materially changed.
TITLE_NOISE = [
    re.compile(r"[⠀-⣿✢✳●○◐-◓✶✹]"),
    re.compile(r"\(\s*\d+[smh](?:\s*\d+[smh])?\s*(?:·[^)]*)?\)"),
    re.compile(r"[↑↓⇡⇣]\s*[\d.,]+\s*[kKmM]?\s*tokens?"),
    re.compile(r"\b\d+[smh]\b"),
    re.compile(r"…|\.{2,}"),
]

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(
        r"(?i)\b(\w*(?:token|secret|password|passwd|api[_-]?key)\w*)\s*[:=]\s*\S+"
    ),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
]

HERDR_NAME_RULE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

PROVIDERS = ("openai", "claude")
CLAUDE_DEFAULT_MODEL = "haiku"
CLAUDE_INSTALL_PATHS = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.claude/local/claude",
)
# Claude Code reads CLAUDE_* variables to find plugins, sessions and its parent
# process. Only these two select the account the CLI authenticates as.
CLAUDE_ENV_KEEP = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
SETTING_KEYS = {
    "OPENAI_API_KEY", "SMART_RENAME_MODEL", "SMART_RENAME_BASE_URL", "SMART_RENAME_TIMEOUT_MS",
    "SMART_RENAME_PROVIDER", "SMART_RENAME_CLAUDE_MODEL", "SMART_RENAME_CLAUDE_BIN",
}


class Config(BaseModel):
    provider: str = "openai"
    api_key: str = ""
    model: str
    base_url: str = ""
    timeout_ms: int
    claude_bin: str | None = None


class Stamp(BaseModel):
    label_set: str | None = None
    name_set: str | None = None
    title_hash: str = ""
    agent_session_id: str | None = None
    set_at: int = 0
    manual: bool = False
    tokens_at_naming: int | None = None
    # label_set is the title-derived stand-in, not a model-generated label.
    fallback: bool = False
    # What this plugin last wrote to the pane's tab, and whether the tab turned
    # out to carry a name the user chose (never retried).
    tab_label_set: str | None = None
    tab_manual: bool = False
    # The terminal title at the last run: herdr-bar titles tabs from it.
    title_seen: str | None = None


class Evidence(BaseModel):
    agent: str
    workspace_label: str
    cwd: str
    branch: str | None
    title: str | None
    custom_title: str | None = None
    ai_title: str | None = None
    first_messages: list[str] = []
    latest_messages: list[str] = []


class NameResponse(BaseModel):
    name: str | None = None
    reason: str | None = None


def normalise_title(title: str) -> str:
    """Strip the parts of an OSC title that churn without the task changing."""
    text = title
    for pattern in TITLE_NOISE:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def title_hash(title: str | None) -> str:
    return sha256(normalise_title(title or "").encode()).hexdigest()[:16]


def title_is_junk(title: str | None, cwd: str, branch: str | None) -> bool:
    """True when a title carries no task information — codex publishes its branch."""
    normalised = normalise_title(title or "")
    if not normalised:
        return True
    if normalised in {Path(cwd).name.lower(), (branch or "").lower()}:
        return True
    if normalised.startswith(("/", "~/", "./")) or normalised in {"bash", "zsh", "fish", "sh"}:
        return True
    return len(normalised) < 6


def redact(text: str, home: str) -> str:
    """Remove secret-shaped values and absolute home paths before the text leaves the machine."""
    cleaned = text.replace(home, "~") if home else text
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            cleaned = pattern.sub(lambda m: f"{m.group(1)}=[redacted]", cleaned)
        else:
            cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned


def cap_tail(text: str, limit: int = EVIDENCE_CHAR_CAP) -> str:
    """Keep the most recent end of the text, which is where the current task lives."""
    return text if len(text) <= limit else text[-limit:]


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def fallback_label(title: str | None) -> str | None:
    """The terminal title cut to the sidebar width at a word boundary."""
    cleaned = re.sub(r"\s+", " ", TITLE_NOISE[0].sub(" ", title or "")).strip()
    if not cleaned:
        return None
    label = ""
    for word in cleaned.split():
        candidate = f"{label} {word}" if label else word
        if display_width(candidate) > MAX_LABEL_CHARS:
            break
        label = candidate
    if label:
        return label
    first = ""
    for char in cleaned.split()[0]:
        if display_width(first + char) > MAX_LABEL_CHARS:
            break
        first += char
    return first


def evidence_is_thin(evidence: Evidence) -> bool:
    if evidence.title and not title_is_junk(evidence.title, evidence.cwd, evidence.branch):
        return False
    return not (
        evidence.custom_title or evidence.ai_title or evidence.first_messages or evidence.latest_messages
    )


def message_lines(messages: list[str], home: str) -> str:
    return "\n".join(
        "- " + redact(" ".join(message.split()), home)[:USER_MESSAGE_CHAR_CAP] for message in messages
    )


def render_evidence(evidence: Evidence, home: str) -> str:
    """Build the sanitised block sent to the model."""
    parts = [
        f"agent: {evidence.agent}",
        f"workspace: {evidence.workspace_label}",
        f"cwd: {redact(evidence.cwd, home)}",
    ]
    if evidence.branch:
        parts.append(f"branch: {evidence.branch}")
    if evidence.custom_title:
        parts.append(f"session title, set by the user: {redact(evidence.custom_title, home)}")
    if evidence.ai_title:
        parts.append(f"session title, auto-generated: {redact(evidence.ai_title, home)}")
    if evidence.title and not title_is_junk(evidence.title, evidence.cwd, evidence.branch):
        parts.append(f"terminal title: {redact(evidence.title, home)}")
    if evidence.first_messages:
        parts.append(
            "the user's first messages (how the session started):\n"
            + message_lines(evidence.first_messages, home)
        )
    if evidence.latest_messages:
        parts.append(
            "the user's latest messages, oldest first:\n"
            + cap_tail(message_lines(evidence.latest_messages, home), EVIDENCE_CHAR_CAP)
        )
    return "\n".join(parts)


def slugify(text: str) -> str:
    """Coerce free text into herdr's agent-name shape, tolerating Title Cased model output."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return re.sub(r"^[^a-z]+", "", slug)


def validate_label(raw: str | None) -> str | None:
    """Return an imperative phrase that fits one sidebar row, or None to abstain."""
    if not raw:
        return None
    label = " ".join(raw.strip().strip("\"'").split())
    if not label or len(label) > MAX_LABEL_CHARS or not label[:1].isalpha():
        return None
    words = label.split()
    if not MIN_LABEL_WORDS <= len(words) <= MAX_LABEL_WORDS:
        return None
    if any(word.lower().strip(".,:;") in AGENT_LABELS for word in words):
        return None
    return label[0].upper() + label[1:]


def name_for(label: str) -> str | None:
    """Slugify a label into the CLI alias herdr will accept, or None if it cannot be one."""
    slug = slugify(label)[:MAX_NAME_CHARS].strip("-")
    return slug if slug and HERDR_NAME_RULE.match(slug) else None


def disambiguate(name: str, workspace_label: str, attempt: int) -> str:
    """Produce an alternative slug when one is already taken elsewhere in the session."""
    if attempt == 1 and workspace_label:
        prefix = slugify(workspace_label).split("-")[0]
        if prefix:
            return f"{prefix}-{name}"[:MAX_NAME_CHARS].strip("-")
    suffix = f"-{attempt + 1}"
    return name[: MAX_NAME_CHARS - len(suffix)].strip("-") + suffix


def has_dropped_after_compaction(baseline: int, tokens: int) -> bool:
    return baseline - tokens >= COMPACTION_MIN_DROP and tokens * 2 <= baseline


def decide(
    stamp: Stamp | None,
    live_label: str | None,
    live_session_id: str | None,
    current_title_hash: str,
    now: int,
    force: bool = False,
    tokens: int | None = None,
    compacted_at: float | None = None,
) -> str:
    """Verdict for one pane: rename, adopt-manual, skip-manual, skip-unchanged or skip-floor."""
    if force:
        return "rename"
    if stamp is None:
        return "rename"
    if stamp.manual:
        return "skip-manual"
    if stamp.agent_session_id != live_session_id:
        return "rename"
    if live_label and live_label != stamp.label_set:
        return "adopt-manual"
    if compacted_at is not None and compacted_at > stamp.set_at:
        return "rename"
    # A stamp written before this pane had a baseline must not read as 100k behind.
    if tokens is not None and stamp.tokens_at_naming is not None:
        if tokens - stamp.tokens_at_naming >= RENAME_TOKEN_STEP:
            return "rename"
        if has_dropped_after_compaction(stamp.tokens_at_naming, tokens):
            return "rename"
    if live_label and stamp.title_hash == current_title_hash and not stamp.fallback:
        return "skip-unchanged"
    if now - stamp.set_at < RENAME_FLOOR_SECONDS:
        return "skip-floor"
    return "rename"


def extract_name(content: str) -> str | None:
    """Pull the name out of a model reply, tolerating prose around the JSON object."""
    start = content.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(content)):
            if content[index] == "{":
                depth += 1
            elif content[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = NameResponse.model_validate_json(content[start : index + 1])
                    except (ValidationError, ValueError):
                        break
                    return parsed.name
        start = content.find("{", start + 1)
    return None


def state_dir() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_STATE_DIR", "/tmp/agent-smart-rename"))


def config_dir() -> Path:
    return Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR", state_dir()))


def log(message: str) -> None:
    """Append a diagnostic line, rotating the file once it passes its size cap.

    Runs overlap, so this appends rather than rewriting and losing their lines.
    """
    path = state_dir() / "rename.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_BYTE_CAP:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def stamp_path(pane_id: str) -> Path:
    return state_dir() / "panes" / f"{pane_id.replace(':', '-')}.json"


def read_stamp(pane_id: str) -> Stamp | None:
    try:
        return Stamp.model_validate_json(stamp_path(pane_id).read_text())
    except (OSError, ValidationError, ValueError):
        return None


def write_stamp(pane_id: str, stamp: Stamp) -> None:
    path = stamp_path(pane_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stamp.model_dump_json())
    except OSError as error:
        log(f"stamp write failed for {pane_id}: {error}")


def lock_path(pane_id: str) -> Path:
    return state_dir() / "locks" / f"{pane_id.replace(':', '-')}.lock"


def acquire_lock(pane_id: str, now: int) -> bool:
    """Take a per-pane lock so concurrent firings cannot both call the model."""
    path = lock_path(pane_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and now - int(path.stat().st_mtime) > STALE_LOCK_SECONDS:
            path.unlink(missing_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(handle)
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def release_lock(pane_id: str) -> None:
    lock_path(pane_id).unlink(missing_ok=True)


def herdr(*args: str) -> dict | None:
    """Return the result payload of a herdr CLI call."""
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        completed = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"herdr {' '.join(args)} failed: {error}")
        return None
    if completed.returncode != 0:
        return {"error": completed.stdout.strip() or completed.stderr.strip()}
    try:
        return json.loads(completed.stdout).get("result")
    except (json.JSONDecodeError, AttributeError):
        return None


def git_branch(cwd: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = completed.stdout.strip()
    return branch if completed.returncode == 0 and branch else None


def pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def resolve_session(agent_info: dict) -> str | None:
    """The agent's session id: herdr's own, else the one Claude Code's session registry implies.

    herdr reports no session for some Claude panes (an attached background session,
    a pane that was already open). The registry lists every live session with its
    cwd and display name, so the pane's session is the unclaimed live one in its cwd.
    """
    reported = (agent_info.get("agent_session") or {}).get("value")
    if reported or agent_info.get("agent") != "claude":
        return reported
    pane_id = agent_info.get("pane_id")
    agents = (herdr("agent", "list") or {}).get("agents") or []
    claimed = {
        (other.get("agent_session") or {}).get("value")
        for other in agents if other.get("pane_id") != pane_id
    }
    candidates = []
    for path in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (
            isinstance(entry, dict)
            and entry.get("entrypoint") == "cli"
            and entry.get("cwd") == agent_info.get("cwd")
            and entry.get("sessionId") not in claimed
            and pid_alive(entry.get("pid"))
        ):
            candidates.append(entry)
    if len(candidates) > 1:
        title = normalise_title(agent_info.get("terminal_title_stripped") or "")
        candidates = [c for c in candidates if title and normalise_title(c.get("name") or "") == title]
    return candidates[0]["sessionId"] if len(candidates) == 1 else None


def transcript_path(session_id: str | None) -> Path | None:
    if not session_id:
        return None
    transcripts = list(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return transcripts[0] if transcripts else None


def transcript_tail_lines(session_id: str | None) -> list[str]:
    """The last lines of a Claude session's transcript, oldest first; empty when there is none."""
    path = transcript_path(session_id)
    if not path:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def parse_entry(line: str) -> dict:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return entry if isinstance(entry, dict) else {}


def is_compact_boundary(entry: dict) -> bool:
    return entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"


def context_tokens(session_id: str | None) -> int | None:
    """Size of a Claude session's context, or None when there is no transcript.

    herdr publishes no token counts, so this reads the transcript the agent wrote.
    A compaction leaves no usage until the next reply, so the figures before the
    latest boundary describe a context that no longer exists and read as unknown.
    """
    for line in reversed(transcript_tail_lines(session_id)):
        entry = parse_entry(line)
        if is_compact_boundary(entry):
            return None
        usage = entry.get("message", {}).get("usage") if isinstance(entry.get("message"), dict) else None
        if usage:
            return sum(usage.get(field, 0) for field in USAGE_FIELDS)
    return None


def last_compaction(session_id: str | None) -> float | None:
    """When Claude Code last compacted this session (epoch seconds), if the transcript tail shows it."""
    for line in reversed(transcript_tail_lines(session_id)):
        entry = parse_entry(line)
        if not is_compact_boundary(entry):
            continue
        try:
            stamped = datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            return None
        return stamped.timestamp()
    return None


def stripped(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def typed_message(entry: dict) -> str:
    """What the user typed in a transcript record, or "" when the record is not a typed message."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return ""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(
            block.get("text") or "" for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    text = stripped(content)
    return "" if NON_TYPED_MESSAGE.match(text) else text


def title_records(entry: dict) -> dict[str, str]:
    """The session title a transcript record carries, keyed "ai_title" or "custom_title"."""
    fields = {"ai-title": ("ai_title", "aiTitle"), "custom-title": ("custom_title", "customTitle")}
    key, field = fields.get(entry.get("type"), (None, None))
    title = stripped(entry.get(field)) if key else ""
    return {key: title} if title else {}


def scan_transcript_head(handle) -> tuple[list[str], dict[str, str], int]:
    """The first typed messages and the titles seen on the way, and the offset the scan stopped at."""
    messages: list[str] = []
    titles: dict[str, str] = {}
    offset = 0
    for line in handle:
        offset += len(line)
        entry = parse_entry(line.decode("utf-8", "replace"))
        titles.update(title_records(entry))
        text = typed_message(entry)
        if text:
            messages.append(text)
        if len(messages) >= FIRST_MESSAGE_COUNT or offset >= TRANSCRIPT_SCAN_CAP_BYTES:
            break
    return messages, titles, offset


def scan_transcript_tail(handle, size: int, floor: int) -> tuple[list[str], dict[str, str]]:
    """The latest typed messages, oldest first, and the latest titles, reading backwards from the end.

    Stops at `floor`, which is always a line boundary, so nothing the head scan took is read again.
    """
    found: list[str] = []
    titles: dict[str, str] = {}
    position = size
    carry = b""
    while position > floor and len(found) < LATEST_MESSAGE_COUNT and size - position < TRANSCRIPT_SCAN_CAP_BYTES:
        start = max(floor, position - TRANSCRIPT_CHUNK_BYTES)
        handle.seek(start)
        lines = (handle.read(position - start) + carry).split(b"\n")
        position = start
        # Unless the chunk starts at a line boundary its first line is only the end of a record.
        carry = lines.pop(0) if start > floor else b""
        for line in reversed(lines):
            entry = parse_entry(line.decode("utf-8", "replace"))
            for key, title in title_records(entry).items():
                titles.setdefault(key, title)
            text = typed_message(entry)
            if text:
                found.append(text)
    return found[:LATEST_MESSAGE_COUNT][::-1], titles


def session_history(session_id: str | None) -> dict:
    """What a Claude session's transcript says about its goal.

    The first typed messages come from the start of the file and the latest ones and the session titles
    from its end. The transcript is mostly tool output, so both reads go by message count, not bytes.
    """
    history: dict = {"ai_title": None, "custom_title": None, "first_messages": [], "latest_messages": []}
    path = transcript_path(session_id)
    if not path:
        return history
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            first, head_titles, head_end = scan_transcript_head(handle)
            latest, tail_titles = scan_transcript_tail(handle, size, head_end)
    except OSError:
        return history
    titles = {**head_titles, **tail_titles}
    history.update(
        ai_title=titles.get("ai_title"),
        custom_title=titles.get("custom_title"),
        first_messages=first,
        latest_messages=latest,
    )
    return history


def read_settings() -> dict[str, str]:
    """Read provider.env, letting the process environment win."""
    values: dict[str, str] = {}
    path = config_dir() / "provider.env"
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update({k: v for k, v in os.environ.items() if k in SETTING_KEYS and v})
    return values


def provider_name(values: dict[str, str]) -> str:
    return values.get("SMART_RENAME_PROVIDER", "openai").strip().lower()


def load_config() -> Config | None:
    """Validate the settings for the chosen provider, or return None when unusable."""
    values = read_settings()
    provider = provider_name(values)
    timeout_ms = int(values.get("SMART_RENAME_TIMEOUT_MS", "20000"))
    if provider == "claude":
        return Config(
            provider="claude",
            model=values.get("SMART_RENAME_CLAUDE_MODEL", CLAUDE_DEFAULT_MODEL),
            timeout_ms=timeout_ms,
            claude_bin=values.get("SMART_RENAME_CLAUDE_BIN"),
        )
    if provider != "openai" or not values.get("OPENAI_API_KEY"):
        return None
    return Config(
        api_key=values["OPENAI_API_KEY"],
        model=values.get("SMART_RENAME_MODEL", "gpt-5.6-luna"),
        base_url=values.get("SMART_RENAME_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        timeout_ms=timeout_ms,
    )


def load_prompt() -> str:
    """Return the naming prompt, refreshing the config dir copy unless it was edited.

    A plugin upgrade ships a new prompt, so an unmodified copy must follow it or
    the upgrade silently does nothing. A copy the user has touched is left alone.
    """
    shipped = (Path(__file__).parent / "naming-prompt.md").read_text()
    user_copy = config_dir() / "naming-prompt.md"
    seen = config_dir() / ".naming-prompt.shipped.sha256"
    current = sha256(shipped.encode()).hexdigest()
    try:
        if not user_copy.exists():
            user_copy.parent.mkdir(parents=True, exist_ok=True)
            user_copy.write_text(shipped)
            seen.write_text(current)
            return shipped
        previous = seen.read_text().strip() if seen.exists() else ""
        unmodified = sha256(user_copy.read_text().encode()).hexdigest() == previous
        if previous != current:
            if unmodified or not previous:
                # No recorded hash means we cannot tell an edited copy from a
                # stale one, so keep a backup before taking the shipped version.
                if not previous:
                    user_copy.with_suffix(".md.bak").write_text(user_copy.read_text())
                user_copy.write_text(shipped)
                seen.write_text(current)
                log("naming prompt updated from the shipped version")
                return shipped
            seen.write_text(current)
            log("shipped naming prompt changed; keeping your edited copy")
        return user_copy.read_text()
    except OSError:
        return shipped


def parse_reply(content: str) -> str | None:
    extracted = extract_name(content)
    if extracted is None:
        # Quoted because abstention and an unparseable reply look identical here.
        log(f"model named nothing: {' '.join(content.split())[:160]}")
    return extracted


def request_name(config: Config, prompt: str, evidence_block: str) -> str | None:
    if config.provider == "claude":
        return request_name_claude(config, prompt, evidence_block)
    return request_name_openai(config, prompt, evidence_block)


def request_name_openai(config: Config, prompt: str, evidence_block: str) -> str | None:
    """Ask the provider for a name. No token cap is sent, for portability across providers."""
    try:
        response = httpx.post(
            f"{config.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={
                "model": config.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": evidence_block},
                ],
            },
            timeout=config.timeout_ms / 1000,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        log(f"model request failed: {type(error).__name__}: {str(error)[:160]}")
        return None
    return parse_reply(content)


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_claude_bin(override: str | None) -> str | None:
    """Find the claude CLI, which herdr's minimal PATH often cannot see."""
    if override:
        path = Path(override).expanduser()
        return str(path) if is_executable_file(path) else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLAUDE_INSTALL_PATHS:
        path = Path(candidate).expanduser()
        if is_executable_file(path):
            return str(path)
    return None


def claude_env(binary: str) -> dict[str, str]:
    """The environment for a naming call, without the parent Claude session's plugins and hooks."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "AI_AGENT" and (not key.startswith("CLAUDE") or key in CLAUDE_ENV_KEEP)
    }
    env["PATH"] = os.pathsep.join(filter(None, [str(Path(binary).parent), env.get("PATH")]))
    return env


def claude_command(binary: str, model: str, prompt: str) -> list[str]:
    return [
        binary, "-p", "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", prompt,
    ]


def request_name_claude(config: Config, prompt: str, evidence_block: str) -> str | None:
    """Ask the Claude Code CLI, which bills the subscription instead of an API key."""
    binary = resolve_claude_bin(config.claude_bin)
    if binary is None:
        log("claude CLI not found: set SMART_RENAME_CLAUDE_BIN or install it on PATH "
            f"(also looked in {', '.join(CLAUDE_INSTALL_PATHS)})"
            + (f"; SMART_RENAME_CLAUDE_BIN={config.claude_bin!r} is not executable" if config.claude_bin else ""))
        return None
    cwd = state_dir()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        cwd = Path("/tmp")
    try:
        completed = subprocess.run(
            claude_command(binary, config.model, prompt),
            input=evidence_block, capture_output=True, text=True, check=False,
            timeout=config.timeout_ms / 1000, cwd=cwd, env=claude_env(binary),
        )
    except subprocess.TimeoutExpired:
        log(f"claude request timed out after {config.timeout_ms}ms")
        return None
    except OSError as error:
        log(f"claude request failed: {type(error).__name__}: {str(error)[:160]}")
        return None
    if completed.returncode != 0:
        log(f"claude exited rc={completed.returncode}: {(completed.stderr or completed.stdout).strip()[:160]}")
        return None
    try:
        payload = json.loads(completed.stdout)
        if payload.get("is_error"):
            raise ValueError(str(payload.get("result"))[:160])
        content = payload["result"]
    except (json.JSONDecodeError, AttributeError, KeyError, ValueError) as error:
        log(f"claude reply unusable: {type(error).__name__}: {str(error)[:160]}")
        return None
    return parse_reply(content)


def gather_evidence(pane_id: str) -> tuple[Evidence, dict] | None:
    """Collect everything known about one pane. Returns the evidence and the live agent info."""
    agent_info = (herdr("agent", "get", pane_id) or {}).get("agent")
    if not agent_info:
        return None
    pane_info = (herdr("pane", "get", pane_id) or {}).get("pane") or {}
    cwd = agent_info.get("cwd") or pane_info.get("cwd") or ""
    workspaces = (herdr("workspace", "list") or {}).get("workspaces") or []
    label = next(
        (w.get("label", "") for w in workspaces if w.get("workspace_id") == agent_info.get("workspace_id")),
        "",
    )
    evidence = Evidence(
        agent=agent_info.get("agent") or "unknown",
        workspace_label=label,
        cwd=cwd,
        branch=git_branch(cwd) if cwd else None,
        title=pane_info.get("terminal_title_stripped"),
    )
    return evidence, agent_info


def apply_label(pane_id: str, label: str, workspace_label: str) -> str | None:
    """Write the sidebar label, plus a slug alias for CLI targeting.

    The label is the deliverable; the alias is best effort, since agent names are
    unique session-wide and may collide. Returns the label that stuck.
    """
    slug = name_for(label)
    for attempt in range(3):
        if slug is None:
            break
        candidate = slug if attempt == 0 else disambiguate(slug, workspace_label, attempt)
        result = herdr("agent", "rename", pane_id, candidate)
        if result and "error" not in result:
            break
        if result and "agent_name_taken" not in str(result.get("error", "")):
            log(f"alias for {pane_id} rejected: {str(result.get('error'))[:120]}")
            break

    result = herdr("pane", "rename", pane_id, label)
    if result and "error" not in result:
        return label
    log(f"label for {pane_id} failed: {str((result or {}).get('error'))[:160]}")
    return None


def tab_fields(stamp: Stamp | None) -> dict:
    return {
        "tab_label_set": stamp.tab_label_set if stamp else None,
        "tab_manual": stamp.tab_manual if stamp else False,
    }


def sync_tab(agent_info: dict, label: str, title: str | None, prior: Stamp | None) -> dict:
    """Give the agent's tab the pane label when the tab is not named by the user.

    Returns the stamp fields describing the tab afterwards. A tab holding several
    agents is left alone and not marked, so it is reconsidered if it becomes single.
    """
    fields = {**tab_fields(prior), "title_seen": title}
    tab_id = agent_info.get("tab_id")
    if not label or not tab_id or fields["tab_manual"] or fields["tab_label_set"] == label:
        return fields
    tabs = (herdr("tab", "list") or {}).get("tabs")
    agents = (herdr("agent", "list") or {}).get("agents")
    if tabs is None or agents is None:
        return fields
    if sum(1 for agent in agents if agent.get("tab_id") == tab_id) != 1:
        return fields
    tab = next((t for t in tabs if t.get("tab_id") == tab_id), None)
    if tab is None:
        return fields
    siblings = [t.get("tab_id") for t in tabs if t.get("workspace_id") == tab.get("workspace_id")]
    current = (tab.get("label") or "").strip()
    known_titles = {
        normalise_title(text) for text in (title, prior.title_seen if prior else None) if text
    } - {""}
    unnamed = {"", str(siblings.index(tab_id) + 1), str(tab.get("number"))}
    ours = (
        current in unnamed
        or current == fields["tab_label_set"]
        or normalise_title(current) in known_titles
    )
    if not ours:
        fields["tab_manual"] = True
        log(f"{tab_id} keeps its own name {current!r}")
        return fields
    if current != label:
        result = herdr("tab", "rename", tab_id, label)
        if not result or "error" in result:
            log(f"tab label for {tab_id} failed: {str((result or {}).get('error'))[:160]}")
            return fields
        log(f"{tab_id} named {label!r}")
    fields["tab_label_set"] = label
    return fields


def sync_pane_tab(pane_id: str, agent_info: dict, title: str | None, before: Stamp | None) -> None:
    """Bring the tab in line with whatever label the pane's stamp now records."""
    current = read_stamp(pane_id)
    if current is None or current.manual or not current.label_set:
        return
    updated = current.model_copy(update=sync_tab(agent_info, current.label_set, title, before))
    if updated != current:
        write_stamp(pane_id, updated)


def hand_back_tab(pane_id: str, stamp: Stamp | None) -> None:
    """Undo our tab name, but only while the tab still shows it."""
    if stamp is None or not stamp.tab_label_set:
        return
    tab_id = ((herdr("pane", "get", pane_id) or {}).get("pane") or {}).get("tab_id")
    tabs = (herdr("tab", "list") or {}).get("tabs") or []
    tab = next((t for t in tabs if t.get("tab_id") == tab_id), None)
    if tab is None or (tab.get("label") or "").strip() != stamp.tab_label_set:
        return
    result = herdr("tab", "rename", tab_id, "")
    if not result or "error" in result:
        siblings = [t.get("tab_id") for t in tabs if t.get("workspace_id") == tab.get("workspace_id")]
        herdr("tab", "rename", tab_id, str(siblings.index(tab_id) + 1))


def pane_from_environment() -> tuple[str | None, str | None]:
    """Resolve which pane this run is about, and the event that triggered it."""
    event = os.environ.get("HERDR_PLUGIN_EVENT")
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    if raw:
        try:
            data = json.loads(raw).get("data") or {}
            if data.get("pane_id"):
                return data["pane_id"], event
        except (json.JSONDecodeError, AttributeError):
            pass
    context = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        try:
            focused = json.loads(context).get("focused_pane_id")
            if focused:
                return focused, event
        except (json.JSONDecodeError, AttributeError):
            pass
    return os.environ.get("HERDR_PANE_ID"), event


def prune_orphan_stamps() -> None:
    """Drop stamps for panes that no longer host an agent."""
    agents = (herdr("agent", "list") or {}).get("agents")
    if agents is None:
        return
    live = {agent["pane_id"].replace(":", "-") for agent in agents if agent.get("pane_id")}
    directory = state_dir() / "panes"
    if not directory.exists():
        return
    for path in directory.glob("*.json"):
        if path.stem not in live:
            path.unlink(missing_ok=True)


def claude_status(config: Config) -> str:
    """Locate the CLI and ask it for its version, which costs no model call."""
    binary = resolve_claude_bin(config.claude_bin)
    if binary is None:
        return "binary=NOT FOUND (set SMART_RENAME_CLAUDE_BIN or put claude on PATH)"
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10,
            check=False, env=claude_env(binary),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"binary={binary} UNRUNNABLE ({type(error).__name__})"
    return f"binary={binary} version={completed.stdout.strip() or 'unknown'}"


def run_check() -> int:
    """Report every diagnostic it can gather, including when the provider is unconfigured."""
    config = load_config()
    if config is None:
        provider = provider_name(read_settings())
        if provider in PROVIDERS:
            print(f"provider: NO API KEY — run configure-ai ({config_dir() / 'provider.env'})")
        else:
            print(f"provider: UNKNOWN {provider!r} — SMART_RENAME_PROVIDER must be one of {', '.join(PROVIDERS)}")
    elif config.provider == "claude":
        print(f"provider: claude {claude_status(config)} model={config.model} timeout={config.timeout_ms}ms")
    else:
        print(f"provider: model={config.model} base_url={config.base_url} timeout={config.timeout_ms}ms")
    print(f"prompt: {'ok' if load_prompt().strip() else 'EMPTY'}")
    agents = (herdr("agent", "list") or {}).get("agents")
    print(f"herdr: socket={'ok' if agents is not None else 'UNREACHABLE'} agents={len(agents or [])}")
    titles = 0
    for agent in agents or []:
        pane = (herdr("pane", "get", agent["pane_id"]) or {}).get("pane") or {}
        if pane.get("terminal_title_stripped"):
            titles += 1
    print(f"titles: {titles}/{len(agents or [])} panes publish one")
    if agents and not titles:
        print("  hint: add terminal_title_stripped to [ui.sidebar.agents] rows in config.toml,")
        print("        otherwise herdr never syncs OSC titles and naming falls back to the user's messages")
    named = sum(1 for agent in agents or [] if agent.get("name"))
    print(f"named: {named}/{len(agents or [])} agents currently carry a name")
    return 0


def run_reset(pane_id: str) -> int:
    hand_back_tab(pane_id, read_stamp(pane_id))
    herdr("agent", "rename", pane_id, "--clear")
    herdr("pane", "rename", pane_id, "")
    stamp_path(pane_id).unlink(missing_ok=True)
    log(f"{pane_id} label cleared by action")
    return 0


def rename_pane(pane_id: str, force: bool) -> int:
    now = int(time.time())
    gathered = gather_evidence(pane_id)
    if gathered is None:
        log(f"{pane_id} hosts no agent")
        return 0
    evidence, agent_info = gathered
    live_label = ((herdr("pane", "get", pane_id) or {}).get("pane") or {}).get("label")
    session = resolve_session(agent_info)
    stamp = read_stamp(pane_id)
    tokens = context_tokens(session)
    compacted_at = last_compaction(session)
    verdict = decide(stamp, live_label, session, title_hash(evidence.title), now, force, tokens, compacted_at)

    if verdict == "adopt-manual":
        write_stamp(pane_id, Stamp(
            label_set=live_label, name_set=agent_info.get("name"),
            title_hash=title_hash(evidence.title),
            agent_session_id=session, set_at=now, manual=True,
            **tab_fields(stamp),
        ))
        log(f"{pane_id} adopted manual label {live_label!r}")
        return 0
    if verdict == "rename":
        outcome = request_real_label(pane_id, evidence, session, tokens, now)
        if outcome == "failed":
            set_fallback_label(pane_id, evidence, session, stamp, live_label, now)
    else:
        if stamp is not None and stamp.tokens_at_naming is None and tokens is not None and not stamp.fallback and not stamp.manual:
            # Named before a baseline existed (or while a compaction left none): measure growth from here.
            write_stamp(pane_id, stamp.model_copy(update={"tokens_at_naming": tokens}))
        if verdict != "skip-unchanged":
            # The watcher polls every minute, so the steady state would drown the log.
            log(f"{pane_id} {verdict}")
    sync_pane_tab(pane_id, agent_info, evidence.title, stamp)
    return 0


def request_real_label(
    pane_id: str, evidence: Evidence, session: str | None, tokens: int | None, now: int
) -> str:
    """Try for a model-generated label: "applied", "failed", or "busy" when another run holds the lock."""
    evidence = evidence.model_copy(update=session_history(session))
    if evidence_is_thin(evidence):
        log(f"{pane_id} shows no task yet, title={evidence.title!r}")
        return "failed"
    if not acquire_lock(pane_id, now):
        log(f"{pane_id} another run holds the lock")
        return "busy"

    try:
        config = load_config()
        if config is None:
            log("no usable provider configuration — run the check-ai action")
            return "failed"
        raw_name = request_name(config, load_prompt(), render_evidence(evidence, str(Path.home())))
        proposed = validate_label(raw_name)
        if proposed is None:
            if raw_name:
                log(f"{pane_id} discarded {raw_name!r}, which is not a valid label")
            return "failed"
        applied = apply_label(pane_id, proposed, evidence.workspace_label)
        if applied is None:
            return "failed"
        write_stamp(pane_id, Stamp(
            label_set=applied, name_set=name_for(applied),
            title_hash=title_hash(evidence.title),
            agent_session_id=session, set_at=now, manual=False,
            tokens_at_naming=tokens,
        ))
        log(f"{pane_id} labelled {applied!r}")
        return "applied"
    finally:
        release_lock(pane_id)


def set_fallback_label(
    pane_id: str, evidence: Evidence, session: str | None,
    stamp: Stamp | None, live_label: str | None, now: int,
) -> None:
    """Show the terminal title until the model produces a label.

    Written through the plugin's own label channel and stamped as plugin-written,
    so manual-rename detection still tells it apart from a hand edit. Another
    label already on the pane is never overwritten.
    """
    label = fallback_label(evidence.title)
    if label is None:
        return
    ours = stamp is not None and stamp.fallback and live_label == stamp.label_set
    if live_label and not ours:
        return
    if ours and (live_label == label or stamp.title_hash == title_hash(evidence.title)):
        return
    result = herdr("pane", "rename", pane_id, label)
    if not result or "error" in result:
        log(f"fallback label for {pane_id} failed: {str((result or {}).get('error'))[:160]}")
        return
    write_stamp(pane_id, Stamp(
        label_set=label, title_hash=title_hash(evidence.title),
        agent_session_id=session, set_at=now, fallback=True,
    ))
    log(f"{pane_id} fallback label {label!r}")


def run_sweep() -> int:
    """Name every open agent and start its watcher, for agents herdr never announced.

    Restored agents and agents open before the plugin was linked get no
    pane.agent_detected. Each pane gets a detached watch.sh, so this returns at once;
    the watcher's lock keeps repeat sweeps from stacking duplicates.
    """
    agents = (herdr("agent", "list") or {}).get("agents") or []
    script = Path(__file__).resolve().parent / "watch.sh"
    started = 0
    for agent in agents:
        pane_id = agent.get("pane_id")
        if not pane_id:
            continue
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("HERDR_PLUGIN_EVENT", "HERDR_PLUGIN_EVENT_JSON", "HERDR_PLUGIN_CONTEXT_JSON")
        }
        env.update(HERDR_PANE_ID=pane_id, SMART_RENAME_RUN_FIRST="1")
        try:
            subprocess.Popen(
                ["sh", str(script)], cwd=script.parent, env=env, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            started += 1
        except OSError as error:
            log(f"sweep could not start a watcher for {pane_id}: {error}")
    log(f"sweep started {started} of {len(agents)} agents")
    return 0


def main() -> int:
    flags = set(sys.argv[1:])
    if "--check" in flags:
        return run_check()
    if "--sweep" in flags:
        return run_sweep()

    pane_id, event = pane_from_environment()
    if not pane_id:
        return 0
    if event == "pane.exited":
        stamp_path(pane_id).unlink(missing_ok=True)
        return 0
    if "--reset" in flags:
        return run_reset(pane_id)

    prune_orphan_stamps()
    return rename_pane(pane_id, force="--now" in flags)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # never let a hook failure surface in herdr
        log(f"unhandled: {type(error).__name__}: {str(error)[:200]}")
        sys.exit(0)
