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
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

# herdr enforces ^[a-z][a-z0-9_-]{0,31}$ on agent names, because a name doubles
# as a CLI target. 24 rather than 32 so the 26-column sidebar does not ellipsize.
MAX_NAME_CHARS = 24
MIN_NAME_WORDS = 2
MAX_NAME_WORDS = 4
RENAME_FLOOR_SECONDS = 60
STALE_LOCK_SECONDS = 120
EVIDENCE_CHAR_CAP = 4000
MIN_OUTPUT_CHARS = 40
RECENT_OUTPUT_LINES = 120
LOG_LINE_CAP = 200

AGENT_LABELS = {
    "claude", "codex", "cursor", "copilot", "devin", "droid", "kilo", "kimi",
    "pi", "omp", "hermes", "qodercli", "mastracode", "aider", "goose", "amp",
}

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


class Config(BaseModel):
    api_key: str
    model: str
    base_url: str
    timeout_ms: int


class Stamp(BaseModel):
    name_set: str | None = None
    title_hash: str = ""
    agent_session_id: str | None = None
    set_at: int = 0
    manual: bool = False


class Evidence(BaseModel):
    agent: str
    workspace_label: str
    cwd: str
    branch: str | None
    title: str | None
    recent_output: str | None


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
    """Keep the most recent end of the output, which is where the current task lives."""
    return text if len(text) <= limit else text[-limit:]


def strip_agent_banner(text: str) -> str:
    """Drop the welcome/banner box agents print at startup, which names no task."""
    lines = [line for line in text.splitlines() if line.strip()]
    kept = [
        line
        for line in lines
        if not re.match(r"^[\s│┃|╭╰─═╔╚*>#]*(welcome|tips? for|/help|cwd:|version)", line, re.I)
        and not re.fullmatch(r"[\s─═━╌╍_\-*=~#│┃|╭╮╯╰┌┐└┘]+", line)
    ]
    return "\n".join(kept)


def evidence_is_thin(evidence: Evidence) -> bool:
    if evidence.title and not title_is_junk(evidence.title, evidence.cwd, evidence.branch):
        return False
    body = strip_agent_banner(evidence.recent_output or "")
    return len(body.strip()) < MIN_OUTPUT_CHARS


def render_evidence(evidence: Evidence, home: str) -> str:
    """Build the sanitised block sent to the model."""
    parts = [
        f"agent: {evidence.agent}",
        f"workspace: {evidence.workspace_label}",
        f"cwd: {redact(evidence.cwd, home)}",
    ]
    if evidence.branch:
        parts.append(f"branch: {evidence.branch}")
    if evidence.title and not title_is_junk(evidence.title, evidence.cwd, evidence.branch):
        parts.append(f"title: {redact(evidence.title, home)}")
    body = strip_agent_banner(evidence.recent_output or "").strip()
    if body:
        parts.append("recent output:\n" + cap_tail(redact(body, home)))
    return "\n".join(parts)


