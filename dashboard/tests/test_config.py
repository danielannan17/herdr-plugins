import os
import tempfile
import unittest

from herdr_dashboard import config
from herdr_dashboard.config import ConfigError, parse, match_workspace, slug


class ParseTests(unittest.TestCase):
    def test_empty_config_is_valid(self):
        cfg = parse("")
        self.assertIsNone(cfg.personal)
        self.assertEqual(cfg.workspaces, ())

    def test_command_as_string_is_split_and_args_are_appended(self):
        cfg = parse('[personal]\ncommand = "gh dash --config \\"my cfg.yml\\""\nargs = ["--debug"]')
        self.assertEqual(cfg.personal.argv, ("gh", "dash", "--config", "my cfg.yml", "--debug"))

    def test_command_as_list_env_and_cwd(self):
        cfg = parse('[workspaces.a]\ncommand = ["gh", "board"]\nenv = { FOO = "1" }\ncwd = "~/x"')
        entry = cfg.workspaces[0]
        self.assertEqual(entry.dashboard.argv, ("gh", "board"))
        self.assertEqual(entry.dashboard.env, {"FOO": "1"})
        self.assertEqual(entry.dashboard.cwd, "~/x")

    def test_entry_without_command_is_ignored(self):
        cfg = parse('[workspaces.a]\nlabel = "A"')
        self.assertEqual(cfg.workspaces, ())

    def test_syntax_error_is_a_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            parse("[personal\ncommand = ")
        self.assertIn("TOML syntax error", str(ctx.exception))

    def test_unknown_keys_are_rejected(self):
        for text in ('[personal]\ncomand = "x"', '[nope]\na = 1', '[keys]\nfoo = 1', '[popup]\nsize = 1'):
            with self.assertRaises(ConfigError, msg=text):
                parse(text)

    def test_wrong_types_are_rejected(self):
        for text in ('[personal]\ncommand = 3', '[personal]\ncommand = "x"\nenv = { A = 1 }',
                     '[personal]\ncommand = []', '[popup]\nwidth = "wide"', '[keys]\nbare = "yes"',
                     '[personal]\ncommand = "unterminated \\"quote"'):
            with self.assertRaises(ConfigError, msg=text):
                parse(text)

    def test_popup_size_accepts_cells_and_percent(self):
        cfg = parse('[popup]\nwidth = 120\nheight = "80%"')
        self.assertEqual((cfg.width, cfg.height), (120, "80%"))

    def test_missing_file_is_empty_and_unreadable_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(config.load(os.path.join(d, "none.toml")).personal)
            with self.assertRaises(ConfigError):
                config.load(d)  # a directory


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse('''
[workspaces.disposable-camera]
command = "a"
[workspaces."Cloud Config"]
command = "b"
label = ["cloud-config", "claude-config"]
[workspaces.repo]
command = "c"
path = "/tmp/hd-match/repo"
[workspaces.nested]
command = "d"
path = "/tmp/hd-match/repo/nested"
''')

    def key(self, label, cwd=None):
        entry = match_workspace(self.cfg, label, cwd)
        return entry and entry.key

    def test_label_is_matched_ignoring_case_and_punctuation(self):
        self.assertEqual(self.key("Disposable Camera"), "disposable-camera")
        self.assertEqual(self.key("disposable_camera"), "disposable-camera")
        self.assertEqual(slug("  Post Planner! "), "post-planner")

    def test_label_list(self):
        self.assertEqual(self.key("claude-config"), "Cloud Config")

    def test_unmatched_workspace_has_no_dashboard(self):
        self.assertIsNone(self.key("scratch"))
        self.assertIsNone(self.key(None))

    def test_renamed_workspace_still_matches_by_path(self):
        self.assertEqual(self.key("renamed", "/tmp/hd-match/repo/src"), "repo")

    def test_longest_path_wins_and_siblings_do_not_match(self):
        self.assertEqual(self.key("x", "/tmp/hd-match/repo/nested/deep"), "nested")
        self.assertIsNone(self.key("x", "/tmp/hd-match/repo-web"))

    def test_label_beats_path(self):
        self.assertEqual(self.key("disposable-camera", "/tmp/hd-match/repo"), "disposable-camera")


class HerdrBindingTests(unittest.TestCase):
    def read(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            return config.herdr_prefix_binding(f.name)
        finally:
            os.unlink(f.name)

    def test_prefix_and_bound_key(self):
        text = '[keys]\nprefix = "ctrl+a"\n[[keys.command]]\nkey = "prefix+g"\ntype = "plugin_action"\ncommand = "herdr-dashboard.open"'
        self.assertEqual(self.read(text), ("ctrl+a", "g"))

    def test_defaults_when_absent_or_unreadable(self):
        self.assertEqual(self.read("onboarding = false"), (None, None))
        self.assertEqual(config.herdr_prefix_binding("/nonexistent/config.toml"), (None, None))


if __name__ == "__main__":
    unittest.main()
