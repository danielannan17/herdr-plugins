import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rename import (  # noqa: E402
    HERDR_NAME_RULE,
    Evidence,
    Stamp,
    cap_tail,
    decide,
    disambiguate,
    evidence_is_thin,
    extract_name,
    normalise_title,
    redact,
    slugify,
    strip_agent_banner,
    title_hash,
    title_is_junk,
    name_for,
    validate_label,
)

HOME = "/Users/cm"


def evidence(**overrides):
    base = dict(
        agent="claude",
        workspace_label="pipette data",
        cwd="/Users/cm/dev/aa-worktrees/pipette-data-pipeline",
        branch="main",
        title=None,
        recent_output=None,
    )
    base.update(overrides)
    return Evidence(**base)


# --- title normalisation -----------------------------------------------------

def test_spinner_glyph_does_not_change_the_hash():
    a = "✳ Scrape and organize tweets from X account"
    b = "⠐ Scrape and organize tweets from X account"
    assert title_hash(a) == title_hash(b)


def test_elapsed_timer_and_token_counter_do_not_change_the_hash():
    a = "✳ Jitterbugging (3m 49s · ↓ 11.5k tokens)"
    b = "✳ Jitterbugging (7m 02s · ↓ 22.9k tokens)"
    assert title_hash(a) == title_hash(b)
    assert normalise_title(a) == "jitterbugging"


def test_trailing_ellipsis_does_not_change_the_hash():
    assert title_hash("Jitterbugging…") == title_hash("Jitterbugging")


def test_a_different_task_does_change_the_hash():
    a = "✳ Scrape and organize tweets from X account"
    b = "✳ Evaluate upserting to BigQuery warehouse"
    assert title_hash(a) != title_hash(b)


# --- title junk classification ----------------------------------------------

def test_codex_branch_name_title_is_junk():
    assert title_is_junk("main", "/Users/cm/dev/aa-worktrees/main", "main")


def test_title_equal_to_cwd_basename_is_junk():
    assert title_is_junk("pipette-data-pipeline", "/Users/cm/dev/pipette-data-pipeline", None)


def test_empty_and_shell_titles_are_junk():
    assert title_is_junk(None, "/tmp", None)
    assert title_is_junk("", "/tmp", None)
    assert title_is_junk("zsh", "/tmp", None)
    assert title_is_junk("~/dev/thing", "/tmp", None)


def test_claude_task_title_is_not_junk():
    assert not title_is_junk(
        "✳ Scrape and organize tweets from X account", "/Users/cm/dev/aa-tweets", "main"
    )


# --- sanitisation ------------------------------------------------------------

def test_home_path_is_rewritten():
    assert redact("cd /Users/cm/dev/thing", HOME) == "cd ~/dev/thing"


def test_openai_and_github_tokens_are_redacted():
    assert "sk-" not in redact("export OPENAI_API_KEY=sk-abc123def456ghi789", HOME)
    assert "ghp_" not in redact("token ghp_0123456789abcdefghij", HOME)
    assert "github_pat_" not in redact("github_pat_11ABCDEFG0123456789_abcdefgh", HOME)


def test_aws_slack_and_bearer_tokens_are_redacted():
    assert "AKIA" not in redact("AKIAIOSFODNN7EXAMPLE", HOME)
    assert "xoxb-" not in redact("xoxb-123456789012-abcdefghijkl", HOME)
    assert "abcdefghijklmnop" not in redact("Authorization: Bearer abcdefghijklmnop", HOME)


def test_private_key_block_is_redacted():
    blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC\n-----END RSA PRIVATE KEY-----"
    assert "MIIEowIBAAKC" not in redact(blob, HOME)


def test_keyed_secret_keeps_its_key_and_loses_its_value():
    assert redact("api_key=hunter2", HOME) == "api_key=[redacted]"
    assert redact("password: hunter2", HOME) == "password=[redacted]"


def test_ordinary_text_survives_redaction():
    assert redact("running 42 tests in tests/test_upsert.py", HOME) == (
        "running 42 tests in tests/test_upsert.py"
    )


def test_cap_keeps_the_most_recent_tail():
    assert cap_tail("a" * 10 + "TAIL", limit=4) == "TAIL"
    assert cap_tail("short", limit=100) == "short"


# --- evidence selection ------------------------------------------------------

def test_banner_lines_are_stripped():
    banner = "╭──────────╮\n│ Welcome to Claude Code │\n╰──────────╯\n  cwd: /Users/cm/dev\n"
    assert strip_agent_banner(banner).strip() == ""


def test_a_good_title_alone_is_enough_evidence():
    assert not evidence_is_thin(evidence(title="✳ Evaluate upserting to BigQuery warehouse"))


def test_junk_title_with_no_output_is_thin():
    assert evidence_is_thin(evidence(title="main", recent_output=""))


def test_junk_title_with_substantial_output_is_not_thin():
    body = "diff --git a/upsert.py b/upsert.py\n- old ordering\n+ new ordering, dedup on key\n"
    assert not evidence_is_thin(evidence(title="main", recent_output=body))


def test_banner_only_output_is_thin():
    assert evidence_is_thin(
        evidence(title=None, recent_output="╭────╮\n│ Welcome to Claude Code │\n╰────╯")
    )


