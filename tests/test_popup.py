"""End-to-end tests of the popup process on a real pty with real child processes."""
import os
import signal
import tempfile
import time
import unittest

from tests.support import Popup

TAB, BTAB, LEADER = b"\t", b"\x1b[Z", b"\x02"

CONFIG = '''
[personal]
command = ["sh", "-c", "echo PERSONAL-UP; exec cat -vt"]

[workspaces.disposable-camera]
command = ["sh", "-c", "echo DC-UP; exec cat -vt"]

[workspaces.cloud-config]
command = ["sh", "-c", "echo CC-UP; exec cat -vt"]
'''


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class PopupCase(unittest.TestCase):
    def open(self, config=CONFIG, workspace="disposable-camera", state_dir=None, **kw):
        popup = Popup(config, workspace, state_dir, **kw)
        self.addCleanup(popup.close)
        return popup


class OpenAndSwitchTests(PopupCase):
    def test_open_configured_work_dashboard(self):
        p = self.open()
        self.assertTrue(p.wait_for("DC-UP"))

    def test_open_configured_personal_dashboard(self):
        p = self.open()
        p.wait_for("DC-UP")
        p.send(LEADER + b"p")
        self.assertTrue(p.wait_for("PERSONAL-UP"))
        self.assertEqual(p.mode(), "personal")

    def test_switch_personal_to_work_and_back(self):
        p = self.open()
        p.wait_for("DC-UP")
        p.send(LEADER + TAB)
        self.assertTrue(p.wait_for("PERSONAL-UP"))
        p.send(LEADER + TAB)
        self.assertTrue(p.wait_for("DC-UP"))
        p.send(LEADER + BTAB)
        self.assertTrue(p.wait_for("PERSONAL-UP"))
        p.send(LEADER + b"w")
        self.assertTrue(p.wait_for("DC-UP"))
        self.assertEqual(p.mode(), "work")

    def test_mode_is_remembered_across_workspaces(self):
        """Open in one workspace, switch to Personal, close, open from another: Personal; Tab: that workspace's."""
        with tempfile.TemporaryDirectory() as state:
            first = self.open(workspace="disposable-camera", state_dir=state)
            self.assertTrue(first.wait_for("DC-UP"))
            first.send(LEADER + TAB)
            self.assertTrue(first.wait_for("PERSONAL-UP"))
            first.send(LEADER + b"d")
            self.assertTrue(first.exited())

            second = self.open(workspace="cloud-config", state_dir=state)
            self.assertTrue(second.wait_for("PERSONAL-UP"))
            second.send(LEADER + TAB)
            self.assertTrue(second.wait_for("CC-UP"))

    def test_input_reaches_the_dashboard_and_footer_shows_the_mode(self):
        p = self.open()
        p.wait_for("DC-UP")
        p.send(b"typed\n")
        self.assertTrue(p.wait_for("typed"))
        self.assertTrue(p.wait_for("Work · disposable-camera"))

    def test_literal_leader_reaches_the_dashboard(self):
        p = self.open()
        p.wait_for("DC-UP")
        p.send(LEADER + LEADER + b"\n")
        self.assertTrue(p.wait_for("^B"))

    def test_resize_reaches_the_dashboard(self):
        import fcntl, struct, termios
        config = '[workspaces.x]\ncommand = ["sh", "-c", "trap \\"stty size\\" WINCH; stty size; while :; do sleep 0.1; done"]'
        p = self.open(config, "x", rows=30, cols=100)
        self.assertTrue(p.wait_for("29 100"))  # one row is the footer
        fcntl.ioctl(p.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.kill(p.pid, signal.SIGWINCH)
        self.assertTrue(p.wait_for("39 120"))

    def test_env_and_cwd_reach_the_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            config = (f'[workspaces.x]\ncommand = ["sh", "-c", "echo CWD=$(pwd -P) FOO=$FOO MODE=$HERDR_DASHBOARD_MODE; sleep 5"]\n'
                      f'cwd = "{d}"\nenv = {{ FOO = "bar" }}')
            p = self.open(config, "x")
            self.assertTrue(p.wait_for(f"CWD={os.path.realpath(d)} FOO=bar MODE=work"))


class BareKeyTests(PopupCase):
    def test_tab_belongs_to_the_dashboard_by_default(self):
        p = self.open()
        p.wait_for("DC-UP")
        p.send(b"a\tb\n")
        self.assertTrue(p.wait_for("a^Ib"))
        self.assertFalse(p.wait_for("PERSONAL-UP", 0.5))

    def test_bare_tab_shift_tab_and_esc_when_enabled(self):
        p = self.open(CONFIG + "\n[keys]\nbare = true\n")
        p.wait_for("DC-UP")
        p.send(TAB)
        self.assertTrue(p.wait_for("PERSONAL-UP"))
        p.send(BTAB)
        self.assertTrue(p.wait_for("DC-UP"))
        p.send(b"\x1b")
        self.assertTrue(p.exited())

    def test_per_dashboard_override(self):
        config = CONFIG.replace('[workspaces.cloud-config]', '[workspaces.cloud-config]\nbare_keys = true')
        p = self.open(config, "cloud-config")
        p.wait_for("CC-UP")
        p.send(TAB)
        self.assertTrue(p.wait_for("PERSONAL-UP"))

    def test_escape_sequences_are_not_mistaken_for_escape(self):
        p = self.open(CONFIG + "\n[keys]\nbare = true\n")
        p.wait_for("DC-UP")
        p.send(b"\x1b[A\n")  # an arrow key
        self.assertTrue(p.wait_for("^[[A"))
        self.assertFalse(p.exited(0.3))


class MissingConfigurationTests(PopupCase):
    def test_workspace_without_dashboard_shows_message_and_can_switch(self):
        p = self.open(workspace="scratch")
        self.assertTrue(p.wait_for("No dashboard configured for this workspace."))
        self.assertTrue(p.wait_for("Work Dashboard"))
        self.assertTrue(p.wait_for("Switch to Personal"))
        p.send(TAB)
        self.assertTrue(p.wait_for("PERSONAL-UP"))
        p.send(LEADER + TAB)
        self.assertTrue(p.wait_for("No dashboard configured for this workspace."))

    def test_never_auto_switches_when_unavailable(self):
        with tempfile.TemporaryDirectory() as state:
            os.makedirs(state, exist_ok=True)
            with open(os.path.join(state, "state.json"), "w") as f:
                f.write('{"mode": "work"}')
            p = self.open(workspace="scratch", state_dir=state)
            p.wait_for("No dashboard configured")
            self.assertFalse(p.wait_for("PERSONAL-UP", 0.6))
            self.assertEqual(p.mode(), "work")

    def test_personal_unconfigured(self):
        config = '[workspaces.cloud-config]\ncommand = ["sh", "-c", "echo CC-UP; exec cat"]'
        p = self.open(config, "cloud-config")
        p.wait_for("CC-UP")
        p.send(LEADER + TAB)
        self.assertTrue(p.wait_for("No personal dashboard configured yet."))
        self.assertTrue(p.wait_for("Switch to Work"))
        p.send(TAB)
        self.assertTrue(p.wait_for("CC-UP"))

    def test_nothing_configured_still_opens_and_closes(self):
        p = self.open("", "anything")
        self.assertTrue(p.wait_for("No dashboard configured for this workspace."))
        p.send(TAB)
        self.assertTrue(p.wait_for("No personal dashboard configured yet."))
        p.send(b"\x1b")
        self.assertTrue(p.exited())

    def test_message_screen_closes_with_escape_and_tab_ctrl_b_d(self):
        p = self.open("", "x")
        p.wait_for("No dashboard")
        p.send(b"\x1b")
        self.assertTrue(p.exited())
        q = self.open("", "x")
        q.wait_for("No dashboard")
        q.send(LEADER + b"d")
        self.assertTrue(q.exited())

    def test_missing_executable(self):
        config = '[workspaces.x]\ncommand = ["definitely-not-installed-xyz", "--flag"]'
        p = self.open(config, "x")
        self.assertTrue(p.wait_for("Executable not found: definitely-not-installed-xyz"))
        self.assertTrue(p.wait_for("Command: definitely-not-installed-xyz --flag"))
        p.send(TAB)
        self.assertTrue(p.wait_for("No personal dashboard configured yet."))

    def test_invalid_config_shows_the_parse_error_and_recovers_on_reload(self):
        p = self.open("[personal\n", "x")
        self.assertTrue(p.wait_for("Invalid configuration."))
        self.assertTrue(p.wait_for("TOML syntax error"))
        with open(os.path.join(p.config_dir, "config.toml"), "w") as f:
            f.write('[workspaces.x]\ncommand = ["sh", "-c", "echo FIXED; exec cat"]')
        p.send(b"r")
        self.assertTrue(p.wait_for("FIXED"))

    def test_config_is_re_read_on_every_switch(self):
        p = self.open()
        p.wait_for("DC-UP")
        with open(os.path.join(p.config_dir, "config.toml"), "w") as f:
            f.write('[personal]\ncommand = ["sh", "-c", "echo EDITED; exec cat"]')
        p.send(LEADER + TAB)
        self.assertTrue(p.wait_for("EDITED"))


class FailureTests(PopupCase):
    def test_command_that_fails_shows_status_and_output_and_stays_open(self):
        config = '[workspaces.x]\ncommand = ["sh", "-c", "echo boom >&2; exit 3"]'
        p = self.open(config, "x")
        self.assertTrue(p.wait_for("Exited with status 3."))
        self.assertTrue(p.wait_for("boom"))
        self.assertTrue(p.wait_for("Switch to Personal"))
        self.assertFalse(p.exited(0.5))

    def test_command_killed_by_a_signal(self):
        config = '[workspaces.x]\ncommand = ["sh", "-c", "sleep 0.3; kill -9 $$"]'
        p = self.open(config, "x")
        self.assertTrue(p.wait_for("Killed by signal 9."))

    def test_bad_working_directory(self):
        config = '[workspaces.x]\ncommand = "sh"\ncwd = "/nonexistent-dir-xyz"'
        p = self.open(config, "x")
        self.assertTrue(p.wait_for("Working directory not found."))

    def test_reload_restarts_a_failed_dashboard(self):
        config = '[workspaces.x]\ncommand = ["sh", "-c", "exit 1"]'
        p = self.open(config, "x")
        p.wait_for("Exited with status 1.")
        with open(os.path.join(p.config_dir, "config.toml"), "w") as f:
            f.write('[workspaces.x]\ncommand = ["sh", "-c", "echo BACK; exec cat"]')
        p.send(b"r")
        self.assertTrue(p.wait_for("BACK"))

    def test_clean_exit_after_use_closes_the_popup(self):
        config = '[workspaces.x]\ncommand = ["sh", "-c", "sleep 1.2"]'
        p = self.open(config, "x")
        self.assertTrue(p.exited(5))


class LifecycleTests(PopupCase):
    def pid_config(self, pidfile):
        return f'[workspaces.x]\ncommand = ["sh", "-c", "echo $$ > {pidfile}; echo UP; exec sleep 987"]'

    def child_pid(self, pidfile, popup):
        popup.wait_for("UP")
        with open(pidfile) as f:
            return int(f.read())

    def test_closing_kills_the_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            p = self.open(self.pid_config(pidfile), "x")
            pid = self.child_pid(pidfile, p)
            p.send(LEADER + b"d")
            self.assertTrue(p.exited())
            time.sleep(0.2)
            self.assertFalse(alive(pid))

    def test_switching_kills_the_old_dashboard(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            p = self.open(self.pid_config(pidfile) + '\n[personal]\ncommand = ["sh", "-c", "echo P2; exec cat"]', "x")
            pid = self.child_pid(pidfile, p)
            p.send(LEADER + TAB)
            self.assertTrue(p.wait_for("P2"))
            self.assertFalse(alive(pid))
            self.assertFalse(p.exited(0.3))

    def test_herdr_closing_the_popup_hangs_up_the_supervisor(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            p = self.open(self.pid_config(pidfile), "x")
            pid = self.child_pid(pidfile, p)
            os.kill(p.pid, signal.SIGHUP)
            self.assertTrue(p.exited())
            self.assertFalse(alive(pid))

    def test_dashboard_dies_even_if_the_supervisor_is_killed(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            p = self.open(self.pid_config(pidfile), "x")
            pid = self.child_pid(pidfile, p)
            os.kill(p.pid, signal.SIGKILL)
            p.exited()
            for _ in range(30):
                if not alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(alive(pid))

    def test_dashboard_that_ignores_hangup_is_still_killed(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            config = f'[workspaces.x]\ncommand = ["sh", "-c", "trap \\"\\" HUP TERM; echo $$ > {pidfile}; echo UP; while :; do sleep 0.1; done"]'
            p = self.open(config, "x")
            pid = self.child_pid(pidfile, p)
            p.send(LEADER + b"d")
            self.assertTrue(p.exited(8))
            self.assertFalse(alive(pid))

    def test_state_records_the_popup_pid_only_while_open(self):
        p = self.open()
        p.wait_for("DC-UP")
        import json
        with open(os.path.join(p.state_dir, "state.json")) as f:
            self.assertEqual(json.load(f)["popup"]["pid"], p.pid)
        p.send(LEADER + b"d")
        p.exited()
        with open(os.path.join(p.state_dir, "state.json")) as f:
            self.assertNotIn("popup", json.load(f))


if __name__ == "__main__":
    unittest.main()
