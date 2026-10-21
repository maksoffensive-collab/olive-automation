"""The macro workspace and the multi-target run, driven through the real controller.

Same simulated desktop as tests/test_gui.py (the real WindowsServices -> verifier -> engine chain over a fake
world), so these tests show the controller sequencing the existing layers rather than a mock of them.

The rule that matters most here is the safety one: an empty target choice REFUSES the run, it never widens into
"all of them". Everything else is about a run covering several targets honestly -- one target at a time, one
target's failure not stopping the others, and the emergency stop ending the whole run instead of moving on.
"""
import os
import time
import unittest
from pathlib import Path

from app.gui.controller import AppController, RunState, TargetStatus
from app.gui.history import COMPLETED, FAILED, PARTIAL, REFUSED, STOPPED, RunHistory
from app.gui.library import MacroLibrary
from app.gui.macro_list import Filter, Scope, Sort
from app.gui.meta import MacroMeta
from app.gui import macro_edit as me
from app.playback.macro import ActionOp
from app.providers.input import Key
from tests.test_gui import Base as GuiBase


class Base(GuiBase):
    """The shared world, plus a controller that really stores favourites, notes and run history."""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.meta = MacroMeta(root / "meta.json")
        self.history = RunHistory(root / "history.json")
        self.c = AppController(self.services, self.library, meta=self.meta, history=self.history)
        self.c.refresh()

    def saved(self, name="Macro", *, folder="", clicks=1):
        """Record a real macro and save it under `name`, returning its key."""
        self.record(clicks=clicks)
        self.assertTrue(self.c.save(name), self.c.status)
        if folder:
            self.c.refresh_macros()
            key = next(i.key for i in self.c.macros if i.name == name)
            self.assertTrue(self.c.move_macro(key, folder), self.c.status)
        self.c.refresh_macros()
        return next(i.key for i in self.c.macros if i.name == name)

    def run_to_end(self, timeout=10.0):
        deadline = time.time() + timeout
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.c.poll()
        self.assertFalse(self.c.run_active, f"run did not finish: {self.c.run_results}")


