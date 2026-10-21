"""The application-wide redesign: the design system, the shell, Macros vs Scripts, the script editor, the Text
screen's boxes, Full Windows as a desktop capture -- driven through the real controller and the real window.

Deterministic: the simulated desktop of tests.test_gui (no real window, no real input), a counting OCR reader (no
engine), and Tk windows that are built but never shown. Pure parts (contrast, nine-slice pictures, the block layout,
the preview geometry) are checked without Tk.
"""
import ctypes
import os
import struct
import sys
import time
import unittest
import zlib

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, TargetNotVerified
from app.decide import RunMacro
from app.gui import flow_edit as fe
from app.gui import flow_layout as fl
from app.gui import macro_edit
from app.gui import region as geo
from app.gui import theme as th
from app.gui.controller import KIND_MACROS, KIND_SCRIPTS, READ_FAILED, AppController, ReadBlock
from app.gui.library import file_stamp
from app.gui.view import skin
from app.ocr.provider import OcrResult, OcrTextBlock
from app.vision.bitmap import decode_png
from app.vision.roi import Roi
from app.windows.backend import DesktopCapture, RawBitmap, WindowCaptureError
from app.windows.provider import MAX_SIDE, DesktopScreenshotProvider
from app.windows.target import FullWindowsTarget, desktop_id
from tests import test_gui as gui_tests
from tests.test_gui import CountingOcr
from tests.test_gui_v2 import flow, recorded
from tests.test_windows_provider import FakeDesktop, info

tk = gui_tests.tk


# ---- the design system ----------------------------------------------------------------------------------------
def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ThemeContrast(unittest.TestCase):
    """Every pair the screens draw text with is legible (WCAG 2.x AA: 4.5:1 for text, 3:1 for a field's edge)."""

    TEXT_PAIRS = (("text", "bg"), ("text", "surface"), ("text", "primary_soft"), ("text", "surface_alt"),
                  ("text_muted", "bg"), ("text_muted", "surface"), ("text_muted", "surface_alt"),
                  ("text_muted", "primary_soft"), ("text_on_primary", "primary"), ("text_on_primary", "primary_hover"),
                  ("text_on_primary", "primary_pressed"), ("primary", "bg"), ("primary", "surface"),
                  ("primary", "primary_soft"), ("error", "surface"), ("error", "error_bg"), ("error", "danger_bg"),
                  ("warning", "bg"), ("warning", "warning_bg"), ("success", "surface"), ("success", "success_bg"),
                  ("info", "surface"), ("info", "info_bg"), ("text_on_primary", "danger_fill"),
                  ("sidebar_text_muted", "sidebar_bg"), ("sidebar_text", "sidebar_active"))

    def test_every_text_pair_is_at_least_4_5_to_1_in_every_theme(self):
        for theme in th.THEMES.values():
            for fg, bg in self.TEXT_PAIRS:
                ratio = contrast(getattr(theme, fg), getattr(theme, bg))
                self.assertGreaterEqual(ratio, 4.5, (theme.id, fg, bg, round(ratio, 2)))

    def test_a_fields_edge_and_the_focus_ring_stand_out_from_their_surface(self):
        for theme in th.THEMES.values():
            self.assertGreaterEqual(contrast(theme.input_border, theme.surface), 3.0, theme.id)
            self.assertGreaterEqual(contrast(theme.focus, theme.bg), 3.0, theme.id)

    def test_the_light_green_theme_is_the_default_and_old_ids_fall_back_to_it(self):
        self.assertEqual(th.DEFAULT_THEME, "light_green")
        for old in ("light_olive", "dark_olive", "olive_fusion"):
            self.assertIs(th.set_theme(old), th.THEMES["light_green"])
        th.set_theme(th.DEFAULT_THEME)


class SkinPictures(unittest.TestCase):
    def pixel(self, rgba: bytes, width: int, x: int, y: int) -> tuple:
        i = (y * width + x) * 4
        return tuple(rgba[i:i + 4])

    def test_every_stretched_picture_keeps_a_middle_to_tile(self):
        """A nine-slice picture whose middle is empty never finishes tiling: Tk hangs (found while verifying the
        redesign: a pill-shaped chip did exactly that)."""
        for variant in skin.button_variants():
            size = skin.image_size(variant)
            for corner in (4, 8, 14, 17, 40):
                left, top, right, bottom = skin.nine_slice(size, corner)
                self.assertGreaterEqual(size[0] - left - right, skin.MIN_CENTRE, (variant, corner))
                self.assertGreaterEqual(size[1] - top - bottom, skin.MIN_CENTRE, (variant, corner))

    def test_a_rounded_picture_has_transparent_corners_an_opaque_middle_and_a_visible_focus_ring(self):
        width, height = 34, 34
        plain = skin.rounded_rgba(width, height, 8, "#2D6A4F", margin=3)
        self.assertEqual(self.pixel(plain, width, 0, 0)[3], 0)                       # outside: transparent
        self.assertEqual(self.pixel(plain, width, 17, 17), (0x2D, 0x6A, 0x4F, 255))   # inside: the fill
        ringed = skin.rounded_rgba(width, height, 8, "#2D6A4F", ring="#1E5A8A", margin=3)
        self.assertEqual(self.pixel(ringed, width, 17, 0)[:3], (0x1E, 0x5A, 0x8A))    # the ring, in the margin
        self.assertEqual(self.pixel(plain, width, 17, 0)[3], 0)                       # ... absent without focus

    def test_the_png_encoder_writes_a_valid_rgba_image(self):
        rgba = bytes([10, 20, 30, 255, 40, 50, 60, 128]) * 3
        png = skin.encode_rgba_png(2, 3, rgba)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, kind = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((width, height, depth, kind), (2, 3, 8, 6))
        start = png.index(b"IDAT") + 4
        length = struct.unpack(">I", png[start - 8:start - 4])[0]
        raw = zlib.decompress(png[start:start + length])
        self.assertEqual(raw, b"".join(b"\x00" + rgba[y * 8:(y + 1) * 8] for y in range(3)))

    def test_a_stretched_picture_carries_a_wide_middle_with_exactly_the_pixels_tk_would_tile(self):
        """Tk tiles a picture's middle one copy per tile: a small middle on a full-screen card meant tens of
        thousands of copies per redraw (seconds when maximised). The middle is repeated in the picture instead."""
        width, height = skin.CARD_SIZE
        border = skin.CARD_BORDER
        rgba = skin.rounded_rgba(width, height, th.RADIUS_CARD, "#ffffff", border="#cccccc")
        for down in (False, True):
            wide, tall, out = skin.widen_middle(rgba, width, height, border, down=down)
            left, top, right, bottom = border
            middle_w, middle_h = width - left - right, height - top - bottom
            self.assertGreaterEqual(wide - left - right, skin.STRETCH)
            self.assertEqual(tall - top - bottom >= skin.STRETCH, down)
            for y in range(tall):
                source_y = y if y < top else (height - (tall - y) if y >= tall - bottom
                                             else top + (y - top) % middle_h)
                for x in range(wide):
                    source_x = x if x < left else (width - (wide - x) if x >= wide - right
                                                  else left + (x - left) % middle_w)
                    self.assertEqual(self.pixel(out, wide, x, y), self.pixel(rgba, width, source_x, source_y))


