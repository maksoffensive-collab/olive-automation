"""The target a macro or a script remembers, found again among the listed targets (the person's list of 2026-10-25,
item 12): exactly one match, or nothing chosen and the person asked. Pure, no window."""
import unittest

from app.gui.services import TargetEntry
from app.gui.target_memory import Remembered, candidates, find, remember, suggestion


def entry(ref, title, process="game.exe", kind="GameWnd", provider="windows", whole=False):
    return TargetEntry(ref, title, process, kind, "800 x 600", provider, "Open Windows", whole_screen=whole)


class FindingItAgain(unittest.TestCase):
    def test_one_alike_window_with_another_title_is_only_offered_never_chosen(self):
        """The independent review of 2026-10-26: the program and kind alone are often no identity."""
        memory = remember(entry("w1", "Game — level 1"))
        listed = [entry("w9", "Game — level 7"), entry("w2", "Notes", "notes.exe", "N")]
        self.assertIsNone(find(memory, listed))
        self.assertEqual(suggestion(memory, listed).ref, "w9")
        self.assertIsNone(suggestion(memory, listed + [entry("w8", "Game — level 2")]))     # two: nothing offered
        self.assertIsNone(suggestion(memory, [entry("w1", "Game — level 1")]))              # found: no offer

    def test_another_emulator_instance_or_another_hosted_app_is_never_taken_for_it(self):
        ld = dict(process="LDPlayer", kind="instance", provider="ldplayer-adb")
        self.assertIsNone(find(remember(entry("ld1", "LDPlayer-1", **ld)), [entry("ld0", "LDPlayer", **ld)]))
        frame = dict(process="ApplicationFrameHost.exe", kind="ApplicationFrameWindow")
        self.assertIsNone(find(remember(entry("w1", "Calculator", **frame)), [entry("w2", "Settings", **frame)]))

    def test_several_alike_are_told_apart_by_the_title_or_not_at_all(self):
        memory = remember(entry("w1", "Account A"))
        two = [entry("w2", "Account A"), entry("w3", "Account B")]
        self.assertEqual(find(memory, two).ref, "w2")
        self.assertIsNone(find(memory, [entry("w2", "Account C"), entry("w3", "Account B")]))   # asked, not guessed
        self.assertIsNone(find(memory, [entry("w2", "Account A"), entry("w3", "Account A")]))
        self.assertEqual([e.ref for e in candidates(memory, two)], ["w2", "w3"])                 # the same title first

    def test_nothing_alike_is_nothing_found(self):
        self.assertIsNone(find(remember(entry("w1", "Game")), [entry("w2", "Game", "other.exe")]))
        self.assertIsNone(find(None, [entry("w2", "Game")]))
        self.assertIsNone(find(remember(entry("w1", "Game")), [entry("w2", "Game", provider="ldplayer-adb")]))

    def test_the_whole_screen_matches_the_whole_screen(self):
        whole = entry("full", "Full Windows", "", "desktop", whole=True)
        memory = remember(whole)
        self.assertEqual(find(memory, [entry("w1", "Game"), whole]).ref, "full")

    def test_it_is_kept_as_plain_data_and_read_back_strictly(self):
        memory = remember(entry("w1", "Game"))
        self.assertEqual(Remembered.from_dict(memory.as_dict()), memory)
        for bad in (None, [], {}, {"provider": ""}, {"provider": "windows", "title": "x" * 500}):
            self.assertIsNone(Remembered.from_dict(bad))
        good = memory.as_dict()
        for change in ({"whole_screen": "false"}, {"whole_screen": 1}, {"process": 5}, {"title": None},
                       {"provider": ""}, {"extra": 1}):
            self.assertIsNone(Remembered.from_dict(dict(good, **change)), change)     # never coerced
        self.assertIsNone(Remembered.from_dict({k: v for k, v in good.items() if k != "kind"}))