class Workspace(Base):
    def test_a_saved_macro_appears_in_the_list_with_a_stable_key(self):
        key = self.saved("First")
        info = self.c.macro_by_key(key)
        self.assertEqual((info.name, info.folder, info.key), ("First", "", "first.macro.json"))
        self.assertGreater(info.actions, 0)

    def test_creating_a_folder_opens_it_so_the_user_can_see_where_they_are(self):
        self.c.create_folder("Tests")
        self.assertEqual((self.c.filter.scope, self.c.filter.folder), (Scope.FOLDER, "Tests"))
        self.assertEqual(self.c.visible_macros(), [])

    def test_scope_search_and_sort_decide_what_is_visible(self):
        self.c.create_folder("Tests")
        alpha = self.saved("Alpha")
        beta = self.saved("Beta", folder="Tests")
        # Two saves in a row often get the SAME file time on this machine (a tie is then sorted by name, which
        # puts Alpha first): give them distinct, known times, so "newest first" is what is being tested.
        for key, mtime in ((alpha, 1_700_000_000), (beta, 1_700_000_100)):
            path = self.c.macro_by_key(key).path
            os.utime(path, (mtime, mtime))
        self.c.refresh_macros()
        self.c.set_scope(Scope.ALL)
        self.assertEqual([i.name for i in self.c.visible_macros()], ["Beta", "Alpha"])   # newest first
        self.c.set_scope(Scope.FOLDER, "Tests")
        self.assertEqual([i.name for i in self.c.visible_macros()], ["Beta"])
        self.c.set_scope(Scope.ALL)
        self.c.set_query("alph")
        self.assertEqual([i.name for i in self.c.visible_macros()], ["Alpha"])
        self.c.set_query("")
        self.c.set_sort(Sort.NAME)
        self.assertEqual([i.name for i in self.c.visible_macros()], ["Alpha", "Beta"])

    def test_favourites_are_a_view_not_a_copy(self):
        key = self.saved("Starred")
        self.assertTrue(self.c.toggle_favorite(key))
        self.assertTrue(self.c.is_favorite(key))
        self.c.set_scope(Scope.FAVORITES)
        self.assertEqual([i.key for i in self.c.visible_macros()], [key])
        self.assertEqual(len(list(self.library.directory.glob("*.macro.json"))), 1)      # still ONE file

    def test_a_description_belongs_to_the_macro_not_to_its_file(self):
        key = self.saved("Described")
        self.c.set_description(key, "Collects the daily rewards.")
        self.assertEqual(self.c.description(key), "Collects the daily rewards.")
        text = self.c.macro_by_key(key).path.read_text(encoding="utf-8")
        self.assertNotIn("daily rewards", text)          # the macro format was not changed for a GUI feature

    def test_renaming_keeps_the_star_the_note_and_the_past_runs(self):
        key = self.saved("Before")
        self.c.toggle_favorite(key)
        self.c.set_description(key, "note")
        self.history.record(macro="Before", macro_key=key, outcome=COMPLETED)
        self.assertTrue(self.c.rename_macro(key, "After"), self.c.status)
        new_key = self.c.selected_key
        self.assertEqual(new_key, "after.macro.json")
        self.assertTrue(self.c.is_favorite(new_key))
        self.assertEqual(self.c.description(new_key), "note")
        self.assertEqual(len(self.history.for_macro(new_key)), 1)
        self.assertFalse(self.c.is_favorite(key))

    def test_moving_into_a_folder_moves_the_file_and_keeps_the_metadata(self):
        key = self.saved("Movable")
        self.c.create_folder("Tests")
        self.c.toggle_favorite(key)
        self.assertTrue(self.c.move_macro(key, "Tests"), self.c.status)
        info = self.c.macro_by_key(self.c.selected_key)
        self.assertEqual(info.folder, "Tests")
        self.assertTrue(info.path.is_file())
        self.assertTrue(self.c.is_favorite(info.key))

    def test_duplicating_makes_an_independent_file(self):
        key = self.saved("Original")
        self.c.set_description(key, "shared note")
        self.assertTrue(self.c.duplicate_macro(key, "Original copy"), self.c.status)
        copy_key = self.c.selected_key
        self.assertNotEqual(copy_key, key)
        self.assertEqual(self.c.description(copy_key), "shared note")
        self.assertTrue(self.c.delete_macro(copy_key), self.c.status)
        self.assertIsNotNone(self.c.macro_by_key(key))        # deleting the copy left the original alone

    def test_deleting_forgets_the_metadata_but_keeps_the_history_of_what_happened(self):
        key = self.saved("Doomed")
        self.c.toggle_favorite(key)
        self.history.record(macro="Doomed", macro_key=key, outcome=COMPLETED)
        self.assertTrue(self.c.delete_macro(key), self.c.status)
        self.assertEqual(self.meta.favorites(), frozenset())
        self.assertEqual(len(self.history.for_macro(key)), 1)

    def test_a_folder_that_still_holds_macros_is_not_deleted(self):
        self.c.create_folder("Tests")
        self.saved("Inside", folder="Tests")
        self.assertFalse(self.c.delete_folder("Tests"))
        self.assertEqual(self.c.status.level, "error")
        self.assertIn("Tests", self.c.folders())

    def test_renaming_a_folder_keeps_the_metadata_of_the_macros_in_it(self):
        self.c.create_folder("Old")
        key = self.saved("Inside", folder="Old")
        self.c.toggle_favorite(key)
        self.assertTrue(self.c.rename_folder("Old", "New"), self.c.status)
        self.assertEqual(self.c.folders(), ["New"])
        self.assertTrue(self.c.is_favorite("New/inside.macro.json"))

    def test_creating_a_macro_needs_a_verified_target_because_a_macro_is_always_for_one(self):
        self.assertIs(self.c.target_status, TargetStatus.NONE)
        self.assertFalse(self.c.create_macro("Handmade"))
        self.assertEqual(self.c.status.category, "invalid_selection")
        self.assertTrue(self.c.select(self.entry("Notes").ref), self.c.status)
        self.assertTrue(self.c.create_macro("Handmade", description="made by hand"), self.c.status)
        info = self.c.macro_by_key(self.c.selected_key)
        self.assertEqual((info.name, info.actions), ("Handmade", 0))
        self.assertEqual(self.c.description(info.key), "made by hand")

    def test_clearing_the_view_deletes_nothing(self):
        key = self.saved("Kept")
        self.c.toggle_favorite(key)
        self.c.set_query("kept")
        self.c.set_scope(Scope.FAVORITES)
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.reset_view())
        self.assertEqual((self.c.filter, self.c.selected_key, self.c.run_refs), (Filter(), "", ()))
        self.c.refresh_macros()
        self.assertIsNotNone(self.c.macro_by_key(key))
        self.assertTrue(self.c.is_favorite(key))                # the star is a property of the macro, not the view


