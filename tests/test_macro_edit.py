"""The editor bridge (app/gui/macro_edit.py): blocks out, recording back in, and never a silent change.

A macro IS a recording and its actions are derived, so the property that matters is not "the editor can change a
field" but "whatever the editor produces still compiles to exactly the actions the user saw". Every test here is
about that: fidelity of an untouched macro, exactness of each edit, and refusal of the edits that could not be
stored truthfully.
"""
import unittest

from app.gui import macro_edit as me
from app.playback.format import macro_to_json
from app.playback.macro import ActionOp, Macro
from app.providers.input import Key
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording

PID, TID = "windows", "win-1"


def recording(*events: E, end_reason: str = "stopped") -> Recording:
    return Recording(PID, TID, tuple(events), end_reason)


def click(delay: int, x: int, y: int, hold: int = 0) -> tuple[E, ...]:
    """A recorded click: down then up. `hold` is the time the button was held, which compilation ignores but a
    faithful editor must not throw away."""
    return (E(K.BUTTON_DOWN, delay, x, y, B.PRIMARY, 1), E(K.BUTTON_UP, hold, x, y, B.PRIMARY, 1))


def macro_of_events(*events: E, name: str = "m") -> Macro:
    return Macro(recording(*events), name)


class Blocks(unittest.TestCase):
    def test_a_pause_is_the_delay_of_the_action_that_follows_it(self):
        macro = macro_of_events(*click(0, 10, 10), *click(1500, 20, 20))
        blocks = me.blocks_of(me.document_of(macro))
        kinds = [(b.kind.value, b.action_index) for b in blocks]
        self.assertEqual(kinds, [("action", 0), ("pause", 1), ("action", 1)])
        self.assertEqual(blocks[1].delay_ms, 1500)
        self.assertEqual(blocks[0].number, 1)
        self.assertEqual(blocks[2].number, 2)

    def test_the_first_action_never_has_a_pause_row(self):
        macro = macro_of_events(*click(0, 10, 10))
        self.assertEqual([b.kind for b in me.blocks_of(me.document_of(macro))], [me.BlockKind.ACTION])

    def test_a_zero_pause_produces_no_row(self):
        macro = macro_of_events(*click(0, 1, 1), *click(0, 2, 2))
        self.assertEqual([b.kind.value for b in me.blocks_of(me.document_of(macro))], ["action", "action"])

    def test_halves_of_a_held_pair_are_marked_not_structural(self):
        macro = macro_of_events(E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1), E(K.POINTER_MOVE, 600, 9, 9),
                                E(K.BUTTON_UP, 50, 9, 9, B.PRIMARY, 1))
        blocks = [b for b in me.blocks_of(me.document_of(macro)) if b.kind is me.BlockKind.ACTION]
        self.assertEqual([b.action.op for b in blocks],
                         [ActionOp.BUTTON_DOWN, ActionOp.MOVE, ActionOp.BUTTON_UP])
        self.assertEqual([b.structural for b in blocks], [False, True, False])


class Fidelity(unittest.TestCase):
    def test_opening_and_saving_an_untouched_macro_is_byte_identical(self):
        """The whole reason the document keeps each action's own recorded events: compilation ignores the time
        inside a click, so rebuilding one from scratch would silently drop a recorded hold."""
        macro = macro_of_events(*click(0, 10, 10, hold=80), *click(900, 20, 20, hold=40),
                                E(K.KEY_DOWN, 300, key=Key.A), E(K.KEY_UP, 70, key=Key.A))
        document = me.document_of(macro)
        self.assertEqual(macro_to_json(me.macro_of(document)), macro_to_json(macro))

    def test_editing_a_position_keeps_the_recorded_hold_time(self):
        macro = macro_of_events(*click(0, 10, 10, hold=80))
        document = me.set_point(me.document_of(macro), 0, 33, 44)
        events = me.recording_of(document).events
        self.assertEqual([(e.x, e.y) for e in events], [(33, 44), (33, 44)])
        self.assertEqual(events[1].delay_ms, 80)                     # the hold survived the edit

    def test_end_reason_and_skipped_counters_survive_editing(self):
        source = Recording(PID, TID, click(0, 1, 1), "control_hotkey", (("unsupported_key", 2),))
        document = me.set_point(me.document_of(Macro(source, "m")), 0, 7, 7)
        rebuilt = me.recording_of(document)
        self.assertEqual(rebuilt.end_reason, "control_hotkey")
        self.assertEqual(rebuilt.skipped, (("unsupported_key", 2),))


