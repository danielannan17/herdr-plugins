import unittest

from herdr_dashboard import screen


class ScreenTests(unittest.TestCase):
    def test_box_has_title_text_and_hints(self):
        out = screen.render("Work Dashboard", ["No dashboard configured for this workspace."],
                            [("Tab", "Switch to Personal"), ("Esc", "Close")], 24, 80)
        plain = screen.strip_ansi(out)
        for text in ("Work Dashboard", "No dashboard configured for this workspace.",
                     "Tab  Switch to Personal", "Esc  Close"):
            self.assertIn(text, plain)

    def test_long_lines_wrap_inside_narrow_terminals(self):
        out = screen.render("T", ["x" * 200], [("Esc", "Close")], 20, 30)
        for line in out.split("\x1b[")[1:]:
            visible = screen.strip_ansi("\x1b[" + line)
            self.assertLessEqual(len(visible.split("H", 1)[-1]), 30)

    def test_excerpt_strip_escape_sequences(self):
        raw = b"\x1b[2J\x1b[Hfirst\r\n\x1b[31merror: boom\x1b[0m\r\n\r\n"
        self.assertEqual(screen.excerpt(raw), ["first", "error: boom"])

    def test_excerpt_keeps_the_first_and_last_lines_of_long_output(self):
        raw = "\n".join(f"line {i}" for i in range(20)).encode()
        self.assertEqual(screen.excerpt(raw), ["line 0", "line 1", "...", "line 16", "line 17", "line 18", "line 19"])


if __name__ == "__main__":
    unittest.main()