# ---- the block layout and the preview geometry ----------------------------------------------------------------
class BlockLayout(unittest.TestCase):
    """A wait, an IF whose "yes" branch clicks the match, a recorded macro -- the shape every test here uses."""
    NODES = (fl.Node((0,), fl.WAIT),
             fl.Node((1,), fl.IF, then=(fl.Node((1, fl.THEN, 0), fl.TAP),)),
             fl.Node((2,), fl.RUN))

    def test_steps_are_drawn_top_to_bottom_in_run_order_between_start_and_end(self):
        layout = fl.layout(self.NODES)
        start = layout.box_of(fl.START_KEY)
        end = layout.box_of(fl.END_KEY)
        steps = [layout.box_of((i,)) for i in range(3)]
        tops = [start.y] + [b.y for b in steps] + [end.y]
        self.assertEqual(tops, sorted(tops))
        self.assertEqual(len({round(b.cx, 6) for b in steps}), 1)                      # one column
        self.assertEqual(layout.order, ((0,), (1,), (1, fl.THEN, 0), (2,)))
        flows = [e for e in layout.edges if e.role == "flow"]
        # start-0, 0-1, yes-tap, tap-junction, no-junction, junction-2, 2-end
        self.assertEqual(len(flows), 7)

    def test_a_decision_runs_one_branch_and_the_run_rejoins_under_it(self):
        layout = fl.layout(self.NODES)
        decision, tap, after = layout.box_of((1,)), layout.box_of((1, fl.THEN, 0)), layout.box_of((2,))
        self.assertLess(decision.bottom, tap.y)                                         # the branch is under it
        self.assertLess(tap.cx, decision.cx)                                            # "yes" on the left
        ((owner, jx, jy),) = layout.junctions
        self.assertEqual(owner, (1,))
        self.assertTrue(tap.bottom < jy < after.y)                                      # rejoined below the branch
        labelled = [e for e in layout.edges if e.label in (fl.THEN, fl.ELSE)]
        self.assertEqual({e.label for e in labelled}, {fl.THEN, fl.ELSE})
        self.assertTrue(all(abs(e.points[0][1] - decision.bottom) < 1e-6 for e in labelled))   # from its bottom

    def test_only_a_wait_has_a_time_out_exit_and_nothing_else_is_invented(self):
        layout = fl.layout(self.NODES)
        exits = [b for b in layout.boxes if b.role == fl.EXIT]
        self.assertEqual([b.path for b in exits], [(0,)])
        self.assertTrue(exits[0].right < layout.box_of((0,)).x)
        self.assertEqual({e.role for e in layout.edges}, {"flow", "exit"})

    def test_every_arrow_has_a_plus_that_says_where_a_new_block_would_go(self):
        layout = fl.layout(self.NODES)
        slots = {e.slot for e in layout.edges if e.slot is not None}
        # none into the click on the match at (1, THEN, 0): nothing may go before it (it clicks right after the look)
        self.assertEqual(slots, {(0,), (1,), (1, fl.THEN, 1), (1, fl.ELSE, 0), (2,), (3,)})
        for edge in layout.edges:
            if edge.plus is not None:
                self.assertEqual(layout.plus_at(*edge.plus), edge.slot)                # the "+" is what is hit
        self.assertIsNone(layout.plus_at(-50, -50))

    def test_a_new_block_is_shown_where_it_will_be_inserted_and_no_other_is_offered_meanwhile(self):
        layout = fl.layout(self.NODES, pending=((1,), fl.RUN, None))
        pending = next(b for b in layout.boxes if b.role == fl.PENDING)
        self.assertTrue(layout.box_of((0,)).bottom < pending.y < layout.box_of((1,)).y)
        self.assertTrue(all(e.slot is None for e in layout.edges))
        inside = fl.layout(self.NODES, pending=((1, fl.ELSE, 0), fl.WAIT, None))
        pending = next(b for b in inside.boxes if b.role == fl.PENDING)
        self.assertGreater(pending.y, inside.box_of((1,)).bottom)                        # in the "no" branch
        self.assertGreater(pending.cx, inside.box_of((1,)).cx)

    def test_an_empty_script_offers_a_placeholder_and_a_click_finds_the_block_and_its_part(self):
        empty = fl.layout(())
        placeholder = next(b for b in empty.boxes if b.role == fl.PLACEHOLDER)
        self.assertIs(empty.hit(placeholder.cx, placeholder.cy), placeholder)
        layout = fl.layout(self.NODES)
        tap = layout.box_of((1, fl.THEN, 0))
        self.assertEqual(layout.hit(tap.cx, tap.cy).path, (1, fl.THEN, 0))
        self.assertIsNone(layout.hit(-50, -50))
        wait = layout.box_of((0,))
        self.assertEqual(wait.part_at(wait.x + 120, wait.y + 40), "condition")
        self.assertEqual(wait.part_at(wait.x + 120, wait.y + 62), "timing")
        self.assertEqual(wait.part_at(wait.x + 120, wait.y + 10), "")                   # its title: the block
        start = layout.box_of(fl.START_KEY)
        self.assertEqual(start.part_at(start.cx, start.cy), "delay")

    def test_a_placed_block_is_drawn_where_it_was_put_and_its_arrows_follow_it(self):
        nodes = self.NODES[:2] + (fl.Node((2,), fl.RUN, (600, 40)),)
        layout = fl.layout(nodes)
        moved = layout.box_of((2,))
        self.assertEqual((moved.x, moved.y), (600, 40))
        into = next(e for e in layout.edges if e.slot == (2,))
        self.assertEqual(into.points[-1], (moved.cx, moved.y))                          # it ends on its top
        dragged = fl.layout(nodes, overrides={(2,): (700, 300)})
        into = next(e for e in dragged.edges if e.slot == (2,))
        self.assertEqual(into.points[-1], (700 + fl.STEP_W / 2, 300))                   # and follows a drag
        self.assertEqual(dragged.descendants((1,)), ((1, fl.THEN, 0),))

    def test_arrows_are_smooth_curves_straight_when_aligned_a_loop_when_the_next_block_is_above(self):
        down = fl.curve((100, 0), (100, 200))
        self.assertTrue(all(abs(x - 100) < 1e-6 for x, _y in down))
        self.assertEqual(fl.bezier(down, 0.5), (100.0, 100.0))
        up = fl.curve((100, 300), (400, 100))
        self.assertGreater(up[1][1], 300)                                               # leaves downwards
        self.assertLess(up[2][1], 100)                                                  # arrives from above

    def test_room_is_made_under_a_block_placed_by_hand(self):
        nodes = tuple(fl.Node((i,), fl.RUN, (100, 100 + i * 120)) for i in range(3))
        layout = fl.layout(nodes, start_at=(100, 10), end_at=(100, 500))
        moves = fl.make_room(nodes, layout, (100, 220))
        self.assertEqual(set(moves), {(1,), (2,), fl.END_KEY})
        self.assertTrue(all(y > layout.box_of(p).y for p, (_x, y) in moves.items()))

    def test_fit_to_view_is_computed_from_the_real_size_of_the_script(self):
        small, big, huge = fl.layout(self.NODES[:1]), fl.layout(_copies(self.NODES, 2)), \
            fl.layout(_copies(self.NODES, 12))
        view = (600, 400)
        self.assertGreater(fl.fit_scale(small.width, small.height, *view), fl.fit_scale(big.width, big.height, *view))
        scale = fl.fit_scale(big.width, big.height, *view)
        self.assertLessEqual(big.height * scale, view[1])                # the whole script is in view ...
        self.assertLessEqual(big.width * scale, view[0])
        self.assertEqual(fl.fit_scale(huge.width, huge.height, *view), 0.3)   # ... down to a readable floor
        self.assertEqual(fl.zoom_step(1.0, +1), 1.1)
        self.assertEqual(fl.zoom_step(1.0, -1), 0.9)
        self.assertEqual(fl.zoom_step(fl.ZOOM_STEPS[-1], +1), fl.ZOOM_STEPS[-1])

    def test_nothing_an_automatic_layout_draws_covers_anything_else(self):
        """Every combination of a few branch contents, nested two deep: no block, Start, End or exit marker covers
        another, and no "+" sits on a block. (A WAIT's exit marker in a "no" branch used to cover the "yes" branch.)"""
        T, E = fl.THEN, fl.ELSE
        leaves = ((), (fl.RUN,), (fl.WAIT,), (fl.TAP, fl.WAIT), (fl.WAIT, fl.RUN))

        def column(kinds, prefix):
            return tuple(fl.Node(prefix + (i,), kind) for i, kind in enumerate(kinds))

        shapes = []
        for then in leaves:
            for otherwise in leaves:
                inner = fl.Node((1, E, 0), fl.IF, then=column(then, (1, E, 0, T)),
                                otherwise=column(otherwise, (1, E, 0, E)))
                shapes.append((fl.Node((0,), fl.WAIT),
                               fl.Node((1,), fl.IF, then=column(then, (1, T)), otherwise=(inner,)),
                               fl.Node((2,), fl.WAIT)))
        for nodes in shapes:
            layout = fl.layout(nodes)
            boxes = [b for b in layout.boxes if b.role in (fl.STEP, fl.EXIT, fl.START, fl.END)]
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    covered = a.x < b.right and b.x < a.right and a.y < b.bottom and b.y < a.bottom
                    self.assertFalse(covered and a.path != b.path, (a, b))
            for edge in layout.edges:
                if edge.plus is not None:
                    px, py = edge.plus
                    self.assertFalse(any(b.x <= px <= b.right and b.y <= py <= b.bottom for b in boxes), edge)

    def test_the_wheel_zooms_smoothly_within_its_bounds(self):
        self.assertAlmostEqual(fl.wheel_zoom(1.0, 1), fl.WHEEL_STEP)
        self.assertAlmostEqual(fl.wheel_zoom(1.0, -1), 1 / fl.WHEEL_STEP)
        self.assertEqual(fl.wheel_zoom(1.9, 10), fl.ZOOM_MAX)
        self.assertEqual(fl.wheel_zoom(0.31, -10), fl.ZOOM_MIN)


