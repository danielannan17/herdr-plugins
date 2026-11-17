import contextlib
import io
import os
import unittest
from unittest import mock

from caffeinate_lib import cli, control, registry, state

from .support import IsolatedCase, agent


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        for name in ("ensure_daemon", "poke"):
            p = mock.patch.object(cli.control, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def test_register_classifies_with_rules_when_no_class_given(self):
        code, out, _ = run_cli("register", "--task-id", "b1", "--session", "s1", "--command", "npm run build")
        self.assertEqual(code, 0)
        task = registry.get("s1", "b1")
        self.assertEqual((task["class"], task["label"], task["source"]), ("finite", "build", "cli"))
        self.poke.assert_called()

    def test_register_with_explicit_options(self):
        run_cli("register", "--task-id", "x", "--class", "persistent", "--no-keep-awake",
                "--pid", "123", "--timeout", "60", "--description", "watching")
        task = registry.get("", "x")
        self.assertEqual((task["class"], task["keep_awake"], task["pid"], task["timeout"]),
                         ("persistent", False, 123, 60.0))

    def test_update_and_complete(self):
        run_cli("register", "--task-id", "x", "--session", "s", "--class", "unknown")
        run_cli("update", "--task-id", "x", "--session", "s", "--class", "finite")
        self.assertEqual(registry.get("s", "x")["class"], "finite")
        run_cli("complete", "--task-id", "x", "--session", "s", "--status", "failed")
        self.assertEqual(registry.get("s", "x")["status"], "failed")
        self.assertEqual(run_cli("complete", "--task-id", "nope")[0], 1)

    def test_override_by_bare_and_qualified_id(self):
        self.task("b1", "s1")
        self.task("b1", "s2")
        code, _, err = run_cli("override", "b1", "ignore")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        self.assertEqual(run_cli("override", "s2:b1", "ignore")[0], 0)
        self.assertEqual(registry.get("s2", "b1")["override"], "ignore")
        self.assertIsNone(registry.get("s1", "b1")["override"])
        run_cli("override", "s2:b1", "clear")
        self.assertIsNone(registry.get("s2", "b1")["override"])

    def test_mode(self):
        self.assertEqual(run_cli("mode")[1].strip(), "auto")
        self.assertEqual(run_cli("mode", "cycle")[1].strip(), "forced")
        self.assertEqual(run_cli("mode", "off")[1].strip(), "off")
        self.assertEqual(state.get_mode(), "off")

    def test_details_lists_each_bucket_with_a_reason(self):
        self.task("active", "s1", now=1e9 - 60, description="Run the test suite")
        self.task("dev", "s1", now=1e9 - 60, cls="persistent", description="Dev server")
        self.task("mystery", "s1", now=1e9 - 600, cls="unknown", label=None, description="Run thing")
        self.task("old", "s1", now=1e9 - 7 * 3600, description="Overnight build")
        with mock.patch("caffeinate_lib.engine.time.time", return_value=1e9), \
             mock.patch("caffeinate_lib.engine.herdr.list_agents", return_value=[agent("s1", "idle")]):
            code, out, _ = run_cli("details")
        self.assertEqual(code, 0)
        for heading in ("Active (1)", "Possibly stuck (1)", "Unclassified (1)", "Ignored (1)"):
            self.assertIn(heading, out)
        self.assertIn("persistent service", out)
        self.assertIn("Overnight build", out)


class ControlTest(IsolatedCase):
    def test_second_lock_holder_is_refused(self):
        first = control.acquire_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(control.acquire_lock())
        self.assertTrue(control.daemon_running())
        first.close()
        self.assertFalse(control.daemon_running())

    def test_poke_without_a_daemon_reports_false(self):
        self.assertFalse(control.poke())
        os.makedirs(self.state, exist_ok=True)
        os.mkfifo(control.fifo_path())
        self.assertFalse(control.poke(), "a fifo nobody reads is not a daemon")

    def test_poke_reaches_a_reader(self):
        os.makedirs(self.state, exist_ok=True)
        os.mkfifo(control.fifo_path())
        reader = os.open(control.fifo_path(), os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        self.assertTrue(control.poke())
        self.assertEqual(os.read(reader, 10), b"x")

    def test_ensure_daemon_spawns_only_when_not_running(self):
        with mock.patch.object(control.subprocess, "Popen") as popen:
            self.assertTrue(control.ensure_daemon())
            self.assertEqual(popen.call_args[0][0][-1], "--daemon")
            self.assertTrue(popen.call_args[1]["start_new_session"])
            lock = control.acquire_lock()
            popen.reset_mock()
            self.assertFalse(control.ensure_daemon())
            popen.assert_not_called()
            lock.close()


if __name__ == "__main__":
    unittest.main()