class Editor(Base):
    def test_opening_editing_and_saving_changes_the_file(self):
        key = self.saved("Editable", clicks=2)
        self.assertTrue(self.c.open_editor(key), self.c.status)
        before = len(self.c.editor.document.actions)
        self.assertTrue(self.c.editor_apply(me.insert, 0, ActionOp.KEY_PRESS, key=Key.ENTER))
        self.assertTrue(self.c.editor_dirty)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertFalse(self.c.editor_dirty)
        self.assertEqual(self.c.macro_by_key(key).actions, before + 1)
        self.assertEqual(self.library.load(self.c.macro_by_key(key).path).actions[0].op, ActionOp.KEY_PRESS)

    def test_a_refused_edit_reports_why_and_changes_nothing(self):
        key = self.saved("Editable")
        self.c.open_editor(key)
        before = self.c.editor.document
        self.assertFalse(self.c.editor_apply(me.set_pause, 0, 1000))     # no pause before the first action
        self.assertEqual(self.c.status.category, "editor")
        self.assertIs(self.c.editor.document, before)

    def test_undo_and_redo_are_available_and_saving_is_not_needed_to_go_back(self):
        key = self.saved("Editable", clicks=2)
        self.c.open_editor(key)
        original = self.c.editor.document
        self.c.editor_apply(me.set_point, 0, 77, 88)
        self.assertTrue(self.c.editor_undo())
        self.assertEqual(self.c.editor.document, original)
        self.assertTrue(self.c.editor_redo())
        self.assertEqual(self.c.editor.document.actions[0].x, 77)

    def test_saving_an_untouched_macro_leaves_the_file_byte_identical(self):
        key = self.saved("Untouched", clicks=3)
        path = self.c.macro_by_key(key).path
        before = path.read_bytes()
        self.c.open_editor(key)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertEqual(path.read_bytes(), before)

    def test_editing_is_refused_while_something_is_running(self):
        key = self.saved("Busy")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.c.start_run()
        self.assertFalse(self.c.open_editor(key))
        self.assertEqual(self.c.status.category, "busy")
        self.run_to_end()


