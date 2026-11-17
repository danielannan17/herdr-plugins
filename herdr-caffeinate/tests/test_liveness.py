import unittest

from caffeinate_lib import liveness
from caffeinate_lib.state import Config

NOW = 50_000.0
CFG = Config()


def task(**kw):
    base = {"task_id": "t", "session_id": "s", "status": "running", "started_at": NOW - 600,
            "snapshot_tracked": True, "pid": None}
    base.update(kw)
    return base


def run(tasks, alive=lambda pid: True, sessions=None, snapshots=None, cfg=CFG):
    return liveness.reconcile(tasks, NOW, cfg, alive, sessions, snapshots)


class ReconcileTest(unittest.TestCase):
    def reason(self, t, **kw):
        _, dropped = run([t], **kw)
        return dropped[0][1] if dropped else None

    def test_alive_task_is_kept(self):
        alive, dropped = run([task()], sessions={"s"})
        self.assertEqual((len(alive), len(dropped)), (1, 0))

    def test_reported_completion_drops_it(self):
        for status in ("completed", "failed", "killed"):
            self.assertIn(status, self.reason(task(status=status)))

    def test_dead_pid_drops_it_even_with_a_live_session(self):
        self.assertIn("exited", self.reason(task(pid=123), alive=lambda p: False, sessions={"s"}))

    def test_vanished_session_without_pid_drops_it(self):
        self.assertIn("session is gone", self.reason(task(), sessions={"other"}))

    def test_vanished_session_with_a_live_pid_keeps_it(self):
        self.assertIsNone(self.reason(task(pid=123), sessions={"other"}))

    def test_vanished_session_and_dead_pid_drops_it(self):
        self.assertIn("exited", self.reason(task(pid=123), alive=lambda p: False, sessions=set()))

    def test_unreadable_agent_list_is_not_treated_as_every_session_gone(self):
        self.assertIsNone(self.reason(task(), sessions=None))

    def test_task_without_a_session_is_not_checked_against_agents(self):
        self.assertIsNone(self.reason(task(session_id=""), sessions=set()))

    def test_missing_from_the_latest_stop_snapshot_drops_it(self):
        snap = {"s": {"ts": NOW - 1, "task_ids": ["other"]}}
        self.assertIn("task list", self.reason(task(), sessions={"s"}, snapshots=snap))

    def test_present_in_the_snapshot_is_kept(self):
        snap = {"s": {"ts": NOW - 1, "task_ids": ["t"]}}
        self.assertIsNone(self.reason(task(), sessions={"s"}, snapshots=snap))

    def test_task_started_after_the_snapshot_or_within_its_grace_is_kept(self):
        snap = {"s": {"ts": NOW - 1, "task_ids": []}}
        self.assertIsNone(self.reason(task(started_at=NOW), sessions={"s"}, snapshots=snap))
        self.assertIsNone(self.reason(task(started_at=NOW - 4), sessions={"s"}, snapshots=snap))

    def test_snapshot_of_another_session_does_not_apply(self):
        snap = {"other": {"ts": NOW - 1, "task_ids": []}}
        self.assertIsNone(self.reason(task(), sessions={"s", "other"}, snapshots=snap))

    def test_untracked_task_ignores_snapshots(self):
        snap = {"s": {"ts": NOW - 1, "task_ids": []}}
        self.assertIsNone(self.reason(task(snapshot_tracked=False), sessions={"s"}, snapshots=snap))

    def test_long_task_is_never_dropped_for_duration(self):
        self.assertIsNone(self.reason(task(started_at=NOW - 30 * 24 * 3600), sessions={"s"}))

    def test_hard_cap_drops_unless_the_user_keeps_it(self):
        cfg = Config(hard_cap_sec=3600.0)
        old = task(started_at=NOW - 7200)
        self.assertIn("hard cap", self.reason(old, sessions={"s"}, cfg=cfg))
        self.assertIsNone(self.reason(dict(old, override="keep"), sessions={"s"}, cfg=cfg))

    def test_timeout_drops_it(self):
        self.assertIn("timeout", self.reason(task(timeout=60.0, session_id=""), sessions=set()))


if __name__ == "__main__":
    unittest.main()
