import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Evidence  # noqa: E402

PLUGIN_DIR = Path(__file__).parent.parent
CWD = "/work/app"
TITLE = "Herdr-bar installation"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "config"))
    sessions = tmp_path / "claude" / "sessions"
    projects = tmp_path / "claude" / "projects" / "-work-app"
    sessions.mkdir(parents=True)
    projects.mkdir(parents=True)
    monkeypatch.setattr(rename, "CLAUDE_SESSIONS_DIR", sessions)
    monkeypatch.setattr(rename, "CLAUDE_PROJECTS_DIR", projects.parent)
    return SimpleNamespace(sessions=sessions, projects=projects)


def dead_pid():
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def register(home, session_id, pid=None, cwd=CWD, name=None, entrypoint="cli"):
    entry = {"pid": os.getpid() if pid is None else pid, "sessionId": session_id,
             "cwd": cwd, "entrypoint": entrypoint, "name": name}
    (home.sessions / f"{session_id}.json").write_text(json.dumps(entry))


def info(session=None, agent="claude", cwd=CWD, title=TITLE):
    base = {"agent": agent, "pane_id": "w4:p4", "cwd": cwd, "terminal_title_stripped": title}
    if session:
        base["agent_session"] = {"value": session}
    return base


def listing(monkeypatch, *others):
    agents = [{"pane_id": f"w9:p{i}", "agent_session": {"value": s}} for i, s in enumerate(others)]
    monkeypatch.setattr(rename, "herdr", lambda *args: {"agents": agents} if args[:2] == ("agent", "list") else None)


# --- resolving a session herdr did not report --------------------------------

def test_the_session_herdr_reports_wins(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "other")
    assert rename.resolve_session(info(session="reported")) == "reported"


def test_an_unreported_session_is_the_live_one_in_the_agents_cwd(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "mine")
    assert rename.resolve_session(info()) == "mine"


def test_sessions_other_panes_already_claim_are_not_candidates(claude_home, monkeypatch):
    listing(monkeypatch, "claimed")
    register(claude_home, "claimed")
    register(claude_home, "mine")
    assert rename.resolve_session(info()) == "mine"


def test_dead_foreign_cwd_and_sdk_sessions_are_not_candidates(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "dead", pid=dead_pid())
    register(claude_home, "elsewhere", cwd="/other")
    register(claude_home, "sdk", entrypoint="sdk-ts")
    assert rename.resolve_session(info()) is None


def test_several_candidates_are_told_apart_by_the_pane_title(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "a", name="Something else")
    register(claude_home, "b", name="herdr-bar installation")
    assert rename.resolve_session(info()) == "b"


def test_several_candidates_with_no_title_match_stay_unresolved(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "a", name="one")
    register(claude_home, "b", name="two")
    assert rename.resolve_session(info()) is None


def test_other_agents_are_never_guessed_at(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "mine")
    assert rename.resolve_session(info(agent="codex")) is None


# --- the token trigger on a pane herdr gives no session ---------------------

def test_a_pane_without_a_herdr_session_is_renamed_after_100k_more_tokens(claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "mine")
    transcript = claude_home.projects / "mine.jsonl"

    def write_usage(tokens):
        transcript.write_text(json.dumps({"message": {"usage": {"input_tokens": tokens}}}) + "\n")

    labels = {"pane": None}
    clock = [1_000_000]

    def fake_herdr(*args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"label": labels["pane"]}}
        if args[:2] == ("pane", "rename"):
            labels["pane"] = args[3]
        return {"ok": True}

    replies = iter(["Install herdr plugins", "Fix scrolling bug"])
    evidence = Evidence(agent="claude", workspace_label="app", cwd=CWD, branch="main",
                        title=TITLE)
    monkeypatch.setattr(rename, "herdr", fake_herdr)
    monkeypatch.setattr(rename, "gather_evidence", lambda pane_id: (evidence, info()))
    monkeypatch.setattr(rename, "sync_pane_tab", lambda *args: None)
    monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
    monkeypatch.setattr(rename, "request_name", lambda *args: next(replies))
    monkeypatch.setattr(rename, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))

    write_usage(50_000)
    rename.rename_pane("w4:p4", force=False)
    stamp = rename.read_stamp("w4:p4")
    assert (stamp.agent_session_id, stamp.tokens_at_naming) == ("mine", 50_000)
    assert labels["pane"] == "Install herdr plugins"

    clock[0] += 600
    write_usage(149_999)
    rename.rename_pane("w4:p4", force=False)
    assert labels["pane"] == "Install herdr plugins"

    write_usage(150_000)
    rename.rename_pane("w4:p4", force=False)
    assert labels["pane"] == "Fix scrolling bug"
    assert rename.read_stamp("w4:p4").tokens_at_naming == 150_000


