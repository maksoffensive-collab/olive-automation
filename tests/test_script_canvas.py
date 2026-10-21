"""The script editor by hand, on the person's requests of 2026-10-19: drag a block anywhere and its arrows follow it;
the wheel alone zooms; the background is squares; the "+" in the middle of an arrow adds a block exactly there; a
click on a part of a block opens that part; the Start block holds a delay; blocks placed by hand are saved and
kept; a script waiting to start says how long.

Real Tk widgets built and never shown (gestures are delivered to the canvas's own handlers); the simulated desktop
of tests.test_gui. Nothing here reaches a real window, mouse or keyboard.
"""
import json
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

from app.decide import MAX_START_DELAY_S, THEN, Condition, Decision, RunMacro, TapMatch, WaitUntil
from app.decide.format import ConditionalMacro
from app.decide.position import MAX_POSITION
from app.gui import controller as controller_module
from app.gui import flow_edit as fe
from app.gui import flow_layout as fl
from app.gui import flows
from app.gui import theme as th
from app.gui.controller import RunState
from app.gui.view import fmt
from app.gui.view import widgets as w
from app.gui.view.script_canvas import key as tag_of
from app.playback.macro import Macro
from app.providers.input import Key
from app.providers.recording import RecordedEvent as E, RecordedEventKind as K, Recording
from app.windows.backend import InputDeliveryError
from tests import test_gui as gui_tests
from tests.test_gui_v2 import recorded

FOUND = Condition.pixel((1, 1), (0, 0, 0))


def th_bitmap(width, height):
    from app.vision.bitmap import Bitmap
    return Bitmap.solid(width, height, (0, 0, 0))


class Event:
    """What Tk hands a binding: widget coordinates (here equal to scene ones: zoom 1, scrolled to the corner)."""

    def __init__(self, x, y, *, delta=0, state=0):
        self.x, self.y, self.delta, self.state = int(x), int(y), delta, state
        self.x_root, self.y_root = int(x) + 500, int(y) + 300


def script(**changes) -> ConditionalMacro:
    values = dict(name="Canvas", steps=(WaitUntil(FOUND, 5, 0.5), Decision(FOUND, (TapMatch(),)),
                                        RunMacro(recorded(name="Inner"))))
    values.update(changes)
    return ConditionalMacro(**values)