def _copies(nodes, times: int) -> tuple:
    """The same three blocks `times` over, each with its own path."""
    out = []
    for n in range(times):
        for node in nodes:
            i = node.path[0] + 3 * n
            then = tuple(fl.Node((i,) + child.path[1:], child.kind) for child in node.then)
            out.append(fl.Node((i,), node.kind, None, then))
    return tuple(out)


class PreviewGeometry(unittest.TestCase):
    def test_a_large_screenshot_is_shrunk_and_a_small_one_enlarged_by_whole_factors(self):
        self.assertEqual(geo.fit_factors(1920, 1080, 640, 400), (3, 1))
        self.assertEqual(geo.fit_factors(160, 90, 640, 400), (1, 4))
        self.assertEqual(geo.fit_factors(160, 90, 640, 400, max_zoom=2), (1, 2))

    def test_a_box_and_a_click_map_between_the_screenshot_and_the_preview_both_ways(self):
        for down, up in ((3, 1), (1, 2), (1, 1)):
            x0, y0, x1, y1 = geo.shown_box(300, 120, 60, 30, down, up)
            inside = geo.to_image_point_scaled((x0 + x1) / 2, (y0 + y1) / 2, down, up, 1920, 1080)
            self.assertTrue(300 <= inside[0] < 360 and 120 <= inside[1] < 150, (down, up, inside))
        self.assertEqual(geo.to_image_point_scaled(-5, 10_000, 2, 1, 100, 50), (0, 49))   # clamped inside


# ---- the controller ---------------------------------------------------------------------------------------------
class Library(gui_tests.Base):
    def saved(self, name):
        self.record()
        self.assertTrue(self.c.save(name), self.c.status)
        self.c.refresh_macros()
        return next(i.key for i in self.c.macros if i.name == name)

    def test_macros_and_scripts_are_two_parts_of_one_library(self):
        recorded_key = self.saved("Farm")
        script = self.library.key_of(self.library.save(flow("Collect")))
        self.library.directory.joinpath("broken.macro.json").write_text("{", encoding="utf-8")
        self.c.refresh_macros()
        self.assertTrue(self.c.set_library_kind(KIND_MACROS))
        self.assertEqual({i.key for i in self.c.visible_macros()}, {recorded_key, "broken.macro.json"})
        self.assertTrue(self.c.set_library_kind(KIND_SCRIPTS))
        self.assertEqual([i.key for i in self.c.visible_macros()], [script])

    def test_each_part_remembers_what_was_chosen_in_it(self):
        farm = self.saved("Farm")
        script = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.c.set_library_kind(KIND_MACROS)
        self.c.select_macro(farm)
        self.c.set_library_kind(KIND_SCRIPTS)
        self.assertEqual(self.c.selected_key, "")                     # a recorded macro is not a script
        self.c.select_macro(script)
        self.c.set_library_kind(KIND_MACROS)
        self.assertEqual(self.c.selected_key, farm)
        self.c.set_library_kind(KIND_SCRIPTS)
        self.assertEqual(self.c.selected_key, script)