class MultiTargetRun(Base):
    def prepare(self, *titles, name="Runnable"):
        key = self.saved(name)
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry(t).ref for t in titles])
        return key

    def test_an_empty_target_choice_refuses_the_run_and_never_means_all(self):
        key = self.saved("NoTargets")
        self.c.select_macro(key)
        self.c.set_run_refs([])
        self.assertFalse(self.c.can_run())
        self.assertEqual(self.c.why_cannot_run(), "msg_no_run_targets")
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.category, "no_targets")
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.world.events, [])              # nothing was sent anywhere
        self.assertEqual(self.history.list(), [])

    def test_an_unknown_target_reference_is_not_selected(self):
        self.c.set_run_refs(["nonsense", self.entry("Notes").ref, "nonsense"])
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))

    def test_running_on_one_target_records_a_completed_run(self):
        self.prepare("Notes")
        self.assertTrue(self.c.can_run())
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        record = self.history.list()[0]
        self.assertEqual((record.outcome, record.target_count), (COMPLETED, 1))
        self.assertEqual(record.macro, "Runnable")
        self.assertGreater(record.completed, 0)

    def test_running_on_several_targets_runs_them_one_at_a_time(self):
        self.prepare("Notes", "Calculator")
        self.assertTrue(self.c.start_run(), self.c.status)
        seen_running = 0
        while self.c.run_active:
            running = [r for r in self.c.run_results if r.state is RunState.RUNNING]
            self.assertLessEqual(len(running), 1, "two targets ran at once")
            seen_running = max(seen_running, len(running))
            self.c.poll()
            time.sleep(0.005)
        self.c.poll()
        self.assertEqual(seen_running, 1)
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED, RunState.COMPLETED])
        self.assertEqual(self.history.list()[0].outcome, COMPLETED)
        self.assertEqual(self.history.list()[0].target_count, 2)

    def test_a_target_that_cannot_be_verified_is_refused_and_the_others_still_run(self):
        self.prepare("Notes", "Calculator")
        self.world.windows.pop(101)                                 # Calculator disappears before the run
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        states = {r.title: r.state for r in self.c.run_results}
        self.assertEqual(states["Notes"], RunState.COMPLETED)
        self.assertEqual(states["Calculator"], RunState.REFUSED)
        self.assertEqual(self.history.list()[0].outcome, PARTIAL)

    def test_a_dry_run_sends_nothing_and_is_not_written_to_the_history(self):
        self.prepare("Notes")
        self.assertTrue(self.c.start_run(dry_run=True), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        self.assertEqual(self.world.events, [])
        self.assertEqual(self.history.list(), [])

    def test_stopping_a_run_leaves_the_remaining_targets_untouched(self):
        self.prepare("Notes", "Calculator")
        self.c.start_run()
        self.assertTrue(self.c.cancel())
        self.assertIn("Stopping", self.c.status.text)               # not "stopped" until the engine confirms
        self.run_to_end()
        states = [r.state for r in self.c.run_results]
        self.assertIn(RunState.STOPPED, states)
        self.assertNotIn(RunState.RUNNING, states)
        self.assertIn(self.history.list()[0].outcome, (STOPPED, PARTIAL))

    def test_the_emergency_stop_ends_the_whole_run_instead_of_moving_to_the_next_target(self):
        self.prepare("Notes", "Calculator")
        self.c.start_run()
        self.c.emergency_stop()
        self.run_to_end()
        self.assertFalse(self.c.run_active)
        self.assertNotIn(RunState.COMPLETED, [r.state for r in self.c.run_results][1:])
        self.assertIn(self.history.list()[0].outcome, (STOPPED, FAILED, REFUSED, PARTIAL))

    def test_a_macro_recorded_for_another_provider_is_refused_rather_than_retargeted(self):
        key = self.saved("Foreign")
        info = self.c.macro_by_key(key)
        from app.playback.macro import Macro
        from app.providers.recording import Recording
        alien = Macro(Recording("other-provider", "x-1", self.library.load(info.path).recording.events), "Foreign")
        self.library.save(alien, overwrite=True)
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.REFUSED])
        self.assertEqual(self.history.list()[0].outcome, REFUSED)
        self.assertEqual(self.world.events, [])

    def test_the_run_targets_are_named_for_the_user_not_by_reference(self):
        self.prepare("Notes", "Calculator")
        self.assertEqual(set(self.c.run_target_titles()), {"Notes", "Calculator"})

    def test_nothing_else_can_start_while_a_run_is_active(self):
        self.prepare("Notes", "Calculator")
        self.c.start_run()
        self.assertTrue(self.c.busy)
        self.assertFalse(self.c.start_recording())
        self.assertFalse(self.c.start_run())
        self.run_to_end()
        self.assertFalse(self.c.busy)


