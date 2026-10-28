import unittest

from newtab import logic
from newtab.logic import Create, Workspace, label_for


def ws(n, label=None, focused=False, tabs=1):
    return Workspace(id=f"w{n}", label=label or f"space{n}", number=n, focused=focused, tab_count=tabs)


def spaces(count, focused=2, tabs=1):
    return [ws(i, focused=(i == focused), tabs=tabs) for i in range(1, count + 1)]


def press(state, *keys):
    effect = None
    for key in keys:
        state, effect = logic.update(state, key)
    return state, effect


def type_text(state, text):
    return press(state, *list(text))[0]


class DestinationTests(unittest.TestCase):
    def test_current_workspace_is_preselected(self):
        state = logic.initial_state(spaces(4, focused=3))
        self.assertEqual(state.dest_id, "w3")
        self.assertFalse(state.dropdown)

    def test_focused_workspace_beats_context_workspace(self):
        state = logic.initial_state(spaces(3, focused=2), context_workspace_id="w1")
        self.assertEqual(state.dest_id, "w2")

    def test_context_workspace_used_when_nothing_is_focused(self):
        state = logic.initial_state([ws(1), ws(2)], context_workspace_id="w2")
        self.assertEqual(state.dest_id, "w2")

    def test_destination_is_never_remembered(self):
        first = logic.initial_state(spaces(3, focused=2))
        first, _ = press(first, "alt+down", "3")
        self.assertEqual(first.dest_id, "w3")
        second = logic.initial_state(spaces(3, focused=2))
        self.assertEqual(second.dest_id, "w2")


class CreateTests(unittest.TestCase):
    def submit(self, state):
        state, effect = press(state, "enter")
        self.assertEqual(effect, logic.EFFECT_SUBMIT)
        return logic.on_listing(state, state.workspaces)

    def test_enter_creates_in_current_workspace_with_no_label(self):
        state = logic.initial_state(spaces(3, focused=2))
        state, create = self.submit(state)
        self.assertEqual(create, Create("w2", None))

    def test_create_in_another_workspace(self):
        state = logic.initial_state(spaces(3, focused=2))
        state, _ = press(state, "alt+down", "3")
        state, create = self.submit(state)
        self.assertEqual(create, Create("w3", None))

    def test_typed_name_becomes_label(self):
        state = type_text(logic.initial_state(spaces(2, focused=1)), "  api  ")
        state, create = self.submit(state)
        self.assertEqual(create, Create("w1", "api"))

    def test_empty_name_sends_no_label(self):
        self.assertIsNone(label_for("", ws(1)))
        self.assertIsNone(label_for("   ", ws(1)))

    def test_name_equal_to_default_sends_no_label(self):
        self.assertEqual(logic.default_name(ws(1, tabs=3)), "4")
        self.assertIsNone(label_for("4", ws(1, tabs=3)))
        self.assertEqual(label_for("3", ws(1, tabs=3)), "3")

    def test_default_name_follows_destination_tab_count(self):
        wss = [ws(1, tabs=1), ws(2, focused=True, tabs=5)]
        state = type_text(logic.initial_state(wss), "6")
        state, create = self.submit(state)
        self.assertEqual(create, Create("w2", None))
        state = type_text(logic.initial_state(wss), "6")
        state, _ = press(state, "alt+down", "1")
        state, create = self.submit(state)
        self.assertEqual(create, Create("w1", "6"))

    def test_digits_are_name_text_when_dropdown_is_closed(self):
        state = type_text(logic.initial_state(spaces(3, focused=2)), "42")
        self.assertEqual(state.name, "42")
        self.assertEqual(state.dest_id, "w2")


