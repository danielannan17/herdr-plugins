import os
import tempfile
import unittest
import unittest.mock

from newtab import logic, shells

CLAUDE = logic.Shell("Claude", "c")
ZSH = logic.Shell("zsh", None)


class ShellsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_dir = os.path.join(self.tmp.name, "config")
        env = {"HERDR_PLUGIN_CONFIG_DIR": self.config_dir}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("NEW_TAB_SHELLS", None)
        self.path = os.path.join(self.config_dir, "shells.toml")

    def test_first_run_copies_the_example_into_the_config_folder(self):
        loaded = shells.load()
        self.assertEqual(loaded, ((CLAUDE, ZSH), None))
        with open(shells.EXAMPLE_PATH) as example, open(self.path) as created:
            self.assertEqual(created.read(), example.read())

    def test_existing_file_is_used_and_never_overwritten(self):
        os.makedirs(self.config_dir)
        mine = '[[shell]]\nname = "fish"\ncommand = "fish"\n'
        with open(self.path, "w") as f:
            f.write(mine)
        self.assertEqual(shells.load(), ((logic.Shell("fish", "fish"),), None))
        with open(self.path) as f:
            self.assertEqual(f.read(), mine)

    def test_existing_invalid_file_falls_back_to_a_plain_shell_with_an_error(self):
        os.makedirs(self.config_dir)
        with open(self.path, "w") as f:
            f.write("[[shell")
        loaded, error = shells.load()
        self.assertEqual(loaded, (logic.PLAIN_SHELL,))
        self.assertTrue(error.startswith("shells.toml:"), error)
        with open(self.path) as f:
            self.assertEqual(f.read(), "[[shell")

    def test_failed_creation_uses_the_example_shells_and_reports_why(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w"):
            pass
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = os.path.join(blocker, "config")
        loaded, error = shells.load()
        self.assertEqual(loaded, (CLAUDE, ZSH))
        self.assertTrue(error.startswith("shells.toml: could not create"), error)

    def test_override_file_is_read_as_is_without_creating_anything(self):
        override = os.path.join(self.tmp.name, "other.toml")
        with open(override, "w") as f:
            f.write('[[shell]]\nname = "x"\n')
        os.environ["NEW_TAB_SHELLS"] = override
        self.assertEqual(shells.load(), ((logic.Shell("x", None),), None))
        self.assertFalse(os.path.exists(self.config_dir))

    def test_missing_override_file_reports_an_error(self):
        os.environ["NEW_TAB_SHELLS"] = os.path.join(self.tmp.name, "absent.toml")
        loaded, error = shells.load()
        self.assertEqual(loaded, (logic.PLAIN_SHELL,))
        self.assertTrue(error.startswith("shells.toml:"), error)


class ConfigDirTests(unittest.TestCase):
    def test_asks_the_cli_when_herdr_passes_no_env_var(self):
        env = {"HERDR_BIN_PATH": "/bin/herdr", "HERDR_PLUGIN_ID": "me.plugin"}
        done = unittest.mock.Mock(stdout="/some/dir\n")
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch("newtab.shells.subprocess.run", return_value=done) as run:
            os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
            self.assertEqual(shells._config_dir(), "/some/dir")
        self.assertEqual(run.call_args.args[0], ["/bin/herdr", "plugin", "config-dir", "me.plugin"])


if __name__ == "__main__":
    unittest.main()