class TargetsBelongToTheirMacro(Library):
    def test_returning_to_a_macro_brings_back_its_own_targets_never_another_macros(self):
        first, second = self.saved("First"), self.saved("Second")
        notes, calculator = self.entry("Notes").ref, self.entry("Calculator").ref
        self.c.select_macro(first)
        self.c.set_run_refs([notes])
        self.c.select_macro(second)
        self.assertEqual(self.c.run_refs, ())                         # not inherited
        self.c.set_run_refs([calculator])
        self.c.select_macro(first)
        self.assertEqual(self.c.run_refs, (notes,))                   # its own, chosen for it
        self.assertTrue(self.c.can_run())
        self.c.select_macro(second)
        self.assertEqual(self.c.run_refs, (calculator,))

    def test_a_macro_replaced_by_a_new_recording_must_have_its_targets_chosen_again(self):
        key = self.saved("Farm")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.record(clicks=2)                                          # a different recording ...
        self.assertTrue(self.c.save("Farm", overwrite=True), self.c.status)     # ... saved over the old one
        self.assertEqual(self.c.status.key, "msg_saved_targets_cleared")
        self.assertEqual(self.c.run_refs, ())
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, ())                         # and not brought back either
        self.assertFalse(self.c.can_run())
        self.assertEqual(self.c.suggested_run_refs(), (self.entry("Notes").ref,))   # only offered, to confirm

    def test_a_macro_changed_in_the_editor_loses_its_targets_an_untouched_save_keeps_them(self):
        key = self.saved("Farm")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.open_editor(key))
        self.assertTrue(self.c.save_editor(), self.c.status)           # nothing changed: nothing to distrust
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))
        self.c.editor_apply(macro_edit.set_point, 0, 12, 34)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertEqual(self.c.status.key, "msg_saved_targets_cleared")
        self.assertEqual(self.c.run_refs, ())

    def test_saving_an_edited_copy_never_overwrites_the_file_it_was_copied_from(self):
        key = self.saved("Farm")
        original = self.library.directory / key
        copy = original.with_name("farm - copy.macro.json")
        copy.write_bytes(original.read_bytes())                       # copied in the file manager: same name inside
        before = original.read_bytes()
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.open_editor(self.library.key_of(copy)))
        self.c.editor_apply(macro_edit.set_point, 0, 12, 34)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertEqual(original.read_bytes(), before)               # the file it was copied from: untouched
        self.assertNotEqual(copy.read_bytes(), before)                # the change went where it was made
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))  # and the original keeps its own targets
        self.assertTrue(self.c.can_run())

    def test_after_a_refresh_a_target_stays_chosen_only_while_its_token_names_the_same_target(self):
        first, second = self.saved("First"), self.saved("Second")
        notes, calculator = self.entry("Notes").ref, self.entry("Calculator").ref
        self.c.select_macro(first)
        self.c.set_run_refs([notes])
        self.c.select_macro(second)
        self.c.set_run_refs([notes, calculator])
        self.assertTrue(self.c.select(calculator))                    # (the recording target is not the one replaced)
        self.assertTrue(self.c.refresh())                             # the same windows: everything stays chosen
        self.assertEqual(self.c.run_refs, (notes, calculator))
        self.world.windows[100] = info(hwnd=100, title="Bank", name="bank.exe", cls="BankWnd", pid=20)
        self.assertTrue(self.c.refresh())                             # that handle now names another window
        self.assertEqual(self.c.status.key, "msg_found_dropped")
        self.assertEqual(self.c.run_refs, (calculator,))
        self.c.select_macro(first)
        self.assertEqual(self.c.run_refs, ())                         # not brought back for the other macro either
        self.assertNotIn(notes, self.c.suggested_run_refs())          # nor offered: it is not that window any more
        self.world.windows.pop(101)
        self.assertTrue(self.c.refresh())
        self.c.select_macro(second)
        self.assertEqual(self.c.run_refs, ())                         # a vanished target is never kept as a token
        self.assertFalse(self.c.can_run())

    def test_a_file_replaced_outside_the_application_must_have_its_targets_chosen_again(self):
        key, other = self.saved("Farm"), self.saved("Other")
        notes = self.entry("Notes").ref
        self.c.select_macro(key)
        self.c.set_run_refs([notes])
        self.assertTrue(self.c.can_run())
        (self.library.directory / key).write_bytes((self.library.directory / other).read_bytes())  # copied over it
        self.assertFalse(self.c.can_run())                            # what would run is not what they were chosen for
        self.assertEqual(self.c.why_cannot_run(), "msg_run_targets_file_changed")
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.key, "msg_run_targets_file_changed")
        self.c.refresh_macros()
        self.assertEqual(self.c.run_refs, ())                         # dropped as soon as the list is read again
        self.assertEqual(self.c.suggested_run_refs(), (notes,))       # only offered, to confirm

    def replaced_outside(self, name="Farm", by="Other"):
        """`name` with Notes chosen for it, then its file replaced by `by`'s in the file manager."""
        key, other = self.saved(name), self.saved(by)
        notes = self.entry("Notes").ref
        self.c.select_macro(key)
        self.c.set_run_refs([notes])
        (self.library.directory / key).write_bytes((self.library.directory / other).read_bytes())
        return key, other, notes

    def test_choosing_a_macro_whose_file_was_replaced_outside_only_offers_its_old_targets(self):
        key, other, notes = self.replaced_outside()
        self.c.select_macro(other)
        self.c.select_macro(key)                                      # no list refresh in between
        self.assertEqual(self.c.run_refs, ())
        self.assertEqual(self.c.suggested_run_refs(), (notes,))

    def test_renaming_or_saving_a_file_replaced_outside_does_not_carry_its_old_targets(self):
        key, _, _ = self.replaced_outside()
        self.assertTrue(self.c.rename_macro(key, "Farm 2"), self.c.status)
        self.assertEqual(self.c.run_refs, ())                         # renaming never launders the change
        key, _, _ = self.replaced_outside("Mine", "Theirs")
        self.assertTrue(self.c.open_editor(key))                      # opens what the file holds NOW
        self.assertTrue(self.c.save_editor(), self.c.status)          # saved unchanged: still not what they were for
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, ())
        self.assertFalse(self.c.can_run())

    def test_a_refresh_never_suggests_a_token_that_now_names_another_window(self):
        first, second = self.saved("First"), self.saved("Second")
        notes = self.entry("Notes").ref
        self.c.select_macro(first)
        self.c.set_run_refs([notes])
        self.c.select_macro(second)                                   # Notes is now only the suggestion
        self.assertEqual(self.c.suggested_run_refs(), (notes,))
        self.assertTrue(self.c.select(self.entry("Calculator").ref))
        self.world.windows[100] = info(hwnd=100, title="Bank", name="bank.exe", cls="BankWnd", pid=20)
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.suggested_run_refs(), ())

    def test_targets_cannot_be_chosen_for_a_version_of_the_file_that_was_not_shown(self):
        key, other = self.saved("Farm"), self.saved("Other")
        notes = self.entry("Notes").ref
        self.c.select_macro(key)
        (self.library.directory / key).write_bytes((self.library.directory / other).read_bytes())  # after the list
        self.assertFalse(self.c.set_run_refs([notes]))                # what would run is not what is shown
        self.assertEqual(self.c.status.key, "msg_run_targets_look_again")
        self.assertEqual(self.c.run_refs, ())
        self.assertEqual(self.c.selected_macro().stamp, file_stamp(self.library.directory / key))  # list read again
        self.assertTrue(self.c.set_run_refs([notes]), self.c.status)  # now chosen for what the details show
        self.assertTrue(self.c.can_run())

    def chosen_for_farm(self):
        key = self.saved("Farm")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.can_run())
        return key, self.library.directory / key

    def test_the_same_bytes_rewritten_later_in_place_are_another_version(self):
        _, path = self.chosen_for_farm()
        before = path.stat()
        with open(path, "r+b") as handle:                             # the same file, the same size ...
            handle.write(path.read_bytes())
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000))   # ... written 10 ms later
        after = path.stat()
        self.assertEqual((after.st_size, after.st_ino), (before.st_size, before.st_ino))
        self.assertFalse(self.c.can_run())

    def test_a_file_made_again_with_the_same_bytes_and_time_is_another_version(self):
        _, path = self.chosen_for_farm()
        before, data = path.stat(), path.read_bytes()
        path.unlink()
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        if after.st_ino == before.st_ino:
            self.skipTest("this file system gives a file made again the same id")
        self.assertEqual((after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertFalse(self.c.can_run())                            # only its file id tells it apart

    def test_moving_a_macro_keeps_its_targets_unless_its_file_was_replaced_outside(self):
        key, _ = self.chosen_for_farm()
        notes = self.entry("Notes").ref
        self.assertTrue(self.c.create_folder("Daily"), self.c.status)
        self.assertTrue(self.c.move_macro(key, "Daily"), self.c.status)
        moved = self.c.selected_key
        self.assertEqual(self.c.run_refs, (notes,))                   # the same file in another folder
        self.assertTrue(self.c.can_run())
        other = self.saved("Other")
        (self.library.directory / moved).write_bytes((self.library.directory / other).read_bytes())
        self.assertTrue(self.c.move_macro(moved, ""), self.c.status)
        self.assertEqual(self.c.run_refs, ())                         # replaced outside before the move: not carried

    def test_a_refresh_that_finds_the_chosen_target_stale_also_says_run_targets_were_dropped(self):
        self.chosen_for_farm()                                        # Notes: the recording target and a run target
        self.world.windows[100] = info(hwnd=100, title="Bank", name="bank.exe", cls="BankWnd", pid=20)
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.status.key, "msg_found_stale_dropped")
        self.assertTrue(any("dropped" in line for line in self.c.log))

    def test_a_macro_renamed_while_it_runs_keeps_that_run_in_its_history(self):
        key, _ = self.chosen_for_farm()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertTrue(self.c.rename_macro(key, "Farm 2"), self.c.status)
        renamed = self.c.selected_key
        deadline = time.monotonic() + 10
        while self.c.run_active and time.monotonic() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.c.poll()
        self.assertFalse(self.c.run_active, self.c.run_results)
        self.assertEqual([r.macro_key for r in self.c.history.for_macro(renamed)], [renamed])

    def test_an_untouched_save_writes_nothing_so_a_newer_version_saved_outside_stays(self):
        key, other = self.saved("Farm"), self.saved("Other")
        path = self.library.directory / key
        self.assertTrue(self.c.open_editor(key))
        newer = (self.library.directory / other).read_bytes()
        path.write_bytes(newer)                                       # replaced outside while it is open here
        self.assertTrue(self.c.save_editor(), self.c.status)          # nothing was edited here: nothing is written
        self.assertEqual(self.c.status.key, "msg_saved_unchanged")
        self.assertEqual(path.read_bytes(), newer)

    def test_an_edited_save_never_silently_replaces_a_version_saved_outside_meanwhile(self):
        key, other = self.saved("Farm"), self.saved("Other")
        path = self.library.directory / key
        self.assertTrue(self.c.open_editor(key))
        self.c.editor_apply(macro_edit.set_point, 0, 12, 34)
        newer = (self.library.directory / other).read_bytes()
        path.write_bytes(newer)
        self.assertFalse(self.c.save_editor())
        self.assertEqual((self.c.status.category, self.c.status.key), ("changed_outside", "msg_editor_changed_outside"))
        self.assertEqual(path.read_bytes(), newer)                    # the outside version is kept ...
        self.assertTrue(self.c.editor_dirty)                          # ... and so is the work done here
        self.assertTrue(self.c.save_editor(replace_outside=True), self.c.status)   # only when the person says so
        self.assertNotEqual(path.read_bytes(), newer)
        self.c.editor_apply(macro_edit.set_point, 0, 56, 78)
        self.assertTrue(self.c.save_editor(), self.c.status)          # its own last save is not an outside change

    def test_a_new_macro_saved_under_the_name_of_a_deleted_one_never_inherits_its_targets(self):
        key = self.saved("Farm")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        (self.library.directory / key).unlink()                       # deleted in the file manager
        self.record(clicks=2)
        self.assertTrue(self.c.save("Farm"), self.c.status)           # a new file under the same name
        self.assertEqual(self.c.status.key, "msg_saved_targets_cleared")
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, ())
        self.assertFalse(self.c.can_run())

    def test_saving_the_same_recording_over_itself_keeps_its_targets(self):
        key = self.saved("Farm")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.save("Farm", overwrite=True), self.c.status)   # nothing different was saved
        self.assertEqual(self.c.status.key, "msg_saved")
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))
        self.assertTrue(self.c.can_run())