class Edits(unittest.TestCase):
    def setUp(self):
        self.macro = macro_of_events(*click(0, 10, 10), *click(1000, 20, 20), *click(500, 30, 30))
        self.document = me.document_of(self.macro)

    def shapes(self, document):
        return [(a.op.value, a.delay_ms, a.x, a.y) for a in document.actions]

    def test_set_pause_changes_only_that_gap(self):
        after = me.set_pause(self.document, 1, 2500)
        self.assertEqual(self.shapes(after),
                         [("tap", 0, 10, 10), ("tap", 2500, 20, 20), ("tap", 500, 30, 30)])

    def test_a_pause_of_zero_removes_the_row(self):
        after = me.set_pause(self.document, 1, 0)
        self.assertEqual([b.kind.value for b in me.blocks_of(after)],
                         ["action", "action", "pause", "action"])

    def test_insert_places_the_action_at_that_number(self):
        after = me.insert(self.document, 1, ActionOp.KEY_PRESS, key=Key.ENTER, delay_ms=750)
        self.assertEqual([a.op.value for a in after.actions], ["tap", "key_press", "tap", "tap"])
        self.assertEqual(after.actions[1].key, Key.ENTER)
        self.assertEqual(after.actions[1].delay_ms, 750)
        self.assertEqual(len(me.recording_of(after).events), 8)      # key_press is a down/up pair

    def test_insert_at_the_start_loses_its_pause_because_a_recording_cannot_wait_first(self):
        after = me.insert(self.document, 0, ActionOp.TAP, x=1, y=2, delay_ms=900)
        self.assertEqual(after.actions[0].delay_ms, 0)
        self.assertEqual(me.recording_of(after).events[0].delay_ms, 0)

    def test_delete_removes_the_action_and_its_own_events(self):
        after = me.delete(self.document, 1)
        self.assertEqual(self.shapes(after), [("tap", 0, 10, 10), ("tap", 500, 30, 30)])
        self.assertEqual(len(me.recording_of(after).events), 4)

    def test_move_swaps_neighbours_and_renumbers(self):
        after = me.move(self.document, 2, -1)
        self.assertEqual([(a.x, a.y) for a in after.actions], [(10, 10), (30, 30), (20, 20)])
        numbers = [b.number for b in me.blocks_of(after) if b.kind is me.BlockKind.ACTION]
        self.assertEqual(numbers, [1, 2, 3])                          # numbering is positional, never stored

    def test_moving_an_action_to_the_front_drops_its_pause(self):
        after = me.move(self.document, 1, -1)
        self.assertEqual(after.actions[0].delay_ms, 0)

    def test_set_text_and_set_key_reach_the_events(self):
        macro = macro_of_events(E(K.TEXT, 0, text="hello"), E(K.KEY_DOWN, 100, key=Key.A),
                                E(K.KEY_UP, 10, key=Key.A))
        document = me.document_of(macro)
        document = me.set_text(document, 0, "goodbye")
        document = me.set_key(document, 1, Key.ENTER)
        self.assertEqual(document.actions[0].text, "goodbye")
        self.assertEqual(document.actions[1].key, Key.ENTER)
        self.assertEqual(me.recording_of(document).events[0].text, "goodbye")

    def test_every_edit_keeps_the_document_compilable_to_exactly_its_own_actions(self):
        document = self.document
        for edit, args in ((me.set_pause, (2, 1200)), (me.set_point, (0, 99, 99)),
                           (me.insert, (3, ActionOp.DOUBLE_TAP)), (me.delete, (1,)), (me.move, (0, 1))):
            kwargs = {"x": 5, "y": 6} if edit is me.insert else {}
            document = edit(document, *args, **kwargs)
            rebuilt = me.recording_of(document)                       # raises if the compiler disagrees
            self.assertEqual([me._shape(a) for a in Macro(rebuilt, "m").actions],
                             [me._shape(a) for a in document.actions])