# --- compaction -------------------------------------------------------------

BOUNDARY = {"type": "system", "subtype": "compact_boundary", "uuid": "b1",
            "content": "Conversation compacted", "timestamp": "2026-10-26T16:26:44.695Z",
            "compactMetadata": {"trigger": "auto", "preTokens": 365194}}
BOUNDARY_AT = 1_793_031_204.695  # 2026-10-26T16:26:44.695Z


def usage_line(tokens):
    return {"type": "assistant", "message": {"usage": {"input_tokens": tokens}}}


def boundary_at(epoch):
    stamped = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))
    return {**BOUNDARY, "timestamp": stamped}


class Pane:
    """One pane driven through rename_pane, with a mock transcript and a counted model."""

    def __init__(self, home, monkeypatch, herdr_session):
        self.transcript = home.projects / "mine.jsonl"
        self.clock = [1_000_000]
        self.labels = {"pane": None}
        self.replies = iter(f"Task number {n}" for n in range(1, 20))
        self.renames = 0
        agent = info("mine" if herdr_session else None)
        evidence = Evidence(agent="claude", workspace_label="app", cwd=CWD, branch="main",
                            title=TITLE)

        def fake_herdr(*args):
            if args[:2] == ("pane", "get"):
                return {"pane": {"label": self.labels["pane"]}}
            if args[:2] == ("pane", "rename"):
                self.labels["pane"] = args[3]
                self.renames += 1
            if args[:2] == ("agent", "list"):
                return {"agents": []}
            return {"ok": True}

        monkeypatch.setattr(rename, "herdr", fake_herdr)
        monkeypatch.setattr(rename, "gather_evidence", lambda pane_id: (evidence, agent))
        monkeypatch.setattr(rename, "sync_pane_tab", lambda *args: None)
        monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
        monkeypatch.setattr(rename, "request_name", lambda *args: next(self.replies))
        monkeypatch.setattr(rename, "time", SimpleNamespace(time=lambda: self.clock[0], strftime=time.strftime))

    def transcript_has(self, *entries):
        self.transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def run(self):
        self.clock[0] += 600
        rename.rename_pane("w4:p4", force=False)
        return rename.read_stamp("w4:p4")


@pytest.fixture(params=["herdr-reported-session", "resolved-session"])
def pane(request, claude_home, monkeypatch):
    listing(monkeypatch)
    register(claude_home, "mine")
    return Pane(claude_home, monkeypatch, request.param == "herdr-reported-session")


def test_the_transcript_marker_is_read_as_compact_boundary(claude_home):
    (claude_home.projects / "mine.jsonl").write_text(
        json.dumps(usage_line(900)) + "\n" + json.dumps(BOUNDARY) + "\n")
    assert rename.last_compaction("mine") == pytest.approx(BOUNDARY_AT)
    assert rename.last_compaction("missing") is None


def test_usage_before_a_boundary_is_not_the_current_context(claude_home):
    transcript = claude_home.projects / "mine.jsonl"
    transcript.write_text(json.dumps(usage_line(900_000)) + "\n" + json.dumps(BOUNDARY) + "\n")
    assert rename.context_tokens("mine") is None
    transcript.write_text(transcript.read_text() + json.dumps(usage_line(40_000)) + "\n")
    assert rename.context_tokens("mine") == 40_000


def test_a_token_drop_renames_once_then_measures_from_the_new_baseline(pane):
    pane.transcript_has(usage_line(300_000))
    assert pane.run().tokens_at_naming == 300_000
    assert pane.renames == 1

    pane.transcript_has(usage_line(300_000), usage_line(40_000))
    assert pane.run().tokens_at_naming == 40_000
    assert pane.renames == 2

    pane.transcript_has(usage_line(300_000), usage_line(40_000), usage_line(45_000))
    pane.run()
    assert pane.renames == 2

    pane.transcript_has(usage_line(140_000))
    assert pane.run().tokens_at_naming == 140_000
    assert pane.renames == 3