class NewScript(gui_tests.Base):
    def v2_files(self):
        return [i for i in self.library.list() if i.version == 2]

    def test_a_new_script_writes_nothing_until_it_has_a_step_and_is_saved(self):
        self.assertTrue(self.c.new_script("Daily"), self.c.status)
        self.assertTrue(self.c.editing_new and self.c.editing_v2)
        self.assertFalse(self.c.editor_dirty)
        self.assertEqual(self.v2_files(), [])
        self.assertFalse(self.c.save_editor())                         # empty: refused, in words
        self.assertEqual(self.c.status.key, "err_script_empty")
        self.assertEqual(self.v2_files(), [])
        step = RunMacro(recorded())
        self.assertTrue(self.c.editor_apply(fe.insert_step, 0, step))
        self.assertTrue(self.c.save_editor(), self.c.status)
        files = self.v2_files()
        self.assertEqual([i.name for i in files], ["Daily"])
        self.assertEqual(self.library.load(files[0].path).steps, (step,))   # what was saved is what reopens
        self.assertFalse(self.c.editing_new)
        self.assertEqual(self.c.selected_key, files[0].key)

    def test_a_new_script_cannot_start_under_a_name_that_is_already_used(self):
        path = self.library.save(flow("Daily"))
        before = path.read_bytes()
        self.assertFalse(self.c.new_script("Daily"))                  # refused before any work is put into it
        self.assertEqual(self.c.status.category, "exists")
        self.assertIsNone(self.c.editor)
        self.assertEqual(path.read_bytes(), before)

    def test_a_name_taken_while_the_script_was_built_is_replaced_and_the_work_is_kept(self):
        self.assertTrue(self.c.new_script("Daily"), self.c.status)
        step = RunMacro(recorded())
        self.assertTrue(self.c.editor_apply(fe.insert_step, 0, step))
        taken = self.library.save(flow("Daily"))                      # meanwhile: a file under that name
        before = taken.read_bytes()
        self.assertFalse(self.c.save_editor())                        # never written over ...
        self.assertEqual(self.c.status.category, "exists")
        self.assertTrue(self.c.editing_new and self.c.editor_dirty)   # ... and nothing lost: still open, unsaved
        self.assertEqual(self.c.free_script_name(), "Daily 2")
        self.assertFalse(self.c.rename_new_script("Daily"))           # still taken
        self.assertEqual(self.c.status.category, "exists")
        self.assertTrue(self.c.rename_new_script("Daily 2"), self.c.status)
        self.assertTrue(self.c.save_editor(), self.c.status)
        self.assertEqual(taken.read_bytes(), before)
        saved = self.library.load(self.library.path_for("Daily 2"))
        self.assertEqual((saved.name, saved.steps), ("Daily 2", (step,)))
        self.assertTrue(self.c.editor_undo())
        self.assertEqual(self.c.editor.document.name, "Daily 2")     # undo never brings the taken name back

    def test_an_untouched_new_script_leaves_nothing_behind_and_a_name_is_required(self):
        self.c.new_script("Nothing")
        self.c.close_editor()
        self.assertEqual(self.v2_files(), [])
        self.assertFalse(self.c.new_script("   "))
        self.assertEqual(self.c.status.key, "err_script_name")


class ReadingWithBoxes(gui_tests.Base):
    def setUp(self):
        super().setUp()
        self.ocr = CountingOcr()
        self.c = AppController(self.services, self.library, ocr=self.ocr)

    def test_the_reading_keeps_every_box_exactly_and_the_picture_it_belongs_to(self):
        self.pick("Notes")
        job = self.c.begin_read_text()
        self.assertTrue(self.c.finish_read_text(job(), job), self.c.status)
        reading = self.c.reading
        self.assertEqual(reading.blocks, (ReadBlock("Hello", 1, 1, 5, 5),))
        self.assertEqual(reading.blocks[0].center, (3, 3))
        picture = decode_png(reading.png)
        frame = self.ocr.frames[0]
        self.assertEqual((picture.width, picture.height), (frame.width, frame.height))
        self.assertEqual(picture, frame.bitmap)                       # the very pixels the boxes were found in

    def test_an_answer_without_the_picture_it_was_read_from_is_never_shown(self):
        self.pick("Notes")
        job = self.c.begin_read_text()
        result = OcrResult((OcrTextBlock("Hello", Roi(1, 1, 5, 5), 1.0),), confidence_measured=False)
        self.assertFalse(self.c.finish_read_text(result, job))       # where it came from cannot be checked
        self.assertEqual(self.c.reading.state, READ_FAILED)
        self.assertEqual((self.c.reading.lines, self.c.reading.blocks), ((), ()))

    def test_a_picture_of_another_target_is_never_shown_with_this_targets_text(self):
        self.pick("Calculator")
        screen_job = self.c.begin_screen()
        other_frame = self.c.finish_screen(screen_job(), screen_job).frame
        self.pick("Notes")
        job = self.c.begin_read_text()
        result = OcrResult((OcrTextBlock("Hello", Roi(1, 1, 5, 5), 1.0),), confidence_measured=False)
        self.assertFalse(self.c.finish_read_text((other_frame, result, b"png"), job))
        self.assertEqual((self.c.reading.state, self.c.reading.reason), (READ_FAILED, "target"))
        self.assertEqual(self.c.reading.blocks, ())


class DesktopCaptureProvider(unittest.TestCase):
    def setUp(self):
        self.desk = FakeDesktop(info())
        self.stop = EmergencyStop()
        self.target = FullWindowsTarget(time.monotonic(), self.desk.desktop)

    def test_it_captures_the_whole_simulated_desktop_and_stamps_the_desktop_target(self):
        shot = DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()
        self.assertEqual((shot.width, shot.height), self.desk.desktop[2:])
        self.assertEqual((shot.source.provider_id, shot.source.target_id), ("windows", desktop_id(self.desk.desktop)))
        self.assertEqual((self.desk.desktop_captures, self.desk.captures), (1, 0))

    def test_a_picture_from_another_screen_layout_is_refused(self):
        """Its pixels would be stamped with the layout the target was chosen in, and a click computed from them
        would land elsewhere."""
        self.desk.desktop = (0, 0, 200, 90)                          # a monitor changed since Full Windows was chosen
        with self.assertRaises(WindowCaptureError):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()

    def test_the_emergency_stop_comes_before_any_capture(self):
        self.stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()
        self.assertEqual(self.desk.desktop_captures, 0)

    def test_a_black_or_inconsistent_picture_is_refused(self):
        self.desk.pixel = (0, 0, 0)
        with self.assertRaises(WindowCaptureError):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()
        self.desk.pixel = (30, 60, 200)
        self.desk.capture_desktop = lambda max_side: DesktopCapture(0, 0, RawBitmap(10, 10, b"\x01" * 12))
        with self.assertRaises(WindowCaptureError):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()

    def test_the_backend_is_told_the_largest_desktop_that_will_be_used_before_it_allocates_anything(self):
        asked = []
        capture = self.desk.capture_desktop
        self.desk.capture_desktop = lambda max_side: (asked.append(max_side), capture(max_side))[1]
        DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()
        self.assertEqual(asked, [MAX_SIDE])
        self.desk.desktop = (0, 0, MAX_SIDE + 1, 10)                 # three wide monitors, say
        with self.assertRaises(WindowCaptureError):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()

    def test_a_stop_pressed_during_the_capture_is_obeyed_before_the_picture_is_used(self):
        capture = self.desk.capture_desktop

        def stopped_meanwhile(max_side):
            shot = capture(max_side)
            self.stop.trigger("test")
            return shot
        self.desk.capture_desktop = stopped_meanwhile
        with self.assertRaises(EmergencyStopped):
            DesktopScreenshotProvider(self.target, self.desk, self.stop).capture()

    def test_only_the_full_windows_target_is_captured_this_way(self):
        from app.windows.target import WindowVerifier
        window = WindowVerifier(self.desk, self.stop).verify(100)
        with self.assertRaises(TargetNotVerified):
            DesktopScreenshotProvider(window, self.desk, self.stop)


class FakeGdi:
    """The screen and drawing calls `Win32Backend.capture_desktop` makes, replaced: they hand out numbered handles,
    remember which are still held and what is selected into which device context, and touch nothing real."""
    SCREEN_DC, MEMORY_DC, BITMAP, DEFAULT_BITMAP = 11, 22, 33, 44

    def __init__(self, width, height, fail=""):
        from app.windows import win32
        self.metrics = {win32._SM_XVIRTUALSCREEN: -10, win32._SM_YVIRTUALSCREEN: 0,
                        win32._SM_CXVIRTUALSCREEN: width, win32._SM_CYVIRTUALSCREEN: height}
        self.fail, self.calls, self.held, self.selected = fail, [], set(), {}
        self.read_while_selected = self.deleted_while_selected = False

    def _take(self, name, handle):
        self.calls.append(name)
        if self.fail == name:
            return 0
        self.held.add(handle)
        return handle

    def ScreenMetric(self, index):
        return self.metrics[index]

    def GetDC(self, _window):
        return self._take("GetDC", self.SCREEN_DC)

    def ReleaseDC(self, _window, dc):
        self.calls.append("ReleaseDC")
        self.held.discard(dc)
        return 1

    def CreateCompatibleDC(self, _dc):
        handle = self._take("CreateCompatibleDC", self.MEMORY_DC)
        if handle:
            self.selected[handle] = self.DEFAULT_BITMAP
        return handle

    def CreateCompatibleBitmap(self, _dc, _width, _height):
        return self._take("CreateCompatibleBitmap", self.BITMAP)

    def SelectObject(self, dc, thing):
        self.calls.append("SelectObject")
        if self.fail == "SelectObject" and thing == self.BITMAP:
            return 0
        previous, self.selected[dc] = self.selected.get(dc), thing
        return previous

    def BitBlt(self, *_args):
        self.calls.append("BitBlt")
        return 0 if self.fail == "BitBlt" else 1

    def GetDIBits(self, _dc, bitmap, _start, lines, buffer, _info, _usage):
        self.calls.append("GetDIBits")
        self.read_while_selected = bitmap in self.selected.values()
        if self.fail == "GetDIBits":
            return 0
        ctypes.memset(buffer, 0x5A, ctypes.sizeof(buffer))
        return lines

    def DeleteObject(self, thing):
        self.calls.append("DeleteObject")
        self.deleted_while_selected |= thing in self.selected.values()
        self.held.discard(thing)
        return 1

    def DeleteDC(self, dc):
        self.calls.append("DeleteDC")
        self.held.discard(dc)
        self.selected.pop(dc, None)
        return 1