# --- label validation --------------------------------------------------------

def test_an_imperative_phrase_passes():
    assert validate_label("Dedupe eval results") == "Dedupe eval results"


def test_first_word_is_capitalised_and_acronyms_survive():
    assert validate_label("fix CSV export") == "Fix CSV export"
    assert validate_label("  review auth flow  ") == "Review auth flow"


def test_the_26_character_limit_is_hard():
    assert validate_label("Mark nonreasoning models") == "Mark nonreasoning models"
    assert len("Mark nonreasoning models") == 24
    assert validate_label("Deduplicate the evaluation results") is None


def test_wrong_word_counts_are_rejected():
    assert validate_label("Dedupe") is None
    assert validate_label("a b c d e f") is None


def test_labels_that_name_the_tool_are_rejected():
    assert validate_label("Run Claude session") is None
    assert validate_label("Codex agent work") is None


def test_junk_shapes_are_rejected():
    assert validate_label("") is None
    assert validate_label(None) is None
    assert validate_label("2 fast 2 furious") is None


def test_a_label_yields_a_valid_cli_alias():
    for label in ["Dedupe eval results", "Fix CSV export", "Mark nonreasoning models"]:
        alias = name_for(label)
        assert alias is not None
        assert HERDR_NAME_RULE.match(alias), alias


def test_slugify_strips_leading_non_letters_because_names_must_start_with_one():
    assert slugify("2 Fast 2 Furious") == "fast-2-furious"
    assert slugify("--Weird--Input--") == "weird-input"


# --- collision disambiguation ------------------------------------------------

def test_first_attempt_prefixes_the_workspace_as_a_slug():
    assert disambiguate("scrape-tweets", "pipette data", 1) == "pipette-scrape-tweets"


def test_later_attempts_use_a_numeric_suffix_within_the_length_cap():
    result = disambiguate("migrate-every-vault-key", "pipette data", 2)
    assert result.endswith("-3")
    assert len(result) <= 32


def test_every_disambiguation_still_satisfies_herdrs_rule():
    for attempt in (1, 2, 3):
        for workspace in ("pipette data", "measurement backend", ""):
            candidate = disambiguate("migrate-every-vault-key", workspace, attempt)
            assert len(candidate) <= 32, candidate
            assert HERDR_NAME_RULE.match(candidate), candidate


# --- model reply parsing -----------------------------------------------------

def test_bare_json_object_is_parsed():
    assert extract_name('{"name": "Scrape Tweets", "reason": "x"}') == "Scrape Tweets"


def test_json_wrapped_in_prose_is_parsed():
    reply = 'Sure! Here you go:\n```json\n{"name": "Review Auth Flow"}\n```\nHope that helps.'
    assert extract_name(reply) == "Review Auth Flow"


def test_explicit_abstention_returns_none():
    assert extract_name('{"name": null, "reason": "insufficient evidence"}') is None


def test_unparseable_reply_returns_none():
    assert extract_name("I cannot name this session.") is None
    assert extract_name("{ not json at all }") is None


# --- stamp state machine -----------------------------------------------------

NOW = 1_784_937_769
SESSION = "66087717-d7aa-4ced-a43e-850d3a1cc51f"


def stamp(**overrides):
    base = dict(
        label_set="Scrape tweets", name_set="scrape-tweets",
        title_hash=title_hash("✳ Scrape and organize tweets"),
        agent_session_id=SESSION,
        set_at=NOW - 600,
        manual=False,
    )
    base.update(overrides)
    return Stamp(**base)


def test_no_stamp_means_rename():
    assert decide(None, None, SESSION, "hash", NOW) == "rename"


def test_manual_stamp_is_never_touched():
    assert decide(stamp(manual=True), "My name", SESSION, "other", NOW) == "skip-manual"


def test_a_hand_rename_is_adopted_as_manual():
    assert decide(stamp(), "Hand picked", SESSION, "other", NOW) == "adopt-manual"


def test_unchanged_title_is_skipped():
    existing = stamp()
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW) == "skip-unchanged"


def test_materially_changed_title_triggers_a_rename():
    assert decide(stamp(), "Scrape tweets", SESSION, "different-hash", NOW) == "rename"


def test_a_replacement_agent_in_the_same_pane_is_renamed():
    assert decide(stamp(), None, "a-different-session", "hash", NOW) == "rename"


def test_a_cleared_name_is_rewritten_once_past_the_floor():
    assert decide(stamp(), None, SESSION, "hash", NOW) == "rename"


def test_the_rename_floor_is_respected():
    assert decide(stamp(set_at=NOW - 5), None, SESSION, "new-hash", NOW) == "skip-floor"


def test_context_growth_past_the_step_triggers_a_rename():
    existing = stamp(tokens_at_naming=120_000)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=220_000) == "rename"


def test_context_growth_short_of_the_step_does_not():
    existing = stamp(tokens_at_naming=120_000)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=219_999) == "skip-unchanged"


def test_a_stamp_with_no_baseline_never_triggers_on_tokens():
    existing = stamp(tokens_at_naming=None)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=900_000) == "skip-unchanged"


def test_force_overrides_every_skip():
    assert decide(stamp(manual=True), "My name", SESSION, "same", NOW, force=True) == "rename"