# ---- in the application: remembered, found again, never guessed ------------------------------------------------
from app.bootstrap.windows_services import WindowsServices                        # noqa: E402
from app.gui.controller import AppController                                      # noqa: E402
from app.gui.meta import MacroMeta                                                # noqa: E402
from tests import test_gui as gui_tests                                           # noqa: E402
from tests.test_gui import OWN_PID, World                                         # noqa: E402
from tests.test_windows_provider import info                                      # noqa: E402


class InTheApplication(gui_tests.Base):
    def setUp(self):
        super().setUp()
        self.meta = MacroMeta(None)
        self.c = AppController(self.services, self.library, meta=self.meta)

    def restarted(self, *windows):
        """Olive and the program started again: other window handles and processes, the same program and title."""
        self.world_now = World(*windows)
        services = WindowsServices(self.world_now, lambda: self.observer, self.stop, own_pid=OWN_PID)
        c = AppController(services, self.library, meta=self.meta)
        c.refresh()
        return c

    def recorded_on_notes(self, name="Farm"):
        self.record()
        self.assertTrue(self.c.save(name), self.c.status)
        return self.library.key_of(self.c.macro_path)

    def test_a_recording_remembers_its_target_and_finds_it_after_a_restart(self):
        key = self.recorded_on_notes()
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ("w500",))
        self.assertTrue(c.can_run())

    def test_several_alike_windows_are_told_apart_by_the_title_or_the_person_is_asked(self):
        key = self.recorded_on_notes()
        c = self.restarted(info(hwnd=500, title="Other", name="notes.exe", cls="NotesWnd", pid=77, start=9),
                           info(hwnd=501, title="Notes", name="notes.exe", cls="NotesWnd", pid=78, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ("w501",))
        c = self.restarted(info(hwnd=500, title="One", name="notes.exe", cls="NotesWnd", pid=77, start=9),
                           info(hwnd=501, title="Two", name="notes.exe", cls="NotesWnd", pid=78, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())                                    # not told apart: asked, not guessed
        self.assertEqual(c.why_cannot_run(), "msg_no_run_targets")

    def test_a_file_changed_outside_olive_does_not_inherit_the_remembered_target(self):
        key = self.recorded_on_notes()
        path = self.library.directory / key
        path.write_text(path.read_text(encoding="utf-8").replace('"name": "Farm"', '"name": "Farm"', 1) + " ",
                        encoding="utf-8")                                   # rewritten by another program
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())

    def test_a_script_opened_in_the_editor_chooses_the_target_it_remembers(self):
        from app.gui import graph_edit as ge
        self.pick("Notes")
        self.assertTrue(self.c.new_script("Daily"))
        self.c.editor_apply_graph(ge.add_node, "wait", (0, 100))
        self.assertTrue(self.c.save_editor(), self.c.status)               # made on Notes: remembered
        key = self.c.editor_key
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9),
                           info(hwnd=101, title="Calculator", name="calc.exe", cls="CalcWnd", pid=11))
        c.refresh_macros()
        self.assertIsNone(c.target)
        self.assertTrue(c.open_editor(key))
        self.assertEqual(c.selected.ref, "w500")
        self.assertTrue(c.target_ready)

    def test_a_window_opened_after_the_macro_was_chosen_is_found_on_the_next_refresh(self):
        """The program was not running yet when the macro was chosen: started, then Refresh -- its target is there."""
        key = self.recorded_on_notes()
        c = self.restarted(info(hwnd=101, title="Calculator", name="calc.exe", cls="CalcWnd", pid=11))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())
        notes = info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9)
        self.world_now.windows[500] = notes                                 # the program is started now
        self.assertTrue(c.refresh())
        self.assertEqual(c.run_refs, ("w500",))
        self.assertTrue(c.can_run())

    def test_the_program_started_again_is_found_again_without_asking_to_choose(self):
        key = self.recorded_on_notes()
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ("w500",))
        del self.world_now.windows[500]                                    # closed ...
        self.world_now.windows[600] = info(hwnd=600, title="Notes", name="notes.exe", cls="NotesWnd", pid=88,
                                           start=12)                       # ... and started again
        self.assertTrue(c.refresh())
        self.assertEqual(c.run_refs, ("w600",))
        self.assertEqual(c.status.key, "msg_found")                       # not "dropped: choose them again"

    def test_unticking_every_target_forgets_the_remembered_one_too(self):
        """An empty choice made on purpose is the person's answer: the macro does not bring its target back."""
        key = self.recorded_on_notes()
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))
        self.assertTrue(self.c.set_run_refs([]))
        self.c.select_macro("")
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, ())
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.run_refs, ())
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())

    # ---- the independent review of 2026-10-26 ----------------------------------------------------------------
    def test_a_window_that_only_looks_like_it_is_offered_pre_ticked_not_chosen(self):
        key = self.recorded_on_notes()
        c = self.restarted(info(hwnd=500, title="Notes — draft", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())
        self.assertFalse(c.can_run())
        self.assertEqual(c.suggested_run_refs(), ("w500",))               # the picker opens with it ticked

    def test_two_remembered_targets_that_come_out_as_one_choose_nothing(self):
        key = self.recorded_on_notes()
        data = self.meta.target(key)
        self.meta.set_target(key, dict(data, targets=data["targets"] * 2))
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(key)
        self.assertEqual(c.run_refs, ())

    def test_a_renamed_macro_still_finds_its_target_after_a_restart(self):
        key = self.recorded_on_notes()
        self.assertTrue(self.c.rename_macro(key, "Farm renamed"), self.c.status)
        renamed = self.c.selected_key
        self.assertNotEqual(renamed, key)
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9))
        c.refresh_macros()
        c.select_macro(renamed)
        self.assertEqual(c.run_refs, ("w500",))

    def saved_script_on_notes(self):
        from app.gui import graph_edit as ge
        self.pick("Notes")
        self.assertTrue(self.c.new_script("Daily"))
        self.c.editor_apply_graph(ge.add_node, "wait", (0, 100))
        self.assertTrue(self.c.save_editor(), self.c.status)
        key = self.c.editor_key
        c = self.restarted(info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=77, start=9),
                           info(hwnd=101, title="Calculator", name="calc.exe", cls="CalcWnd", pid=11))
        c.refresh_macros()
        self.assertTrue(c.select("w101"))                                   # the person had chosen Calculator
        return key, c

    def test_the_editor_says_it_works_on_the_scripts_own_target(self):
        key, c = self.saved_script_on_notes()
        self.assertTrue(c.open_editor(key))
        self.assertEqual((c.selected.ref, c.status.key), ("w500", "msg_editing_on_target"))   # said, not silent

    def test_a_scripts_target_that_does_not_verify_is_said_and_the_earlier_choice_kept(self):
        key, c = self.saved_script_on_notes()
        self.world_now.windows[500] = info(hwnd=500, title="Notes", name="notes.exe", cls="NotesWnd", pid=99,
                                           start=40)                       # another process now, the list is old
        self.assertTrue(c.open_editor(key))
        self.assertEqual(c.status.key, "msg_editing_target_unverified")
        self.assertEqual(c.selected.ref, "w101")
        self.assertTrue(c.target_ready)


class TheMetadataFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "macro_meta.json"

    def test_a_file_that_cannot_be_read_is_never_written_over(self):
        """The independent review of 2026-10-26: one failed read must not wipe every star, note and memory."""
        broken = '{"farm.macro.json": {"favorite": true}'                   # cut short
        self.path.write_text(broken, encoding="utf-8")
        meta = MacroMeta(self.path)
        meta.set_target("farm.macro.json", {"targets": [], "file": None})
        meta.set_favorite("daily.macro.json", True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)
        self.assertTrue(meta.is_favorite("daily.macro.json"))                # kept in memory for this session

    def test_it_is_written_whole_through_a_temporary_file(self):
        meta = MacroMeta(self.path)
        meta.set_favorite("farm.macro.json", True)
        self.assertTrue(MacroMeta(self.path).is_favorite("farm.macro.json"))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["macro_meta.json"])   # no leftover


if __name__ == "__main__":
    unittest.main()