class RunTargetsBelongToTheirMacro(Base):
    """Targets are chosen FOR one macro. Choosing another macro must not let it run on them without the person
    confirming them again for that macro -- and an empty choice still never widens into "all"."""

    def two_macros(self):
        first, second = self.saved("First"), self.saved("Second")
        self.c.select_macro(first)
        self.c.set_run_refs([self.entry("Notes").ref, self.entry("Calculator").ref])
        return first, second

    def test_choosing_another_macro_drops_the_targets_and_it_cannot_run_on_them(self):
        first, second = self.two_macros()
        chosen = self.c.run_refs
        self.c.select_macro(second)
        self.assertEqual(self.c.run_refs, ())
        self.assertFalse(self.c.can_run())
        self.assertEqual(self.c.why_cannot_run(), "msg_no_run_targets")
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.category, "no_targets")
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.world.events, [])                       # nothing was sent anywhere
        self.assertEqual(self.c.suggested_run_refs(), chosen)         # offered again, to confirm -- not used

    def test_the_same_macro_chosen_again_keeps_its_own_targets(self):
        first, _ = self.two_macros()
        chosen = self.c.run_refs
        self.c.select_macro(first)
        self.assertEqual(self.c.run_refs, chosen)
        self.assertTrue(self.c.can_run())

    def test_confirming_the_suggestion_for_the_new_macro_runs_exactly_there(self):
        _, second = self.two_macros()
        self.c.select_macro(second)
        self.c.set_run_refs(self.c.suggested_run_refs())              # what the picker returns once confirmed
        refs = self.c.run_refs
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertEqual(self.c.run_macro_key, second)                # the macro shown is the macro that runs
        self.assertEqual(tuple(r.ref for r in self.c.run_results), refs)   # on exactly the targets shown
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED, RunState.COMPLETED])
        self.assertEqual(self.history.list()[0].macro, "Second")

    def test_targets_that_belong_to_another_macro_are_refused_even_if_they_were_kept(self):
        first, second = self.two_macros()
        self.c.selected_key = second                                  # past select_macro(): the defence behind it
        self.assertFalse(self.c.can_run())
        self.assertEqual(self.c.why_cannot_run(), "msg_run_targets_other_macro")
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.key, "msg_run_targets_other_macro")
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.world.events, [])

    def test_renaming_the_macro_keeps_its_targets(self):
        first, _ = self.two_macros()
        chosen = self.c.run_refs
        self.assertTrue(self.c.rename_macro(first, "First renamed"), self.c.status)
        self.assertEqual(self.c.run_refs, chosen)
        self.assertTrue(self.c.can_run())

    def test_a_deleted_macros_targets_do_not_pass_to_a_macro_that_takes_its_name(self):
        """Reviewer finding: the targets are tied to a file key, and a deleted macro's key can be taken again."""
        first, second = self.two_macros()
        name = self.c.macro_by_key(first).name
        self.assertTrue(self.c.delete_macro(first), self.c.status)
        self.assertEqual((self.c.run_refs, self.c.run_refs_key), ((), ""))
        self.assertTrue(self.c.rename_macro(second, name), self.c.status)     # B now lives under A's old name
        reused = self.c.selected_key
        self.assertEqual(reused, first)                                       # really the same file key again
        self.assertFalse(self.c.can_run())
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.world.events, [])

    def test_clearing_the_selection_also_forgets_the_suggestion(self):
        _, second = self.two_macros()
        self.c.select_macro(second)
        self.assertTrue(self.c.suggested_run_refs())
        self.c.reset_view()
        self.assertEqual((self.c.run_refs, self.c.suggested_run_refs()), ((), ()))

    def test_an_explicit_choice_replaces_the_suggestion_even_when_it_is_empty(self):
        _, second = self.two_macros()
        self.c.select_macro(second)
        self.c.set_run_refs([])                                       # "none" chosen on purpose for this macro
        self.assertEqual((self.c.run_refs, self.c.previous_run_refs), ((), ()))
        self.assertFalse(self.c.start_run())

    def test_an_explicit_choice_of_several_targets_still_runs_on_all_of_them_and_no_other(self):
        first, _ = self.two_macros()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual({r.title for r in self.c.run_results}, {"Notes", "Calculator"})
        self.assertEqual(self.c.run_macro_key, first)


if __name__ == "__main__":
    unittest.main()
