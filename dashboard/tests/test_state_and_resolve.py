import os
import stat
import tempfile
import unittest

from herdr_dashboard.config import ConfigError, parse
from herdr_dashboard.resolve import resolve
from herdr_dashboard.state import State

CONFIG = '''
[personal]
command = "sh"

[workspaces.disposable-camera]
command = ["sh", "-c", "exit 0"]
[workspaces.cloud-config]
command = "sh"
env = { FOO = "bar" }
'''


def plan(mode, label, text=CONFIG, env=None, cwd=None):
    config = parse(text) if isinstance(text, str) else text
    workspace = {"id": "w1", "label": label, "cwd": cwd} if label else None
    return resolve(mode, config, workspace, env or dict(os.environ), "/cfg/config.toml")


class StateTests(unittest.TestCase):
    def test_default_and_persisted_mode(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            self.assertEqual(state.mode(), "work")
            state.set_mode("personal")
            self.assertEqual(State(d).mode(), "personal")

    def test_corrupt_or_unknown_state_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "state.json"), "w") as f:
                f.write("{not json")
            self.assertEqual(State(d).mode(), "work")
            State(d).set_mode("personal")
            with open(os.path.join(d, "state.json"), "w") as f:
                f.write('{"mode": "other"}')
            self.assertEqual(State(d).mode(), "work")

    def test_popup_record_roundtrip_keeps_mode(self):
        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            state.set_mode("personal")
            state.set_popup(123, "w1:t2")
            self.assertEqual((state.popup(), state.mode()), ((123, "w1:t2"), "personal"))
            state.set_popup(None)
            self.assertEqual(state.popup(), (None, None))


class ResolveTests(unittest.TestCase):
    def test_configured_work_runs_with_environment(self):
        p = plan("work", "cloud-config")
        self.assertEqual(p.kind, "run")
        self.assertTrue(p.launch.argv[0].endswith("/sh"))
        self.assertEqual(p.launch.env["FOO"], "bar")
        self.assertEqual(p.launch.env["HERDR_DASHBOARD_MODE"], "work")

    def test_configured_personal_runs_in_home_by_default(self):
        p = plan("personal", "anything")
        self.assertEqual((p.kind, p.launch.cwd), ("run", os.path.expanduser("~")))

    def test_work_cwd_defaults_to_workspace_cwd_and_explicit_cwd_wins(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(plan("work", "cloud-config", cwd=d).launch.cwd, d)
            p = plan("work", "x", f'[workspaces.x]\ncommand = "sh"\ncwd = "{d}"', cwd="/")
            self.assertEqual(p.launch.cwd, d)

    def test_workspace_without_dashboard(self):
        p = plan("work", "scratch")
        self.assertEqual((p.kind, p.title), ("message", "Work Dashboard"))
        self.assertIn("No dashboard configured for this workspace.", p.lines)

    def test_personal_unconfigured(self):
        p = plan("personal", "cloud-config", '[workspaces.cloud-config]\ncommand = "sh"')
        self.assertEqual((p.kind, p.title), ("message", "Personal Dashboard"))

    def test_neither_configured_each_mode_has_its_own_message(self):
        a, b = plan("personal", "scratch", ""), plan("work", "scratch", "")
        self.assertEqual((a.kind, b.kind), ("message", "message"))
        self.assertNotEqual(a.signature(), b.signature())

    def test_unknown_current_workspace(self):
        self.assertEqual(plan("work", None).kind, "message")

    def test_missing_executable_is_reported_with_the_command(self):
        p = plan("work", "x", '[workspaces.x]\ncommand = ["definitely-not-installed-xyz", "--flag"]')
        self.assertEqual(p.kind, "message")
        self.assertIn("Executable not found: definitely-not-installed-xyz", p.lines)
        self.assertIn("Command: definitely-not-installed-xyz --flag", p.lines)
        self.assertTrue(p.reload_hint)

    def test_relative_executable_resolves_against_cwd_and_needs_exec_bit(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "tool")
            with open(script, "w") as f:
                f.write("#!/bin/sh\n")
            text = f'[workspaces.x]\ncommand = "./tool"\ncwd = "{d}"'
            self.assertEqual(plan("work", "x", text).kind, "message")
            os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
            self.assertEqual(plan("work", "x", text).kind, "run")

    def test_tilde_in_arguments_is_expanded(self):
        p = plan("work", "x", '[workspaces.x]\ncommand = "sh"\nargs = ["~/cfg.yml", "a~b"]')
        self.assertEqual(p.launch.argv[1:], (os.path.expanduser("~/cfg.yml"), "a~b"))

    def test_missing_cwd(self):
        p = plan("work", "x", '[workspaces.x]\ncommand = "sh"\ncwd = "/nonexistent-dir-xyz"')
        self.assertIn("Working directory not found.", p.lines)

    def test_invalid_config_shows_the_error_in_either_mode(self):
        error = ConfigError("TOML syntax error: bad")
        for mode in ("personal", "work"):
            p = plan(mode, "x", error)
            self.assertEqual(p.kind, "message")
            self.assertIn("TOML syntax error: bad", p.lines)

    def test_executable_lookup_uses_the_dashboard_env_path(self):
        with tempfile.TemporaryDirectory() as d:
            tool = os.path.join(d, "mytool")
            with open(tool, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(tool, 0o755)
            text = f'[workspaces.x]\ncommand = "mytool"\nenv = {{ PATH = "{d}" }}'
            self.assertEqual(plan("work", "x", text).kind, "run")


if __name__ == "__main__":
    unittest.main()