class Refusals(unittest.TestCase):
    def setUp(self):
        self.document = me.document_of(macro_of_events(*click(0, 1, 1), *click(300, 2, 2)))
        self.held = me.document_of(macro_of_events(
            E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1), E(K.POINTER_MOVE, 600, 9, 9),
            E(K.BUTTON_UP, 50, 9, 9, B.PRIMARY, 1)))

    def test_a_pause_cannot_stand_before_the_first_action(self):
        with self.assertRaises(me.EditError):
            me.set_pause(self.document, 0, 500)

    def test_a_pause_is_bounded(self):
        for bad in (-1, me.MAX_PAUSE_MS + 1, True, 1.5):
            with self.assertRaises(me.EditError):
                me.set_pause(self.document, 1, bad)

    def test_half_of_a_held_pair_cannot_be_deleted_or_reordered(self):
        for edit, args in ((me.delete, (0,)), (me.move, (0, 1)), (me.move, (2, -1))):
            with self.assertRaises(me.EditError):
                edit(self.held, *args)

    def test_a_held_pair_may_still_have_its_position_corrected(self):
        after = me.set_point(self.held, 0, 11, 12)
        self.assertEqual((after.actions[0].x, after.actions[0].y), (11, 12))
        self.assertEqual(after.actions[0].op, ActionOp.BUTTON_DOWN)   # still a press, not silently a click

    def test_an_edit_that_would_merge_a_held_pair_into_a_click_is_refused(self):
        """The real danger of editing a recording: put the release next to its press, at the same point and with
        no time between, and `compile_actions` would fold them into one tap -- a different macro."""
        document = me.delete(self.held, 1)                            # remove the move between press and release
        self.assertEqual([a.op for a in document.actions], [ActionOp.BUTTON_DOWN, ActionOp.BUTTON_UP])
        with self.assertRaises(me.EditNotRepresentable):
            me.set_point(me.set_pause(document, 1, 0), 1, 5, 5)       # same point, no gap -> would become a tap

    def test_only_runnable_self_contained_actions_can_be_added(self):
        for op in (ActionOp.BUTTON_DOWN, ActionOp.BUTTON_UP, ActionOp.SECONDARY_DOWN, ActionOp.SECONDARY_UP,
                   ActionOp.KEY_DOWN, ActionOp.KEY_UP):
            self.assertNotIn(op, me.INSERTABLE)
            with self.assertRaises(me.EditError):
                me.insert(self.document, 0, op, x=1, y=1, key=Key.A)

    def test_invalid_values_are_reported_not_stored(self):
        with self.assertRaises(me.EditError):
            me.set_text(self.document, 0, "no text on a tap")
        with self.assertRaises(me.EditError):
            me.set_key(self.document, 0, Key.A)
        with self.assertRaises(me.EditError):
            me.set_point(self.document, 0, 1.5, 2)
        with self.assertRaises(me.EditError):
            me.insert(self.document, 99, ActionOp.TAP, x=1, y=1)

    def test_a_refused_edit_leaves_the_session_untouched(self):
        session = me.EditSession(macro_of_events(*click(0, 1, 1), *click(300, 2, 2)))
        before = session.document
        with self.assertRaises(me.EditError):
            session.apply(me.set_pause, 0, 500)
        self.assertIs(session.document, before)
        self.assertFalse(session.dirty)
        self.assertFalse(session.can_undo)


class Session(unittest.TestCase):
    def setUp(self):
        self.macro = macro_of_events(*click(0, 10, 10, hold=25), *click(1000, 20, 20))
        self.session = me.EditSession(self.macro)

    def test_a_fresh_session_is_clean(self):
        self.assertFalse(self.session.dirty)
        self.assertFalse(self.session.can_undo)
        self.assertFalse(self.session.can_redo)

    def test_undo_and_redo_walk_the_history(self):
        self.session.apply(me.set_pause, 1, 4000)
        self.session.apply(me.set_point, 0, 7, 7)
        self.assertTrue(self.session.dirty)
        self.assertTrue(self.session.undo())
        self.assertEqual((self.session.document.actions[0].x, self.session.document.actions[1].delay_ms), (10, 4000))
        self.assertTrue(self.session.redo())
        self.assertEqual(self.session.document.actions[0].x, 7)

    def test_undoing_everything_restores_the_original_file_exactly(self):
        self.session.apply(me.set_point, 0, 7, 7)
        self.session.apply(me.delete, 1)
        while self.session.can_undo:
            self.session.undo()
        self.assertEqual(macro_to_json(self.session.macro()), macro_to_json(self.macro))
        self.assertFalse(self.session.dirty)

    def test_a_new_edit_clears_the_redo_branch(self):
        self.session.apply(me.set_pause, 1, 2000)
        self.session.undo()
        self.assertTrue(self.session.can_redo)
        self.session.apply(me.set_pause, 1, 3000)
        self.assertFalse(self.session.can_redo)

    def test_mark_saved_makes_the_current_state_the_clean_one(self):
        self.session.apply(me.set_pause, 1, 2000)
        self.session.mark_saved()
        self.assertFalse(self.session.dirty)
        self.session.undo()
        self.assertTrue(self.session.dirty)          # going back from the saved state is itself a change

    def test_the_session_can_be_renamed_and_produces_that_macro(self):
        self.session.apply(me.rename, "  Morning run  ")
        self.assertEqual(self.session.document.name, "Morning run")
        self.assertEqual(self.session.macro().name, "Morning run")


class RealFile(unittest.TestCase):
    """The one real recorded macro in the repository, if it is still there: 419 actions including a drag."""

    def test_the_repository_macro_round_trips_untouched(self):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "data" / "macros"
        from app.playback.format import MacroFormatError, macro_from_json
        macros = []                       # the person's own folder (read only): it also holds scripts (macro v2)
        for file in sorted(path.glob("*.macro.json")) if path.is_dir() else []:
            try:
                macros.append(macro_from_json(file.read_text(encoding="utf-8")))
            except MacroFormatError:
                continue
        if not macros:
            self.skipTest("no recorded macro in data/macros")
        macro = macros[0]
        document = me.document_of(macro)
        self.assertEqual(len(document.actions), len(macro.actions))
        self.assertEqual(macro_to_json(me.macro_of(document)), macro_to_json(macro))


if __name__ == "__main__":
    unittest.main()
