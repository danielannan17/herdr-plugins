import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import rename  # noqa: E402
from rename import Config  # noqa: E402

SETTINGS = [
    "OPENAI_API_KEY", "SMART_RENAME_MODEL", "SMART_RENAME_BASE_URL", "SMART_RENAME_TIMEOUT_MS",
    "SMART_RENAME_PROVIDER", "SMART_RENAME_CLAUDE_MODEL", "SMART_RENAME_CLAUDE_BIN",
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "config").mkdir()
    return tmp_path


def write_env(tmp_path, text):
    (tmp_path / "config" / "provider.env").write_text(text)


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def claude_config(**overrides):
    base = dict(provider="claude", model="haiku", timeout_ms=7000)
    base.update(overrides)
    return Config(**base)


def read_log(tmp_path):
    return (tmp_path / "state" / "rename.log").read_text()


# --- provider selection ------------------------------------------------------

def test_openai_is_the_default_and_needs_a_key(isolated, monkeypatch):
    assert rename.load_config() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = rename.load_config()
    assert (config.provider, config.model) == ("openai", "gpt-5.6-luna")


def test_claude_provider_needs_no_api_key_and_defaults_to_haiku(isolated, monkeypatch):
    monkeypatch.setenv("SMART_RENAME_PROVIDER", "claude")
    config = rename.load_config()
    assert (config.provider, config.model, config.timeout_ms) == ("claude", "haiku", 20000)


def test_claude_ignores_the_openai_model_setting(isolated):
    write_env(isolated, "SMART_RENAME_PROVIDER=claude\nSMART_RENAME_MODEL=gpt-5.6-luna\n")
    assert rename.load_config().model == "haiku"


def test_provider_comes_from_provider_env_and_the_process_environment_wins(isolated, monkeypatch):
    write_env(isolated, "SMART_RENAME_PROVIDER=claude\nSMART_RENAME_CLAUDE_MODEL=sonnet\n")
    assert rename.load_config().model == "sonnet"
    monkeypatch.setenv("SMART_RENAME_PROVIDER", "openai")
    assert rename.load_config() is None  # openai selected, no key


def test_an_unknown_provider_is_unusable(isolated, monkeypatch):
    monkeypatch.setenv("SMART_RENAME_PROVIDER", "carrier-pigeon")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert rename.load_config() is None


def test_request_name_dispatches_on_the_provider(monkeypatch):
    monkeypatch.setattr(rename, "request_name_claude", lambda *args: "from-claude")
    monkeypatch.setattr(rename, "request_name_openai", lambda *args: "from-openai")
    assert rename.request_name(claude_config(), "p", "e") == "from-claude"
    assert rename.request_name(Config(api_key="k", model="m", base_url="u", timeout_ms=1), "p", "e") == "from-openai"


# --- binary resolution -------------------------------------------------------

def test_override_wins_over_path(isolated, monkeypatch):
    on_path = executable(isolated / "bin" / "claude")
    mine = executable(isolated / "mine" / "claude")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert rename.resolve_claude_bin(str(mine)) == str(mine)


def test_a_bad_override_is_not_silently_replaced(isolated, monkeypatch):
    on_path = executable(isolated / "bin" / "claude")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert rename.resolve_claude_bin(str(isolated / "missing")) is None


def test_path_is_searched(isolated, monkeypatch):
    on_path = executable(isolated / "bin" / "claude")
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert rename.resolve_claude_bin(None) == str(on_path)


def test_install_locations_cover_a_minimal_path(isolated, monkeypatch):
    monkeypatch.setenv("PATH", str(isolated / "empty"))
    installed = executable(isolated / "home" / ".local" / "bin" / "claude")
    assert rename.resolve_claude_bin(None) == str(installed)


def test_nothing_found_returns_none(isolated, monkeypatch):
    monkeypatch.setenv("PATH", str(isolated / "empty"))
    monkeypatch.setattr(rename, "CLAUDE_INSTALL_PATHS", ("~/.local/bin/claude",))
    assert rename.resolve_claude_bin(None) is None


def test_a_non_executable_file_is_skipped(isolated, monkeypatch):
    plain = isolated / "home" / ".local" / "bin" / "claude"
    plain.parent.mkdir(parents=True)
    plain.write_text("x")
    monkeypatch.setenv("PATH", str(isolated / "empty"))
    monkeypatch.setattr(rename, "CLAUDE_INSTALL_PATHS", ("~/.local/bin/claude",))
    assert rename.resolve_claude_bin(None) is None


# --- command construction ----------------------------------------------------

def test_command_disables_tools_mcp_settings_hooks_and_sessions():
    command = rename.claude_command("/bin/claude", "haiku", "SYSTEM PROMPT")
    assert command[:2] == ["/bin/claude", "-p"]
    assert command[command.index("--model") + 1] == "haiku"
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--system-prompt") + 1] == "SYSTEM PROMPT"
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in command


