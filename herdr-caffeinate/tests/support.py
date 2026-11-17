"""Shared test scaffolding: an isolated state and config dir per test."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from caffeinate_lib import registry, state


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="caff-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state")
        self.config = os.path.join(self.root, "config")
        os.makedirs(self.config)
        patcher = mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": self.state,
            "HERDR_PLUGIN_CONFIG_DIR": self.config,
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = state.Config()

    def task(self, task_id="t1", session="s1", now=1000.0, **kw):
        defaults = dict(command="npm run build", description="", cls="finite", keep_awake=True,
                        source="rule", kind="shell", label="build", snapshot_tracked=True)
        defaults.update(kw)
        return registry.register(task_id, session, now=now, **defaults)


def agent(session, status, pane="w1:p1", name="a"):
    return {"agent": "claude", "agent_status": status, "pane_id": pane, "name": name,
            "agent_session": {"value": session}}
