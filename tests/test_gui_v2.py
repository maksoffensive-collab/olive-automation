"""Macro v2 in the GUI: library, conversion, editing, pickers, runs and history -- over the real layers.

The library works on real files in a temporary folder. The controller runs over the real `LDPlayerServices` ->
`MappingService` -> `create_ldplayer_playback` -> `InputSession` chain with only the outermost `AdbClient` faked
(the same wiretap `tests/test_parallel_run.py` uses), extended with a screen per device serial -- so a tap is
checked where it is actually addressed, with its serial, and a screenshot is the real screenshot path's.
The view tests build the real Tk screens and dialogs but never show them.
"""
import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core.cancel import EmergencyStop
from app.decide import (MAX_NESTING, Condition, ConditionalMacro, Decision, RunMacro, TapMatch, WaitUntil,
                        migrate_v1)
from app.decide.format import asset_dir_for, asset_name
from app.gui import flow_edit as fe
from app.gui import region as geo
from app.gui.controller import AppController, RunState, Screen
from app.gui.history import COMPLETED, FAILED, REFUSED, STOPPED, RunHistory
from app.gui.library import LibraryError, MacroExists, MacroLibrary
from app.gui.macro_edit import EditError
from app.observe import TextLine, TextLinesQuery
from app.playback.macro import ActionOp, Macro
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.vision.bitmap import Bitmap, encode_png
from app.vision.roi import Roi
from tests.test_ldplayer_services import FakeConsole, port_owners_for, read_config_for
from tests.test_parallel_run import WiretapAdb, ld_world

WHITE, RED, BLUE = (255, 255, 255), (220, 20, 20), (20, 20, 220)


def pattern(bitmap: Bitmap, x: int, y: int) -> Bitmap:
    """A small, distinctive mark (not a flat square: a template must be locatable)."""
    return bitmap.with_rect(x, y, 8, 8, RED).with_rect(x + 2, y + 2, 4, 4, BLUE).with_rect(x + 3, y + 3, 1, 1, WHITE)


TEMPLATE = pattern(Bitmap.solid(8, 8, WHITE), 0, 0)
SPOTS = {1: (30, 20), 2: (140, 60)}                 # where each instance shows the mark
AREA = Roi(0, 0, 200, 100)                          # where the conditions look: keeps matching quick


def scene(index: int) -> Bitmap:
    """A device-sized screen (the real screenshot path checks the size the device reports)."""
    return pattern(Bitmap.solid(1280, 720, WHITE), *SPOTS[index])


def mark(**kwargs) -> Condition:
    return Condition.template(TEMPLATE, region=AREA, **kwargs)


def recorded(provider="fake", target="t-1", name="rec", x=11, y=12) -> Macro:
    return Macro(Recording(provider, target, (E(K.BUTTON_DOWN, 0, x, y, B.PRIMARY, 1),
                                              E(K.BUTTON_UP, 40, x, y, B.PRIMARY, 1))), name)


def flow(name="Flow", template=TEMPLATE, *, then=None) -> ConditionalMacro:
    found = Condition.template(template, region=AREA, label="the mark")
    return ConditionalMacro(name, (WaitUntil(found, 5, 0.05), Decision(found, then=then or TapMatch())))