class ScriptCanvasByHand(gui_tests.Base):
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
        self.editor = self.app.screens["editor"]
        self.c.refresh()
        self.addCleanup(self.app.close)

    def open(self, macro) -> str:
        key = self.library.key_of(self.library.save(macro))
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        self.app.render()
        self.editor._cancel_zoom()                    # the first fit would change the zoom later, when idle
        self.view = self.editor.canvas_view
        self.view.scale = 1.0
        self.view.redraw(force=True)
        self.view.canvas.xview_moveto(0)
        self.view.canvas.yview_moveto(0)
        return key

    def drag(self, start, end, *, steps=4):
        view = self.view
        view._press_1(Event(*start))
        for i in range(1, steps + 1):
            view._motion_1(Event(start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps))
        view._release_1(Event(*end))

    def click(self, x, y):
        self.view._press_1(Event(x, y))
        self.view._release_1(Event(x, y))

    # ---- moving blocks ---------------------------------------------------------------------------------------
    def test_a_dragged_block_is_put_where_it_is_dropped_and_its_arrows_follow_it(self):
        self.open(script())
        view = self.view
        box = view.layout.box_of((2,))
        auto_first = view.layout.box_of((0,))
        view._press_1(Event(box.cx, box.cy))
        view._motion_1(Event(box.cx + 2, box.cy + 2))
        self.assertIsNone(view._drag)                                    # a twitch is not a drag
        view._motion_1(Event(box.cx + 120, box.cy + 60))
        into = next(e for e in view.layout.edges if e.slot == (2,))
        self.assertEqual(into.points[-1], (box.cx + 120, box.y + 60))    # the arrow follows while it moves ...
        drawn = [tuple(round(v) for v in view.canvas.coords(i)[-2:]) for i in view.canvas.find_withtag("links")
                 if view.canvas.type(i) == "line"]
        self.assertIn((round(box.cx + 120), round(box.y + 60)), drawn)   # ... on the canvas, not only in figures
        out = next(e for e in view.layout.edges if e.slot == (3,))
        self.assertEqual(out.points[0], (box.cx + 120, box.bottom + 60))
        view._release_1(Event(box.cx + 120, box.cy + 60))
        document = self.c.editor.document
        self.assertEqual(fe.item_at(document, (2,)).at, (round(box.x + 120), round(box.y + 60)))
        # the first move keeps every other block where it was drawn, so only the dragged one moved
        self.assertEqual(fe.item_at(document, (0,)).at, (round(auto_first.x), round(auto_first.y)))
        self.assertNotIn("disabled", self.editor.btn_arrange.state())
        self.assertTrue(self.c.editor_dirty)
        self.app._key_undo()
        self.assertFalse(fe.placed(self.c.editor.document))

    def test_an_if_moves_with_everything_its_branches_hold(self):
        self.open(script())
        decision, tap = self.view.layout.box_of((1,)), self.view.layout.box_of((1, THEN, 0))
        self.drag((decision.cx, decision.y + 12), (decision.cx + 200, decision.y + 12))
        document = self.c.editor.document
        self.assertEqual(fe.item_at(document, (1,)).at[0], round(decision.x + 200))
        self.assertEqual(fe.item_at(document, (1, THEN, 0)).at, (round(tap.x + 200), round(tap.y)))

    def test_moving_blocks_keeps_what_is_typed_in_the_panel_and_apply_keeps_the_new_places(self):
        """A form open in the panel is not thrown away by a drag, and applying it never moves a block back -- nor
        undoes a move inside an IF's branches."""
        self.open(script())
        decision = self.view.layout.box_of((1,))
        self.click(decision.cx, decision.y + 12)                         # the IF's form is in the panel
        form = self.editor.form
        form.condition.label_var.set("typed, not applied")
        tap = self.view.layout.box_of((1, THEN, 0))
        self.drag((tap.cx, tap.cy), (tap.cx - 40, tap.cy + 30))          # a block of its branch moves
        self.drag((decision.cx, decision.y + 12), (decision.cx + 60, decision.y + 12))
        self.assertIs(self.editor.form, form)                            # the same form, what was typed kept
        self.assertEqual(form.condition.label_var.get(), "typed, not applied")
        moved = fe.item_at(self.c.editor.document, (1,))
        self.assertTrue(self.editor.apply_form(), self.c.status)
        applied = fe.item_at(self.c.editor.document, (1,))
        self.assertEqual(applied.condition.label, "typed, not applied")
        self.assertEqual((applied.at, applied.then), (moved.at, moved.then))   # nothing moved back

    def test_a_group_dragged_past_the_edge_of_the_scene_stops_there_together(self):
        self.open(script())
        decision, tap = self.view.layout.box_of((1,)), self.view.layout.box_of((1, THEN, 0))
        far = 2 * MAX_POSITION
        self.drag((decision.cx, decision.y + 12), (decision.cx + far, decision.y + 12 + far), steps=2)
        moved_if, moved_tap = (fe.item_at(self.c.editor.document, p).at for p in ((1,), (1, THEN, 0)))
        self.assertEqual(max(moved_if[0], moved_tap[0]), MAX_POSITION)           # at the edge ...
        self.assertEqual(max(moved_if[1], moved_tap[1]), MAX_POSITION)
        self.assertEqual((moved_tap[0] - moved_if[0], moved_tap[1] - moved_if[1]),   # ... in the same shape
                         (round(tap.x) - round(decision.x), round(tap.y) - round(decision.y)))

    def test_a_move_the_script_does_not_take_leaves_the_blocks_drawn_where_the_script_has_them(self):
        self.open(script())
        box = self.view.layout.box_of((2,))
        before = self.view.canvas.bbox(tag_of((2,)))
        with mock.patch.object(self.c, "editor_apply", return_value=False):      # refused, say
            self.drag((box.cx, box.cy), (box.cx + 150, box.cy + 40))
        self.assertEqual(self.view.canvas.bbox(tag_of((2,))), before)
        self.assertEqual(self.view.layout.box_of((2,)), box)
        self.assertFalse(fe.placed(self.c.editor.document))

    def test_arrange_automatically_puts_every_block_back(self):
        self.open(script())
        box = self.view.layout.box_of((0,))
        self.drag((box.cx, box.cy), (box.cx + 300, box.cy + 10))
        self.assertTrue(fe.placed(self.c.editor.document))
        self.editor.arrange()
        self.assertFalse(fe.placed(self.c.editor.document))
        self.assertIn("disabled", self.editor.btn_arrange.state())

    def test_places_are_saved_in_the_file_and_kept_when_it_is_opened_again(self):
        key = self.open(script())
        box = self.view.layout.box_of((2,))
        self.drag((box.cx, box.cy), (box.cx + 90, box.cy + 30))
        moved = fe.item_at(self.c.editor.document, (2,)).at
        self.app._key_save()
        path = self.c.macro_by_key(key).path
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 3)
        self.editor.go_back()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        self.app.render()
        self.assertEqual(fe.item_at(self.c.editor.document, (2,)).at, moved)
        self.assertEqual(self.editor.canvas_view.layout.box_of((2,)).x, moved[0])

    def test_an_unchanged_script_is_not_drawn_again_on_every_render(self):
        self.open(script())
        with mock.patch.object(self.view.canvas, "delete", wraps=self.view.canvas.delete) as deleted:
            self.app.render()
            self.app.render()
        self.assertEqual([c for c in deleted.call_args_list if c.args == ("all",)], [])

    # ---- zoom and background ------------------------------------------------------------------------------
    def test_the_wheel_alone_zooms_around_the_pointer_and_shift_scrolls_instead(self):
        self.open(script())
        view = self.view
        view._wheel(Event(100, 100, delta=120))
        self.assertAlmostEqual(view.scale, fl.WHEEL_STEP)
        self.assertEqual(self.editor.lbl_zoom.cget("text"), view.zoom_text())
        view._wheel(Event(100, 100, delta=-240))
        self.assertAlmostEqual(view.scale, fl.WHEEL_STEP / fl.WHEEL_STEP ** 2)
        before = view.scale
        view._wheel(Event(100, 100, delta=120, state=0x0001))                # Shift: sideways, not zoom
        self.assertEqual(view.scale, before)

    def test_the_background_is_a_grid_of_squares(self):
        self.open(script())
        from app.gui.view.script_canvas import GRID_CELL
        picture = self.view._grid_picture

        def rgb(colour: str):
            return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
        theme = th.ACTIVE
        self.assertEqual(tuple(picture.get(GRID_CELL, 5)), rgb(theme.canvas_dot))         # a thin line
        self.assertEqual(tuple(picture.get(0, 5)), rgb(theme.border_strong))               # a stronger one
        self.assertEqual(tuple(picture.get(5, 5)), rgb(theme.canvas_bg))                   # inside a square

    # ---- adding at a "+" -------------------------------------------------------------------------------
    def plus(self, slot):
        edge = next(e for e in self.view.layout.edges if e.slot == tuple(slot) and e.plus is not None)
        return edge.plus

    def menu_at(self, slot) -> dict:
        shown = {}

        def popup(_ui, _widget, items, *, at=None):
            shown.update({key: (command, enabled) for key, command, enabled in items})
            shown["@"] = at
        with mock.patch.object(w, "popup", popup):
            self.click(*self.plus(slot))
        return shown

    def test_the_plus_on_an_arrow_adds_a_block_exactly_there(self):
        self.open(script(steps=(WaitUntil(FOUND, 5, 0.5), Decision(FOUND), RunMacro(recorded(name="Inner")))))
        menu = self.menu_at((1, THEN, 0))
        self.assertIn("add_tap_match", menu)                       # "yes" may begin with a click on the match
        self.assertIsNotNone(menu["@"])                            # the menu opens where the pointer is
        self.assertNotIn("add_tap_match", self.menu_at((1, "else", 0)))  # "no" has nothing to click
        self.assertNotIn("add_tap_match", self.menu_at((2,)))      # nor does the script's own list
        command, enabled = self.menu_at((1, THEN, 0))["add_tap_match"]
        self.assertTrue(enabled)
        command()
        self.assertEqual(self.editor._pending[:2], ((1, THEN, 0), fe.TAP))
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertEqual(fe.item_at(self.c.editor.document, (1,)).then, (TapMatch(),))
        self.assertEqual(self.editor.tree.selection(), ("s:1/then/0",))

    def test_on_a_script_placed_by_hand_the_new_block_goes_at_the_plus_and_the_blocks_below_make_room(self):
        self.open(script())
        box = self.view.layout.box_of((0,))
        self.drag((box.cx, box.cy), (box.cx + 5, box.cy))           # placed by hand now
        below = fe.item_at(self.c.editor.document, (2,)).at
        px, py = self.plus((2,))
        command, _ = self.menu_at((2,))["add_wait"]
        command()
        self.editor.form.build = lambda: WaitUntil(FOUND, 3, 0.5)
        self.assertTrue(self.editor.apply_form(), self.c.status)
        document = self.c.editor.document
        new = fe.item_at(document, (2,))
        self.assertEqual(new.at, (round(px - fl.STEP_W / 2), round(py - fl.STEP_H / 2)))
        self.assertGreater(fe.item_at(document, (3,)).at[1], below[1])  # the block that was there moved down
        self.app._key_undo()                                         # one edit: the insert and the room
        self.assertEqual(fe.item_at(self.c.editor.document, (2,)).at, below)

    def test_a_block_added_at_a_plus_by_the_edge_of_the_scene_stays_on_it(self):
        self.open(script(steps=(Decision(FOUND, at=(40, MAX_POSITION)),)))     # an IF at the bottom edge
        slot = (0, THEN, 0)                                                     # the "+" in its empty "yes" branch
        self.assertGreater(self.plus(slot)[1] - fl.STEP_H / 2, MAX_POSITION)   # a block drawn AT the "+" is past it
        command, _ = self.menu_at(slot)["add_wait"]
        command()
        self.editor.form.build = lambda: WaitUntil(FOUND, 3, 0.5)
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertEqual(fe.item_at(self.c.editor.document, slot).at[1], MAX_POSITION)

    def test_nothing_can_go_before_a_click_on_the_match(self):
        """The click opens its branch: the arrow into it has no "+", and the branch's row adds after it."""
        self.open(script())                                              # step 1: IF -> yes: a click on the match
        slots = {e.slot for e in self.view.layout.edges if e.plus is not None}
        self.assertNotIn((1, THEN, 0), slots)
        self.assertTrue({(1, THEN, 1), (1, "else", 0)} <= slots)
        self.editor.tree.selection_set("h:1/then")
        self.assertEqual(self.editor._insert_slot(), (1, THEN, 1))

    def test_a_branch_row_in_the_list_adds_at_the_start_of_that_branch(self):
        self.open(script())
        self.editor.tree.selection_set("h:1/else")
        self.assertEqual(self.editor._insert_slot(), (1, "else", 0))
        self.assertEqual(self.editor.tree.get_children("h:1/then"), ("s:1/then/0",))

    # ---- a part of a block, the Start block --------------------------------------------------------------
    def test_a_click_on_a_part_of_a_block_puts_the_cursor_in_that_part(self):
        self.open(script())
        wait = self.view.layout.box_of((0,))
        timing = next(p for p in wait.parts if p.name == "timing")
        focused = []
        with mock.patch.object(ttk.Entry, "focus_set", lambda widget: focused.append(widget)):
            self.click(timing.x + 20, timing.y + timing.h / 2)
        self.assertEqual(self.editor.tree.selection(), ("s:0",))
        self.assertEqual(focused[-1:], [self.editor.form.timeout_entry])
        run = self.view.layout.box_of((2,))
        macro = next(p for p in run.parts if p.name == "macro")
        chosen = []
        with mock.patch.object(ttk.Combobox, "focus_set", lambda widget: chosen.append(widget)):
            self.click(macro.x + 20, macro.y + 10)
        self.assertEqual(chosen[-1:], [self.editor.form.chooser.box])

    def test_the_start_block_holds_a_delay_in_seconds_minutes_or_hours(self):
        self.open(script())
        self.app.apply_language("ru")
        start = self.view.layout.box_of(fl.START_KEY)
        self.click(start.cx, start.cy)
        self.assertEqual(self.editor.tree.selection(), ("start",))
        form = self.editor.form
        form.value.set("3")
        form.unit.set(self.app.t("unit_word_minutes"))
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertEqual(self.c.editor.document.start_delay_s, 180)
        self.assertEqual(self.view.scene.words.start_detail, "через 3 мин")
        self.assertEqual(self.editor.tree.item("start", "values")[2], fmt.delay_text(self.app, 180))
        self.editor.form.value.set("11")                               # past the 10 minutes of before 2026-10-20
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertEqual(self.c.editor.document.start_delay_s, 660)
        self.editor.form.value.set("2")
        self.editor.form.unit.set(self.app.t("unit_word_hours"))
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertEqual(self.c.editor.document.start_delay_s, 7200)
        self.assertEqual(self.view.scene.words.start_detail, "через 2 ч")
        self.assertEqual(self.editor.form.unit.get(), self.app.t("unit_word_hours"))   # shown as it was typed
        self.editor.form.value.set("25")
        self.assertFalse(self.editor.apply_form())                     # more than 24 hours: said, not taken
        self.assertEqual(self.c.editor.document.start_delay_s, 7200)
        self.assertEqual(fmt.delay_text(self.app, 5400), "через 1 ч 30 мин")
        self.assertEqual((fmt.countdown(3725), fmt.countdown(65)), ("1:02:05", "1:05"))

    # ---- adapting to the size (the person's decisions of 2026-10-20) ------------------------------------------
    def test_the_start_block_says_whether_the_script_adapts_to_the_size(self):
        self.open(script())
        start = self.view.layout.box_of(fl.START_KEY)
        self.click(start.cx, start.cy)
        self.assertTrue(self.editor.form.adapt.get())                     # scripts adapt by default
        self.editor.form.adapt.set(False)
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.assertFalse(self.c.editor.document.adapt)
        self.app._key_undo()                                             # one edit, undone in one step
        self.assertTrue(self.c.editor.document.adapt)

    def test_a_point_picked_on_the_screen_gives_the_script_its_size_and_later_ones_are_scaled_into_it(self):
        from app.gui.controller import Screen
        from tests.test_vision_fast import frame as frame_of
        self.open(script())
        wait = self.view.layout.box_of((0,))
        for size, point, kept in (((200, 100), (5, 5), (5, 5)), ((400, 200), (10, 12), (5, 6))):
            with self.subTest(size=size):
                self.click(wait.cx, wait.cy)
                screen = Screen(b"", frame_of(th_bitmap(*size)))
                self.editor.form.condition._set_pixel(screen, {"point": point})
                self.assertTrue(self.editor.apply_form(), self.c.status)
                document = self.c.editor.document
                self.assertEqual((document.size, document.steps[0].condition.query.point), ((200, 100), kept))

    def test_the_whole_screen_button_picks_nothing_on_a_picture(self):
        self.open(script(steps=(WaitUntil(Condition.template(th_bitmap(4, 4)), 5, 0.5),)))
        wait = self.view.layout.box_of((0,))
        self.click(wait.cx, wait.cy)
        self.editor.form.condition._set_region(None, None)                 # "the whole screen"
        self.assertEqual(self.editor.form.condition.picked, {})

    def test_each_part_keeps_the_size_it_was_picked_at(self):
        from app.gui.controller import Screen
        from app.vision.roi import Roi
        from tests.test_vision_fast import frame as frame_of
        self.open(script(steps=(WaitUntil(Condition.template(th_bitmap(8, 8)), 5, 0.5),), size=(100, 50)))
        wait = self.view.layout.box_of((0,))
        self.click(wait.cx, wait.cy)
        form = self.editor.form.condition
        form._set_region(Screen(b"", frame_of(th_bitmap(200, 100))), {"roi": Roi(20, 10, 80, 40)})
        self.assertEqual(form.picked, {"region": (200, 100)})
        self.assertTrue(self.editor.apply_form(), self.c.status)
        query = self.c.editor.document.steps[0].condition.query
        self.assertEqual((query.region, (query.template.width, query.template.height)), (Roi(10, 5, 40, 20), (8, 8)))

    def test_testing_a_condition_now_asks_it_as_the_run_would(self):
        from app.gui.controller import Screen
        from tests.test_vision_fast import frame as frame_of
        self.pick("Notes")
        self.open(script(size=(100, 50)))
        wait = self.view.layout.box_of((0,))
        self.click(wait.cx, wait.cy)
        self.editor.form.condition._set_pixel(Screen(b"", frame_of(th_bitmap(200, 100))), {"point": (60, 40)})
        asked = []
        with mock.patch.object(self.c, "begin_probe", side_effect=lambda cond, **kw: asked.append((cond, kw))):
            self.editor.form.condition._test()
        ((condition, options),) = asked
        self.assertEqual((condition.query.point, options), ((30, 20), {"picked_on": (100, 50)}))
        with mock.patch.object(controller_module, "ObservationContext",
                               wraps=controller_module.ObservationContext) as made:
            self.assertIsNotNone(self.c.begin_probe(condition, picked_on=(100, 50)))
        self.assertEqual(made.call_args.kwargs["picked_on"], (100, 50))

    def test_a_recorded_macro_adapts_only_when_chosen_and_only_with_its_size_known(self):
        from dataclasses import replace
        sized = recorded(name="Sized")
        sized = replace(sized, recording=replace(sized.recording, size=(60, 40)))
        for macro, can in ((recorded(name="Unsized"), False), (sized, True)):
            with self.subTest(macro.name):
                key = self.library.key_of(self.library.save(macro))
                self.c.refresh_macros()
                self.assertTrue(self.c.open_editor(key))
                self.app.show_screen("editor")
                self.app.render()
                self.assertEqual("disabled" not in self.editor.chk_adapt.state(), can)
                if can:
                    self.editor.adapt_var.set(True)
                    self.editor._toggle_adapt()
                    self.assertTrue(self.c.save_editor(), self.c.status)
                    self.assertTrue(self.library.load(self.c.macro_by_key(key).path).adapt)
                self.editor.go_back()

    def test_the_window_rings_once_when_a_run_asks_for_attention(self):
        self.open(script())
        with mock.patch.object(self.app.root, "bell") as bell:
            self.app.render()
            bell.assert_not_called()
            self.c.attention += 1
            self.c._touch()
            self.app.render()
            self.app.render()
        bell.assert_called_once()


