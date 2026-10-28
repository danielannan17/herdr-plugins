import os
import tempfile
import unittest
from unittest import mock

from herdr_dashboard import cli
from herdr_dashboard.herdr_api import ApiError
from herdr_dashboard.state import State


class OpenActionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"HERDR_PLUGIN_STATE_DIR": self.dir.name, "HERDR_PLUGIN_CONFIG_DIR": self.dir.name,
                    "HERDR_PLUGIN_ID": "herdr-dashboard"}
        self.api = mock.patch.object(cli, "herdr_api").start()
        self.api.ApiError = ApiError
        self.addCleanup(mock.patch.stopall)
        self.ours = mock.patch.object(cli, "_is_our_popup", return_value=True).start()
        mock.patch.object(cli, "_wait_gone").start()

    def running(self, tab):
        State(self.dir.name).set_popup(4242, tab)

    def invoke(self, tab="w1:t1"):
        self.api.context.return_value = {"tab_id": tab}
        return cli.open_action(self.env)

    def test_opens_a_popup_when_none_is_running(self):
        self.assertEqual(self.invoke(), 0)
        args = self.api.open_popup.call_args.args
        self.assertEqual(args[:2], ("herdr-dashboard", "dashboard"))
        self.api.close_popup.assert_not_called()

    def test_toggles_closed_when_open_on_this_tab(self):
        self.running("w1:t1")
        self.assertEqual(self.invoke("w1:t1"), 0)
        self.api.close_popup.assert_called_once()
        self.api.open_popup.assert_not_called()

    def test_replaces_a_dashboard_left_on_another_tab(self):
        self.running("w1:t9")
        self.assertEqual(self.invoke("w1:t1"), 0)
        self.api.close_popup.assert_called_once()
        self.api.open_popup.assert_called_once()

    def test_a_stale_pid_is_ignored(self):
        self.running("w1:t1")
        self.ours.return_value = False
        self.invoke()
        self.api.close_popup.assert_not_called()
        self.api.open_popup.assert_called_once()

    def test_another_popup_is_never_replaced(self):
        self.api.open_popup.side_effect = ApiError("ui_busy", "a popup pane is already open")
        self.assertEqual(self.invoke(), 1)
        self.api.close_popup.assert_not_called()
        self.api.notify.assert_called_once()

    def test_popup_size_comes_from_config(self):
        with open(os.path.join(self.dir.name, "config.toml"), "w") as f:
            f.write('[popup]\nwidth = 100\nheight = "70%"')
        self.invoke()
        self.assertEqual(self.api.open_popup.call_args.args[2:4], (100, "70%"))

    def test_invalid_config_still_opens(self):
        with open(os.path.join(self.dir.name, "config.toml"), "w") as f:
            f.write("[personal")
        self.assertEqual(self.invoke(), 0)
        self.api.open_popup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