# ---- the library ------------------------------------------------------------------------------------------
class LibraryV2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "macros"
        self.lib = MacroLibrary(self.root)

    def save_both(self):
        v1 = self.lib.save(recorded(name="Old one"))
        v2 = self.lib.save(flow("New one"))
        return v1, v2

    def test_v1_and_v2_are_both_listed_and_told_apart(self):
        self.save_both()
        by_name = {i.name: i for i in self.lib.list()}
        self.assertEqual((by_name["Old one"].version, by_name["Old one"].actions), (1, 1))
        self.assertEqual((by_name["New one"].version, by_name["New one"].actions), (2, 2))
        self.assertTrue(by_name["New one"].is_v2 and by_name["New one"].valid)
        self.assertEqual(by_name["New one"].provider_id, "")                  # a v2 macro is bound to no target

    def test_a_template_folder_is_part_of_its_macro_not_a_folder_of_macros(self):
        _, path = self.save_both()
        self.assertTrue(asset_dir_for(path).is_dir())
        self.lib.create_folder("Daily")
        self.assertEqual(self.lib.folders(), ["Daily"])
        with self.assertRaises(LibraryError) as caught:
            self.lib.create_folder("Tricky.assets")
        self.assertEqual(caught.exception.key, "err_folder_reserved")

    def test_listing_opens_no_template(self):
        _, path = self.save_both()
        asset = next(asset_dir_for(path).iterdir())
        asset.write_bytes(encode_png(Bitmap.solid(8, 8, WHITE)))     # pixels no longer match the name
        info = next(i for i in self.lib.list() if i.name == "New one")
        self.assertTrue(info.valid, info.error)                      # listed from its text alone
        with self.assertRaises(LibraryError) as caught:
            self.lib.load(path)                                      # opening reads and hash-checks it
        self.assertIn("does not contain the image", str(caught.exception))

    def test_a_missing_template_is_an_error_when_opened(self):
        _, path = self.save_both()
        for child in asset_dir_for(path).iterdir():
            child.unlink()
        with self.assertRaises(LibraryError) as caught:
            self.lib.load(path)
        self.assertIn("missing", str(caught.exception))

    def test_an_unsafe_template_name_is_invalid_even_in_the_list(self):
        _, path = self.save_both()
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(asset_name(TEMPLATE), "../escape.png"), encoding="utf-8")
        info = next(i for i in self.lib.list() if i.key == self.lib.key_of(path))
        self.assertFalse(info.valid)
        self.assertIn("not allowed", info.error)

    def test_rename_takes_the_templates_along(self):
        _, path = self.save_both()
        new = self.lib.rename(path, "Renamed")
        self.assertFalse(path.exists() or asset_dir_for(path).exists())
        self.assertTrue(asset_dir_for(new).is_dir())
        self.assertEqual(self.lib.load(new), dataclasses.replace(flow("Renamed")))

    def test_move_takes_the_templates_along(self):
        _, path = self.save_both()
        self.lib.create_folder("Daily")
        new = self.lib.move(path, "Daily")
        self.assertEqual(new.parent.name, "Daily")
        self.assertFalse(asset_dir_for(path).exists())
        self.assertEqual(self.lib.load(new), flow("New one"))
        back = self.lib.move(new, "")
        self.assertEqual(self.lib.load(back), flow("New one"))

    def test_delete_removes_the_templates_too(self):
        _, path = self.save_both()
        self.lib.delete(path)
        self.assertFalse(path.exists())
        self.assertFalse(asset_dir_for(path).exists())
        self.assertEqual([p.name for p in self.root.iterdir()], ["old_one.macro.json"])

    def test_a_file_that_is_not_a_template_blocks_every_operation_and_nothing_changes(self):
        _, path = self.save_both()
        stray = asset_dir_for(path) / "notes.txt"
        stray.write_text("mine", encoding="utf-8")
        self.lib.create_folder("Daily")
        for operation in (lambda: self.lib.delete(path), lambda: self.lib.move(path, "Daily"),
                          lambda: self.lib.rename(path, "Other")):
            with self.assertRaises(LibraryError) as caught:
                operation()
            self.assertEqual(caught.exception.key, "err_assets_foreign_file")
        self.assertTrue(path.exists() and stray.read_text(encoding="utf-8") == "mine")
        self.assertEqual(self.lib.load(path), flow("New one"))

    def test_a_move_onto_an_existing_template_folder_is_refused(self):
        _, path = self.save_both()
        self.lib.create_folder("Daily")
        (self.root / "Daily" / "new_one.assets").mkdir()
        with self.assertRaises(LibraryError) as caught:
            self.lib.move(path, "Daily")
        self.assertEqual(caught.exception.key, "err_assets_exist_there")
        self.assertTrue(path.exists() and asset_dir_for(path).is_dir())

    def test_a_delete_that_cannot_remove_the_file_puts_the_templates_back(self):
        _, path = self.save_both()
        before = sorted(p.name for p in asset_dir_for(path).iterdir())
        real_unlink = Path.unlink

        def unlink(self_, *a, **k):
            if self_.name == path.name:
                raise PermissionError(13, "in use")
            return real_unlink(self_, *a, **k)

        with mock.patch.object(Path, "unlink", unlink), self.assertRaises(LibraryError):
            self.lib.delete(path)
        self.assertTrue(path.exists())
        self.assertEqual(sorted(p.name for p in asset_dir_for(path).iterdir()), before)
        self.assertEqual(self.lib.folders(), [])                          # no parked folder left in sight

    def test_a_move_that_fails_half_way_leaves_both_where_they_were(self):
        _, path = self.save_both()
        self.lib.create_folder("Daily")
        with mock.patch("app.gui.library.os.link", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(LibraryError):
            self.lib.move(path, "Daily")
        self.assertTrue(path.exists() and asset_dir_for(path).is_dir())
        self.assertFalse((self.root / "Daily" / "new_one.assets").exists())
        self.assertEqual(self.lib.load(path), flow("New one"))

    def test_saving_validates_first_and_a_refused_save_keeps_the_previous_version(self):
        _, path = self.save_both()
        before = path.read_bytes()
        broken = ConditionalMacro("New one", (WaitUntil(Condition.text("x"), 5, 0.01),))   # below the floor
        with self.assertRaises(LibraryError) as caught:
            self.lib.save(broken, overwrite=True)
        self.assertIn("0.05", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_saving_keeps_only_the_templates_the_macro_still_uses(self):
        _, path = self.save_both()
        other = pattern(Bitmap.solid(10, 10, WHITE), 1, 1)
        self.lib.save(flow("New one", other), overwrite=True)
        self.assertEqual([p.name for p in asset_dir_for(path).iterdir()], [asset_name(other)])
        self.lib.save(ConditionalMacro("New one", (RunMacro(recorded()),)), overwrite=True)
        self.assertFalse(asset_dir_for(path).exists())

    def test_a_taken_name_is_refused_never_overwritten(self):
        _, path = self.save_both()
        before = path.read_bytes()
        with self.assertRaises(MacroExists):
            self.lib.save(flow("New one", pattern(Bitmap.solid(10, 10, WHITE), 1, 1)))
        self.assertEqual(path.read_bytes(), before)

    def test_v1_is_listed_loaded_and_saved_exactly_as_before(self):
        v1, _ = self.save_both()
        before = v1.read_bytes()
        macro = self.lib.load(v1)
        self.assertIsInstance(macro, Macro)
        self.lib.save(macro, overwrite=True)
        self.assertEqual(v1.read_bytes(), before)
        self.assertFalse(asset_dir_for(v1).exists())


# ---- editing ---------------------------------------------------------------------------------------------
class FlowEditing(unittest.TestCase):
    def form(self, kind, **fields):
        base = fe.condition_form(None)
        base.kind, base.fields = kind, {**{"text": "", "match": "contains", "ignore_case": "1", "language": "",
                                           "min_confidence": "0", "max_results": "1"}, **fields}
        return base

    def test_each_kind_is_built_by_its_own_query(self):
        pixel = fe.build_condition(self.form(fe.PIXEL, x="3", y="4", color="#ff0000", tolerance="5"))
        self.assertEqual((pixel.query.point, pixel.query.color, pixel.query.tolerance), ((3, 4), (255, 0, 0), 5))
        color = self.form(fe.COLOR, color="1, 2, 3", tolerance="0", min_ratio="0,25")
        color.region = Roi(1, 2, 3, 4)
        built = fe.build_condition(color)
        self.assertEqual((built.query.min_ratio, built.query.region), (0.25, Roi(1, 2, 3, 4)))
        template = self.form(fe.TEMPLATE, min_confidence="0.9", max_results="2")
        template.template = TEMPLATE
        self.assertEqual(fe.build_condition(template).query.template, TEMPLATE)
        # text: lines, each "is there" or "is not there", looked for anywhere in the area, ignoring case;
        # blank lines are left out (the old single text with its match choice: tests.test_text_lines)
        text = self.form(fe.TEXT, language="en")
        text.lines = [("Collect", True), ("  ", True), (" Error ", False)]
        built = fe.build_condition(text)
        self.assertEqual(built.query, TextLinesQuery((TextLine("Collect"), TextLine("Error", False)), True, None,
                                                     "en", 0.0, True))

    def test_an_existing_condition_comes_back_into_the_same_fields(self):
        for condition in (Condition.pixel((1, 2), (3, 4, 5), tolerance=6, expected=False, label="p"),
                          Condition.color((9, 9, 9), region=Roi(0, 0, 5, 5), min_ratio=0.3),
                          Condition.template(TEMPLATE, region=Roi(1, 1, 50, 50), min_confidence=0.9),
                          Condition.text("Hi", match="exact", region=Roi(2, 2, 9, 9), language="ru")):
            self.assertEqual(fe.build_condition(fe.condition_form(condition)), condition)

    def test_refusals_come_from_the_backend_in_its_own_words(self):
        cases = [
            (lambda: fe.build_condition(self.form(fe.PIXEL, x="a", y="1", color="0,0,0")), "err_flow_whole"),
            (lambda: fe.build_condition(self.form(fe.PIXEL, x="1", y="1", color="0,0,0", tolerance="300")),
             "err_flow_rejected"),
            (lambda: fe.build_condition(self.form(fe.PIXEL, x="1", y="1", color="red")), "err_flow_color"),
            (lambda: fe.build_condition(self.form(fe.TEMPLATE)), "err_flow_no_template"),
            (lambda: fe.build_wait(Condition.text("x"), "0", "0.5"), "err_flow_rejected"),
            (lambda: fe.build_wait(Condition.text("x"), "601", "0.5"), "err_flow_rejected"),
            (lambda: fe.build_wait(Condition.text("x"), "5", "0.01"), "err_flow_rejected"),       # the file's floor
            (lambda: fe.build_wait(Condition.text("x" * 300), "5", "0.5"), "err_flow_rejected"),  # the file's limit
            (lambda: fe.build_wait(Condition.text("x", language="e n"), "5", "0.5"), "err_flow_rejected"),
            (lambda: fe.build_wait(Condition.text("x"), "nan", "0.5"), "err_flow_number"),
            (lambda: fe.build_decision(Condition.text("x", expected=False), TapMatch(), None), "err_flow_rejected"),
            (lambda: fe.build_decision(Condition.text("x"), (RunMacro(recorded()), TapMatch()), None),
             "err_flow_rejected"),                                                  # a click only ever comes first
            (lambda: fe.build_branch(fe.TAP, dx="99999", dy="0"), "err_flow_rejected"),
            (lambda: fe.build_run(None), "err_flow_no_macro"),
        ]
        for build, key in cases:
            with self.assertRaises(EditError) as caught:
                build()
            self.assertEqual(caught.exception.key, key, caught.exception)

    def test_branches_hold_blocks_and_ifs_nest_up_to_the_limit(self):
        """Since 2026-10-19 a branch is a list of blocks, an IF among them (before, nesting was refused)."""
        inner = Decision(Condition.text("y"), then=TapMatch())
        built = fe.build_decision(Condition.text("x"), (inner, RunMacro(recorded())), None)
        self.assertEqual(built.then[0], inner)
        self.assertEqual(fe.build_decision(Condition.text("x"), None, None).then, ())   # empty: it only looks
        deep = Decision(Condition.text("d"), then=TapMatch())
        for _ in range(MAX_NESTING - 1):
            deep = Decision(Condition.text("d"), then=(deep,))
        with self.assertRaises(EditError):
            fe.build_decision(Condition.text("x"), (deep,), None)                   # one level too deep

    def test_a_session_edits_undoes_redoes_and_knows_what_is_unsaved(self):
        session = fe.FlowSession(flow())
        wait = fe.build_wait(Condition.text("Ready"), "10", "0.5")
        session.apply(fe.insert_step, 0, wait)
        self.assertTrue(session.dirty)
        self.assertEqual(session.document.steps[0], wait)
        session.apply(fe.duplicate_step, 0)
        session.apply(fe.move_step, 2, 1)
        self.assertEqual(len(session.document.steps), 4)
        session.apply(fe.delete_step, 0)
        session.apply(fe.replace_step, 0, fe.build_run(recorded()))
        self.assertIsInstance(session.document.steps[0], RunMacro)
        for _ in range(5):
            self.assertTrue(session.undo())
        self.assertEqual(session.document, flow())
        self.assertFalse(session.dirty)
        self.assertTrue(session.redo())
        self.assertTrue(session.dirty)

    def test_a_refused_edit_changes_nothing(self):
        session = fe.FlowSession(ConditionalMacro("One", (RunMacro(recorded()),)))
        with self.assertRaises(EditError) as caught:
            session.apply(fe.delete_step, 0)
        self.assertEqual(caught.exception.key, "err_flow_last_step")
        with self.assertRaises(EditError):
            session.apply(fe.move_step, 0, 1)
        self.assertFalse(session.can_undo or session.dirty)

    def test_there_is_room_for_exactly_the_formats_number_of_steps(self):
        session = fe.FlowSession(ConditionalMacro("Many", (RunMacro(recorded()),) * 100))
        with self.assertRaises(EditError) as caught:
            session.apply(fe.duplicate_step, 0)
        self.assertEqual(caught.exception.key, "err_flow_too_many")


class PickerGeometry(unittest.TestCase):
    def test_the_factor_is_the_smallest_whole_one_that_fits(self):
        self.assertEqual(geo.display_factor(1280, 720, 880, 480), 2)
        self.assertEqual(geo.display_factor(1920, 1080, 880, 480), 3)
        self.assertEqual(geo.display_factor(640, 360, 880, 480), 1)

    def test_a_drag_in_either_direction_is_the_same_region_in_screenshot_pixels(self):
        roi = Roi(100, 60, 50, 40)
        self.assertEqual(geo.to_image_roi(50, 30, 75, 50, 2, 1280, 720), roi)
        self.assertEqual(geo.to_image_roi(75, 50, 50, 30, 2, 1280, 720), roi)
        self.assertEqual(geo.to_shown_rect(roi, 2), (50, 30, 75, 50))

    def test_marks_outside_the_image_are_clamped_and_a_click_is_no_region(self):
        self.assertEqual(geo.to_image_roi(-20, -20, 9999, 9999, 3, 1920, 1080), Roi(0, 0, 1920, 1080))
        self.assertEqual(geo.to_image_roi(639, 359, 700, 700, 3, 1919, 1079), Roi(1917, 1077, 2, 2))
        self.assertIsNone(geo.to_image_roi(10, 10, 10, 40, 2, 1280, 720))
        self.assertEqual(geo.to_image_point(639, 359, 2, 1280, 720), (1278, 718))
        self.assertEqual(geo.to_image_point(5000, -3, 2, 1280, 720), (1279, 0))

    def test_a_search_area_around_a_template_stays_inside_the_image(self):
        self.assertEqual(geo.expand(Roi(5, 5, 10, 10), 20, 100, 100), Roi(0, 0, 35, 35))


# ---- the controller over the real device chain ------------------------------------------------------------
class ScreenAdb(WiretapAdb):
    """The wiretap, plus a screen for every device serial and a count of every screenshot taken."""

    def __init__(self, world, screens):
        super().__init__(world, delay_s=0.0)
        self.screens = screens
        self.screencaps: list[str] = []
        self._shots = threading.Lock()

    def screencap_png(self, serial, *, grant):
        with self._shots:
            self.screencaps.append(serial)
        return self.screens[serial]


SCENES = {i: encode_png(scene(i)) for i in SPOTS}


class ControllerV2(unittest.TestCase):
    instances = 2

    def setUp(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.world = ld_world(self.instances)
        self.adb = ScreenAdb(self.world, {self.serial(i): SCENES[i] for i in range(1, self.instances + 1)})
        self.stop = EmergencyStop()
        self.services = LDPlayerServices(FakeConsole(self.world), self.adb, read_config_for(self.world),
                                         port_owners_for(self.world), self.stop)
        self.library = MacroLibrary(root / "macros")
        self.history = RunHistory(root / "history.json")
        self.c = AppController(self.services, self.library, history=self.history)
        self.addCleanup(self.c.shutdown)
        self.c.refresh()

    # ---- helpers ----------------------------------------------------------------------------------
    @staticmethod
    def serial(index):
        return f"emulator-{5552 + 2 * index}"

    def entry(self, index):
        return next(e for e in self.c.entries if e.title.endswith(f"-{index}"))

    def target(self, index):
        return self.services.verify(self.entry(index))

    def put(self, macro) -> str:
        path = self.library.save(macro)
        self.c.refresh_macros()
        key = self.library.key_of(path)
        self.c.select_macro(key)
        return key

    def run_to_end(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        while self.c.run_active and time.monotonic() < deadline:
            self.c.poll()
            time.sleep(0.005)
        self.c.poll()
        self.assertFalse(self.c.run_active, self.c.run_results)

    def taps(self):
        return [(serial, args) for serial, op, args in self.adb.log if op == "tap"]

    # ---- library workflow ----------------------------------------------------------------------------
    def test_convert_makes_a_new_v2_macro_and_leaves_the_original_byte_for_byte(self):
        key = self.put(recorded(self.target(1).provider_id, self.target(1).target_id, "Morning"))
        original = self.c.macro_by_key(key).path
        before = original.read_bytes()
        self.assertTrue(self.c.convert_to_v2(key, "Morning v2"), self.c.status)
        self.assertEqual(original.read_bytes(), before)
        info = self.c.selected_macro()
        self.assertEqual((info.name, info.version, info.actions), ("Morning v2", 2, 1))
        self.assertEqual(self.library.load(info.path),
                         migrate_v1(Macro(self.library.load(original).recording, "Morning v2")))

    def test_converting_to_a_taken_name_is_refused_and_nothing_is_overwritten(self):
        key = self.put(recorded(name="Morning"))
        self.put(flow("Taken"))
        taken = self.c.macro_by_key(self.library.key_of(self.library.path_for("Taken")))
        before = taken.path.read_bytes()
        self.assertFalse(self.c.convert_to_v2(key, "Taken"))
        self.assertEqual(self.c.status.category, "exists")
        self.assertEqual(taken.path.read_bytes(), before)
        self.assertFalse(self.c.convert_to_v2(self.library.key_of(taken.path), "Again"))    # already v2

    def test_open_edit_save_and_reopen(self):
        key = self.put(flow())
        path = self.c.macro_by_key(key).path
        self.assertTrue(self.c.open_editor(key))
        self.assertTrue(self.c.editing_v2)
        self.assertTrue(self.c.save_editor())                            # nothing changed: same bytes
        before = path.read_bytes()
        self.assertTrue(self.c.save_editor())
        self.assertEqual(path.read_bytes(), before)
        wait = fe.build_wait(Condition.text("Ready", label="ready"), "7", "0.25")
        self.assertTrue(self.c.editor_apply(fe.replace_step, 0, wait))
        self.assertTrue(self.c.editor_dirty)
        self.assertTrue(self.c.editor_undo() and not self.c.editor_dirty)
        self.assertTrue(self.c.editor_redo() and self.c.editor_dirty)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertFalse(self.c.editor_dirty)
        self.c.close_editor()
        self.assertTrue(self.c.open_editor(key))
        self.assertEqual(self.c.editor.document.steps[0], wait)
        self.assertEqual(self.c.editor.document.steps[1], flow().steps[1])

    def test_a_refused_edit_says_why_and_changes_nothing(self):
        key = self.put(flow())
        self.c.open_editor(key)
        self.assertFalse(self.c.editor_apply(fe.delete_step, 5))
        self.assertEqual((self.c.status.level, self.c.status.key), ("error", "err_step_gone"))
        self.assertFalse(self.c.editor_dirty)

    def test_a_macro_whose_template_was_tampered_with_cannot_be_opened_or_run(self):
        key = self.put(flow())
        path = self.c.macro_by_key(key).path
        next(asset_dir_for(path).iterdir()).write_bytes(encode_png(Bitmap.solid(8, 8, WHITE)))
        self.assertFalse(self.c.open_editor(key))
        self.assertIn("does not contain the image", self.c.status.text)
        self.c.set_run_refs([self.entry(1).ref])
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.taps(), [])

    # ---- screens for the pickers --------------------------------------------------------------------
    def test_a_template_is_cut_from_the_chosen_targets_screenshot_and_saved_as_a_hashed_asset(self):
        self.assertTrue(self.c.select(self.entry(2).ref))
        job = self.c.begin_screen()
        screen = self.c.finish_screen(job(), job)                  # the job runs off the UI thread in the app
        self.assertIsInstance(screen, Screen)
        self.assertEqual((screen.width, screen.height, screen.target_id), (1280, 720, self.c.target.target_id))
        x, y = SPOTS[2]
        roi = geo.to_image_roi(x, y, x + 8, y + 8, 1, screen.width, screen.height)
        form = fe.condition_form(None)
        form.kind, form.template = fe.TEMPLATE, screen.frame.bitmap.crop(roi.x, roi.y, roi.width, roi.height)
        form.fields = {"min_confidence": "0.95", "max_results": "1"}
        condition = fe.build_condition(form)
        self.assertEqual(condition.query.template, TEMPLATE)
        key = self.put(ConditionalMacro("Captured", (WaitUntil(condition, 5, 0.05),)))
        path = self.c.macro_by_key(key).path
        self.assertEqual([p.name for p in asset_dir_for(path).iterdir()], [asset_name(TEMPLATE)])
        self.assertEqual(self.library.load(path).steps[0].condition, condition)

    def test_a_screenshot_for_a_target_that_is_no_longer_chosen_is_not_used(self):
        self.c.select(self.entry(1).ref)
        job = self.c.begin_screen()
        result = job()
        self.c.select(self.entry(2).ref)
        self.assertIsNone(self.c.finish_screen(result, job))
        self.assertEqual(self.c.status.key, "msg_screen_discarded")

    def test_no_screenshot_without_an_explicitly_chosen_target(self):
        self.assertIsNone(self.c.begin_screen())
        self.assertEqual(self.c.status.key, "msg_verify_first")
        self.assertEqual(self.adb.screencaps, [])

    def test_testing_a_condition_only_looks(self):
        self.c.select(self.entry(1).ref)
        job = self.c.begin_probe(mark())
        result = self.c.finish_probe(job(), job)
        self.assertTrue(result.satisfied)
        self.assertEqual((result.center.x, result.center.y), (SPOTS[1][0] + 4, SPOTS[1][1] + 4))
        self.assertEqual(self.taps(), [])
        self.assertEqual(self.adb.screencaps, [self.serial(1)])

    # ---- runs ---------------------------------------------------------------------------------------
    def test_both_targets_run_at_once_and_each_taps_its_own_match_on_its_own_serial(self):
        self.put(flow())
        self.c.set_run_refs([self.entry(1).ref, self.entry(2).ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertEqual({r.state for r in self.c.run_results}, {RunState.RUNNING})   # overlapping, not queued
        self.run_to_end()
        self.assertEqual({r.state for r in self.c.run_results}, {RunState.COMPLETED})
        self.assertEqual(sorted(self.taps()), sorted([(self.serial(i), (SPOTS[i][0] + 4, SPOTS[i][1] + 4))
                                                      for i in (1, 2)]))
        record = self.history.list()[0]
        self.assertEqual((record.macro, record.outcome, record.actions_total, record.completed),
                         ("Flow", COMPLETED, 4, 4))
        self.assertEqual({(r.outcome, r.completed, r.total) for r in record.results}, {("completed", 2, 2)})
        self.assertGreater(record.duration_s, 0.0)

    def test_the_emergency_stop_ends_every_target_and_nothing_is_sent(self):
        self.put(ConditionalMacro("Waits", (WaitUntil(mark(expected=False), 20, 0.05),   # never becomes true
                                             Decision(mark(), then=TapMatch()))))
        self.c.set_run_refs([self.entry(1).ref, self.entry(2).ref])
        self.assertTrue(self.c.start_run())
        time.sleep(0.2)
        started = time.monotonic()
        self.c.emergency_stop()
        self.run_to_end(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({r.state for r in self.c.run_results}, {RunState.STOPPED})
        self.assertEqual({r.detail for r in self.c.run_results}, {"emergency_stop"})
        self.assertEqual(self.taps(), [])
        self.assertEqual(self.history.list()[0].outcome, STOPPED)
        self.assertTrue(self.c.clear_emergency_stop())

    def test_stopping_the_run_cancels_every_target(self):
        self.put(ConditionalMacro("Waits", (WaitUntil(mark(expected=False), 20, 0.05),)))
        self.c.set_run_refs([self.entry(1).ref, self.entry(2).ref])
        self.c.start_run()
        time.sleep(0.1)
        self.c.cancel()
        self.run_to_end(10)
        self.assertEqual({(r.state, r.detail) for r in self.c.run_results}, {(RunState.STOPPED, "cancelled")})

    def test_a_click_the_target_does_not_declare_is_refused_before_anything_is_looked_at(self):
        self.put(flow(then=TapMatch(ActionOp.DOUBLE_TAP)))
        self.c.set_run_refs([self.entry(1).ref])
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(self.c.run_results[0].state, RunState.REFUSED)
        self.assertIn("double_tap", self.c.run_results[0].detail)
        self.assertEqual((self.adb.screencaps, self.taps()), ([], []))
        self.assertEqual(self.history.list()[0].outcome, REFUSED)

    def test_a_recorded_step_of_another_provider_is_refused(self):
        self.put(ConditionalMacro("Other", (RunMacro(recorded("windows", "win-1")),)))
        self.c.set_run_refs([self.entry(1).ref])
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(self.c.run_results[0].state, RunState.REFUSED)
        self.assertIn("wrong_provider", self.c.run_results[0].detail)
        self.assertEqual(self.taps(), [])

    def test_binding_a_recorded_step_to_another_target_is_the_runs_choice_not_the_files(self):
        first = self.target(1)
        self.put(ConditionalMacro("Recorded on 1", (RunMacro(recorded(first.provider_id, first.target_id,
                                                                       x=5, y=6)),)))
        text = self.c.selected_macro().path.read_text(encoding="utf-8")
        self.assertNotIn("allow_different_target", text)
        self.c.set_run_refs([self.entry(2).ref])                        # chosen explicitly: that is the binding
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(self.c.run_results[0].state, RunState.COMPLETED, self.c.run_results[0].detail)
        self.assertEqual(self.taps(), [(self.serial(2), (5, 6))])

    def test_an_empty_choice_of_targets_is_refused_never_all(self):
        self.put(flow())
        self.c.set_run_refs([])
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.key, "msg_no_run_targets")
        self.c.set_run_refs(["not-a-target"])
        self.assertEqual(self.c.run_refs, ())
        self.assertEqual((self.adb.screencaps, self.taps()), ([], []))

    def test_a_dry_run_looks_but_sends_nothing_and_is_not_history(self):
        self.put(flow())
        self.c.set_run_refs([self.entry(1).ref])
        self.c.start_run(dry_run=True)
        self.run_to_end()
        self.assertEqual(self.c.run_results[0].state, RunState.COMPLETED)
        self.assertEqual(self.taps(), [])
        self.assertTrue(self.adb.screencaps)
        self.assertEqual(self.history.list(), [])

    def test_a_wait_that_runs_out_is_a_failure_that_says_so(self):
        self.put(ConditionalMacro("Late", (WaitUntil(mark(expected=False), 0.2, 0.05),
                                           Decision(mark(), then=TapMatch()))))
        self.c.set_run_refs([self.entry(1).ref])
        self.c.start_run()
        self.run_to_end()
        result = self.c.run_results[0]
        self.assertEqual((result.state, result.completed, result.total), (RunState.FAILED, 0, 2))
        self.assertTrue(result.detail.startswith("timeout"), result.detail)
        self.assertEqual(self.taps(), [])
        self.assertEqual(self.history.list()[0].outcome, FAILED)

    def test_each_target_observes_through_its_own_context(self):
        """Two targets, two contexts, two sets of screenshots: target 1's frame never answers target 2."""
        self.put(ConditionalMacro("Look", (Decision(mark(), then=TapMatch()),)))
        self.c.set_run_refs([self.entry(1).ref, self.entry(2).ref])
        self.c.start_run()
        jobs = dict(self.c._running)
        self.assertEqual(len({id(job.context) for job in jobs.values()}), 2)
        self.assertEqual({job.context.target_id for job in jobs.values()},
                         {self.target(1).target_id, self.target(2).target_id})
        self.run_to_end()
        self.assertEqual(sorted(self.adb.screencaps), [self.serial(1), self.serial(2)])


# ---- the view: real Tk screens and dialogs, never shown -------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:                                   # pragma: no cover
    tk = ttk = None

import tests.test_gui as gui_tests                     # noqa: E402  (Base: the simulated desktop; no tests of its own)
from app.providers.target import Capability           # noqa: E402


class Click:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Declares:
    """A stand-in for the chosen target that declares exactly `capabilities` (for the dialog's warning only)."""

    target_id = "declares"

    def __init__(self, capabilities):
        self.capabilities = frozenset(capabilities)

    def has_capability(self, capability):
        return capability in self.capabilities


@unittest.skipIf(tk is None, "tkinter is not available")
class ViewV2(gui_tests.Base):
    LONG = "A deliberately long macro name that keeps going to see what the layout does with it"

    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                       preview_runner=lambda fn: fn())
        self.c = self.app.controller
        self.macros = self.app.screens["macros"]
        self.editor = self.app.screens["editor"]
        self.c.refresh()
        self.addCleanup(self.app.close)

    def stub(self, module, name, value):
        """Answer one dialog without showing it; `value` may be a function of the dialog's arguments."""
        class Stub:
            def __init__(self, *args, **kwargs):
                Stub.args = args

            def show(self):
                return value(*Stub.args) if callable(value) else value

        original = getattr(module, name)
        setattr(module, name, Stub)
        self.addCleanup(setattr, module, name, original)

    def put(self, macro) -> str:
        path = self.library.save(macro)
        self.c.refresh_macros()
        key = self.library.key_of(path)
        self.macros.select_row(key)
        return key

    def many_steps(self, name="Many"):
        wait = WaitUntil(Condition.text("Collect the daily reward from the mailbox", label=self.LONG), 20, 0.5)
        pick = Decision(Condition.pixel((1279, 719), (255, 255, 255), expected=False), otherwise=TapMatch())
        return ConditionalMacro(name, (wait, pick, RunMacro(recorded(name=self.LONG))) * 30)

    def dialog(self, cls, *args, **kwargs):
        """A real dialog, built but never mapped."""
        made = cls(self.app, *args, **kwargs)
        made.top.withdraw()
        self.addCleanup(lambda: made.top.winfo_exists() and made.top.destroy())
        return made

    # ---- the list ---------------------------------------------------------------------------------------
    def test_recorded_macros_and_scripts_are_listed_apart_and_say_what_they_are(self):
        recorded_key = self.put(recorded(name="Recorded"))
        key = self.put(flow("Decides"))
        self.app.show_screen("scripts")
        scripts = self.app.screens["scripts"]
        scripts.select_row(key)
        self.app.render()
        self.assertEqual(list(scripts._row_widgets), [key])                  # the scripts list: scripts only
        row = scripts._row_widgets[key]
        texts = [label.cget("text") for label in row["labels"] if isinstance(label, tk.Label)]
        self.assertTrue(any("2 steps" in t for t in texts), texts)
        self.assertFalse(any("v2" in t for t in texts), texts)               # never a format version number
        self.assertEqual(scripts.lbl_kind.cget("text"), self.app.t("kind_script"))
        preview = scripts.lbl_steps.cget("text")
        self.assertIn(self.app.t("step_wait_until"), preview)
        self.assertIn("the mark", preview)
        self.app.show_screen("macros")
        self.assertEqual(list(self.macros._row_widgets), [recorded_key])    # the macros list: recordings only

    def test_the_details_read_a_v2_macro_once_not_on_every_redraw(self):
        self.put(flow())
        with mock.patch.object(self.library, "load", wraps=self.library.load) as load:
            for _ in range(5):
                self.app.render()
        self.assertLessEqual(load.call_count, 1)

    def test_convert_to_v2_from_the_row_menu(self):
        key = self.put(recorded(name="Morning"))
        from app.gui.view import dialogs
        self.stub(dialogs, "TextPrompt", "Morning v2")
        self.macros._convert(key)
        info = self.c.selected_macro()
        self.assertEqual((info.name, info.version), ("Morning v2", 2))
        self.assertTrue(self.c.macro_by_key(key).path.exists())

    # ---- the editor -------------------------------------------------------------------------------------
    def open(self, macro) -> str:
        key = self.put(macro)
        self.macros._edit_selected()
        self.assertEqual(self.app.screen, "editor")
        self.app.render()
        return key

    def rows(self):
        """The script's top-level steps in the list (the Start row, first, is not a step)."""
        return [self.editor.tree.item(iid, "values") for iid in self.editor.tree.get_children() if iid != "start"]

    def apply_in_panel(self, step):
        """What Apply does with a valid form in the properties panel -- without typing every field by hand."""
        self.app.render()
        self.assertIsNotNone(self.editor.form)
        self.editor.form.build = lambda: step
        self.assertTrue(self.editor.apply_form(), self.c.status)

    def test_the_editor_lists_the_steps_and_edits_them_with_undo_and_save(self):
        key = self.open(flow())
        self.assertEqual([r[1] for r in self.rows()], [self.app.t("step_wait_until"), self.app.t("step_if")])
        self.assertIn("the mark", self.editor.lbl_step.cget("text"))
        self.editor.tree.selection_set("s:0")
        self.editor.duplicate_step()
        self.assertEqual(len(self.rows()), 3)
        self.editor._move(1)
        self.editor.tree.selection_set("s:2")
        self.editor._delete()
        self.assertEqual(len(self.rows()), 2)
        new = fe.build_wait(Condition.text("Ready"), "3", "0.5")
        self.editor.tree.selection_set("s:0")
        self.apply_in_panel(new)                                     # edited in place, in the properties panel
        self.assertEqual(self.c.editor.document.steps[0], new)
        self.assertNotIn("disabled", self.editor.btn_save.state())
        self.app._key_undo()
        self.assertNotEqual(self.c.editor.document.steps[0], new)
        self.app._key_redo()
        self.app._key_save()
        self.assertFalse(self.c.editor_dirty)
        self.assertEqual(self.library.load(self.c.macro_by_key(key).path).steps[0], new)

    def test_adding_a_step_inserts_it_after_the_chosen_one(self):
        self.open(flow())
        added = fe.build_run(recorded(name="Inner"))
        self.editor.tree.selection_set("s:0")
        self.editor._add_step(fe.RUN)
        self.assertEqual(len(self.c.editor.document.steps), 2)          # shown where it will go, not added yet
        self.apply_in_panel(added)
        self.assertEqual(self.c.editor.document.steps[1], added)
        self.assertEqual(self.editor.tree.selection(), ("s:1",))

    def test_adding_after_the_last_step_appends_it_and_chooses_it(self):
        """Found by gui_smoke: the new row was selected BEFORE the list was redrawn, so a step added at the end
        raised TclError ("Item 2 not found") and the editor stopped responding to that click."""
        self.open(flow())
        added = fe.build_run(recorded(name="Inner"))
        self.editor.tree.selection_set("s:1")
        self.editor._add_step(fe.RUN)
        self.apply_in_panel(added)
        self.assertEqual(len(self.c.editor.document.steps), 3)
        self.assertEqual(self.c.editor.document.steps[2], added)
        self.assertEqual(self.editor.tree.selection(), ("s:2",))
        from app.gui.view import fmt
        self.assertEqual(self.editor.lbl_kind.cget("text"), fmt.step_title(self.app, added))

    def test_adding_with_no_step_chosen_appends_it_and_chooses_it(self):
        self.open(flow())
        added = fe.build_run(recorded(name="Inner"))
        self.editor.tree.selection_remove(*self.editor.tree.selection())
        self.editor._add_step(fe.RUN)
        self.apply_in_panel(added)
        self.assertEqual(self.c.editor.document.steps[-1], added)
        self.assertEqual(self.editor.tree.selection(), ("s:2",))

    def test_duplicating_the_last_step_chooses_the_copy(self):
        self.open(flow())
        self.editor.tree.selection_set("s:1")
        self.editor.duplicate_step()
        steps = self.c.editor.document.steps
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[2], steps[1])
        self.assertEqual(self.editor.tree.selection(), ("s:2",))

    def test_leaving_with_unsaved_v2_changes_asks(self):
        from app.gui.view import dialogs
        self.open(flow())
        self.editor.tree.selection_set("s:0")
        self.editor.duplicate_step()
        self.stub(dialogs, "UnsavedChanges", None)
        self.app.show_screen("macros")
        self.assertEqual(self.app.screen, "editor")                  # cancelled: nothing was lost
        self.stub(dialogs, "UnsavedChanges", "discard")
        self.app.show_screen("macros")
        self.assertEqual(self.app.screen, "macros")

    def test_a_v1_macro_still_opens_in_the_recorded_editor(self):
        self.open(recorded(name="Recorded"))
        self.assertFalse(self.c.editing_v2)
        self.assertFalse(self.editor.btn_duplicate.winfo_manager())
        self.assertTrue(self.editor.btn_apply.winfo_manager())

    # ---- the step dialog --------------------------------------------------------------------------------
    def test_the_step_dialog_builds_a_wait_from_its_fields(self):
        from app.gui.view.flow import StepDialog
        dialog = self.dialog(StepDialog, fe.WAIT)
        editor = dialog.condition
        editor.kind_var.set(self.app.t("cond_pixel"))
        editor._on_kind()
        editor.vars[fe.PIXEL]["x"].set("10")
        editor.vars[fe.PIXEL]["y"].set("20")
        editor.vars[fe.PIXEL]["color"].set("1, 2, 3")
        dialog.timeout.set("12")
        dialog.confirm()
        self.assertEqual(dialog.result, WaitUntil(Condition.pixel((10, 20), (1, 2, 3)), 12, 0.5))

    def test_a_refused_value_is_explained_in_the_dialog_which_stays_open(self):
        from app.gui.view.flow import StepDialog
        self.app.apply_language("ru")
        dialog = self.dialog(StepDialog, fe.WAIT)
        dialog.condition.lines[0]["text"].set("Collect")
        dialog.interval.set("0,01")
        dialog.confirm()
        self.assertIsNone(dialog.result)
        self.assertTrue(dialog.top.winfo_exists())
        self.assertTrue(dialog.lbl_error.cget("text").startswith("Значение не принято"),
                        dialog.lbl_error.cget("text"))
        dialog.timeout.set("abc")
        dialog.confirm()
        self.assertIn("должно быть число", dialog.lbl_error.cget("text"))

    def test_an_if_is_its_condition_and_what_its_branches_hold_is_kept(self):
        """The blocks of an IF's branches are blocks of their own (added with the "+" on their arrows): the IF's
        form is its condition, and changing it keeps the branches and where the IF is drawn."""
        from app.gui.view.flow import PRESET_TAP, StepDialog
        dialog = self.dialog(StepDialog, fe.IF)
        self.assertFalse(hasattr(dialog, "then"))
        dialog.condition.lines[0]["text"].set("Collect")
        dialog.confirm()
        self.assertEqual(dialog.result, Decision(Condition.text_lines([("Collect", True)])))   # both branches empty
        tapping = self.dialog(StepDialog, fe.IF, preset=PRESET_TAP)
        tapping.condition.lines[0]["text"].set("Collect")
        tapping.confirm()
        self.assertEqual(tapping.result.then, (TapMatch(),))
        inner = RunMacro(recorded(name="Inner"))
        step = Decision(Condition.text_lines([("Collect", True)]), (TapMatch(), inner), (inner,), at=(40, 50))
        again = self.dialog(StepDialog, fe.IF, step)
        again.condition.lines[0]["text"].set("Gather")
        again.confirm()
        self.assertEqual((again.result.then, again.result.otherwise, again.result.at),
                         (step.then, step.otherwise, (40, 50)))
        self.assertEqual(again.result.condition, Condition.text_lines([("Gather", True)]))

    def test_a_click_on_the_match_is_a_block_of_its_own_and_says_when_the_target_cannot_do_it(self):
        from app.gui.view.flow import StepDialog
        self.pick("Notes")
        self.c.target = Declares({Capability.SCREENSHOT, Capability.TAP})
        dialog = self.dialog(StepDialog, fe.TAP)
        dialog.tap.op_var.set(self.app.t("act_double_tap"))
        dialog.tap._note()
        self.assertEqual(dialog.tap.lbl_note.cget("text"), self.app.t("tap_unsupported_here").format(name="Notes"))
        dialog.tap.op_var.set(self.app.t("act_tap"))
        dialog.tap._note()
        self.assertEqual(dialog.tap.lbl_note.cget("text"), "")
        dialog.tap.dy.set("150")
        dialog.confirm()
        self.assertEqual(dialog.result, TapMatch(offset=(0, 150)))

    # ---- the picker -------------------------------------------------------------------------------------
    def screen(self):
        self.pick("Notes")
        job = self.c.begin_screen()
        return self.c.finish_screen(job(), job)

    def test_the_picker_turns_a_drag_into_screenshot_pixels(self):
        from app.gui.view.flow import ScreenPicker
        picker = self.dialog(ScreenPicker, self.screen(), "region")
        self.assertEqual(picker.factor, 1)
        picker._press(Click(40, 30))
        picker._release(Click(10, 5))
        picker.confirm()
        self.assertEqual(picker.result, {"roi": Roi(10, 5, 30, 25), "limit": False})

    # ---- text lines, the pixel's colour, the eyedropper (2026-10-19) ---------------------------------------
    def test_a_text_condition_is_lines_each_there_or_not_and_enter_adds_one(self):
        from app.gui.view.flow import StepDialog
        t = self.app.t
        dialog = self.dialog(StepDialog, fe.WAIT)
        cond = dialog.condition
        self.assertEqual(cond.kind, fe.TEXT)
        self.assertEqual(cond.expect_box.winfo_manager(), "")         # each line says "is there" / "is not there"
        self.assertTrue(cond._line_entries[0].bind("<Return>"))        # Enter makes the next line
        cond.lines[0]["text"].set("урон")
        self.assertEqual(cond.new_line(0), "break")
        cond.lines[1]["text"].set("здоровье")
        cond.lines[1]["present"].set(t("line_absent"))
        cond.new_line(1)                                              # an empty third line ...
        self.assertEqual(cond._backspace(2), "break")                 # ... Backspace in it removes it
        self.assertIsNone(cond._backspace(1))                         # in a line with text, Backspace just edits
        self.assertEqual(len(cond.lines), 2)
        dialog.timeout.set("12")
        dialog.confirm()
        self.assertEqual(dialog.result, WaitUntil(Condition.text_lines([("урон", True), ("здоровье", False)]),
                                                  12, 0.5))

    def test_a_saved_text_condition_comes_back_unchanged_whatever_its_case_rule_and_expectation(self):
        """Applying without a change gives back exactly the saved step -- also "case matters", "NOT (these lines)"
        and an old "IF the text is not there ... ELSE click it"; a negated text condition shows that it is."""
        from app.gui.view.flow import StepDialog
        steps = (WaitUntil(Condition.text_lines([("a", True)], ignore_case=False), 5, 0.5),
                 WaitUntil(Condition.text_lines([("a", True), ("b", False)], expected=False), 5, 0.5),
                 Decision(Condition.text("OK", expected=False), otherwise=TapMatch()))
        for step in steps:
            with self.subTest(step=step):
                dialog = self.dialog(StepDialog, fe.kind_of(step), step)
                negated = not step.condition.expected
                self.assertEqual(dialog.condition.expect_box.winfo_manager(), "grid" if negated else "")
                dialog.confirm()
                self.assertEqual(dialog.result, step)
        dialog = self.dialog(StepDialog, fe.IF, steps[2])
        dialog.condition.lines[0]["text"].set("OK!")                 # edited: text lines now, still "NOT", ELSE click
        dialog.confirm()
        self.assertEqual(dialog.result, Decision(Condition.text_lines([("OK!", True)], expected=False),
                                                 otherwise=TapMatch()))

    def test_closing_a_condition_editor_leaves_nothing_behind(self):
        from app.gui.view.flow import StepDialog
        dialog = self.dialog(StepDialog, fe.WAIT)
        colour = dialog.condition.vars[fe.PIXEL]["color"]
        self.assertTrue(colour.trace_info())
        dialog.top.destroy()
        self.assertEqual(colour.trace_info(), [])                      # the sample's watch went with the editor

    def test_lines_can_be_removed_and_there_is_a_limit(self):
        from app.gui.view.flow import StepDialog
        from app.observe import MAX_TEXT_LINES
        cond = self.dialog(StepDialog, fe.WAIT).condition
        cond.lines[0]["text"].set("a")
        cond.new_line(0)
        cond.remove_line(0)
        self.assertEqual([line["text"].get() for line in cond.lines], [""])
        cond.lines[0]["text"].set("b")
        cond.remove_line(0)                                           # the last one is emptied, never removed
        self.assertEqual([line["text"].get() for line in cond.lines], [""])
        while len(cond.lines) < MAX_TEXT_LINES:
            cond.new_line(len(cond.lines) - 1)
        cond.new_line(0)
        self.assertEqual(len(cond.lines), MAX_TEXT_LINES)
        self.assertEqual(cond.dialog.lbl_error.cget("text"),
                         self.app.t("err_flow_too_many_lines").format(count=MAX_TEXT_LINES))

    def test_a_click_on_the_found_text_is_refused_when_a_line_must_be_absent(self):
        from app.gui.view.flow import StepDialog
        t = self.app.t
        from app.gui.view.flow import PRESET_TAP
        dialog = self.dialog(StepDialog, fe.IF, preset=PRESET_TAP)       # an IF that clicks what it finds
        dialog.condition.lines[0]["text"].set("OK")
        dialog.condition.new_line(0)
        dialog.condition.lines[1]["text"].set("Ошибка")
        dialog.condition.lines[1]["present"].set(t("line_absent"))
        dialog.confirm()
        self.assertIsNone(dialog.result)
        self.assertEqual(dialog.lbl_error.cget("text"), t("err_flow_tap_absent_line"))

    def test_a_text_line_condition_reads_as_its_lines(self):
        from app.gui.view import fmt
        self.app.apply_language("ru")
        line = fmt.condition_line(self.app, Condition.text_lines([("урон", True), ("здоровье", False)]))
        self.assertEqual(line, "Текст «урон» или нет «здоровье»")

    def test_the_pixel_colour_has_a_sample_and_an_eyedropper_that_takes_only_the_colour(self):
        from app.gui.view.flow import SWATCH_EMPTY, StepDialog
        t = self.app.t
        cond = self.dialog(StepDialog, fe.WAIT).condition
        self.assertEqual(set(cond._kinds.values()), {fe.PIXEL, fe.TEMPLATE, fe.TEXT})   # no colour area for new ones
        cond.kind_var.set(t("cond_pixel"))
        cond._on_kind()
        self.assertEqual(cond.expect_box.winfo_manager(), "grid")
        cond.vars[fe.PIXEL]["color"].set("255, 0, 0")
        self.assertEqual(cond._swatch.cget("background"), "#ff0000")
        cond.vars[fe.PIXEL]["color"].set("not a colour")
        self.assertEqual(cond._swatch.cget("background"), SWATCH_EMPTY)
        picks = []
        cond._pick = lambda mode, apply: picks.append((mode, apply))
        buttons = [w for w in self.descendants(cond.specific) if isinstance(w, ttk.Button)
                   and w.cget("text") == t("btn_eyedropper")]
        self.assertEqual(len(buttons), 1)
        buttons[0].invoke()
        self.assertEqual(picks, [("color", cond._set_pixel_color)])
        screen = self.screen()
        cond.vars[fe.PIXEL]["x"].set("7")
        cond._set_pixel_color(screen, {"point": (3, 4)})
        self.assertEqual(cond.vars[fe.PIXEL]["color"].get(), fe.format_color(screen.frame.bitmap.pixel(3, 4)))
        self.assertEqual(cond.vars[fe.PIXEL]["x"].get(), "7")                          # the point stays

    def test_a_colour_area_condition_saved_earlier_still_opens_as_itself(self):
        from app.gui.view.flow import StepDialog
        step = WaitUntil(Condition.color((9, 9, 9), region=Roi(0, 0, 5, 5), min_ratio=0.3), 20, 0.5)
        dialog = self.dialog(StepDialog, fe.WAIT, step)
        self.assertEqual(dialog.condition.kind, fe.COLOR)
        self.assertIn(fe.COLOR, dialog.condition._kinds.values())
        dialog.confirm()
        self.assertEqual(dialog.result, step)

    def test_the_eyedropper_shows_the_colour_under_the_pointer_and_returns_the_pixel_clicked(self):
        from app.gui.view.flow import ScreenPicker
        screen = self.screen()
        picker = self.dialog(ScreenPicker, screen, "color")
        self.assertTrue(picker.canvas.bind("<Motion>"))                # shown while the pointer moves
        picker._hover(Click(3, 4))
        self.assertEqual(picker.swatch.cget("background"), "#%02x%02x%02x" % screen.frame.bitmap.pixel(3, 4))
        self.assertIn("(3, 4)", picker.lbl_info.cget("text"))
        picker._press(Click(5, 6))
        picker._hover(Click(1, 1))                                    # a picked pixel stays shown
        self.assertIn("(5, 6)", picker.lbl_info.cget("text"))
        picker.confirm()
        self.assertEqual(picker.result, {"point": (5, 6)})

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_the_point_picker_returns_the_pixel_and_says_its_colour(self):
        from app.gui.view.flow import ScreenPicker
        picker = self.dialog(ScreenPicker, self.screen(), "point")
        picker._press(Click(3, 4))
        self.assertIn("(3, 4)", picker.lbl_info.cget("text"))
        picker.confirm()
        self.assertEqual(picker.result, {"point": (3, 4)})

    def test_confirming_with_nothing_chosen_keeps_the_picker_open(self):
        from app.gui.view.flow import ScreenPicker
        picker = self.dialog(ScreenPicker, self.screen(), "template")
        picker.confirm()
        self.assertIsNone(picker.result)
        self.assertEqual(picker.lbl_error.cget("text"), self.app.t("pick_nothing"))

    def test_a_captured_template_goes_into_the_condition_from_the_screenshot(self):
        from app.gui.view.flow import StepDialog
        screen = self.screen()
        dialog = self.dialog(StepDialog, fe.WAIT)
        editor = dialog.condition
        editor.kind_var.set(self.app.t("cond_template"))
        editor._on_kind()
        editor._set_template(screen, {"roi": Roi(2, 3, 10, 8), "limit": True})
        self.assertEqual(editor.template, screen.frame.bitmap.crop(2, 3, 10, 8))
        self.assertEqual(editor.region, geo.expand(Roi(2, 3, 10, 8), 20, screen.width, screen.height))

    def test_the_dialog_takes_the_screenshot_off_the_ui_thread_and_uses_it_on_it(self):
        from app.gui.view import flow as flow_view
        from app.gui.view.flow import StepDialog
        self.pick("Notes")
        picked = []
        self.stub(flow_view, "ScreenPicker", lambda ui, screen, mode, region: picked.append(screen) or
                  {"roi": Roi(0, 0, 5, 5), "limit": False})
        dialog = self.dialog(StepDialog, fe.WAIT)
        dialog.condition._pick("region", dialog.condition._set_region)
        self.assertEqual(picked, [])                                  # nothing used before the render loop runs
        self.app._drain_jobs()
        self.assertEqual(len(picked), 1)
        self.assertEqual(dialog.condition.region, Roi(0, 0, 5, 5))

    # ---- 1120 x 680, both languages, long content -------------------------------------------------------
    def test_everything_fits_the_smallest_window_in_both_languages(self):
        from app.gui.view.flow import ScreenPicker, StepDialog
        self.put(flow(self.LONG[:60]))
        self.open(self.many_steps(self.LONG[:60] + " 2"))
        for language in ("en", "ru"):
            self.app.apply_language(language)
            self.app.show_screen("editor")
            self.editor.tree.selection_set("s:0")
            self.app.render()
            self.root.geometry("1120x680")
            self.root.update_idletasks()
            self.assertLessEqual(self.editor.frame.winfo_reqwidth(), 1120, language)
            self.assertLessEqual(self.editor.frame.winfo_reqheight(), 680 - 110, language)   # topbar + status
            self.assertLessEqual(self.root.winfo_reqwidth(), 1120, language)                # the topbar too
            for index in range(3):
                step = self.c.editor.document.steps[index]
                dialog = self.dialog(StepDialog, fe.kind_of(step), step)
                dialog.top.update_idletasks()
                self.assertLessEqual(dialog.top.winfo_reqwidth(), 1120, (language, index))
                self.assertLessEqual(dialog.top.winfo_reqheight(), 680, (language, index))
                dialog.top.destroy()
        self.app.show_screen("macros")
        self.macros.select_row(self.c.macros[0].key)
        self.app.render()
        self.root.update_idletasks()
        self.assertLessEqual(self.macros.frame.winfo_reqwidth(), 1120)
        picker = self.dialog(ScreenPicker, self.screen(), "template")
        picker.top.update_idletasks()
        self.assertLessEqual(picker.top.winfo_reqheight(), 680)

    def test_every_step_reads_in_both_languages_without_a_raw_key(self):
        from app.gui.view import fmt
        steps = self.many_steps().steps[:3] + flow().steps + (
            Decision(mark(expected=False), otherwise=TapMatch(ActionOp.SECONDARY_TAP, (3, 4))),)
        for language in ("en", "ru"):
            self.app.apply_language(language)
            for step in steps:
                line = fmt.step_line(self.app, step)
                self.assertNotRegex(line, r"\b(step|cond|branch|act|count)_[a-z_]+", (language, line))


if __name__ == "__main__":
    unittest.main()
