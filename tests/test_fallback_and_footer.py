import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Evidence, Stamp  # noqa: E402

RULE = "─" * 100


# --- footer trimming ---------------------------------------------------------

def screen(*body, prompt="❯"):
    footer = [RULE, prompt, RULE, "    ~/Projects/app (main)", "    🧠 Opus 5.5 │ Ctx: 15%", "  ⏵⏵ auto mode on"]
    return "\n".join([*body, *footer])


def test_the_input_box_and_statusline_are_dropped():
    text = screen("⏺ Fixed the export.", "✻ Baked for 8s")
    assert rename.trim_agent_footer(text) == "⏺ Fixed the export.\n✻ Baked for 8s"


def test_a_titled_rule_marks_the_box_too():
    titled = "─" * 80 + " Routing messages to Acra sessions ─"
    text = "\n".join(["⏺ Done.", titled, "❯ ok restart agentd", RULE, "    statusline"])
    assert rename.trim_agent_footer(text) == "⏺ Done."


def test_blank_lines_between_rule_and_prompt_are_tolerated():
    text = "\n".join(["⏺ Done.", RULE, "", "❯", RULE, "statusline"])
    assert rename.trim_agent_footer(text) == "⏺ Done."


def test_a_multi_line_draft_in_the_box_is_dropped_whole():
    text = screen("⏺ Done.", prompt="❯ first line of a draft\n  second line")
    assert rename.trim_agent_footer(text) == "⏺ Done."


def test_earlier_user_messages_survive():
    text = screen("❯ add csv export", "⏺ Added.", "❯ now fix the test", "⏺ Fixed.")
    assert rename.trim_agent_footer(text) == "❯ add csv export\n⏺ Added.\n❯ now fix the test\n⏺ Fixed."


def test_a_prompt_without_a_rule_above_it_is_not_a_marker():
    text = "⏺ Added.\n❯ a user message with no input box on screen\n⏺ Working on it"
    assert rename.trim_agent_footer(text) == text


def test_output_without_any_prompt_is_untouched():
    text = "$ ls\nfile.txt\n" + RULE
    assert rename.trim_agent_footer(text) == text


def test_a_short_rule_is_not_a_marker():
    text = "⏺ Added.\n---------\n❯\nstatus"
    assert rename.trim_agent_footer(text) == text


def test_gather_evidence_sends_the_trimmed_output(monkeypatch):
    def fake_herdr(*args):
        if args[:2] == ("agent", "get"):
            return {"agent": {"agent": "claude", "cwd": "/w", "workspace_id": "w1"}}
        if args[:2] == ("pane", "get"):
            return {"pane": {"terminal_title_stripped": "Some task title"}}
        return {"workspaces": []}

    monkeypatch.setattr(rename, "herdr", fake_herdr)
    monkeypatch.setattr(rename, "herdr_raw", lambda *args: screen("⏺ Fixed the export."))
    monkeypatch.setattr(rename, "git_branch", lambda cwd: None)
    evidence, _ = rename.gather_evidence("w1:p1")
    assert evidence.recent_output == "⏺ Fixed the export."


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
            title=fake.title, recent_output=None,
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
