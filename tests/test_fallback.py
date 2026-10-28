import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Evidence, Stamp  # noqa: E402

# --- fallback text -----------------------------------------------------------

def test_a_short_title_is_used_whole():
    assert rename.fallback_label("Fix CSV export") == "Fix CSV export"


def test_a_long_title_is_cut_at_a_word_boundary_within_26_columns():
    label = rename.fallback_label("Routing messages to Acra sessions")
    assert label == "Routing messages to Acra"
    assert rename.display_width(label) <= rename.MAX_LABEL_CHARS


def test_exactly_26_columns_fits():
    assert rename.fallback_label("Install Herdr-bar easyjump") == "Install Herdr-bar easyjump"


def test_one_overlong_word_is_hard_cut():
    assert rename.fallback_label("a" * 40) == "a" * 26


def test_wide_characters_count_double():
    label = rename.fallback_label("漢字漢字漢字漢字漢字漢字漢字 漢字")
    assert rename.display_width(label) <= rename.MAX_LABEL_CHARS


def test_spinner_glyphs_and_blank_titles():
    assert rename.fallback_label("✳ Scrape tweets") == "Scrape tweets"
    assert rename.fallback_label("   ") is None
    assert rename.fallback_label(None) is None


# --- fallback through the whole rename flow ----------------------------------

class Herdr:
    """Just enough of the herdr CLI to follow what the plugin does to one pane."""

    def __init__(self):
        self.label = None
        self.title = "Routing messages to Acra sessions"
        self.pane_renames = []

    def __call__(self, *args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"label": self.label}}
        if args[:2] == ("pane", "rename"):
            self.label = args[3] or None
            self.pane_renames.append(args[3])
            return {"pane": {}}
        if args[:2] == ("agent", "rename"):
            return {"agent": {}}
        return {}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "config"))
    fake = Herdr()
    clock = [1_000_000]
    model = {"reply": None}

    def gather(pane_id):
        evidence = Evidence(
            agent="claude", workspace_label="app", cwd="/w", branch="main",
            title=fake.title,
        )
        return evidence, {"name": None, "agent_session": {"value": "s1"}}

    monkeypatch.setattr(rename, "herdr", fake)
    monkeypatch.setattr(rename, "gather_evidence", gather)
    monkeypatch.setattr(rename, "context_tokens", lambda session: None)
    monkeypatch.setattr(rename, "load_config", lambda: SimpleNamespace(provider="x"))
    monkeypatch.setattr(rename, "request_name", lambda *args: model["reply"])
    monkeypatch.setattr(rename, "time", SimpleNamespace(time=lambda: clock[0], strftime=time.strftime))

    def run(advance=120):
        clock[0] += advance
        rename.rename_pane("w1:p1", force=False)

    return SimpleNamespace(herdr=fake, model=model, run=run)


def test_a_pane_with_no_model_label_shows_its_title(flow):
    flow.run()
    assert flow.herdr.label == "Routing messages to Acra"
    stamp = rename.read_stamp("w1:p1")
    assert stamp.fallback and stamp.label_set == flow.herdr.label and not stamp.manual


def test_the_model_label_replaces_the_fallback(flow):
    flow.run()
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    assert flow.herdr.label == "Route voice to sessions"
    stamp = rename.read_stamp("w1:p1")
    assert not stamp.fallback and stamp.label_set == "Route voice to sessions"


def test_an_unchanged_title_does_not_rewrite_the_fallback(flow):
    flow.run()
    flow.run()
    flow.run()
    assert flow.herdr.pane_renames == ["Routing messages to Acra"]


def test_a_changed_title_refreshes_the_fallback(flow):
    flow.run()
    flow.herdr.title = "Whole-codebase review loop"
    flow.run()
    assert flow.herdr.label == "Whole-codebase review loop"
    assert rename.read_stamp("w1:p1").fallback


def test_a_model_error_and_an_invalid_label_both_leave_the_fallback(flow):
    flow.model["reply"] = "this label is far too long to fit the sidebar row"
    flow.run()
    assert flow.herdr.label == "Routing messages to Acra"


def test_a_hand_rename_over_the_fallback_is_adopted_and_then_left_alone(flow):
    flow.run()
    flow.herdr.label = "my own name"
    flow.run()
    assert rename.read_stamp("w1:p1").manual
    flow.herdr.title = "A completely different task"
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    assert flow.herdr.label == "my own name"
    assert flow.herdr.pane_renames == ["Routing messages to Acra"]


def test_a_label_already_on_the_pane_is_not_overwritten_by_the_fallback(flow):
    flow.herdr.label = "set before the plugin saw it"
    flow.run()
    assert flow.herdr.pane_renames == []


def test_a_model_label_is_not_replaced_by_a_fallback_after_a_later_failure(flow):
    flow.model["reply"] = "Route voice to sessions"
    flow.run()
    flow.herdr.title = "Something else entirely"
    flow.model["reply"] = None
    flow.run()
    assert flow.herdr.label == "Route voice to sessions"
    assert not rename.read_stamp("w1:p1").fallback


def test_no_title_means_no_fallback(flow):
    flow.herdr.title = ""
    flow.run()
    assert flow.herdr.pane_renames == []


def test_a_fallback_stamp_keeps_asking_the_model_even_when_the_title_is_unchanged():
    stamp = Stamp(label_set="Routing messages", title_hash="h", agent_session_id="s", set_at=0, fallback=True)
    assert rename.decide(stamp, "Routing messages", "s", "h", now=1000) == "rename"
    stamp.fallback = False
    assert rename.decide(stamp, "Routing messages", "s", "h", now=1000) == "skip-unchanged"
