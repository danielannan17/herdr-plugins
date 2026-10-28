import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Evidence  # noqa: E402


class Herdr:
    """Pane, agent and tab state for one workspace, as the herdr CLI reports it."""

    def __init__(self):
        self.pane_label = None
        self.title = "Herdr workspace dashboard plugin"
        self.tabs = [
            {"tab_id": "w1:t4", "workspace_id": "w1", "label": "Older tab", "number": 4},
            {"tab_id": "w1:t6", "workspace_id": "w1", "label": "2", "number": 6},
        ]
        self.agent_tabs = ["w1:t4", "w1:t6"]
        self.tab_renames = []

    def tab(self, tab_id="w1:t6"):
        return next(t for t in self.tabs if t["tab_id"] == tab_id)

    def __call__(self, *args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"label": self.pane_label, "tab_id": "w1:t6"}}
        if args[:2] == ("pane", "rename"):
            self.pane_label = args[3] or None
            return {"pane": {}}
        if args[:2] == ("tab", "list"):
            return {"tabs": self.tabs}
        if args[:2] == ("agent", "list"):
            return {"agents": [{"pane_id": f"p{i}", "tab_id": tab} for i, tab in enumerate(self.agent_tabs)]}
        if args[:2] == ("tab", "rename"):
            self.tab(args[2])["label"] = args[3]
            self.tab_renames.append((args[2], args[3]))
            return {"tab": {}}
        return {"agent": {}}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "config"))
    fake = Herdr()
    clock = [1_000_000]
    model = {"reply": "Build Herdr dashboard"}

    def gather(pane_id):
        evidence = Evidence(
            agent="claude", workspace_label="app", cwd="/w", branch="main",
            title=fake.title, recent_output=None,
        )
        return evidence, {"name": None, "agent_session": {"value": "s1"}, "tab_id": "w1:t6"}

    monkeypatch.setattr(rename, "herdr", fake)
    monkeypatch.setattr(rename, "gather_evidence", gather)
    monkeypatch.setattr(rename, "context_tokens", lambda session: None)
    monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
    monkeypatch.setattr(rename, "request_name", lambda *args: model["reply"])
    monkeypatch.setattr(rename, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))

    def run(advance=120, force=False):
        clock[0] += advance
        rename.rename_pane("w1:p1", force=force)

    return SimpleNamespace(herdr=fake, model=model, run=run)


def test_a_default_numbered_tab_takes_the_pane_label(flow):
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"
    assert rename.read_stamp("w1:p1").tab_label_set == "Build Herdr dashboard"


def test_the_workspace_position_counts_as_the_default_when_the_number_differs(flow):
    flow.herdr.tab()["label"] = "2"
    flow.herdr.tab()["number"] = 6
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_the_tab_number_counts_as_the_default(flow):
    flow.herdr.tab()["label"] = "6"
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_an_empty_tab_label_is_taken(flow):
    flow.herdr.tab()["label"] = ""
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_a_herdr_bar_title_is_taken_over(flow):
    flow.herdr.tab()["label"] = "Herdr workspace dashboard plugin"
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_a_previous_terminal_title_is_taken_over(flow):
    flow.model["reply"] = None
    flow.run()
    flow.herdr.tab()["label"] = flow.herdr.title
    flow.herdr.title = "A different task now"
    flow.model["reply"] = "Build Herdr dashboard"
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_a_manual_tab_name_is_kept_and_not_retried(flow):
    flow.herdr.tab()["label"] = "my own tab"
    flow.run()
    assert flow.herdr.tab_renames == []
    assert rename.read_stamp("w1:p1").tab_manual
    flow.model["reply"] = "Fix export bug"
    flow.run()
    flow.run()
    assert flow.herdr.tab()["label"] == "my own tab"
    assert flow.herdr.tab_renames == []


def test_a_tab_with_several_agents_is_left_alone(flow):
    flow.herdr.agent_tabs = ["w1:t6", "w1:t6"]
    flow.run()
    assert flow.herdr.tab_renames == []
    assert not rename.read_stamp("w1:p1").tab_manual


def test_a_follow_up_label_change_updates_our_tab(flow):
    flow.run()
    flow.model["reply"] = "Fix export bug"
    flow.herdr.title = "Something else entirely"
    flow.run()
    assert flow.herdr.pane_label == "Fix export bug"
    assert flow.herdr.tab()["label"] == "Fix export bug"


def test_a_tab_renamed_by_hand_after_ours_stops_following(flow):
    flow.run()
    flow.herdr.tab()["label"] = "hand written"
    flow.model["reply"] = "Fix export bug"
    flow.herdr.title = "Something else entirely"
    flow.run()
    assert flow.herdr.tab()["label"] == "hand written"
    assert rename.read_stamp("w1:p1").tab_manual


def test_the_fallback_label_names_the_tab_and_the_model_label_replaces_it(flow):
    flow.model["reply"] = None
    flow.run()
    assert flow.herdr.tab()["label"] == "Herdr workspace dashboard"
    flow.model["reply"] = "Build Herdr dashboard"
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_an_existing_label_pane_gets_its_tab_on_the_next_run(flow):
    flow.run()
    flow.herdr.tab()["label"] = "2"
    stamp = rename.read_stamp("w1:p1")
    rename.write_stamp("w1:p1", stamp.model_copy(update={"tab_label_set": None}))
    flow.run(advance=10)
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_the_steady_state_makes_no_tab_calls(flow, monkeypatch):
    flow.run()
    calls = []
    original = flow.herdr
    monkeypatch.setattr(rename, "herdr", lambda *args: (calls.append(args), original(*args))[1])
    flow.run()
    flow.run()
    assert not any(call[0] == "tab" for call in calls)


def test_a_manually_labelled_pane_leaves_its_tab_alone(flow):
    flow.run()
    flow.herdr.pane_label = "my own pane name"
    flow.run()
    flow.model["reply"] = "Fix export bug"
    flow.run()
    assert flow.herdr.tab()["label"] == "Build Herdr dashboard"


def test_reset_hands_back_a_tab_that_still_shows_our_label(flow):
    flow.run()
    rename.run_reset("w1:p1")
    assert flow.herdr.tab_renames[-1] == ("w1:t6", "")
    assert not rename.stamp_path("w1:p1").exists()


def test_reset_leaves_a_tab_the_user_renamed_since(flow):
    flow.run()
    flow.herdr.tab()["label"] = "hand written"
    rename.run_reset("w1:p1")
    assert flow.herdr.tab()["label"] == "hand written"


def test_reset_falls_back_to_the_position_when_herdr_rejects_an_empty_tab_label(flow, monkeypatch):
    flow.run()
    original = flow.herdr

    def reject_empty(*args):
        if args[:2] == ("tab", "rename") and args[3] == "":
            return {"error": "label required"}
        return original(*args)

    monkeypatch.setattr(rename, "herdr", reject_empty)
    rename.run_reset("w1:p1")
    assert flow.herdr.tab()["label"] == "2"