@unittest.skipUnless(sys.platform == "win32", "the Win32 backend's own code (its system calls are replaced)")
class Win32DesktopCapture(unittest.TestCase):
    """The real `Win32Backend.capture_desktop`, with every system call it makes replaced by `FakeGdi`: nothing
    reaches the real screen. Whatever it takes is given back on every path, the bitmap is out of its device context
    before its pixels are read (GetDIBits' own rule), and a desktop too large is refused before anything is taken."""

    def backend(self, width=40, height=30, fail=""):
        from app.windows import win32
        gdi = FakeGdi(width, height, fail)
        backend = win32.Win32Backend.__new__(win32.Win32Backend)       # no real library is loaded
        backend._SetThreadDpi = None
        for name in ("ScreenMetric", "GetDC", "ReleaseDC", "CreateCompatibleDC", "CreateCompatibleBitmap",
                     "SelectObject", "BitBlt", "GetDIBits", "DeleteObject", "DeleteDC"):
            setattr(backend, f"_{name}", getattr(gdi, name))
        return backend, gdi

    def test_it_reads_the_virtual_screen_and_gives_back_everything_it_took(self):
        backend, gdi = self.backend()
        shot = backend.capture_desktop(max_side=MAX_SIDE)
        self.assertEqual((shot.left, shot.top, shot.bitmap.width, shot.bitmap.height), (-10, 0, 40, 30))
        self.assertEqual(shot.bitmap.bgra, b"\x5a" * (40 * 30 * 4))
        self.assertEqual(gdi.held, set())
        self.assertFalse(gdi.read_while_selected)                    # taken out of the context before it is read
        self.assertFalse(gdi.deleted_while_selected)

    def test_every_failure_gives_back_what_was_taken_and_says_so(self):
        for fail in ("GetDC", "CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject", "BitBlt", "GetDIBits"):
            with self.subTest(fail=fail):
                backend, gdi = self.backend(fail=fail)
                with self.assertRaises(WindowCaptureError):
                    backend.capture_desktop(max_side=MAX_SIDE)
                self.assertEqual(gdi.held, set())
                self.assertFalse(gdi.deleted_while_selected)

    def test_a_desktop_larger_than_will_be_used_is_refused_before_anything_is_taken(self):
        backend, gdi = self.backend(width=11520, height=2160)       # three 4K monitors side by side
        with self.assertRaises(WindowCaptureError):
            backend.capture_desktop(max_side=MAX_SIDE)
        self.assertEqual(gdi.calls, [])                              # no device context, no bitmap, no buffer


# ---- the window ---------------------------------------------------------------------------------------------------
@unittest.skipIf(tk is None, "tkinter is not available")
class Window(gui_tests.Base):
    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.ocr = CountingOcr()
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                       preview_runner=lambda fn: fn(), ocr=self.ocr)
        self.c = self.app.controller
        self.c.refresh()
        self.addCleanup(self.app.close)
        self.editor = self.app.screens["editor"]

    def stub_dialog(self, name, value):
        from app.gui.view import dialogs

        class Stub:
            def __init__(self, *args, **kwargs):
                pass

            def show(self):
                return value
        original = getattr(dialogs, name)
        setattr(dialogs, name, Stub)
        self.addCleanup(setattr, dialogs, name, original)


class Notifications(Window):
    def test_routine_results_leave_by_themselves_errors_stay_until_closed(self):
        toaster = self.app.toaster
        ok = toaster.show("ok", "Saved.")
        self.assertIsNotNone(ok.duration_ms)
        self.assertGreaterEqual(ok.duration_ms, 4000)
        error = toaster.show("error", "It could not be saved.")
        self.assertIsNone(error.duration_ms)
        long = toaster.show("info", "x" * 200)
        self.assertGreater(long.duration_ms, ok.duration_ms)          # a longer sentence stays longer

    def test_a_newer_sentence_about_the_same_thing_replaces_the_older_and_few_stay_at_once(self):
        toaster = self.app.toaster
        toaster.dismiss_all()
        toaster.show("info", "Reading the text…", key="text")
        toaster.show("ok", "Read 3 lines.", key="text")
        self.assertEqual([t.text for t in toaster.toasts], ["Read 3 lines."])
        for i in range(4):
            toaster.show("info", f"note {i}")
        self.assertLessEqual(len(toaster.toasts), 2)

    def test_the_controllers_sentences_become_notifications_and_the_stop_clears_them(self):
        self.c.note("ok", "storage", "Copied to the clipboard.")
        self.app.render()
        self.assertIn("Copied to the clipboard.", [t.text for t in self.app.toaster.toasts])
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        self.app.render()
        self.app._on_estop()
        self.assertEqual(self.app.toaster.toasts, [])                 # the stop bar says it; old notes are gone
        self.assertTrue(self.app.stop_bar.winfo_manager())


class LargeWindow(Window):
    """What made a maximised window lag (seconds per redraw on a 3440 x 1440 display), kept from coming back."""

    def test_stretched_pictures_are_large_but_controls_still_ask_for_their_small_size(self):
        from tkinter import ttk
        tk_call = self.root.tk.call
        self.assertGreaterEqual(int(tk_call("image", "width", "olive_card")), skin.STRETCH)
        self.assertGreaterEqual(int(tk_call("image", "height", "olive_card")), skin.STRETCH)
        self.assertGreaterEqual(int(tk_call("image", "width", "olive_btn_primary_normal")), skin.STRETCH)
        card = ttk.Frame(self.root, style="Card.TFrame")
        button = ttk.Button(self.root, text="OK", style="Primary.TButton")
        field = ttk.Entry(self.root, width=4)
        self.root.update_idletasks()
        self.assertLessEqual(card.winfo_reqwidth(), skin.CARD_SIZE[0] + 4)
        self.assertLess(button.winfo_reqwidth(), 120)
        self.assertLessEqual(button.winfo_reqheight(), skin.BUTTON_SIZE[1] + 8)
        self.assertLess(field.winfo_reqwidth(), 120)

    def test_only_the_page_being_looked_at_is_laid_out(self):
        for name in ("scripts", "text", "history", "settings", "macros"):
            self.app.show_screen(name)
            laid_out = [n for n, screen in self.app.screens.items() if screen.frame.winfo_manager()]
            self.assertEqual(laid_out, [name])

    def test_the_grid_is_one_picture_whatever_the_size_of_the_canvas(self):
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        view = self.editor.canvas_view
        view.canvas.winfo_width = lambda: 3400                       # as if maximised on a very wide display
        view.canvas.winfo_height = lambda: 1300
        view.redraw(force=True)
        grid = view.canvas.find_withtag("grid")
        self.assertEqual(len(grid), 1)
        self.assertLess(len(view.canvas.find_all()), 90)              # the blocks and arrows, not thousands of lines
        picture = view.canvas.itemcget(grid[0], "image")
        self.assertGreaterEqual(int(self.root.tk.call("image", "width", picture)), 3400)
        self.assertGreaterEqual(int(self.root.tk.call("image", "height", picture)), 1300)

    def test_the_target_list_takes_the_height_of_its_dialog(self):
        from app.gui.view import dialogs
        picker = dialogs.ChooseTargets(self.app, self.c.entries, (), single=True)
        self.addCleanup(picker.top.destroy)
        picker.top.withdraw()                                         # built, never shown
        self.assertEqual(set(picker.content.grid_info()["sticky"]), set("nsew"))
        self.assertEqual(int(picker.body.grid_rowconfigure(1)["weight"]), 1)
        canvas = next(child for child in picker.list_area.winfo_children() if isinstance(child, tk.Canvas))
        self.assertEqual(int(canvas.cget("height")), dialogs.TARGET_LIST_HEIGHT)
        self.assertEqual(set(picker.rows), {e.ref for e in self.c.entries})   # every group, every target


