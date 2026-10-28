import unittest

from herdr_dashboard.keys import Action, Router, decode, leader_byte, split


def kinds(actions):
    return [a.kind for a in actions]


class RouterTests(unittest.TestCase):
    def test_plain_input_is_forwarded_untouched(self):
        r = Router()
        self.assertEqual(r.feed(b"hello\t\x1b[A\x1b[Z\x1b", 0), [Action("forward", b"hello\t\x1b[A\x1b[Z")])
        self.assertTrue(r.pending)  # a lone Esc waits to see whether it starts a sequence

    def test_lone_escape_is_forwarded_after_the_timeout(self):
        r = Router()
        r.feed(b"\x1b", 0)
        self.assertEqual(r.poll(0.01), [])
        self.assertEqual(r.poll(0.1), [Action("forward", b"\x1b")])

    def test_leader_chords(self):
        cases = {b"d": "close", b"\t": "next", b"\x1b[Z": "prev", b"p": "personal", b"w": "work", b"r": "retry"}
        for key, kind in cases.items():
            self.assertEqual(kinds(Router().feed(b"\x02" + key, 0)), [kind], key)

    def test_leader_twice_sends_a_literal_leader_and_unknown_keys_are_dropped(self):
        self.assertEqual(Router().feed(b"\x02\x02", 0), [Action("forward", b"\x02")])
        self.assertEqual(Router().feed(b"\x02z", 0), [])

    def test_leader_expires(self):
        r = Router()
        r.feed(b"\x02", 0)
        r.poll(3.0)
        self.assertEqual(r.feed(b"d", 3.1), [Action("forward", b"d")])

    def test_custom_toggle_key(self):
        self.assertEqual(kinds(Router(toggle="g").feed(b"\x02g", 0)), ["close"])
        self.assertEqual(Router(toggle="g").feed(b"\x02d", 0), [])

    def test_bare_keys_only_intercepted_when_enabled(self):
        self.assertEqual(kinds(Router(bare=True).feed(b"\t", 0)), ["next"])
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[Z", 0)), ["prev"])
        r = Router(bare=True)
        r.feed(b"\x1b", 0)
        self.assertEqual(kinds(r.poll(0.1)), ["close"])
        self.assertEqual(kinds(Router(bare=False).feed(b"\t", 0)), ["forward"])

    def test_message_screen_always_intercepts_bare_keys_and_retries_on_r(self):
        r = Router(message=True)
        self.assertEqual(kinds(r.feed(b"\t", 0)), ["next"])
        self.assertEqual(kinds(r.feed(b"r", 0)), ["retry"])

    def test_bracketed_paste_is_never_interpreted(self):
        r = Router(bare=True)
        out = r.feed(b"\x1b[200~a\tb\x02d\x1b[201~", 0)
        self.assertEqual(out, [Action("forward", b"\x1b[200~a\tb\x02d\x1b[201~")])
        out = r.feed(b"\x1b[200~x\t", 0) + r.feed(b"\ty\x1b[201~\t", 0)
        self.assertEqual(kinds(out), ["forward", "forward", "next"])

    def test_sequence_split_across_reads(self):
        r = Router(bare=True)
        self.assertEqual(r.feed(b"\x1b[", 0), [])
        self.assertEqual(kinds(r.feed(b"Z", 0.01)), ["prev"])

    def test_kitty_keyboard_encodings(self):
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[9u", 0)), ["next"])
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[9;2u", 0)), ["prev"])
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[27u", 0)), ["close"])
        self.assertEqual(kinds(Router().feed(b"\x1b[98;5u\x1b[100u", 0)), ["close"])
        self.assertEqual(Router(bare=True).feed(b"\x1b[9;1:3u", 0), [])  # key release of an intercepted key

    def test_modify_other_keys_encodings(self):
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[27;2;9~", 0)), ["prev"])
        self.assertEqual(kinds(Router(bare=True).feed(b"\x1b[27;1;9~", 0)), ["next"])
        self.assertEqual(kinds(Router().feed(b"\x1b[27;5;98~\x1b[27;2;9~", 0)), ["prev"])

    def test_other_escape_sequences_and_utf8_pass_through(self):
        data = "\x1b[1;5C\x1bOP\x1bb héllo \x1b[?1;2c".encode()
        self.assertEqual(Router(bare=True).feed(data, 0), [Action("forward", data)])

    def test_double_escape_is_not_an_alt_key(self):
        r = Router(bare=True)
        self.assertEqual(kinds(r.feed(b"\x1b\x1b[Z", 0)), ["close", "prev"])


class HelperTests(unittest.TestCase):
    def test_leader_byte(self):
        self.assertEqual(leader_byte("ctrl+b"), b"\x02")
        self.assertEqual(leader_byte("Ctrl+A"), b"\x01")
        with self.assertRaises(ValueError):
            leader_byte("alt+b")

    def test_decode(self):
        self.assertEqual(decode(b"\t"), "tab")
        self.assertEqual(decode(b"\x1b[Z"), "shift+tab")
        self.assertEqual(decode(b"d"), "d")
        self.assertIsNone(decode(b"\x1b[A"))

    def test_split_keeps_incomplete_tail(self):
        self.assertEqual(split(b"a\x1b[1;", False), ([b"a"], b"\x1b[1;"))
        self.assertEqual(split(b"a\x1b[1;", True)[1], b"")


if __name__ == "__main__":
    unittest.main()