class DropdownTests(unittest.TestCase):
    def test_alt_down_opens_with_destination_highlighted(self):
        state, _ = press(logic.initial_state(spaces(5, focused=4)), "alt+down")
        self.assertTrue(state.dropdown)
        self.assertEqual(state.highlight, 3)

    def test_number_key_selects_and_closes(self):
        state, _ = press(logic.initial_state(spaces(5, focused=2)), "alt+down", "5")
        self.assertEqual(state.dest_id, "w5")
        self.assertFalse(state.dropdown)

    def test_number_beyond_list_is_ignored(self):
        state, _ = press(logic.initial_state(spaces(3, focused=2)), "alt+down", "7")
        self.assertTrue(state.dropdown)
        self.assertEqual(state.dest_id, "w2")

    def test_arrows_then_enter_select(self):
        state, _ = press(logic.initial_state(spaces(4, focused=2)), "alt+down", "down", "down", "enter")
        self.assertEqual(state.dest_id, "w4")
        self.assertFalse(state.dropdown)

    def test_arrows_wrap(self):
        state, _ = press(logic.initial_state(spaces(3, focused=1)), "alt+down", "up")
        self.assertEqual(state.highlight, 2)
        state, _ = press(state, "down")
        self.assertEqual(state.highlight, 0)

    def test_highlight_does_not_change_destination_until_confirmed(self):
        state, _ = press(logic.initial_state(spaces(4, focused=2)), "alt+down", "down")
        self.assertEqual(state.dest_id, "w2")

    def test_more_than_nine_workspaces_reachable_by_arrows(self):
        state, _ = press(logic.initial_state(spaces(14, focused=1)), "alt+down", "end", "enter")
        self.assertEqual(state.dest_id, "w14")
        state, _ = press(logic.initial_state(spaces(14, focused=1)), "alt+down", *["down"] * 11, "enter")
        self.assertEqual(state.dest_id, "w12")

    def test_digit_selects_only_first_nine(self):
        state, _ = press(logic.initial_state(spaces(14, focused=1)), "alt+down", "9")
        self.assertEqual(state.dest_id, "w9")

    def test_esc_closes_dropdown_only(self):
        state, effect = press(logic.initial_state(spaces(3, focused=2)), "alt+down", "esc")
        self.assertFalse(state.dropdown)
        self.assertIsNone(effect)
        self.assertEqual(state.phase, logic.EDITING)

    def test_name_preserved_across_workspace_change(self):
        state = type_text(logic.initial_state(spaces(3, focused=1)), "docs")
        state, _ = press(state, "alt+down", "3")
        self.assertEqual(state.name, "docs")
        self.assertEqual(state.cursor, 4)

    def test_input_has_focus_again_after_selection(self):
        state, _ = press(logic.initial_state(spaces(3, focused=1)), "alt+down", "2")
        state, _ = press(state, "x", "y")
        self.assertEqual(state.name, "xy")
        state, effect = press(state, "enter")
        self.assertEqual(effect, logic.EFFECT_SUBMIT)

    def test_enter_right_after_selection_creates(self):
        state, _ = press(logic.initial_state(spaces(3, focused=1)), "alt+down", "2", "enter")
        self.assertEqual(state.phase, logic.CREATING)
        _, create = logic.on_listing(state, state.workspaces)
        self.assertEqual(create.workspace_id, "w2")

    def test_ctrl_c_in_dropdown_cancels_everything(self):
        _, effect = press(logic.initial_state(spaces(3)), "alt+down", "ctrl+c")
        self.assertEqual(effect, logic.EFFECT_CANCEL)


class CancelTests(unittest.TestCase):
    def test_esc_cancels_without_creating(self):
        state = type_text(logic.initial_state(spaces(2)), "keep")
        state, effect = press(state, "esc")
        self.assertEqual(effect, logic.EFFECT_CANCEL)
        self.assertEqual(state.phase, logic.EDITING)


