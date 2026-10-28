import unittest

from newtab.keys import KeyParser


def parse(data):
    return KeyParser().feed(data)


class KeyParserTests(unittest.TestCase):
    def test_plain_text_and_enter(self):
        self.assertEqual(parse(b"ab\r"), ["a", "b", "enter"])
        self.assertEqual(parse(b"\n"), ["enter"])

    def test_unicode_split_across_reads(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\xc3"), [])
        self.assertEqual(p.feed(b"\xa9"), ["é"])

    def test_backspace_variants(self):
        self.assertEqual(parse(b"\x7f\x08"), ["backspace", "backspace"])

    def test_arrows(self):
        self.assertEqual(parse(b"\x1b[A\x1b[B\x1b[C\x1b[D"), ["up", "down", "right", "left"])
        self.assertEqual(parse(b"\x1bOA\x1bOB"), ["up", "down"])

    def test_alt_down_forms(self):
        self.assertEqual(parse(b"\x1b[1;3B"), ["alt+down"])
        self.assertEqual(parse(b"\x1b\x1b[B"), ["alt+down"])
        self.assertEqual(parse(b"\x1b[1;9B"), ["alt+down"])

    def test_other_modifiers_are_not_alt_down(self):
        self.assertEqual(parse(b"\x1b[1;5B"), ["ctrl+down"])
        self.assertEqual(parse(b"\x1b[1;2B"), ["shift+down"])

    def test_home_end_delete(self):
        self.assertEqual(parse(b"\x1b[H\x1b[F\x1b[3~"), ["home", "end", "delete"])

    def test_csi_u_forms(self):
        self.assertEqual(parse(b"\x1b[13u"), ["enter"])
        self.assertEqual(parse(b"\x1b[27u"), ["esc"])
        self.assertEqual(parse(b"\x1b[49u"), ["1"])
        self.assertEqual(parse(b"\x1b[1;3B"), ["alt+down"])

    def test_lone_escape_waits_then_flushes_as_esc(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertTrue(p.pending)
        self.assertEqual(p.flush(), ["esc"])
        self.assertFalse(p.pending)

    def test_escape_then_sequence_completing_in_time_is_not_esc(self):
        p = KeyParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        self.assertEqual(p.feed(b"[1;3B"), ["alt+down"])
        self.assertFalse(p.pending)

    def test_escape_followed_by_text_in_one_read(self):
        self.assertEqual(parse(b"\x1bx"), ["alt+x"])

    def test_truncated_csi_is_dropped_after_timeout(self):
        p = KeyParser()
        p.feed(b"\x1b[1;")
        self.assertEqual(p.flush(), ["esc"])
        self.assertEqual(p.feed(b"a"), ["a"])

    def test_double_escape(self):
        p = KeyParser()
        p.feed(b"\x1b\x1b")
        self.assertEqual(p.flush(), ["esc", "esc"])

    def test_control_keys(self):
        self.assertEqual(parse(b"\x03\x15\x17"), ["ctrl+c", "ctrl+u", "ctrl+w"])
        self.assertEqual(parse(b"\t"), ["tab"])


if __name__ == "__main__":
    unittest.main()
