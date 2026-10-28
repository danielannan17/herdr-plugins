import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Evidence, Stamp  # noqa: E402

NOW = 1_784_937_769
SESSION = "s1"
LONG_TITLE = "Investigate the flaky upload retry behaviour"


class Herdr:
    """Just enough of the herdr CLI to follow what the plugin does to one pane."""

    def __init__(self):
        self.label = None
        self.title = "Routing messages to Acra sessions"
        self.pane_renames = []
        self.agent_renames = []

    def __call__(self, *args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"label": self.label}}
        if args[:2] == ("pane", "rename"):
            self.label = args[3] or None
            self.pane_renames.append(args[3])
            return {"pane": {}}
        if args[:2] == ("agent", "rename"):
            self.agent_renames.append(args[3])
            return {"agent": {}}
        return {}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "config"))
    projects = tmp_path / "claude" / "projects" / "-w"
    projects.mkdir(parents=True)
    monkeypatch.setattr(rename, "CLAUDE_PROJECTS_DIR", projects.parent)
    fake = Herdr()
    clock = [1_000_000]
    model = {"reply": None, "calls": 0}
    context = {"tokens": None, "compacted_at": None}

    def gather(pane_id):
        evidence = Evidence(
            agent="claude", workspace_label="app", cwd="/w", branch="main",
            title=fake.title,
        )
        return evidence, {"name": None, "agent_session": {"value": SESSION}}

    def request(*args):
        model["calls"] += 1
        return model["reply"]

    monkeypatch.setattr(rename, "herdr", fake)
    monkeypatch.setattr(rename, "gather_evidence", gather)
    monkeypatch.setattr(rename, "context_tokens", lambda session: context["tokens"])
    monkeypatch.setattr(rename, "last_compaction", lambda session: context["compacted_at"])
    monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
    monkeypatch.setattr(rename, "request_name", request)
    monkeypatch.setattr(rename, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))

    def write_titles(*titles):
        records = [{"type": "user", "message": {"role": "user", "content": "route voice to sessions"}}]
        records += [{"type": "custom-title", "customTitle": title, "sessionId": SESSION} for title in titles]
        (projects / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def run(advance=120):
        clock[0] += advance
        rename.rename_pane("w1:p1", force=False)

    return SimpleNamespace(
        herdr=fake, model=model, context=context, clock=clock, run=run, write_titles=write_titles
    )


# --- reading the title from the transcript -----------------------------------

def test_an_empty_custom_title_record_is_returned_so_it_can_outrank_older_ones():
    assert rename.title_records({"type": "custom-title", "customTitle": "  "}) == {"custom_title": ""}
    assert rename.title_records({"type": "custom-title"}) == {}
    assert rename.title_records({"type": "custom-title", "customTitle": None}) == {}


def test_an_empty_latest_custom_title_record_means_no_title(flow):
    flow.write_titles("google", "")
    assert rename.session_history(SESSION)["custom_title"] is None


def test_an_older_empty_record_does_not_hide_a_later_title(flow):
    flow.write_titles("google", "", "phases")
    assert rename.session_history(SESSION)["custom_title"] == "phases"


# --- the verdict -------------------------------------------------------------

def stamp(**overrides):
    base = dict(
        label_set="google", name_set="google", title_hash="h", agent_session_id=SESSION,
        set_at=NOW - 600, custom_title="google",
    )
    base.update(overrides)
    return Stamp(**base)


def test_a_session_title_not_yet_shown_is_applied():
    assert rename.decide(None, None, SESSION, "h", NOW, custom_title="google") == "rename"
    auto = stamp(custom_title=None, label_set="Route voice")
    assert rename.decide(auto, "Route voice", SESSION, "h", NOW, custom_title="google") == "rename"


def test_an_unchanged_session_title_is_never_relabelled():
    far_on = NOW + 10_000_000
    assert rename.decide(
        stamp(tokens_at_naming=10_000), "google", SESSION, "other", far_on, tokens=900_000,
        compacted_at=far_on - 1, custom_title="google",
    ) == "skip-unchanged"


def test_a_changed_session_title_is_applied():
    assert rename.decide(stamp(), "google", SESSION, "h", NOW, custom_title="phases") == "rename"


def test_a_removed_session_title_returns_the_pane_to_automatic_naming():
    assert rename.decide(stamp(), "google", SESSION, "h", NOW, custom_title=None) == "rename"


def test_a_cleared_label_is_written_again():
    assert rename.decide(stamp(), None, SESSION, "h", NOW, custom_title="google") == "rename"


def test_a_manual_rename_outranks_the_session_title():
    assert rename.decide(stamp(), "mine", SESSION, "h", NOW, custom_title="phases") == "adopt-manual"
    assert rename.decide(stamp(manual=True), "mine", SESSION, "h", NOW, custom_title="phases") == "skip-manual"


# --- through the whole rename flow -------------------------------------------

def test_the_session_title_is_the_label_with_no_model_call(flow):
    flow.write_titles("google")
    flow.run()
    assert flow.herdr.label == "google"
    assert flow.herdr.agent_renames == ["google"]
    assert flow.model["calls"] == 0
    stamp = rename.read_stamp("w1:p1")
    assert stamp.custom_title == "google" and stamp.label_set == "google" and not stamp.fallback


def test_a_long_session_title_is_shown_in_full(flow):
    flow.write_titles(LONG_TITLE)
    flow.run()
    assert len(LONG_TITLE) > rename.MAX_LABEL_CHARS
    assert flow.herdr.label == LONG_TITLE
    assert flow.herdr.agent_renames == [rename.name_for(LONG_TITLE)]


def test_a_session_title_naming_an_agent_is_still_shown(flow):
    flow.write_titles("claude")
    flow.run()
    assert flow.herdr.label == "claude"


def test_a_title_with_no_possible_alias_is_still_the_label(flow):
    flow.write_titles("123")
    flow.run()
    assert flow.herdr.label == "123"
    assert flow.herdr.agent_renames == []


def test_an_unchanged_session_title_is_never_relabelled_however_the_session_moves(flow):
    flow.write_titles("google")
    flow.run()
    flow.context["tokens"] = 50_000
    flow.run()
    flow.herdr.title = "A completely different task"
    flow.context["tokens"] = 400_000
    flow.run(advance=100_000)
    flow.context["compacted_at"] = flow.clock[0] + 1
    flow.run()
    assert flow.herdr.pane_renames == ["google"]
    assert flow.model["calls"] == 0


def test_a_changed_session_title_is_applied(flow):
    flow.write_titles("google")
    flow.run()
    flow.write_titles("google", "phases")
    flow.run()
    assert flow.herdr.label == "phases"
    assert rename.read_stamp("w1:p1").custom_title == "phases"
    assert flow.model["calls"] == 0


def test_a_session_title_replaces_a_model_label(flow):
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    assert flow.herdr.label == "Route voice to sessions"
    flow.write_titles("google")
    flow.run()
    assert flow.herdr.label == "google"


def test_removing_the_title_returns_to_automatic_naming(flow):
    flow.write_titles("google")
    flow.run()
    flow.write_titles("google", "")
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    assert flow.herdr.label == "Route voice to sessions"
    stamp = rename.read_stamp("w1:p1")
    assert stamp.custom_title is None and not stamp.fallback


def test_removing_the_title_with_no_model_label_shows_the_terminal_title(flow):
    flow.write_titles("google")
    flow.run()
    flow.write_titles("google", "")
    flow.run()
    assert flow.herdr.label == "Routing messages to Acra"
    stamp = rename.read_stamp("w1:p1")
    assert stamp.fallback and stamp.custom_title is None


def test_a_hand_rename_over_the_session_title_is_adopted_and_then_left_alone(flow):
    flow.write_titles("google")
    flow.run()
    flow.herdr.label = "my own name"
    flow.run()
    assert rename.read_stamp("w1:p1").manual
    flow.write_titles("google", "phases")
    flow.run()
    flow.write_titles("google", "phases", "")
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    assert flow.herdr.label == "my own name"
    assert flow.herdr.pane_renames == ["google"]


def test_the_tab_follows_the_session_title(flow, monkeypatch):
    flow.write_titles(LONG_TITLE)
    flow.run()
    tab = {"tab_id": "w1:t1", "workspace_id": "w1", "label": "", "number": 1}
    calls = []

    def fake(*args):
        if args[:2] == ("tab", "list"):
            return {"tabs": [tab]}
        if args[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": "w1:p1", "tab_id": "w1:t1"}]}
        if args[:2] == ("tab", "rename"):
            calls.append(args[3])
            return {"tab": {}}
        return {}

    monkeypatch.setattr(rename, "herdr", fake)
    rename.sync_pane_tab("w1:p1", {"tab_id": "w1:t1"}, flow.herdr.title, None)
    assert calls == [LONG_TITLE]