def test_a_boundary_marker_renames_once_and_backfills_the_baseline(pane):
    pane.transcript_has(usage_line(300_000))
    pane.run()
    assert pane.renames == 1

    # The compaction has just happened: the summary carries no usage yet.
    boundary = boundary_at(pane.clock[0] + 300)
    pane.transcript_has(usage_line(300_000), boundary)
    stamp = pane.run()
    assert (pane.renames, stamp.tokens_at_naming) == (2, None)

    pane.run()
    assert pane.renames == 2

    pane.transcript_has(usage_line(300_000), boundary, usage_line(35_000))
    assert pane.run().tokens_at_naming == 35_000
    assert pane.renames == 2

    pane.transcript_has(usage_line(134_999))
    pane.run()
    assert pane.renames == 2
    pane.transcript_has(usage_line(135_000))
    pane.run()
    assert pane.renames == 3


def test_a_boundary_already_behind_the_stamp_is_ignored(pane):
    pane.transcript_has(boundary_at(pane.clock[0] - 5000), usage_line(35_000))
    pane.run()
    assert pane.renames == 1
    pane.run()
    pane.run()
    assert pane.renames == 1


def test_normal_growth_still_renames_each_100k(pane):
    pane.transcript_has(usage_line(50_000))
    pane.run()
    pane.transcript_has(usage_line(149_999))
    pane.run()
    assert pane.renames == 1
    pane.transcript_has(usage_line(150_000))
    assert pane.run().tokens_at_naming == 150_000
    assert pane.renames == 2


def test_a_missing_transcript_never_triggers_a_rename(pane):
    pane.transcript_has(usage_line(300_000))
    pane.run()
    pane.transcript.unlink()
    pane.run()
    pane.run()
    assert pane.renames == 1
    assert rename.read_stamp("w4:p4").tokens_at_naming == 300_000


# --- the sweep ----------------------------------------------------------------

def test_sweep_detaches_one_watcher_per_agent_and_returns(monkeypatch):
    agents = [{"pane_id": "w1:p1"}, {"pane_id": "w4:p4"}, {}]
    monkeypatch.setattr(rename, "herdr", lambda *args: {"agents": agents})
    monkeypatch.setenv("HERDR_PLUGIN_EVENT", "startup")
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", "{}")
    launched = []
    monkeypatch.setattr(rename.subprocess, "Popen", lambda argv, **kwargs: launched.append((argv, kwargs)))

    assert rename.run_sweep() == 0

    assert [kw["env"]["HERDR_PANE_ID"] for _, kw in launched] == ["w1:p1", "w4:p4"]
    for argv, kwargs in launched:
        assert argv == ["sh", str(PLUGIN_DIR / "watch.sh")]
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["SMART_RENAME_RUN_FIRST"] == "1"
        assert "HERDR_PLUGIN_EVENT" not in kwargs["env"]
        assert "HERDR_PLUGIN_CONTEXT_JSON" not in kwargs["env"]
        assert kwargs["stdout"] == subprocess.DEVNULL


def test_sweep_with_no_agents_starts_nothing(monkeypatch):
    monkeypatch.setattr(rename, "herdr", lambda *args: None)
    monkeypatch.setattr(rename.subprocess, "Popen", lambda *a, **k: pytest.fail("started"))
    assert rename.run_sweep() == 0


# --- watch.sh: first run and duplicate protection ------------------------------

@pytest.fixture
def watcher(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runs = tmp_path / "runs"
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho "$HERDR_PANE_ID" >> "{runs}"\n')
    (bin_dir / "herdr").write_text("#!/bin/sh\nexit 1\n")
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    state = tmp_path / "state"

    def run(run_first):
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin", "HERDR_PLUGIN_STATE_DIR": str(state),
            "HERDR_PANE_ID": "w4:p4", "SMART_RENAME_POLL_SECONDS": "0.1",
        }
        if run_first:
            env["SMART_RENAME_RUN_FIRST"] = "1"
        subprocess.run(["sh", str(PLUGIN_DIR / "watch.sh")], env=env, timeout=20, check=True)
        return runs.read_text().split() if runs.exists() else []

    return SimpleNamespace(run=run, state=state)


def test_a_swept_watcher_names_its_pane_once_before_waiting(watcher):
    assert watcher.run(run_first=True) == ["w4:p4"]
    assert not (watcher.state / "watchers" / "w4-p4.lock").exists()


def test_an_event_watcher_waits_before_its_first_run(watcher):
    assert watcher.run(run_first=False) == []