class Shell(Window):
    def test_the_header_names_the_page_and_says_what_it_is_for_or_where_you_are(self):
        self.app.show_screen("scripts")
        self.assertEqual(self.app.lbl_title.cget("text"), self.app.t("nav_scripts"))
        self.assertEqual(self.app.lbl_where.cget("text"), self.app.t("sub_scripts"))
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.app.screens["scripts"].select_row(key)
        self.assertEqual(self.app.lbl_where.cget("text"), f"{self.app.t('nav_scripts')}  /  Collect")

    def test_history_searches_like_the_macros_page_and_its_column_names_scripts_too(self):
        self.app.show_screen("history")
        history = self.app.screens["history"]
        self.assertTrue(str(history._search_icon.cget("image")))       # the same search field as on Macros
        for language, heading in (("en", "Macro or script"), ("ru", "Макрос или сценарий")):
            self.app.apply_language(language)
            self.app.render()
            self.assertEqual(history.tree.heading("macro", "text"), heading)

    def test_the_details_show_the_version_of_the_file_the_list_describes(self):
        from app.decide import Condition, ConditionalMacro, WaitUntil
        path = self.library.save(flow("Collect"))
        self.c.refresh_macros()
        self.app.show_screen("scripts")
        scripts = self.app.screens["scripts"]
        scripts.select_row(self.library.key_of(path))
        self.app.render()
        self.assertIn("the mark", scripts.lbl_steps.cget("text"))
        before = path.stat()
        changed = ConditionalMacro("Collect", (WaitUntil(Condition.text("Something else"), 5, 0.5),))
        self.library.save(changed, overwrite=True)                    # another version ...
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # ... with the old time
        self.c.refresh_macros()
        self.app.render()
        self.assertIn("Something else", scripts.lbl_steps.cget("text"))

    def test_a_page_never_names_an_item_of_the_other_page(self):
        """Found on the rendered window: the Macros page's header said "Macros / Collect rewards" (a script)
        while its details card rightly said nothing was chosen."""
        key = self.library.key_of(self.library.save(flow("Collect rewards")))
        self.c.refresh_macros()
        self.app.show_screen("macros")
        self.c.select_macro(key)                     # chosen by some other path than the Macros list
        self.app.render()
        self.assertIsNone(self.c.shown_macro())
        self.assertNotIn("Collect rewards", self.app.lbl_where.cget("text"))
        self.assertFalse(self.app.screens["macros"]._head.winfo_manager())    # details: nothing chosen
        self.app.show_screen("scripts")
        self.c.select_macro(key)
        self.app.render()
        self.assertEqual(self.c.shown_macro().key, key)
        self.assertIn("Collect rewards", self.app.lbl_where.cget("text"))

    def test_the_target_chip_says_which_target_is_chosen(self):
        self.app.render()
        self.assertEqual(self.app.btn_target.cget("text"), self.app.t("header_no_target"))
        self.pick("Notes")
        self.app.render()
        self.assertEqual(self.app.btn_target.cget("text"), self.app.t("header_target").format(name="Notes"))

    def test_recording_starts_only_on_the_macros_page_and_a_script_only_on_the_scripts_page(self):
        macros, scripts = self.app.screens["macros"], self.app.screens["scripts"]
        self.assertIsNotNone(macros.btn_record)
        self.assertIsNone(scripts.btn_record)


