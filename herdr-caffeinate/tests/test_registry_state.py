import os
import unittest

from caffeinate_lib import registry, state

from .support import IsolatedCase


class RegistryTest(IsolatedCase):
    def test_same_command_in_two_sessions_is_two_tasks(self):
        self.task("b1", "s1")
        self.task("b1", "s2")
        self.assertEqual(len(registry.load_all()), 2)
        registry.complete("s1", "b1")
        self.assertEqual({t["session_id"]: t["status"] for t in registry.load_all()},
                         {"s1": "completed", "s2": "running"})

    def test_write_leaves_no_temp_files_and_survives_garbage(self):
        self.task()
        with open(os.path.join(registry.tasks_dir(), "broken__x.json"), "w") as fh:
            fh.write('{"task_id": "x"')  # half-written by another writer
        self.assertEqual([t["task_id"] for t in registry.load_all()], ["t1"])
        self.assertEqual([n for n in os.listdir(registry.tasks_dir()) if n.startswith(".tmp")], [])

    def test_agent_reclassification_beats_a_rule(self):
        self.task(cls="unknown", source="rule", label=None)
        registry.update("s1", "t1", source="agent", **{"class": "persistent"})
        registry.update("s1", "t1", source="rule", **{"class": "finite"})
        self.assertEqual(registry.get("s1", "t1")["class"], "persistent")

    def test_fork_answer_does_not_overwrite_the_agent(self):
        self.task(cls="finite", source="agent")
        registry.update("s1", "t1", source="fork", **{"class": "persistent"})
        self.assertEqual(registry.get("s1", "t1")["class"], "finite")

    def test_cli_change_beats_the_agent(self):
        self.task(cls="finite", source="agent")
        registry.update("s1", "t1", source="cli", **{"class": "persistent"})
        self.assertEqual(registry.get("s1", "t1")["class"], "persistent")

    def test_registering_again_keeps_start_time_and_override(self):
        self.task(now=100.0)
        registry.set_override("s1", "t1", "keep")
        again = self.task(now=900.0)
        self.assertEqual(again["started_at"], 100.0)
        self.assertEqual(again["override"], "keep")

    def test_override_values(self):
        self.task()
        registry.set_override("s1", "t1", "ignore")
        self.assertEqual(registry.get("s1", "t1")["override"], "ignore")
        registry.set_override("s1", "t1", None)
        self.assertIsNone(registry.get("s1", "t1")["override"])
        with self.assertRaises(ValueError):
            registry.set_override("s1", "t1", "maybe")

    def test_find_by_bare_or_qualified_id(self):
        self.task("b1", "s1")
        self.task("b1", "s2")
        self.assertEqual(len(registry.find("b1")), 2)
        self.assertEqual(len(registry.find("s2:b1")), 1)


class StateTest(IsolatedCase):
    def test_mode_persists_and_defaults_to_auto(self):
        self.assertEqual(state.get_mode(), "auto")
        state.set_mode("forced")
        self.assertEqual(state.get_mode(), "forced")

    def test_cycle(self):
        seen = [state.cycle_mode() for _ in range(4)]
        self.assertEqual(seen, ["forced", "off", "auto", "forced"])

    def test_garbage_mode_file_falls_back_to_default(self):
        os.makedirs(self.state)
        with open(state.mode_path(), "w") as fh:
            fh.write("sleepy")
        self.assertEqual(state.get_mode(), "auto")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            state.set_mode("turbo")

    def test_config_parsing_and_bad_values(self):
        cfg = state.parse_config({"unknown_grace_sec": 30, "unknown_fallback": "keep",
                                  "stuck_after_hours": 2, "hard_cap_hours": 12,
                                  "default_mode": "nonsense", "tick_sec": 1})
        self.assertEqual((cfg.unknown_grace_sec, cfg.unknown_fallback), (30.0, "keep"))
        self.assertEqual((cfg.stuck_after_sec, cfg.hard_cap_sec), (7200.0, 43200.0))
        self.assertEqual(cfg.default_mode, "auto")
        self.assertEqual(cfg.tick_sec, 5.0)
        self.assertIsNone(state.parse_config({"hard_cap_hours": "x"}).hard_cap_sec)

    def test_config_file_is_read(self):
        with open(os.path.join(self.config, "config.toml"), "w") as fh:
            fh.write('unknown_fallback = "keep"\ndefault_mode = "off"\n')
        cfg = state.load_config()
        self.assertEqual((cfg.unknown_fallback, cfg.default_mode), ("keep", "off"))
        self.assertEqual(state.get_mode(cfg), "off")

    def test_snapshots_round_trip(self):
        state.write_snapshot("s1", ["a", "b"], now=5.0)
        self.assertEqual(state.load_snapshots()["s1"]["task_ids"], ["a", "b"])
        state.remove_snapshot("s1")
        self.assertEqual(state.load_snapshots(), {})


if __name__ == "__main__":
    unittest.main()
