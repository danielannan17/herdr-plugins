import os
import unittest
from unittest import mock

from caffeinate_lib import daemon, registry, state

from .support import IsolatedCase, agent

T0 = 100_000.0


class FakeProc:
    def __init__(self, *a, **k):
        self.returncode = None
        self.terminated = False

    pid = 4242

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class DaemonTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.agents = []
        self.popen = mock.patch.object(daemon.subprocess, "Popen", side_effect=FakeProc)
        self.popen_mock = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.listing = mock.patch.object(daemon.herdr, "list_agents", side_effect=lambda: self.agents)
        self.listing.start()
        self.addCleanup(self.listing.stop)
        for name, value in (("list_workspace_ids", ["w1", "w2"]), ("report_token", None),
                            ("clear_token", None), ("notify", None)):
            p = mock.patch.object(daemon.herdr, name, return_value=value)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.d = daemon.Daemon()

    @property
    def held(self):
        return self.d.holder.held

    def test_holds_while_an_agent_works_and_releases_after_the_grace(self):
        self.agents = [agent("s1", "working")]
        self.d.evaluate(T0)
        self.assertTrue(self.held)
        self.assertEqual(self.popen_mock.call_args[0][0], ["caffeinate", "-i", "-w", str(os.getpid())])

        self.agents = [agent("s1", "idle")]
        self.d.evaluate(T0 + 3)
        self.assertTrue(self.held, "within the 5 s grace")
        self.d.evaluate(T0 + 6)
        self.assertFalse(self.held)
        self.assertEqual(self.popen_mock.call_count, 1, "one child for the whole hold")

    def test_one_caffeinate_for_many_agents_and_tasks(self):
        self.agents = [agent("s1", "working"), agent("s2", "working")]
        self.task("b1", "s1")
        for i in range(3):
            self.d.evaluate(T0 + i)
        self.assertEqual(self.popen_mock.call_count, 1)

    def test_idle_agent_with_running_build_stays_awake_until_it_finishes(self):
        self.agents = [agent("s1", "idle")]
        self.task("b1", "s1", now=T0)
        self.d.evaluate(T0 + 10)
        self.assertTrue(self.held)
        registry.complete("s1", "b1")
        self.d.evaluate(T0 + 11)
        self.assertTrue(self.held, "grace")
        self.d.evaluate(T0 + 20)
        self.assertFalse(self.held)
        self.assertEqual(registry.load_all(), [], "the finished task was cleaned up")

    def test_dev_server_never_holds(self):
        self.agents = [agent("s1", "idle")]
        self.task("dev", "s1", now=T0, cls="persistent", label="dev")
        self.d.evaluate(T0 + 10)
        self.assertFalse(self.held)
        self.assertEqual(len(registry.load_all()), 1)

    def test_unknown_task_holds_for_the_grace_then_falls_back(self):
        self.agents = [agent("s1", "idle")]
        self.task("u1", "s1", now=T0, cls="unknown", label=None)
        self.d.evaluate(T0 + 10)
        self.assertTrue(self.held)
        self.d.evaluate(T0 + 121)
        self.d.evaluate(T0 + 130)
        self.assertFalse(self.held)

    def test_mode_transitions(self):
        self.agents = [agent("s1", "working")]
        state.set_mode("off")
        self.d.evaluate(T0)
        self.assertFalse(self.held)
        state.set_mode("forced")
        self.agents = []
        self.d.evaluate(T0 + 1)
        self.assertTrue(self.held)
        state.set_mode("auto")
        self.d.evaluate(T0 + 2)
        self.assertTrue(self.held, "grace after forced")
        self.d.evaluate(T0 + 9)
        self.assertFalse(self.held)
        state.set_mode("forced")
        self.d.evaluate(T0 + 10)
        state.set_mode("off")
        self.d.evaluate(T0 + 11)
        self.assertFalse(self.held, "off releases without a grace")

    def test_crash_recovery_drops_tasks_of_a_vanished_session_and_dead_pid(self):
        self.agents = [agent("live", "idle")]
        self.task("orphan", "gone", now=T0)
        self.task("deadpid", "live", now=T0, pid=999_999)
        self.task("fine", "live", now=T0)
        with mock.patch("caffeinate_lib.engine.pid_alive", side_effect=lambda pid: False):
            self.d.evaluate(T0 + 10)
        self.assertEqual([t["task_id"] for t in registry.load_all()], ["fine"])
        self.assertTrue(self.held)

    def test_two_sessions_same_command_are_independent(self):
        self.agents = [agent("s1", "idle"), agent("s2", "idle")]
        self.task("b1", "s1", now=T0, command="npm test")
        self.task("b1", "s2", now=T0, command="npm test")
        registry.complete("s1", "b1")
        self.d.evaluate(T0 + 10)
        self.assertTrue(self.held, "s2's build still runs")
        self.assertEqual([(t["session_id"], t["task_id"]) for t in registry.load_all()], [("s2", "b1")])

    def test_stuck_task_notifies_once_and_keeps_holding(self):
        self.agents = [agent("s1", "idle")]
        self.task("old", "s1", now=T0)
        later = T0 + state.Config().stuck_after_sec + 60
        self.d.evaluate(later)
        self.d.evaluate(later + 40)
        self.assertTrue(self.held)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("stuck", self.notify.call_args[0][0].lower())
        self.assertIn("⚠ 1 stuck", self.d_status())

    def d_status(self):
        with open(os.path.join(self.state, "status.txt")) as fh:
            return fh.read()

    def test_override_ignore_releases_a_stuck_task(self):
        self.agents = [agent("s1", "idle")]
        self.task("old", "s1", now=T0)
        later = T0 + state.Config().stuck_after_sec + 60
        self.d.evaluate(later)
        registry.set_override("s1", "old", "ignore")
        self.d.evaluate(later + 1)
        self.d.evaluate(later + 10)
        self.assertFalse(self.held)

    def test_agent_reclassification_stops_the_hold(self):
        self.agents = [agent("s1", "idle")]
        self.task("x", "s1", now=T0, cls="unknown", label=None, source="rule")
        self.d.evaluate(T0 + 10)
        self.assertTrue(self.held)
        registry.update("s1", "x", source="agent", **{"class": "persistent"})
        self.d.evaluate(T0 + 11)
        self.d.evaluate(T0 + 20)
        self.assertFalse(self.held)

    def test_herdr_hiccup_keeps_the_last_agents_for_a_minute(self):
        self.agents = [agent("s1", "working")]
        self.d.evaluate(T0)
        self.listing.stop()
        with mock.patch.object(daemon.herdr, "list_agents", return_value=None):
            self.d.evaluate(T0 + 20)
            self.assertTrue(self.held)
            self.d.evaluate(T0 + 100)
            self.d.evaluate(T0 + 110)
            self.assertFalse(self.held)
        self.listing.start()

    def test_dead_caffeinate_child_is_replaced_while_work_continues(self):
        self.agents = [agent("s1", "working")]
        self.d.evaluate(T0)
        self.d.holder.proc.returncode = 1
        self.d.evaluate(T0 + 1)
        self.assertTrue(self.held)
        self.assertEqual(self.popen_mock.call_count, 2)

    def test_indicator_is_sent_to_every_workspace_and_only_on_change(self):
        self.agents = [agent("s1", "working")]
        self.d.evaluate(T0)
        self.assertEqual({c[0][0] for c in self.report_token.call_args_list}, {"w1", "w2"})
        self.assertEqual(self.report_token.call_args[0][1], "☕ Awake · 1 agent")
        self.report_token.reset_mock()
        self.d.evaluate(T0 + 5)
        self.report_token.assert_not_called()
        self.d.evaluate(T0 + 70)
        self.assertEqual(self.report_token.call_count, 2, "refreshed before the token expires")

    def test_shutdown_releases_and_clears_indicator(self):
        self.agents = [agent("s1", "working")]
        self.d.evaluate(T0)
        proc = self.d.holder.proc
        self.d.shutdown()
        self.assertTrue(proc.terminated)
        self.assertEqual({c[0][0] for c in self.clear_token.call_args_list}, {"w1", "w2"})

    def test_old_snapshot_of_a_dead_session_is_pruned(self):
        self.agents = [agent("live", "idle")]
        state.write_snapshot("dead", [], now=T0 - 3 * 24 * 3600)
        state.write_snapshot("live", [], now=T0 - 3 * 24 * 3600)
        self.d.evaluate(T0)
        self.assertEqual(set(state.load_snapshots()), {"live"})


if __name__ == "__main__":
    unittest.main()