def test_env_drops_parent_claude_session_variables_but_keeps_auth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/some/plugin")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("AI_AGENT", "claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = rename.claude_env("/opt/x/claude")
    assert not {"CLAUDE_CODE_PLUGIN_DIRS", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "AI_AGENT"} & env.keys()
    assert env["CLAUDE_CONFIG_DIR"] == "/cfg" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["PATH"] == f"/opt/x{os.pathsep}/usr/bin"


# --- running the CLI ---------------------------------------------------------

class FakeRun:
    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.raises = raises
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def binary(isolated):
    return executable(isolated / "bin" / "claude")


def reply(name, **extra):
    return json.dumps({"result": json.dumps({"name": name, "reason": "r"}), "is_error": False, **extra})


def test_a_successful_call_returns_the_label_and_feeds_evidence_on_stdin(binary, monkeypatch):
    run = FakeRun(reply("Route voice to sessions"))
    monkeypatch.setattr(rename.subprocess, "run", run)
    config = claude_config(claude_bin=str(binary), timeout_ms=7000)
    assert rename.request_name_claude(config, "PROMPT", "EVIDENCE") == "Route voice to sessions"
    command, kwargs = run.calls[0]
    assert command[0] == str(binary) and "PROMPT" in command
    assert kwargs["input"] == "EVIDENCE"
    assert kwargs["timeout"] == 7
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in kwargs["env"]


def test_model_abstention_returns_none(binary, monkeypatch):
    monkeypatch.setattr(rename.subprocess, "run", FakeRun(reply(None)))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None


def test_timeout_abstains_and_is_logged(binary, isolated, monkeypatch):
    monkeypatch.setattr(rename.subprocess, "run", FakeRun(raises=subprocess.TimeoutExpired("claude", 7)))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None
    assert "timed out" in read_log(isolated)


def test_nonzero_exit_abstains_and_logs_stderr(binary, isolated, monkeypatch):
    monkeypatch.setattr(rename.subprocess, "run", FakeRun(returncode=1, stderr="not logged in"))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None
    assert "not logged in" in read_log(isolated)


def test_cli_error_payload_abstains(binary, isolated, monkeypatch):
    payload = json.dumps({"is_error": True, "result": "Credit balance too low"})
    monkeypatch.setattr(rename.subprocess, "run", FakeRun(payload))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None
    assert "Credit balance" in read_log(isolated)


def test_unparseable_output_abstains(binary, monkeypatch):
    monkeypatch.setattr(rename.subprocess, "run", FakeRun("not json"))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None


def test_spawn_failure_abstains(binary, monkeypatch):
    monkeypatch.setattr(rename.subprocess, "run", FakeRun(raises=PermissionError("denied")))
    assert rename.request_name_claude(claude_config(claude_bin=str(binary)), "p", "e") is None


def test_missing_binary_is_logged_without_spawning(isolated, monkeypatch):
    run = FakeRun(reply("Never reached"))
    monkeypatch.setattr(rename.subprocess, "run", run)
    monkeypatch.setenv("PATH", str(isolated / "empty"))
    monkeypatch.setattr(rename, "CLAUDE_INSTALL_PATHS", ())
    assert rename.request_name_claude(claude_config(), "p", "e") is None
    assert run.calls == []
    assert "claude CLI not found" in read_log(isolated)


# --- check-ai ----------------------------------------------------------------

def test_check_reports_the_binary_version_without_a_model_call(binary, monkeypatch):
    run = FakeRun("2.1.312 (Claude Code)\n")
    monkeypatch.setattr(rename.subprocess, "run", run)
    status = rename.claude_status(claude_config(claude_bin=str(binary)))
    assert f"binary={binary}" in status and "2.1.312" in status
    assert run.calls[0][0] == [str(binary), "--version"]


def test_check_flags_a_missing_binary(isolated, monkeypatch):
    monkeypatch.setenv("PATH", str(isolated / "empty"))
    monkeypatch.setattr(rename, "CLAUDE_INSTALL_PATHS", ())
    assert "NOT FOUND" in rename.claude_status(claude_config())


def test_run_check_names_the_claude_provider(binary, monkeypatch, capsys):
    monkeypatch.setenv("SMART_RENAME_PROVIDER", "claude")
    monkeypatch.setenv("SMART_RENAME_CLAUDE_BIN", str(binary))
    monkeypatch.setattr(rename, "herdr", lambda *args: None)
    monkeypatch.setattr(rename.subprocess, "run", FakeRun("2.1.312 (Claude Code)\n"))
    rename.run_check()
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("provider: claude ") and "model=haiku" in first
