import json
import subprocess
import unittest
from unittest import mock

from caffeinate_lib import herdr


def ran(stdout="{}", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class HerdrCallsTest(unittest.TestCase):
    def args(self, run):
        return run.call_args[0][0][1:]

    def test_workspace_and_title_come_before_options(self):
        with mock.patch.object(herdr.subprocess, "run", return_value=ran()) as run:
            herdr.report_token("w8", "☕ Off", 1000)
            self.assertEqual(self.args(run)[:3], ["workspace", "report-metadata", "w8"])
            herdr.clear_token("w8")
            self.assertEqual(self.args(run)[:3], ["workspace", "report-metadata", "w8"])
            herdr.notify("Title", "body")
            self.assertEqual(self.args(run)[:3], ["notification", "show", "Title"])

    def test_list_agents(self):
        payload = json.dumps({"result": {"agents": [{"pane_id": "w1:p1", "agent_status": "idle"}]}})
        with mock.patch.object(herdr.subprocess, "run", return_value=ran(payload)):
            self.assertEqual(herdr.list_agents()[0]["pane_id"], "w1:p1")

    def test_failures_read_as_none(self):
        with mock.patch.object(herdr.subprocess, "run", return_value=ran("", 1)):
            self.assertIsNone(herdr.list_agents())
        with mock.patch.object(herdr.subprocess, "run", return_value=ran("not json")):
            self.assertIsNone(herdr.list_agents())
        with mock.patch.object(herdr.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(herdr.list_workspace_ids())


if __name__ == "__main__":
    unittest.main()
