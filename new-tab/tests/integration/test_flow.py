"""Keybinding to popup to tab, with a real Herdr client attached to an isolated server."""
import os
import time
import unittest

from .harness import HERDR, IsolatedHerdr, Pty

PREFIX = "\x02"
ALT_DOWN = "\x1b[1;3B"
ESC = "\x1b"

KEYS_BLOCK = '''[keys]
new_tab = "prefix+shift+c"

'''
COMMAND_BLOCK = '''[[keys.command]]
key = "prefix+c"
type = "plugin_action"
command = "danielannan17.new-tab.open"
description = "new tab (choose workspace)"

'''


def with_new_tab_binding(config: str) -> str:
    """Apply the documented config change to existing config text."""
    first = config.find("[[keys.command]]")
    if first == -1:
        return config.rstrip("\n") + "\n\n" + KEYS_BLOCK + COMMAND_BLOCK
    config = config[:first] + KEYS_BLOCK + config[first:]
    last = config.rfind("[[keys.command]]")
    end = config.find("\n\n", last)
    end = len(config) if end == -1 else end + 2
    return config[:end] + COMMAND_BLOCK + config[end:]


def without_new_tab_binding(config: str) -> str:
    """Remove a previously applied change: the `new_tab` key (and its `[keys]`
    header when nothing else is under it) and the plugin's `[[keys.command]]` block."""
    blocks, current = [], []
    for line in config.splitlines(keepends=True):
        if line.startswith("[") and current:
            blocks.append(current)
            current = []
        current.append(line)
    blocks.append(current)
    kept = []
    for block in blocks:
        text = "".join(block)
        if block[0].startswith("[[keys.command]]") and 'command = "danielannan17.new-tab.open"' in text:
            continue
        if block[0].startswith("[keys]"):
            block = [l for l in block if not l.startswith("new_tab")]
            if not any(l.strip() and not l.startswith("[keys]") for l in block):
                continue
        kept.append("".join(block))
    return "".join(kept)


class FlowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.herdr = IsolatedHerdr()
        cls.herdr.write_config("onboarding = false\n" + with_new_tab_binding(""))
        cls.herdr.start()
        cls.herdr.link_plugin()

    @classmethod
    def tearDownClass(cls):
        cls.herdr.stop()

    def setUp(self):
        for ws in self.herdr.workspaces():
            self.herdr.run("workspace", "close", ws["workspace_id"], check=False)
        self.ids = {
            "alpha": self.herdr.make_workspace("alpha"),
            "beta": self.herdr.make_workspace("beta", focus=True),
            "gamma": self.herdr.make_workspace("gamma"),
        }
        self.client = Pty([HERDR], self.herdr.env, rows=40, cols=120)
        self.assertTrue(self.client.wait_for("beta"), self.client.text())
        self.client.pump(0.5)

    def tearDown(self):
        self.client.close()

    def wait_tabs(self, workspace, count, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.herdr.tabs(self.ids[workspace])) == count:
                return True
            time.sleep(0.05)
        return False


