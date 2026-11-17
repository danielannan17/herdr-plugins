import unittest

from caffeinate_lib import policy
from caffeinate_lib.state import Config

from .support import agent

NOW = 10_000.0
CFG = Config()


def task(**kw):
    base = {"task_id": "t", "session_id": "s", "class": "finite", "keep_awake": True,
            "source": "rule", "kind": "shell", "started_at": NOW - 60, "label": "build"}
    base.update(kw)
    return base


def pairs(*tasks, now=NOW, cfg=CFG):
    return [(t, policy.verdict(t, now, cfg)) for t in tasks]


class VerdictTest(unittest.TestCase):
    def test_finite_counts_and_persistent_does_not(self):
        self.assertTrue(policy.verdict(task(), NOW, CFG).counts)
        v = policy.verdict(task(**{"class": "persistent"}), NOW, CFG)
        self.assertFalse(v.counts)
        self.assertEqual(v.bucket, policy.IGNORED)

    def test_finite_task_the_agent_said_need_not_keep_awake_is_ignored(self):
        v = policy.verdict(task(keep_awake=False, source="agent"), NOW, CFG)
        self.assertFalse(v.counts)
        self.assertIn("agent", v.reason)

    def test_unknown_counts_during_grace_then_follows_fallback(self):
        unknown = task(**{"class": "unknown"})
        self.assertTrue(policy.verdict(unknown, NOW, CFG).counts)
        late = NOW + CFG.unknown_grace_sec
        ignored = policy.verdict(unknown, late, CFG)
        self.assertFalse(ignored.counts)
        self.assertEqual(ignored.bucket, policy.UNCLASSIFIED)
        keep = Config(unknown_fallback="keep")
        self.assertTrue(policy.verdict(unknown, late, keep).counts)

    def test_user_override_beats_classification(self):
        self.assertTrue(policy.verdict(task(**{"class": "persistent"}, override="keep"), NOW, CFG).counts)
        self.assertFalse(policy.verdict(task(override="ignore"), NOW, CFG).counts)
        self.assertFalse(policy.verdict(task(**{"class": "unknown"}, override="ignore"), NOW, CFG).counts)

    def test_long_task_is_flagged_stuck_but_still_counts(self):
        old = task(started_at=NOW - CFG.stuck_after_sec - 1)
        v = policy.verdict(old, NOW, CFG)
        self.assertTrue(v.counts)
        self.assertTrue(v.stuck)

    def test_persistent_task_is_never_stuck(self):
        old = task(**{"class": "persistent"}, started_at=NOW - 10 * 24 * 3600)
        self.assertFalse(policy.verdict(old, NOW, CFG).stuck)

    def test_kept_by_override_is_not_flagged_stuck(self):
        old = task(started_at=NOW - CFG.stuck_after_sec - 1, override="keep")
        self.assertFalse(policy.verdict(old, NOW, CFG).stuck)


class DecideTest(unittest.TestCase):
    def test_several_agents_working_count_together(self):
        d = policy.decide("auto", [agent("a", "working"), agent("b", "working"), agent("c", "idle")], [])
        self.assertTrue(d.wanted)
        self.assertEqual(len(d.working_agents), 2)

    def test_idle_agent_with_running_build_keeps_awake(self):
        d = policy.decide("auto", [agent("a", "idle")], pairs(task()))
        self.assertTrue(d.wanted)
        self.assertEqual(len(d.active_tasks), 1)

    def test_waiting_agents_never_count(self):
        for status in ("blocked", "idle", "done", "unknown"):
            self.assertFalse(policy.decide("auto", [agent("a", status)], []).wanted, status)

    def test_dev_server_only_does_not_keep_awake(self):
        d = policy.decide("auto", [agent("a", "idle")], pairs(task(**{"class": "persistent"})))
        self.assertFalse(d.wanted)

    def test_modes(self):
        busy = [agent("a", "working")]
        self.assertFalse(policy.decide("off", busy, pairs(task())).wanted)
        self.assertTrue(policy.decide("forced", [], []).wanted)
        self.assertTrue(policy.decide("auto", busy, []).wanted)
        self.assertFalse(policy.decide("auto", [], []).wanted)


class GraceTest(unittest.TestCase):
    def test_holds_for_the_grace_after_the_last_activity(self):
        g = policy.Grace(5)
        self.assertTrue(g.hold("auto", True, 100))
        self.assertTrue(g.hold("auto", False, 104.9))
        self.assertFalse(g.hold("auto", False, 105.0))

    def test_new_activity_restarts_the_grace(self):
        g = policy.Grace(5)
        g.hold("auto", True, 100)
        g.hold("auto", True, 103)
        self.assertTrue(g.hold("auto", False, 107))
        self.assertFalse(g.hold("auto", False, 108))

    def test_never_held_before_activity(self):
        self.assertFalse(policy.Grace(5).hold("auto", False, 100))

    def test_off_releases_at_once_and_forgets(self):
        g = policy.Grace(5)
        g.hold("auto", True, 100)
        self.assertFalse(g.hold("off", False, 101))
        self.assertFalse(g.hold("auto", False, 102))

    def test_forced_holds_and_then_auto_gives_a_grace(self):
        g = policy.Grace(5)
        self.assertTrue(g.hold("forced", True, 100))
        self.assertTrue(g.hold("auto", False, 102))
        self.assertFalse(g.hold("auto", False, 106))


class WakeupTest(unittest.TestCase):
    def test_wakes_for_the_release_and_for_unknown_grace_expiry(self):
        unknown = task(**{"class": "unknown"}, started_at=NOW - 100)
        wait = policy.next_wakeup(NOW, 30, NOW + 3, [policy.verdict(unknown, NOW, CFG)])
        self.assertAlmostEqual(wait, 3)
        wait = policy.next_wakeup(NOW, 30, None, [policy.verdict(unknown, NOW, CFG)])
        self.assertAlmostEqual(wait, 20)
        self.assertEqual(policy.next_wakeup(NOW, 30, None, []), 30)


class SummaryTest(unittest.TestCase):
    def test_texts(self):
        build = task()
        p = pairs(build)
        d = policy.decide("auto", [agent("a", "working"), agent("b", "working")], p)
        self.assertEqual(policy.summary(d, True, p), "☕ Awake · 2 agents · 1 build")
        idle = policy.decide("auto", [], [])
        self.assertEqual(policy.summary(idle, False, []), "☕ Auto · Idle")
        self.assertEqual(policy.summary(policy.decide("off", [], []), False, []), "☕ Off")
        self.assertEqual(policy.summary(policy.decide("forced", [], []), True, []), "☕ Forced")

    def test_stuck_is_shown(self):
        old = task(started_at=NOW - CFG.stuck_after_sec - 1)
        p = pairs(old)
        d = policy.decide("auto", [], p)
        self.assertIn("⚠ 1 stuck", policy.summary(d, True, p))


if __name__ == "__main__":
    unittest.main()