def slugify(text: str) -> str:
    """Coerce free text into herdr's agent-name shape, tolerating Title Cased model output."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return re.sub(r"^[^a-z]+", "", slug)


def validate_name(raw: str | None) -> str | None:
    """Return a name herdr will accept and the sidebar can show whole, or None to abstain."""
    if not raw:
        return None
    slug = slugify(raw)
    if not slug or len(slug) > MAX_NAME_CHARS:
        return None
    words = slug.split("-")
    if not MIN_NAME_WORDS <= len(words) <= MAX_NAME_WORDS:
        return None
    if slug in AGENT_LABELS or any(word in AGENT_LABELS for word in words):
        return None
    return slug if HERDR_NAME_RULE.match(slug) else None


def disambiguate(name: str, workspace_label: str, attempt: int) -> str:
    """Produce an alternative when a name is already taken elsewhere in the session."""
    if attempt == 1 and workspace_label:
        prefix = slugify(workspace_label).split("-")[0]
        if prefix:
            return f"{prefix}-{name}"[:MAX_NAME_CHARS].strip("-")
    suffix = f"-{attempt + 1}"
    return name[: MAX_NAME_CHARS - len(suffix)].strip("-") + suffix


def decide(
    stamp: Stamp | None,
    live_name: str | None,
    live_session_id: str | None,
    current_title_hash: str,
    now: int,
    force: bool = False,
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
    if live_name and live_name != stamp.name_set:
        return "adopt-manual"
    if live_name and stamp.title_hash == current_title_hash:
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
    """Append a diagnostic line, trimming the log to its most recent lines."""
    path = state_dir() / "rename.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text().splitlines() if path.exists() else []
        previous.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
        path.write_text("\n".join(previous[-LOG_LINE_CAP:]) + "\n")
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


def acquire_lock(pane_id: str, now: int) -> bool:
    """Take a per-pane lock so concurrent firings cannot both call the model."""
    path = state_dir() / "locks" / f"{pane_id.replace(':', '-')}.lock"
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
    (state_dir() / "locks" / f"{pane_id.replace(':', '-')}.lock").unlink(missing_ok=True)


def herdr_raw(*args: str) -> str | None:
    """Call the herdr CLI over the injected socket and return raw stdout."""
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    try:
        completed = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"herdr {' '.join(args)} failed: {error}")
        return None
    if completed.returncode != 0:
        log(f"herdr {' '.join(args)} rc={completed.returncode}: "
            f"{(completed.stdout or completed.stderr).strip()[:160]}")
        return None
    return completed.stdout


def herdr(*args: str) -> dict | None:
    """Return the result payload of a herdr CLI call. `agent read` is raw text — use herdr_raw."""
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


def load_config() -> Config | None:
    """Read provider.env, letting the process environment win, and validate it."""
    values: dict[str, str] = {}
    path = config_dir() / "provider.env"
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update({k: v for k, v in os.environ.items() if k in {
        "OPENAI_API_KEY", "SMART_RENAME_MODEL", "SMART_RENAME_BASE_URL", "SMART_RENAME_TIMEOUT_MS"
    } and v})
    if not values.get("OPENAI_API_KEY"):
        return None
    return Config(
        api_key=values["OPENAI_API_KEY"],
        model=values.get("SMART_RENAME_MODEL", "gpt-5.6-luna"),
        base_url=values.get("SMART_RENAME_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        timeout_ms=int(values.get("SMART_RENAME_TIMEOUT_MS", "20000")),
    )


def load_prompt() -> str:
    """Return the naming prompt, seeding the config dir copy on first run."""
    user_copy = config_dir() / "naming-prompt.md"
    shipped = Path(__file__).parent / "naming-prompt.md"
    if not user_copy.exists():
        try:
            user_copy.parent.mkdir(parents=True, exist_ok=True)
            user_copy.write_text(shipped.read_text())
        except OSError:
            return shipped.read_text()
    return user_copy.read_text()


def request_name(config: Config, prompt: str, evidence_block: str) -> str | None:
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
    return extract_name(content)


def gather_evidence(pane_id: str) -> tuple[Evidence, dict] | None:
    """Collect everything known about one pane. Returns the evidence and the live agent info."""
    agent_info = (herdr("agent", "get", pane_id) or {}).get("agent")
    if not agent_info:
        return None
    pane_info = (herdr("pane", "get", pane_id) or {}).get("pane") or {}
    cwd = agent_info.get("cwd") or pane_info.get("cwd") or ""
    recent = herdr_raw(
        "agent", "read", pane_id, "--source", "recent",
        "--lines", str(RECENT_OUTPUT_LINES), "--format", "text",
    )
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
        recent_output=recent,
    )
    return evidence, agent_info


def apply_name(pane_id: str, name: str, workspace_label: str) -> str | None:
    """Write the name, working around session-wide uniqueness. Returns the name that stuck."""
    for attempt in range(3):
        candidate = name if attempt == 0 else disambiguate(name, workspace_label, attempt)
        result = herdr("agent", "rename", pane_id, candidate)
        if result and "error" not in result:
            return candidate
        if result and "agent_name_taken" not in str(result.get("error", "")):
            log(f"rename {pane_id} failed: {str(result.get('error'))[:160]}")
            return None
    return None


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


def run_check() -> int:
    """Report every diagnostic it can gather, including when the provider is unconfigured."""
    config = load_config()
    if config is None:
        print(f"provider: NO API KEY — run configure-ai ({config_dir() / 'provider.env'})")
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
        print("        otherwise herdr never syncs OSC titles and naming falls back to pane output")
    named = sum(1 for agent in agents or [] if agent.get("name"))
    print(f"named: {named}/{len(agents or [])} agents currently carry a name")
    return 0


def run_reset(pane_id: str) -> int:
    herdr("agent", "rename", pane_id, "--clear")
    stamp_path(pane_id).unlink(missing_ok=True)
    log(f"{pane_id} name cleared by action")
    return 0


def rename_pane(pane_id: str, force: bool) -> int:
    now = int(time.time())
    gathered = gather_evidence(pane_id)
    if gathered is None:
        return 0
    evidence, agent_info = gathered
    live_name = agent_info.get("name")
    session = (agent_info.get("agent_session") or {}).get("value")
    stamp = read_stamp(pane_id)
    verdict = decide(stamp, live_name, session, title_hash(evidence.title), now, force)

    if verdict == "adopt-manual":
        write_stamp(pane_id, Stamp(
            name_set=live_name, title_hash=title_hash(evidence.title),
            agent_session_id=session, set_at=now, manual=True,
        ))
        log(f"{pane_id} adopted manual name {live_name!r}")
        return 0
    if verdict != "rename":
        return 0
    if evidence_is_thin(evidence):
        return 0
    if not acquire_lock(pane_id, now):
        return 0

    try:
        config = load_config()
        if config is None:
            log("no OPENAI_API_KEY — run the configure-ai action")
            return 0
        proposed = validate_name(
            request_name(config, load_prompt(), render_evidence(evidence, str(Path.home())))
        )
        if proposed is None:
            return 0
        applied = apply_name(pane_id, proposed, evidence.workspace_label)
        if applied is None:
            return 0
        write_stamp(pane_id, Stamp(
            name_set=applied, title_hash=title_hash(evidence.title),
            agent_session_id=session, set_at=now, manual=False,
        ))
        log(f"{pane_id} renamed to {applied!r}")
    finally:
        release_lock(pane_id)
    return 0


def main() -> int:
    flags = set(sys.argv[1:])
    if "--check" in flags:
        return run_check()

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