class KeybindingTests(FlowCase):
    def test_config_has_no_diagnostics(self):
        out = self.herdr.run("config", "check").stdout
        self.assertEqual(out.strip(), "config: ok")

    def test_prefix_c_opens_the_popup_for_the_current_workspace(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"), self.client.text())
        self.assertIn("Workspace: beta", self.client.text())
        self.assertEqual(len(self.herdr.tabs(self.ids["beta"])), 1)

    def test_manifest_height_gives_eight_inner_rows_and_three_workspaces_fit(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        rows = self.client.text().split("\n")
        top = next(i for i, r in enumerate(rows) if "┌Create New Tab" in r)
        bottom = next(i for i, r in enumerate(rows) if i > top and "└" in r)
        self.assertEqual(bottom - top - 1, 8)
        self.assertIn("Enter to create", rows[bottom - 1])
        self.client.send(ALT_DOWN)
        self.assertTrue(self.client.wait_for("1-9 select"))
        rows = self.client.text().split("\n")
        items = [r for r in rows[top + 3:bottom - 1] if r.split("│")[-2:-1] and r.strip(" │")]
        self.assertIn("alpha", rows[top + 3])
        self.assertIn("gamma", rows[top + 5])
        self.assertIn("1-9 select", rows[bottom - 1])

    def test_prefix_c_enter_without_waiting_for_the_popup_creates_a_tab(self):
        # Typed at a human pace (150 ms between C and Enter) without looking at the screen.
        self.client.send(PREFIX + "c")
        time.sleep(0.15)
        start = time.time()
        self.client.send("\r")
        self.assertTrue(self.wait_tabs("beta", 2, timeout=1.0))
        self.assertLess(time.time() - start, 1.0)
        self.assertEqual(self.herdr.focused_workspace(), self.ids["beta"])
        self.assertTrue(self.client.wait_gone("Enter to create"), self.client.text())

    def test_prefix_c_enter_after_a_pause_also_works(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send("\r")
        self.assertTrue(self.wait_tabs("beta", 2))

    def test_create_in_another_workspace_switches_to_it(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send(ALT_DOWN)
        self.assertTrue(self.client.wait_for("1-9 select"), self.client.text())
        self.client.send("3")
        self.assertTrue(self.client.wait_for("Workspace: gamma"), self.client.text())
        self.assertEqual(self.herdr.focused_workspace(), self.ids["beta"])
        self.client.send("\r")
        self.assertTrue(self.wait_tabs("gamma", 2))
        self.assertEqual(len(self.herdr.tabs(self.ids["beta"])), 1)
        self.assertEqual(self.herdr.focused_workspace(), self.ids["gamma"])
        self.assertTrue(self.client.wait_gone("Create New Tab"))

    def test_alt_down_sent_as_meta_prefixed_escape_opens_the_list(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send("\x1b\x1b[B")
        self.assertTrue(self.client.wait_for("1-9 select"), self.client.text())

    def test_typed_name_and_arrow_selection(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send("deploy")
        self.client.send(ALT_DOWN)
        self.assertTrue(self.client.wait_for("1-9 select"))
        self.client.send("\x1b[A\r")
        self.assertTrue(self.client.wait_for("Workspace: alpha"), self.client.text())
        self.client.send("\r")
        self.assertTrue(self.wait_tabs("alpha", 2))
        labels = [t["label"] for t in self.herdr.tabs(self.ids["alpha"])]
        self.assertEqual(labels, ["1", "deploy"])

    def test_esc_cancels_and_leaves_herdr_untouched(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send(ESC)
        self.assertTrue(self.client.wait_gone("Create New Tab"), self.client.text())
        time.sleep(0.3)
        for name in ("alpha", "beta", "gamma"):
            self.assertEqual(len(self.herdr.tabs(self.ids[name])), 1)
        self.assertEqual(self.herdr.focused_workspace(), self.ids["beta"])

    def test_esc_in_dropdown_then_esc_again(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.client.send(ALT_DOWN)
        self.assertTrue(self.client.wait_for("1-9 select"))
        self.client.send(ESC)
        self.assertTrue(self.client.wait_gone("1-9 select"))
        self.assertIn("Create New Tab", self.client.text())
        self.client.send(ESC)
        self.assertTrue(self.client.wait_gone("Create New Tab"))

    def test_native_dialog_still_works_on_prefix_shift_c(self):
        self.client.send(PREFIX + "C")
        self.assertTrue(self.client.wait_for("new tab"), self.client.text())
        self.assertNotIn("Create New Tab", self.client.text())
        self.client.send("\r")
        self.assertTrue(self.wait_tabs("beta", 2))

    def test_prefix_c_is_no_longer_the_native_dialog(self):
        self.client.send(PREFIX + "c")
        self.assertTrue(self.client.wait_for("Enter to create"))
        self.assertNotIn("new tab", self.client.text().replace("Create New Tab", ""))


class BindingHelperTests(unittest.TestCase):
    def test_strip_undoes_apply(self):
        base = 'onboarding = false\n\n[ui]\nx = 1\n\n[[keys.command]]\nkey = "ctrl+k"\ncommand = "a"\n\n[theme]\nname = "d"\n'
        applied = with_new_tab_binding(base)
        self.assertIn("danielannan17.new-tab.open", applied)
        self.assertEqual(without_new_tab_binding(applied), base)
        self.assertEqual(without_new_tab_binding(base), base)


class RealConfigTests(unittest.TestCase):
    """The change applied to a copy of the live config produces no new diagnostics."""

    LIVE = os.path.expanduser("~/.config/herdr/config.toml")

    @unittest.skipUnless(os.path.exists(LIVE), "no live config to copy")
    def test_change_applied_to_live_config_is_clean(self):
        with open(self.LIVE) as f:
            original = without_new_tab_binding(f.read())
        herdr = IsolatedHerdr()
        try:
            herdr.write_config(original)
            before = herdr.run("config", "check", check=False).stdout
            herdr.write_config(with_new_tab_binding(original))
            after = herdr.run("config", "check", check=False).stdout
            self.assertEqual(after, before)
            self.assertEqual(after.strip(), "config: ok")
        finally:
            herdr.stop()


if __name__ == "__main__":
    unittest.main()