class WaitingToStart(gui_tests.Base):
    """A script with a start delay, run through the real controller on the simulated desktop."""

    def start(self, *titles, delay, name="Later", dry_run=False):
        key = self.library.key_of(self.library.save(ConditionalMacro(
            name, (RunMacro(recorded(provider="windows", target=self.c.target.target_id)),), start_delay_s=delay)))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry(title).ref for title in titles])
        self.assertTrue(self.c.start_run(dry_run=dry_run), self.c.status)

    def run_to_end(self, timeout=10.0):
        deadline = time.time() + timeout
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.02)
        self.assertFalse(self.c.run_active)

    def test_the_start_delay_is_waited_once_for_the_whole_run(self):
        """Two windows run one after the other: the run waits its delay once, before the first -- not once more
        before each (the person's decision of 2026-10-20)."""
        self.pick("Notes")
        delays = []
        real = flows.FlowJob.start_in_background

        def starting(job):
            delays.append(job.delay_s)
            real(job)
        with mock.patch.object(flows.FlowJob, "start_in_background", starting):
            self.start("Notes", "Calculator", delay=0.6)
            self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED, RunState.COMPLETED])
        self.assertEqual(len(delays), 2)
        self.assertGreater(delays[0], 0.3)
        self.assertEqual(delays[1], 0)

    def test_shortly_before_a_delayed_start_it_says_so_once_and_asks_for_a_sound(self):
        self.pick("Notes")
        with mock.patch.object(controller_module, "START_NOTICE_S", 3.5):
            for run, name in enumerate(("Later", "Again"), start=1):            # once in every run
                self.start("Notes", delay=5, name=name)
                deadline = time.time() + 4
                while time.time() < deadline and self.c.attention < run:
                    self.c.poll()
                    time.sleep(0.02)
                self.assertEqual(self.c.attention, run)
                self.assertEqual((self.c.status.level, self.c.status.key), ("warn", "msg_run_starts_soon"))
                self.assertLessEqual(dict(self.c.status.params)["seconds"], 4)  # when it is that close, not before
                for _ in range(10):
                    self.c.poll()
                    time.sleep(0.02)
                self.assertEqual(self.c.attention, run)                          # once
                self.c.emergency_stop()
                self.run_to_end()
                self.assertTrue(self.c.clear_emergency_stop(), self.c.status)
        self.assertEqual([e for e in self.world.events if e[0] != "query"], [])     # nothing was sent

    def test_the_longest_delay_is_never_handed_on_as_a_little_more(self):
        """Read twice at the same moment, start + 24 h - now can come out a hair above 24 h in floating point."""
        self.pick("Notes")
        frozen = 65536 + 3 * 2 ** -36
        self.c._clock = lambda: frozen
        delays = []
        real = flows.FlowJob.start_in_background

        def starting(job):
            delays.append(job.delay_s)
            job.cancel("test")                                          # do not wait a day
            real(job)
        with mock.patch.object(flows.FlowJob, "start_in_background", starting):
            self.start("Notes", delay=MAX_START_DELAY_S)
            self.run_to_end()
        self.assertLessEqual(delays[0], MAX_START_DELAY_S)
        self.assertEqual(self.c.run_results[0].state, RunState.STOPPED)    # not refused

    def test_no_notice_once_the_run_is_stopped(self):
        self.pick("Notes")
        with mock.patch.object(controller_module, "START_NOTICE_S", 4.6):
            self.start("Notes", delay=5)
            time.sleep(0.6)                                     # inside the notice's seconds, not polled yet
            self.c.emergency_stop()
            self.run_to_end()
        self.assertEqual(self.c.attention, 0)

    def test_a_slow_first_target_still_gets_the_notice(self):
        """What is left of the delay for the first target can be under the notice's seconds; the run's is not."""
        self.pick("Notes")
        real = self.services.verify

        def slow(entry):
            time.sleep(1.0)
            return real(entry)
        with mock.patch.object(controller_module, "START_NOTICE_S", 4.6), \
                mock.patch.object(self.services, "verify", slow):
            self.start("Notes", delay=5)
            deadline = time.time() + 3
            while time.time() < deadline and not self.c.attention:
                self.c.poll()
                time.sleep(0.02)
            self.assertEqual(self.c.attention, 1)
            self.c.emergency_stop()
            self.run_to_end()

    def test_what_a_run_lets_go_of_is_logged_and_what_it_could_not_is_said(self):
        self.pick("Notes")
        shift = Macro(Recording("windows", self.c.target.target_id, (E(K.KEY_DOWN, 0, key=Key.SHIFT),)), "Shift")
        script = ConditionalMacro("Shift script", (RunMacro(shift),))

        def refuse(key):
            raise InputDeliveryError("refused")
        for name, macro in (("script", script), ("macro", shift)):
            with self.subTest(name):
                key = self.library.key_of(self.library.save(macro))
                self.c.refresh_macros()
                self.c.select_macro(key)
                self.c.set_run_refs([self.entry("Notes").ref])
                before = len(self.c.log)
                self.assertTrue(self.c.start_run(), self.c.status)
                self.run_to_end()
                new = list(self.c.log)[before:]
                self.assertTrue(any("let go" in line and "key:shift" in line for line in new), new)
                with mock.patch.object(self.world, "release_key", refuse):
                    self.assertTrue(self.c.start_run(), self.c.status)
                    self.run_to_end()
                self.assertIn("still held: key:shift", self.c.run_results[0].detail)

    def test_no_notice_for_a_delay_shorter_than_it_or_a_dry_run(self):
        self.pick("Notes")
        with mock.patch.object(controller_module, "START_NOTICE_S", 4.6):
            self.start("Notes", delay=0.4, name="Soon")
            self.run_to_end()
            self.start("Notes", delay=5, name="Dry", dry_run=True)
            self.run_to_end()
        self.assertEqual(self.c.attention, 0)

    def test_the_run_says_how_long_it_still_waits_and_the_stop_ends_the_wait(self):
        self.pick("Notes")
        key = self.library.key_of(self.library.save(ConditionalMacro(
            "Later", (RunMacro(recorded(provider="windows", target=self.c.target.target_id)),), start_delay_s=5)))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        deadline = time.time() + 3
        while time.time() < deadline and not self.c.run_results[0].starts_in:
            self.c.poll()
            time.sleep(0.02)
        self.assertIn(self.c.run_results[0].starts_in, (4, 5))
        self.assertEqual(self.c.run_results[0].state, RunState.RUNNING)
        self.c.emergency_stop()
        deadline = time.time() + 5
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.02)
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.c.run_results[0].state, RunState.STOPPED)
        self.assertEqual([e for e in self.world.events if e[0] != "query"], [])     # nothing was sent


if __name__ == "__main__":
    unittest.main()