class SubmitFailureTests(unittest.TestCase):
    def test_deleted_workspace_at_submit_keeps_dialog_open(self):
        state, _ = press(logic.initial_state(spaces(3, focused=2)), "enter")
        fresh = [ws(1), ws(3, focused=True)]
        state, create = logic.on_listing(state, fresh)
        self.assertIsNone(create)
        self.assertEqual(state.phase, logic.EDITING)
        self.assertIsNone(state.dest_id)
        self.assertEqual(state.workspaces, tuple(fresh))
        self.assertIn("space2", state.error)

    def test_enter_after_deleted_workspace_asks_for_a_pick(self):
        state, _ = press(logic.initial_state(spaces(3, focused=2)), "enter")
        state, _ = logic.on_listing(state, [ws(1), ws(3)])
        state, effect = press(state, "enter")
        self.assertIsNone(effect)
        self.assertIn("Pick a workspace", state.error)

    def test_pick_after_deleted_workspace_then_create(self):
        state, _ = press(logic.initial_state(spaces(3, focused=2)), "enter")
        state, _ = logic.on_listing(state, [ws(1), ws(3)])
        state, _ = press(state, "alt+down", "2", "enter")
        _, create = logic.on_listing(state, state.workspaces)
        self.assertEqual(create.workspace_id, "w3")

    def test_create_failure_keeps_dialog_and_text(self):
        state = type_text(logic.initial_state(spaces(2, focused=1)), "build")
        state, _ = press(state, "enter")
        state = logic.on_create_failed(state, "boom")
        self.assertEqual(state.phase, logic.EDITING)
        self.assertEqual(state.name, "build")
        self.assertIn("boom", state.error)
        state, effect = press(state, "enter")
        self.assertEqual(effect, logic.EFFECT_SUBMIT)

    def test_listing_failure_keeps_dialog_open(self):
        state, _ = press(logic.initial_state(spaces(2)), "enter")
        state = logic.on_list_failed(state, "no server")
        self.assertEqual(state.phase, logic.EDITING)
        self.assertIn("no server", state.error)

    def test_duplicate_enter_is_guarded(self):
        state, first = press(logic.initial_state(spaces(2)), "enter")
        self.assertEqual(first, logic.EFFECT_SUBMIT)
        state, second = press(state, "enter", "enter")
        self.assertIsNone(second)
        self.assertEqual(state.phase, logic.CREATING)

    def test_keys_ignored_while_creating(self):
        state, _ = press(logic.initial_state(spaces(2)), "enter")
        after, _ = press(state, "x", "alt+down", "esc")
        self.assertEqual(after, state)

    def test_success_closes(self):
        state, _ = press(logic.initial_state(spaces(2)), "enter")
        self.assertEqual(logic.on_create_ok(state).phase, logic.CLOSED)

    def test_no_workspaces_blocks_enter(self):
        state, effect = press(logic.State(workspaces=(), dest_id=None), "enter")
        self.assertIsNone(effect)
        self.assertIsNotNone(state.error)


class EditingTests(unittest.TestCase):
    def test_typing_and_backspace(self):
        state = type_text(logic.initial_state(spaces(1)), "abc")
        state, _ = press(state, "backspace")
        self.assertEqual(state.name, "ab")

    def test_cursor_editing(self):
        state = type_text(logic.initial_state(spaces(1)), "ac")
        state, _ = press(state, "left", "b")
        self.assertEqual(state.name, "abc")
        state, _ = press(state, "home", "delete")
        self.assertEqual(state.name, "bc")
        state, _ = press(state, "end", "d")
        self.assertEqual(state.name, "bcd")

    def test_ctrl_u_and_ctrl_w(self):
        state = type_text(logic.initial_state(spaces(1)), "one two")
        state, _ = press(state, "ctrl+w")
        self.assertEqual(state.name, "one ")
        state, _ = press(state, "ctrl+u")
        self.assertEqual(state.name, "")

    def test_non_printable_and_unknown_keys_do_nothing(self):
        state = logic.initial_state(spaces(1))
        after, effect = press(state, "unknown", "ctrl+x", "alt+b", "up")
        self.assertEqual(after, state)
        self.assertIsNone(effect)

    def test_name_length_is_capped(self):
        state = type_text(logic.initial_state(spaces(1)), "x" * 200)
        self.assertEqual(len(state.name), logic.MAX_NAME)


if __name__ == "__main__":
    unittest.main()