class NothingIsCutOff(Window):
    """Found on the rendered window in Russian: buttons wider than their card, names sliced at an edge, and a
    disabled Run button showing a white box instead of its glyph."""

    def test_a_long_name_is_shortened_with_an_ellipsis_to_the_room_it_has(self):
        label = tk.Label(self.root, font=("Segoe UI", 11))
        name = "Farm daily rewards and collect every item"
        whole = int(label.tk.call("font", "measure", label.cget("font"), name))
        from app.gui.view import widgets as w
        self.assertEqual(w.fit_text(label, name, whole), name)                  # fits: untouched
        short = w.fit_text(label, name, whole // 2)
        self.assertTrue(short.endswith(w.ELLIPSIS) and name.startswith(short[:-1]), short)
        self.assertLessEqual(int(label.tk.call("font", "measure", label.cget("font"), short)), whole // 2)
        self.assertEqual(w.fit_text(label, name, 1), w.ELLIPSIS)

    def test_the_create_and_detail_buttons_fit_their_cards_in_every_language(self):
        from app.gui.view.macros import DETAILS_WIDTH, NAV_WIDTH
        key = self.library.key_of(self.library.save(flow("Collect rewards")))
        self.c.refresh_macros()
        for language in ("en", "ru"):
            self.app.apply_language(language)
            for kind in ("macros", "scripts"):
                self.app.show_screen(kind)
                screen = self.app.screens[kind]
                if kind == "scripts":
                    screen.select_row(key)
                self.app.render()
                self.root.update_idletasks()
                create = screen.btn_record if screen.btn_record is not None else next(
                    b for b in screen.frame.winfo_children()[0].winfo_children()[0].winfo_children()
                    if b.winfo_class() == "TButton")
                self.assertLessEqual(create.winfo_reqwidth(), NAV_WIDTH - 2 * 14 - 2, (language, kind))
                room = DETAILS_WIDTH - 2 * 18 - 4
                edit, dry = screen.btn_edit.winfo_reqwidth(), screen.btn_dry.winfo_reqwidth()
                if screen._extra_side:
                    self.assertLessEqual(edit + dry + 8, room, (language, kind))
                else:                                       # stacked: each one alone fits
                    self.assertLessEqual(max(edit, dry), room, (language, kind))
                    self.assertEqual(screen.btn_dry.pack_info()["side"], "top")

    def test_a_disabled_icon_button_has_its_own_greyed_glyph(self):
        screen = self.app.screens["macros"]
        spec = screen.btn_run.cget("image")
        images = [str(part) for part in (spec if isinstance(spec, (tuple, list)) else str(spec).split())]
        self.assertEqual(len(images), 3, images)                          # normal, "disabled", greyed
        self.assertEqual(images[1], "disabled")
        self.assertNotEqual(images[0], images[2])


class ScriptEditor(Window):
    def new_script(self):
        self.stub_dialog("NewScript", ("Daily", ""))
        self.app.show_screen("scripts")
        self.app.screens["scripts"]._new_script()
        self.assertEqual(self.app.screen, "editor")
        self.app.render()

    def test_new_script_opens_an_empty_editor_and_saving_waits_for_the_first_block(self):
        self.new_script()
        self.assertEqual(self.editor.lbl_state.cget("text"), self.app.t("editor_new_empty"))
        self.assertIn("disabled", self.editor.btn_save.state())
        roles = {b.role for b in self.editor.canvas_view.layout.boxes}
        self.assertIn(fl.PLACEHOLDER, roles)
        self.editor._add_step(fe.RUN)
        self.assertIn(fl.PENDING, {b.role for b in self.editor.canvas_view.layout.boxes})
        step = RunMacro(recorded(name="Inner"))
        self.editor.form.build = lambda: step
        self.assertTrue(self.editor.apply_form(), self.c.status)
        self.app.render()
        self.assertNotIn("disabled", self.editor.btn_save.state())
        self.app._key_save()
        saved = [i for i in self.library.list() if i.version == 2]
        self.assertEqual([i.name for i in saved], ["Daily"])
        self.assertEqual(self.library.load(saved[0].path).steps, (step,))

    def test_a_block_that_cannot_be_built_is_marked_and_explained_and_nothing_is_added(self):
        self.new_script()
        self.editor._add_step(fe.RUN)                                  # no recorded macro exists to choose
        self.assertFalse(self.editor.apply_form())
        self.assertTrue(self.editor.lbl_form_error.cget("text"))
        self.assertTrue(self.editor.canvas_view.scene.pending_invalid)
        self.assertEqual(self.c.editor.document.steps, ())

    def test_choosing_a_block_on_the_canvas_chooses_it_in_the_list_and_the_panel(self):
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        self.app.render()
        box = self.editor.canvas_view.layout.box_of((1,))
        self.editor.canvas_view._on_select(self.editor.canvas_view.block_at(box.cx, box.cy).path, "")
        self.assertEqual(self.editor.tree.selection(), ("s:1",))
        self.assertEqual(self.editor.canvas_view.scene.selected, (1,))
        from app.gui.view import fmt
        self.assertEqual(self.editor.lbl_kind.cget("text"), self.editor.block_views()[(1,)].title)
        self.assertEqual(fmt.step_title(self.app, self.c.editor.document.steps[0]), self.app.t("step_wait_until"))

    def test_fit_to_view_and_zoom_follow_the_script_not_a_fixed_number(self):
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.c.open_editor(key)
        self.app.show_screen("editor")
        self.app.render()
        view = self.editor.canvas_view
        view.fit()
        canvas = view.canvas
        width = canvas.winfo_width() if canvas.winfo_width() > 1 else int(canvas.cget("width"))
        height = canvas.winfo_height() if canvas.winfo_height() > 1 else int(canvas.cget("height"))
        self.assertAlmostEqual(view.scale, fl.fit_scale(view.layout.width, view.layout.height, width, height))
        before = view.scale
        view.zoom_in()
        self.assertGreater(view.scale, before)
        self.assertEqual(self.editor.lbl_zoom.cget("text"), view.zoom_text())

    def test_the_properties_panel_is_never_wider_than_itself_for_any_kind_of_condition(self):
        """A form wider than the panel is cut off at its right edge (found on the rendered window: the text
        condition's "Ignore case" box and its note were clipped)."""
        from app.decide import Condition, Decision, TapMatch, WaitUntil
        from app.decide.format import ConditionalMacro
        from app.gui.view.editor import PANEL_WIDTH
        script = ConditionalMacro("Texts", (WaitUntil(Condition.text("Daily rewards"), 5, 0.5),
                                            Decision(Condition.text("OK"), then=TapMatch())))
        key = self.library.key_of(self.library.save(script))
        self.c.refresh_macros()
        self.c.open_editor(key)
        self.app.show_screen("editor")
        for language in ("en", "ru"):
            self.app.apply_language(language)
            for index in (0, 1):
                self.editor.tree.selection_set(f"s:{index}")
                self.app.render()
                condition = self.editor.form.condition
                for kind in fe.CONDITION_KINDS:
                    condition.kind_var.set(self.app.t(f"cond_{kind}"))
                    condition._on_kind()
                    self.root.update_idletasks()
                    self.assertLessEqual(self.editor._panel.winfo_reqwidth(), PANEL_WIDTH - 12,
                                         (language, index, kind))
                self.editor.revert_form()

    def test_the_list_view_shows_the_same_steps_and_the_same_choice(self):
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.c.open_editor(key)
        self.app.show_screen("editor")
        self.editor.set_view("list")
        self.assertTrue(self.editor._list.winfo_manager())
        self.assertFalse(self.editor.canvas_view.frame.winfo_manager())
        self.assertEqual(self.editor.tree.get_children(), ("start", "s:0", "s:1"))      # Start and the two steps
        self.assertEqual(self.editor.tree.get_children("s:1"), ("h:1/then", "h:1/else"))  # the IF's two branches
        self.assertEqual(self.editor.tree.get_children("h:1/then"), ("s:1/then/0",))
        self.editor.set_view("canvas")
        self.assertTrue(self.editor.canvas_view.frame.winfo_manager())

    def built_but_taken(self):
        """A new script with a step, whose name a file made meanwhile has taken."""
        self.new_script()
        step = RunMacro(recorded(name="Inner"))
        self.assertTrue(self.c.editor_apply(fe.insert_step, 0, step))
        taken = self.library.save(flow("Daily"))
        return step, taken, taken.read_bytes()

    def test_saving_under_a_name_taken_meanwhile_asks_for_another_and_keeps_the_work(self):
        step, taken, before = self.built_but_taken()
        self.stub_dialog("TextPrompt", "Daily 2")
        self.editor.save()
        self.assertEqual(taken.read_bytes(), before)
        self.assertFalse(self.c.editing_new)
        saved = self.library.load(self.library.path_for("Daily 2"))
        self.assertEqual((saved.name, saved.steps), ("Daily 2", (step,)))

    def test_declining_another_name_keeps_the_new_script_open_and_unsaved(self):
        _, taken, before = self.built_but_taken()
        self.stub_dialog("TextPrompt", None)
        self.editor.save()
        self.assertTrue(self.c.editing_new and self.c.editor_dirty)
        self.assertEqual(self.c.status.category, "exists")
        self.assertEqual(taken.read_bytes(), before)

    def test_leaving_with_save_asks_for_a_free_name_too(self):
        self.built_but_taken()
        self.stub_dialog("UnsavedChanges", "save")
        self.stub_dialog("TextPrompt", "Daily 2")
        self.editor.go_back()
        self.assertIsNone(self.c.editor)
        self.assertTrue(self.library.path_for("Daily 2").exists())

    def test_saving_over_a_version_saved_outside_meanwhile_asks_first(self):
        path = self.library.save(flow("Collect"))
        newer = self.library.save(flow("Gather")).read_bytes()
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(self.library.key_of(path)))
        self.app.show_screen("editor")
        self.assertTrue(self.c.editor_apply(fe.insert_step, 0, RunMacro(recorded(name="Inner"))))
        path.write_bytes(newer)                                       # replaced outside while it is open here
        asked = []
        self.app._confirm = lambda title, text: asked.append(text) or False
        self.editor.save()
        self.assertEqual(len(asked), 1)
        self.assertEqual(path.read_bytes(), newer)                    # declined: the outside version stays
        self.assertTrue(self.c.editor_dirty)
        self.app._confirm = lambda title, text: True
        self.editor.save()
        self.assertFalse(self.c.editor_dirty)                         # replaced only because the person said so
        self.assertNotEqual(path.read_bytes(), newer)

    def test_the_first_zoom_waiting_for_the_layout_is_cancelled_when_the_canvas_goes_away(self):
        key = self.library.key_of(self.library.save(flow("Collect")))
        self.c.refresh_macros()
        self.assertTrue(self.c.open_editor(key))
        self.app.show_screen("editor")
        self.app.render()
        job = self.editor._zoom_job
        self.assertIsNotNone(job)

        def pending():
            return self.root.tk.splitlist(self.root.tk.call("after", "info"))
        self.assertIn(job, pending())
        self.editor.canvas_view.frame.destroy()                       # its callback would be gone: so is the job
        self.assertNotIn(job, pending())


class TextScreenBoxes(Window):
    def read(self, title="Notes"):
        self.pick(title)
        screen = self.app.screens["text"]
        self.app.show_screen("text")
        self.app._drain_jobs()
        screen._read()
        self.app._drain_jobs()
        self.app.render()
        return screen

    def test_each_line_is_listed_with_where_it_is_and_choosing_it_shows_its_box(self):
        screen = self.read()
        self.assertEqual(len(screen.blocks.get_children()), 1)
        self.assertEqual(screen.blocks.item("0", "values")[1], "Hello")
        screen.choose_block(0)
        self.assertEqual(screen.selected_block, 0)
        self.assertIn("x 1, y 1", screen.lbl_block.cget("text"))
        self.assertIn("5 × 5", screen.lbl_block.cget("text"))
        self.root.update_idletasks()
        screen._draw_preview(force=True)
        kinds = [screen.preview.type(item) for item in screen.preview.find_all()]
        self.assertEqual(kinds.count("image"), 1)
        self.assertGreaterEqual(kinds.count("rectangle"), 3)        # the frame, the box, its number tag

    def test_the_results_card_wraps_its_sentences_inside_itself_in_every_language(self):
        """The card has a fixed width: a sentence wider than it is cut off (found on the rendered window, in
        Russian: the summary line lost its last words)."""
        from app.gui.view.text import RESULTS_WIDTH
        screen = self.read()
        for language in ("en", "ru"):
            self.app.apply_language(language)
            for chosen in (None, 0):
                screen.choose_block(chosen)
                self.app.render()
                self.root.update_idletasks()
                for label in (screen.lbl_result, screen.lbl_block):
                    self.assertTrue(label.cget("text"))
                    self.assertLessEqual(label.winfo_reqwidth(), RESULTS_WIDTH - 32, (language, chosen))

    def test_a_click_on_a_box_chooses_its_line(self):
        screen = self.read()
        self.root.update_idletasks()
        screen._draw_preview(force=True)
        left, top, down, up = screen._map
        x0, y0, x1, y1 = geo.shown_box(1, 1, 5, 5, down, up)

        class Click:
            x, y = left + (x0 + x1) // 2, top + (y0 + y1) // 2
        screen._click_preview(Click)
        self.assertEqual(screen.selected_block, 0)
        self.assertEqual(screen.blocks.selection(), ("0",))


if __name__ == "__main__":
    unittest.main()
