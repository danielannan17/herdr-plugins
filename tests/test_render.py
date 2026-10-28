import re
import unittest

from newtab import logic, render
from newtab.logic import Workspace


def ws(n, label=None, focused=False, tabs=1):
    return Workspace(id=f"w{n}", label=label or f"space{n}", number=n, focused=focused, tab_count=tabs)


def lines(state, rows=7, cols=54):
    return [render.strip_sgr(x) for x in render.render(state, rows, cols)]


def press(state, *keys):
    for k in keys:
        state, _ = logic.update(state, k)
    return state


class RenderTests(unittest.TestCase):
    def test_shows_current_workspace_and_input(self):
        state = logic.initial_state([ws(1, "alpha"), ws(2, "beta", focused=True, tabs=3)])
        out = lines(state)
        self.assertEqual(len(out), 7)
        self.assertIn("Workspace: beta", out[1])
        self.assertIn("[⌄]", out[1])
        self.assertIn("Tab name:", out[2])
        self.assertIn("default: 4", out[2])
        self.assertIn("Enter to create", out[-1])

    def test_typed_text_replaces_default_hint(self):
        state = press(logic.initial_state([ws(1, focused=True)]), *"api")
        self.assertIn("Tab name: api", lines(state)[2])
        self.assertNotIn("default:", lines(state)[2])

    def test_dropdown_marks_destination_and_active_workspace(self):
        state = logic.initial_state([ws(1, "alpha"), ws(2, "beta", focused=True), ws(3, "gamma")])
        state = press(state, "alt+down", "down", "enter", "alt+down")
        out = lines(state)
        self.assertIn("[⌃]", out[1])
        body = "\n".join(out)
        self.assertRegex(body, r"✓\s+3\s+gamma")
        self.assertRegex(body, r"2\s+beta · active")
        self.assertNotRegex(body, r"✓\s+2")

    def test_dropdown_scrolls_to_keep_highlight_visible(self):
        wss = [ws(i, f"space{i}", focused=(i == 1)) for i in range(1, 21)]
        state = press(logic.initial_state(wss), "alt+down", "end")
        out = lines(state)
        self.assertTrue(any("space20" in line for line in out))
        self.assertFalse(any(re.search(r"\bspace1\b", line) for line in out[2:]))
        self.assertIn("↑ 16 more", out[-1])
        self.assertIn("1-9 select", out[-1])

    def test_every_row_fits_the_width(self):
        wss = [ws(i, "a very long workspace label " * 4, focused=(i == 1)) for i in range(1, 6)]
        state = press(logic.initial_state(wss), *"x" * 70)
        for st in (state, press(state, "alt+down")):
            for line in render.render(st, 7, 54):
                self.assertLessEqual(render.text_width(line), 54)

    def test_four_workspaces_fill_the_list_exactly(self):
        wss = [ws(i, f"space{i}", focused=(i == 2)) for i in range(1, 5)]
        out = lines(press(logic.initial_state(wss), "alt+down"))
        self.assertEqual(out[0], "")
        self.assertIn("Workspace:", out[1])
        for i in range(4):
            self.assertIn(f"space{i + 1}", out[2 + i])
        self.assertIn("1-9 select", out[6])
        self.assertNotIn("more", out[6])

    def test_hidden_items_lead_the_hint_and_the_hint_is_trimmed_to_fit(self):
        wss = [ws(i, f"space{i}", focused=(i == 1)) for i in range(1, 10)]
        out = lines(press(logic.initial_state(wss), "alt+down"))
        self.assertTrue(out[6].lstrip().startswith("↓ 5 more · 1-9 select"), out[6])
        both = lines(press(logic.initial_state(wss), "alt+down", *["down"] * 4))
        self.assertTrue(both[6].lstrip().startswith("↑ 2 more · ↓ 3 more · 1-9 select"), both[6])
        self.assertLessEqual(render.text_width(both[6]), 54)

    def test_closed_layout_has_nothing_between_error_and_hint(self):
        state = logic.on_create_failed(logic.initial_state([ws(1, focused=True)]), "nope")
        out = lines(state)
        self.assertEqual(out[0], "")
        self.assertIn("Workspace:", out[1])
        self.assertIn("Tab name:", out[2])
        self.assertEqual(out[3], "")
        self.assertIn("nope", out[5])
        self.assertIn("Enter to create", out[6])
        long = logic.on_create_failed(logic.initial_state([ws(1, focused=True)]), "x" * 40 + " " + "y" * 40)
        out = lines(long)
        self.assertEqual(out[3], "")
        self.assertTrue(out[4].strip() and out[5].strip())
        self.assertIn("Enter to create", out[6])

    def test_error_is_shown(self):
        state = logic.on_create_failed(logic.initial_state([ws(1, focused=True)]), "nope")
        self.assertTrue(any("nope" in line for line in lines(state)))

    def test_creating_status(self):
        state = press(logic.initial_state([ws(1, focused=True)]), "enter")
        self.assertIn("Creating", lines(state)[-1])


if __name__ == "__main__":
    unittest.main()