def test_a_live_watcher_stops_a_second_one_but_the_sweep_still_names_the_pane(watcher):
    lock = watcher.state / "watchers" / "w4-p4.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    assert watcher.run(run_first=False) == []
    assert watcher.run(run_first=True) == ["w4:p4"]
    assert lock.exists()


def test_a_dead_watchers_lock_is_replaced(watcher):
    lock = watcher.state / "watchers" / "w4-p4.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(dead_pid()))
    assert watcher.run(run_first=True) == ["w4:p4"]


# --- the user's own messages from the transcript ----------------------------

def user(content, **flags):
    return {"type": "user", "message": {"role": "user", "content": content}, **flags}


def write_transcript(home, session_id, entries, first_line=None):
    lines = [first_line] if first_line is not None else []
    lines += [e if isinstance(e, str) else json.dumps(e) for e in entries]
    (home.projects / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_string_content_is_a_user_message(claude_home):
    write_transcript(claude_home, "s", [user("fix the upsert ordering")])
    assert rename.recent_user_messages("s") == ["fix the upsert ordering"]


def test_only_text_blocks_of_a_block_list_are_kept(claude_home):
    write_transcript(claude_home, "s", [
        user([{"type": "text", "text": "  add csv export "}, {"type": "image", "source": {}}]),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
    ])
    assert rename.recent_user_messages("s") == ["add csv export"]


def test_meta_and_sidechain_records_are_skipped(claude_home):
    write_transcript(claude_home, "s", [
        user("meta caveat", isMeta=True),
        user("subagent prompt", isSidechain=True),
        {"type": "assistant", "message": {"content": "an answer"}},
        user("real request"),
    ])
    assert rename.recent_user_messages("s") == ["real request"]


@pytest.mark.parametrize("wrapped", [
    "<command-name>/clear</command-name>",
    "<command-message>review</command-message>",
    "<local-command-stdout>done</local-command-stdout>",
    "<local-command-caveat>Caveat: generated locally</local-command-caveat>",
    "<bash-input>ls</bash-input>",
    "<bash-stdout>file.txt</bash-stdout>",
    "<task-notification>build finished</task-notification>",
    "<System-Reminder>be careful</System-Reminder>",
])
def test_wrapped_non_typed_messages_are_dropped(claude_home, wrapped):
    write_transcript(claude_home, "s", [user(wrapped), user("   "), user("typed by hand")])
    assert rename.recent_user_messages("s") == ["typed by hand"]


def test_a_message_that_merely_mentions_a_tag_is_kept(claude_home):
    write_transcript(claude_home, "s", [user("why does <command-name> show up in the log?")])
    assert rename.recent_user_messages("s") == ["why does <command-name> show up in the log?"]


def test_a_cut_first_line_is_skipped(claude_home):
    write_transcript(claude_home, "s", [user("first")], first_line='tId": "x", "message": {"content": "cut off')
    assert rename.recent_user_messages("s") == ["first"]


def test_only_the_last_five_come_back_oldest_first(claude_home):
    write_transcript(claude_home, "s", [user(f"message {n}") for n in range(8)])
    assert rename.recent_user_messages("s") == [f"message {n}" for n in range(3, 8)]


def test_no_session_or_transcript_means_no_messages(claude_home):
    assert rename.recent_user_messages(None) == []
    assert rename.recent_user_messages("missing") == []


def test_the_model_is_sent_the_users_messages_not_pane_output(claude_home, monkeypatch):
    write_transcript(claude_home, "s", [user("fix the upsert ordering")])
    sent = []
    monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
    monkeypatch.setattr(rename, "load_prompt", lambda: "prompt")
    monkeypatch.setattr(rename, "request_name", lambda config, prompt, text: sent.append(text) or None)
    evidence = Evidence(agent="claude", workspace_label="app", cwd=CWD, branch=None, title="main")
    assert rename.request_real_label("w4:p4", evidence, "s", None, 1_000_000) == "failed"
    assert "- fix the upsert ordering" in sent[0]
    assert "recent output" not in sent[0]


def test_a_junk_title_and_no_messages_never_reaches_the_model(claude_home, monkeypatch):
    monkeypatch.setattr(rename, "request_name", lambda *args: pytest.fail("model called"))
    evidence = Evidence(agent="claude", workspace_label="app", cwd=CWD, branch=None, title="main")
    assert rename.request_real_label("w4:p4", evidence, "s", None, 1_000_000) == "failed"
