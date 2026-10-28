import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
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
    render_evidence,
    slugify,
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

def test_a_good_title_alone_is_enough_evidence():
    assert not evidence_is_thin(evidence(title="✳ Evaluate upserting to BigQuery warehouse"))


def test_junk_title_with_no_messages_is_thin():
    assert evidence_is_thin(evidence(title="main"))


def test_junk_title_with_a_user_message_is_not_thin():
    assert not evidence_is_thin(evidence(title="main", latest_messages=["fix the upsert ordering"]))


def test_no_title_and_no_messages_is_thin():
    assert evidence_is_thin(evidence(title=None))


def test_a_session_title_alone_is_enough_evidence():
    assert not evidence_is_thin(evidence(title="main", ai_title="Claude Code random scrolling bug"))
    assert not evidence_is_thin(evidence(title="main", custom_title="Herdr tab plugin"))


def test_messages_are_rendered_redacted_and_capped():
    rendered = render_evidence(
        evidence(latest_messages=["look at /Users/cm/dev/app", "word " * 100, "line one\n   line two"]), HOME
    )
    assert "- look at ~/dev/app" in rendered
    assert "- " + ("word " * 100)[:200] + "\n" in rendered
    assert "- line one line two" in rendered


def test_first_and_latest_messages_are_separate_labelled_blocks():
    rendered = render_evidence(evidence(first_messages=["opening request"], latest_messages=["latest step"]), HOME)
    first, latest = rendered.index("first messages (how the session started):\n- opening request"), rendered.index(
        "latest messages, oldest first:\n- latest step"
    )
    assert first < latest


def test_session_titles_come_before_the_terminal_title_and_are_redacted():
    rendered = render_evidence(
        evidence(
            title="✳ Evaluate upserting to BigQuery",
            ai_title="Claude Code scrolling bug in /Users/cm/dev",
            custom_title="Herdr tab plugin",
            first_messages=["opening request"],
        ),
        HOME,
    )
    custom = rendered.index("session title, set by the user: Herdr tab plugin")
    auto = rendered.index("session title, auto-generated: Claude Code scrolling bug in ~/dev")
    terminal = rendered.index("terminal title: ✳ Evaluate upserting to BigQuery")
    assert custom < auto < terminal < rendered.index("first messages")


def test_a_full_set_of_messages_fits_the_evidence_cap():
    first = [f"first {n} " + "x" * 300 for n in range(5)]
    latest = [f"latest {n} " + "x" * 300 for n in range(15)]
    rendered = render_evidence(evidence(first_messages=first, latest_messages=latest), HOME)
    assert all(f"- first {n} " in rendered for n in range(5))
    assert all(f"- latest {n} " in rendered for n in range(15))


def test_the_evidence_cap_cuts_old_latest_messages_never_the_titles(monkeypatch):
    monkeypatch.setattr(rename, "EVIDENCE_CHAR_CAP", 60)
    rendered = render_evidence(
        evidence(
            ai_title="auto title " * 30,
            custom_title="custom title " * 30,
            title="✳ Evaluate upserting to BigQuery",
            first_messages=["opening request " * 10],
            latest_messages=[f"step {n} with some words" for n in range(10)],
        ),
        HOME,
    )
    assert ("auto title " * 30).strip() in rendered and ("custom title " * 30).strip() in rendered
    assert "terminal title: ✳ Evaluate upserting to BigQuery" in rendered
    assert ("opening request " * 10).strip() in rendered
    assert "step 9 " in rendered and "step 0 " not in rendered


def test_pane_output_is_not_in_the_evidence():
    assert "recent output" not in render_evidence(evidence(title="main"), HOME)


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


def test_a_compaction_since_naming_triggers_a_rename():
    existing = stamp(tokens_at_naming=300_000, set_at=NOW - 600)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=None, compacted_at=NOW - 60) == "rename"


def test_a_compaction_before_naming_does_not():
    existing = stamp(tokens_at_naming=40_000, set_at=NOW - 600)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=45_000, compacted_at=NOW - 900) == "skip-unchanged"


def test_a_token_drop_of_half_and_50k_reads_as_compaction():
    existing = stamp(tokens_at_naming=300_000)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=150_000) == "rename"


def test_a_token_dip_short_of_half_is_not_compaction():
    existing = stamp(tokens_at_naming=300_000)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=150_001) == "skip-unchanged"


def test_a_small_context_cannot_drop_far_enough_to_be_compaction():
    existing = stamp(tokens_at_naming=40_000)
    assert decide(existing, "Scrape tweets", SESSION, existing.title_hash, NOW,
                  tokens=5_000) == "skip-unchanged"


def test_force_overrides_every_skip():
    assert decide(stamp(manual=True), "My name", SESSION, "same", NOW, force=True) == "rename"
