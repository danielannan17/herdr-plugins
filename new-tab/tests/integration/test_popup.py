"""The popup program against a real, isolated Herdr server (no Herdr client)."""
import json
import os
import socket
import sys
import threading
import time
import unittest

from .harness import IsolatedHerdr, PLUGIN_ROOT, Pty

ALT_DOWN = "\x1b[1;3B"
ESC = "\x1b"
ENTER = "\r"


class FailingMethodProxy:
    """Forwards the Herdr API socket, except that `method` fails while `fail` is set."""

    def __init__(self, path, upstream, method="tab.create"):
        self.path, self.upstream, self.method, self.fail = path, upstream, method, True
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(8)
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            line = conn.makefile("rb").readline()
            request = json.loads(line)
            if self.fail and request.get("method") == self.method:
                reply = {"id": request["id"], "error": {"code": "failed", "message": "no pty available"}}
                conn.sendall((json.dumps(reply) + "\n").encode())
                return
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as up:
                up.connect(self.upstream)
                up.sendall(line)
                conn.sendall(up.makefile("rb").readline())

    def stop(self):
        self.running = False
        self.listener.close()


class PopupCase(unittest.TestCase):
    herdr: IsolatedHerdr

    @classmethod
    def setUpClass(cls):
        cls.herdr = IsolatedHerdr()
        cls.herdr.write_config("onboarding = false\n")
        cls.herdr.env["SHELL"] = "/bin/zsh"
        with open(os.path.join(cls.herdr.env["HOME"], ".zshrc"), "w") as f:
            f.write("alias c='echo ALIAS_RAN'\nPS1='READY> '\n")
        cls.herdr.start()

    @classmethod
    def tearDownClass(cls):
        cls.herdr.stop()

    def setUp(self):
        for ws in self.herdr.workspaces():
            self.herdr.run("workspace", "close", ws["workspace_id"], check=False)
        self.popup = None

    def tearDown(self):
        if self.popup:
            self.popup.close()

    def workspaces_labeled(self, *labels, focus=None):
        ids = {}
        for label in labels:
            ids[label] = self.herdr.make_workspace(label, focus=(label == focus))
        return ids

    def open_popup(self, env_extra=None, wait_for="Workspace:"):
        env = dict(self.herdr.env)
        env["HERDR_SOCKET_PATH"] = self.herdr.socket_path
        env.update(env_extra or {})
        self.popup = Pty([sys.executable, os.path.join(PLUGIN_ROOT, "main.py")], env, rows=8, cols=54, cwd=PLUGIN_ROOT)
        self.assertTrue(self.popup.wait_for(wait_for), self.popup.text())
        return self.popup

    def tab_labels(self, workspace_id):
        return [t["label"] for t in self.herdr.tabs(workspace_id)]

    def newest_pane_id(self, workspace_id):
        tab_id = self.herdr.tabs(workspace_id)[-1]["tab_id"]
        panes = self.herdr.api("pane", "list", "--workspace", workspace_id)["panes"]
        return next(x["pane_id"] for x in panes if x["tab_id"] == tab_id)

    def pane_text(self, pane_id):
        return self.herdr.run("pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", "20").stdout

    def wait_pane_text(self, pane_id, needle, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.pane_text(pane_id):
                return True
            time.sleep(0.1)
        return False

    def write_shells(self, text):
        path = os.path.join(self.herdr.root, "shells.toml")
        with open(path, "w") as f:
            f.write(text)
        return {"NEW_TAB_SHELLS": path}


class CreateTests(PopupCase):
    def test_enter_creates_default_tab_in_current_workspace_and_focuses_it(self):
        ids = self.workspaces_labeled("alpha", "beta", focus="beta")
        p = self.open_popup()
        self.assertIn("Workspace: beta", p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["beta"])), 2)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 1)
        self.assertEqual(self.herdr.focused_workspace(), ids["beta"])
        self.assertEqual(self.tab_labels(ids["beta"]), ["1", "2"])

    def test_typed_name_becomes_tab_label(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup()
        p.send("build\r")
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(self.tab_labels(ids["alpha"]), ["1", "build"])

    def test_name_equal_to_default_gets_no_custom_label(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        self.herdr.run("tab", "create", "--workspace", ids["alpha"], "--label", "keep")
        p = self.open_popup()
        p.send("3\r")
        self.assertEqual(p.poll_exit(), 0)
        tab = self.herdr.tabs(ids["alpha"])[-1]
        self.assertEqual(tab["label"], "3")
        info = self.herdr.api("tab", "get", tab["tab_id"])["tab"]
        self.assertFalse(info.get("custom_label", False))

    def test_label_that_looks_like_a_flag_is_a_label(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup()
        p.send("--help\r")
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(self.tab_labels(ids["alpha"]), ["1", "--help"])

    def test_create_in_another_workspace_by_number_switches_to_it(self):
        ids = self.workspaces_labeled("alpha", "beta", "gamma", focus="beta")
        p = self.open_popup()
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"), p.text())
        text = p.text()
        self.assertRegex(text, r"✓\s+2\s+beta · active")
        self.assertIn("alpha", text)
        self.assertIn("gamma", text)
        p.send("3")
        self.assertTrue(p.wait_for("Workspace: gamma"), p.text())
        self.assertNotIn("1-9 select", p.text())
        # Selecting must not switch the active workspace yet.
        self.assertEqual(self.herdr.focused_workspace(), ids["beta"])
        self.assertEqual(len(self.herdr.tabs(ids["gamma"])), 1)
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["gamma"])), 2)
        self.assertEqual(len(self.herdr.tabs(ids["beta"])), 1)
        self.assertEqual(self.herdr.focused_workspace(), ids["gamma"])

    def test_arrow_selection_and_name_survive_workspace_change(self):
        ids = self.workspaces_labeled("alpha", "beta", "gamma", focus="alpha")
        p = self.open_popup()
        p.send("logs")
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        p.send("\x1b[B\x1b[B")
        p.send(ENTER)
        self.assertTrue(p.wait_for("Workspace: gamma"), p.text())
        self.assertIn("Tab name: logs", p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(self.tab_labels(ids["gamma"]), ["1", "logs"])

    def test_digits_are_text_when_dropdown_closed(self):
        ids = self.workspaces_labeled("alpha", "beta", focus="alpha")
        p = self.open_popup()
        p.send("2\r")
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(self.tab_labels(ids["alpha"]), ["1", "2"])
        self.assertEqual(len(self.herdr.tabs(ids["beta"])), 1)

    def test_new_tab_inherits_cwd_of_destination_workspace(self):
        a = self.herdr.api("workspace", "create", "--label", "A", "--cwd", "/usr", "--focus")["workspace"]["workspace_id"]
        b = self.herdr.api("workspace", "create", "--label", "B", "--cwd", "/private/var", "--no-focus")["workspace"]["workspace_id"]
        p = self.open_popup()
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        p.send("2")
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        panes = self.herdr.api("pane", "list", "--workspace", b)["panes"]
        self.assertTrue(any(x["cwd"] == "/private/var" for x in panes), panes)
        new_tab = self.herdr.tabs(b)[-1]["tab_id"]
        new_pane = [x for x in panes if x["tab_id"] == new_tab][0]
        self.assertEqual(new_pane["cwd"], "/private/var")


SHELLS = '''[[shell]]
name = "Claude"
command = "c"

[[shell]]
name = "zsh"
'''
SHIFT_TAB = "\x1b[Z"


class ShellTests(PopupCase):
    def test_first_shell_is_the_default_and_its_command_runs_in_the_interactive_shell(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup(self.write_shells(SHELLS))
        self.assertIn("Shell: Claude", p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        # `c` is a shell alias, so only the interactive shell can have run it.
        self.assertTrue(self.wait_pane_text(self.newest_pane_id(ids["alpha"]), "ALIAS_RAN"))

    def test_shift_tab_picks_the_next_shell_and_a_plain_shell_gets_no_command(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup(self.write_shells(SHELLS))
        p.send(SHIFT_TAB)
        self.assertTrue(p.wait_for("Shell: zsh"), p.text())
        self.assertIn("Shift+Tab shell", p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        pane = self.newest_pane_id(ids["alpha"])
        self.assertTrue(self.wait_pane_text(pane, "READY>"))
        time.sleep(0.3)
        self.assertNotIn("ALIAS_RAN", self.pane_text(pane))

    def test_shift_tab_wraps_back_to_the_first_shell(self):
        self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup(self.write_shells(SHELLS))
        p.send(SHIFT_TAB)
        self.assertTrue(p.wait_for("Shell: zsh"), p.text())
        p.send(SHIFT_TAB)
        self.assertTrue(p.wait_for("Shell: Claude"), p.text())

    def test_missing_config_shows_the_error_and_still_creates_a_plain_tab(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup({"NEW_TAB_SHELLS": os.path.join(self.herdr.root, "absent.toml")})
        self.assertTrue(p.wait_for("shells.toml:"), p.text())
        self.assertIn("Shell: shell", p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 2)

    def test_invalid_config_shows_the_error_instead_of_crashing(self):
        self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup(self.write_shells("[[shell"))
        self.assertTrue(p.wait_for("shells.toml:"), p.text())
        self.assertIsNone(p.poll_exit(0.3))

    def test_failed_command_says_the_tab_exists_and_enter_closes_without_a_second_tab(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        proxy = FailingMethodProxy(os.path.join(self.herdr.root, "p.sock"), self.herdr.socket_path, "pane.send_input")
        self.addCleanup(proxy.stop)
        env = self.write_shells(SHELLS)
        env["HERDR_SOCKET_PATH"] = proxy.path
        p = self.open_popup(env)
        p.send(ENTER)
        self.assertTrue(p.wait_for("Tab created"), p.text())
        self.assertIn("no pty", p.text())
        self.assertIn("Enter or Esc to close", p.text())
        self.assertIsNone(p.poll_exit(0.4))
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 2)
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 2)


class CancelTests(PopupCase):
    def test_esc_cancels_without_creating(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup()
        p.send("typed")
        p.send(ESC)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 1)

    def test_esc_in_dropdown_closes_only_the_dropdown(self):
        ids = self.workspaces_labeled("alpha", "beta", focus="alpha")
        p = self.open_popup()
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        p.send(ESC)
        self.assertTrue(p.wait_for("Enter to create"), p.text())
        self.assertIsNone(p.poll_exit(0.4))
        p.send(ESC)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 1)

    def test_alt_down_sent_as_meta_prefix_is_not_taken_for_escape(self):
        self.workspaces_labeled("alpha", "beta", focus="alpha")
        p = self.open_popup()
        p.send("\x1b\x1b[B")
        self.assertTrue(p.wait_for("1-9 select"), p.text())
        self.assertIsNone(p.poll_exit(0.3))

    def test_alt_down_split_across_writes_is_not_taken_for_escape(self):
        self.workspaces_labeled("alpha", "beta", focus="alpha")
        p = self.open_popup()
        p.send("\x1b")
        p.pump(0.01)
        p.send("[1;3B")
        self.assertTrue(p.wait_for("1-9 select"), p.text())

    def test_escape_resolves_quickly(self):
        self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup()
        start = time.time()
        p.send(ESC)
        self.assertEqual(p.poll_exit(), 0)
        self.assertLess(time.time() - start, 0.5)


class ManyWorkspacesTests(PopupCase):
    def test_more_than_nine_workspaces_scroll_and_select_by_arrow(self):
        ids = {}
        for i in range(1, 15):
            ids[i] = self.herdr.make_workspace(f"space{i}", focus=(i == 1))
        p = self.open_popup()
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        p.send("\x1b[B" * 12)
        self.assertTrue(p.wait_for("space13"), p.text())
        self.assertIn("more", p.text())
        p.send(ENTER)
        self.assertTrue(p.wait_for("Workspace: space13"), p.text())
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids[13])), 2)
        self.assertEqual(self.herdr.focused_workspace(), ids[13])

    def test_number_key_nine_selects_ninth(self):
        ids = {}
        for i in range(1, 12):
            ids[i] = self.herdr.make_workspace(f"space{i}", focus=(i == 1))
        p = self.open_popup()
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        p.send("9")
        self.assertTrue(p.wait_for("Workspace: space9"), p.text())


class FailureTests(PopupCase):
    def test_workspace_closed_while_dialog_is_open(self):
        ids = self.workspaces_labeled("alpha", "beta", "gamma", focus="beta")
        p = self.open_popup()
        self.herdr.run("workspace", "close", ids["beta"])
        p.send(ENTER)
        self.assertTrue(p.wait_for("was closed"), p.text())
        self.assertIsNone(p.poll_exit(0.4))
        self.assertIn("Workspace: (none)", p.text())
        # Another Enter creates nothing and explains why.
        p.send(ENTER)
        self.assertTrue(p.wait_for("Pick a workspace first"), p.text())
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 1)
        # The refreshed list is what the dropdown offers; choose a survivor.
        p.send(ALT_DOWN)
        self.assertTrue(p.wait_for("1-9 select"))
        text = p.text()
        self.assertNotIn("beta", text.split("Workspace:")[1])
        p.send("2")
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(len(self.herdr.tabs(ids["gamma"])), 2)

    def test_create_failure_keeps_dialog_open_with_the_error(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        proxy = FailingMethodProxy(os.path.join(self.herdr.root, "p.sock"), self.herdr.socket_path)
        self.addCleanup(proxy.stop)
        p = self.open_popup({"HERDR_SOCKET_PATH": proxy.path})
        p.send("keepme\r")
        self.assertTrue(p.wait_for("no pty available"), p.text())
        self.assertIn("Tab name: keepme", p.text())
        self.assertIsNone(p.poll_exit(0.4))
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 1)
        proxy.fail = False
        p.send(ENTER)
        self.assertEqual(p.poll_exit(), 0)
        self.assertEqual(self.tab_labels(ids["alpha"]), ["1", "keepme"])

    def test_server_unreachable_at_open_shows_error_and_esc_closes(self):
        env = {"HERDR_SOCKET_PATH": "/nonexistent/herdr.sock"}
        p = self.open_popup(env, wait_for="Could not read workspaces")
        p.send(ENTER)
        p.pump(0.2)
        self.assertIsNone(p.poll_exit(0.2))
        p.send(ESC)
        self.assertEqual(p.poll_exit(), 0)


class DuplicateGuardTests(PopupCase):
    def test_repeated_enter_in_one_burst_creates_one_tab(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        p = self.open_popup()
        p.send("\r\r\r\r")
        self.assertEqual(p.poll_exit(), 0)
        time.sleep(0.3)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 2)

    def test_enter_typed_ahead_of_open_does_not_double_create(self):
        ids = self.workspaces_labeled("alpha", focus="alpha")
        env = dict(self.herdr.env)
        env["HERDR_SOCKET_PATH"] = self.herdr.socket_path
        self.popup = Pty([sys.executable, os.path.join(PLUGIN_ROOT, "main.py")], env, rows=8, cols=54, cwd=PLUGIN_ROOT)
        self.popup.send("\r\r")
        self.assertEqual(self.popup.poll_exit(8), 0)
        time.sleep(0.3)
        self.assertEqual(len(self.herdr.tabs(ids["alpha"])), 2)


if __name__ == "__main__":
    unittest.main()
