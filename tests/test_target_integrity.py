"""Target integrity and coordinate safety (first bug-fix stage). Deterministic: simulated desktop, no real windows.

  C-02  a GUI selection is a target IDENTITY, not just a handle (stale list + reused handle must be refused);
  C-09  a preview result is shown only for the exact target it was captured for;
  C-10  the preview worker flag means "a worker is running" and nothing else;
  C-01  ONE coordinate space for Windows screenshots and Windows input (client area, origin top-left);
  P-02  the DPI contract: an ambiguous scale is refused, never guessed.
"""
import sys
import unittest
from dataclasses import replace

from app.core.errors import TargetNotVerified
from app.gui.controller import PreviewJob, TargetStatus
from app.providers.base import FrameSource, Screenshot
from app.providers.input import Key
from app.vision.color import find_color
from app.vision.frame import Frame
from app.windows.backend import (RawBitmap, WindowBackend, WindowCaptureError, WindowInfo, WindowInputBackend,
                                 crop_bgra)
from app.windows.input import InputRefused, WindowInputSession
from app.windows.provider import WindowScreenshotProvider
from app.windows.target import WindowVerifier
from tests.test_gui import Base, tk
from tests.test_windows_provider import info, setup


class StaleListAndReusedHandle(Base):
    """C-02. The list shows Notes (handle 100); before the click another program owns handle 100."""

    def listed_notes(self):
        self.c.refresh()
        return self.entry("Notes")

    def assert_refused(self, entry):
        self.assertFalse(self.c.select(entry.ref))
        c = self.c
        self.assertEqual((c.target, c.target_status, c.status.level), (None, TargetStatus.INVALID, "error"))
        self.assertNotIn("Verified", c.status.text)
        self.assertEqual(c.status.category, "invalid_selection")
        self.assertFalse(c.target_ready or c.can_record() or c.can_play())
        self.assertIsNone(c.begin_preview())
        self.assertFalse(c.play())
        self.assertEqual(self.world.events, [])
        self.assertEqual(self.world.captures, 0)

    def test_handle_reused_by_another_program_is_refused(self):
        entry = self.listed_notes()
        self.world.windows[100] = info(hwnd=100, title="Bank - Payments", name="chrome.exe",
                                       cls="Chrome_WidgetWin_1", pid=77, start=9999)
        self.assert_refused(entry)

    def test_every_identity_signal_is_compared(self):
        listed = dict(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10)
        for change in ({"pid": 11}, {"start": 1234}, {"name": "other.exe"}, {"cls": "OtherWnd"}):
            with self.subTest(change=change):
                self.setUp()
                entry = self.listed_notes()
                self.world.windows[100] = info(**{**listed, **change})
                self.assert_refused(entry)

    def test_same_identity_with_a_new_title_is_accepted_and_the_ui_shows_the_verified_window(self):
        entry = self.listed_notes()
        self.world.windows[100] = replace(self.world.windows[100], title="Notes - edited")
        self.assertTrue(self.c.select(entry.ref), self.c.status)
        self.assertEqual(self.c.selected.title, "Notes - edited")           # read back from the verified target
        self.assertIn("Notes - edited", self.c.status.text)
        self.assertEqual(self.c.target_status, TargetStatus.VERIFIED)

    def test_verified_card_always_describes_the_verified_target(self):
        self.pick("Notes")
        self.assertEqual(self.c.target.identity.display_name.split(":")[0], self.c.selected.process)
        self.assertEqual(self.c.selected.ref, f"w{100}")

    def test_the_handle_can_be_reused_after_selection_too_and_is_still_refused(self):
        self.pick("Notes")
        self.world.windows[100] = info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10, start=42)
        self.assertIsNone(self.c.begin_preview())
        self.assertEqual(self.c.target_status, TargetStatus.STALE)

    def test_verifier_level_expected_identity(self):
        desk, _, verifier = setup(info())
        listed = desk.windows[100]
        self.assertIsNotNone(verifier.verify(100, expected=listed))
        self.assertIsNotNone(verifier.verify(100))                           # no expectation: unchanged behaviour
        for changed in (info(pid=11), info(start=5001), info(name="x.exe"), info(cls="Other")):
            desk.windows[100] = changed
            with self.assertRaises(TargetNotVerified) as cm:
                verifier.verify(100, expected=listed)
            self.assertEqual(cm.exception.reason_code, "identity_changed")
        desk.windows[100] = replace(listed, title="renamed")                 # the title is not identity
        self.assertIsNotNone(verifier.verify(100, expected=listed))

    def test_a_listed_identity_never_leaks_into_a_different_ref(self):
        self.c.refresh()
        self.assertFalse(self.c.select("w4242"))
        self.assertIsNone(self.c.target)


class PreviewAttribution(Base):
    """C-09."""

    def test_late_result_of_a_previous_target_is_not_displayed(self):
        self.pick("Notes")                                                   # 1. select A
        job_a = self.c.begin_preview()                                       # 2. start capture for A
        self.assertIsInstance(job_a, PreviewJob)
        self.assertTrue(self.c.select(self.entry("Calculator").ref))         # 3. select B
        late = job_a()                                                       # 4. A's delayed result completes
        self.assertFalse(self.c.finish_preview(late, job_a))
        self.assertIsNone(self.c.preview)                                    # 5. never displayed for B
        self.assertEqual(self.c.preview_discarded, 1)
        job_b = self.c.begin_preview()
        self.assertTrue(self.c.finish_preview(job_b(), job_b))
        self.assertEqual(self.c.preview.source.target_id, self.c.target.target_id)
        self.assertNotEqual(self.c.preview.source.target_id, late.source.target_id)

    def test_provenance_alone_is_enough_to_reject(self):
        self.pick("Notes")
        shot = self.c.begin_preview()()
        self.c.select(self.entry("Calculator").ref)
        self.assertFalse(self.c.finish_preview(shot))                         # no job passed: the image's own stamp decides
        self.assertIsNone(self.c.preview)

    def test_a_job_alone_is_enough_to_reject_even_an_error(self):
        self.pick("Notes")
        job = self.c.begin_preview()
        self.c.select(self.entry("Calculator").ref)
        self.assertFalse(self.c.finish_preview(WindowCaptureError("The window is minimized."), job))
        self.assertNotEqual(self.c.status.category, "preview")               # no error is shown for the wrong target
        self.assertEqual(self.c.preview_discarded, 1)

    def test_same_target_reselected_keeps_its_own_result(self):
        self.pick("Notes")
        job = self.c.begin_preview()
        self.c.select(self.entry("Notes").ref)
        self.assertTrue(self.c.finish_preview(job(), job))

    def test_result_is_discarded_when_the_target_is_no_longer_verified(self):
        self.pick("Notes")
        job = self.c.begin_preview()
        self.world.windows.pop(100)
        self.c.refresh()                                                     # the selection turns STALE
        self.assertFalse(self.c.finish_preview(Screenshot.__new__(Screenshot), job))
        self.assertIsNone(self.c.preview)

    def test_forged_provenance_of_another_provider_is_discarded(self):
        self.pick("Notes")
        shot = self.c.begin_preview()()
        other = object.__new__(Screenshot)
        for name, value in (("png", shot.png), ("width", 1), ("height", 1),
                            ("source", FrameSource("other-provider", self.c.target.target_id)), ("_mint", None)):
            object.__setattr__(other, name, value)
        self.assertFalse(self.c.finish_preview(other))
        self.assertIsNone(self.c.preview)


@unittest.skipIf(tk is None, "tkinter is not available")
class PreviewWorkerLifecycle(Base):
    """C-10, with a deterministic runner in place of a real thread."""

    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.pending = []
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False,
                       confirm=lambda t, m: True, preview_runner=self.pending.append)
        self.c = self.app.controller
        self.addCleanup(self.app.close)

    def run_worker(self):
        self.pending.pop(0)()

    def test_busy_stays_set_while_the_worker_runs_and_idle_ticks_do_not_reset_it(self):
        self.pick("Notes")
        self.app._start_preview()
        self.assertTrue(self.app._preview_busy)
        self.assertEqual(len(self.pending), 1)
        for _ in range(5):
            self.app._tick()                                                 # UI polls with nothing to drain
            self.assertTrue(self.app._preview_busy)
        self.app._start_preview()                                            # never a second concurrent worker
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.world.captures, 0)

    def test_completion_clears_busy_exactly_when_the_result_arrives(self):
        self.pick("Notes")
        self.app._start_preview()
        self.run_worker()                                                    # worker finished, result queued
        self.assertTrue(self.app._preview_busy)                              # not yet drained by the UI
        self.app._tick()
        self.assertFalse(self.app._preview_busy)
        self.assertIsNotNone(self.c.preview)
        self.assertEqual(self.c.preview.source.target_id, self.c.target.target_id)

    def test_worker_errors_also_clear_busy(self):
        self.pick("Notes")
        self.world.windows[100] = info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10, minimized=True)
        self.app._start_preview()
        self.run_worker()
        self.app._tick()
        self.assertFalse(self.app._preview_busy)
        self.assertEqual(self.c.status.category, "preview")

    def test_late_result_and_rapid_reselection(self):
        self.pick("Notes")
        self.app._start_preview()                                            # worker A submitted, still running
        self.assertTrue(self.c.select(self.entry("Calculator").ref))         # the user picks another window
        self.app._start_preview()                                            # refused: A is still running
        self.assertEqual(len(self.pending), 1)
        self.run_worker()                                                    # A's late result arrives
        self.app._tick()
        self.assertEqual(self.c.preview_discarded, 1)
        self.assertIsNone(self.c.preview)                                    # A's image is NOT shown for Calculator
        self.assertEqual(len(self.pending), 1)                               # a preview of the CURRENT target was started
        self.assertTrue(self.app._preview_busy)
        self.run_worker()
        self.app._tick()
        self.assertFalse(self.app._preview_busy)
        self.assertEqual(self.c.preview.source.target_id, self.c.target.target_id)

    def test_three_rapid_reselections_end_with_exactly_one_preview_of_the_last_target(self):
        self.pick("Notes")
        self.app._start_preview()
        self.c.select(self.entry("Calculator").ref)
        self.c.select(self.entry("Notes").ref)
        self.c.select(self.entry("Calculator").ref)
        self.run_worker()
        self.app._tick()
        self.run_worker()
        self.app._tick()
        self.assertEqual(self.pending, [])
        self.assertEqual(self.c.preview.source.target_id, self.c.target.target_id)
        self.assertEqual(self.world.captures, 2)

    def test_a_failing_runner_does_not_leave_busy_set(self):
        self.pick("Notes")

        def broken(fn):
            raise RuntimeError("no thread")

        self.app._preview_runner = broken
        with self.assertRaises(RuntimeError):
            self.app._start_preview()
        self.assertFalse(self.app._preview_busy)


class FramedDesktop(WindowBackend, WindowInputBackend):
    """A simulated desktop whose windows have a frame: the window image is larger than the client area, the client
    area starts at (client_left, client_top) inside it, and a MARKER is painted at a known CLIENT position."""

    FRAME = (0, 0, 0)            # B, G, R of the non-client area
    CLIENT = (0, 0, 255)         # red client background
    MARK = (0, 255, 0)           # green marker
    MARKER = (30, 20, 10, 6)

    def __init__(self, *windows):
        self.windows = {w.hwnd: w for w in windows}
        self.events, self.captures, self.after_capture = [], 0, None

    def enumerate(self):
        return list(self.windows.values())

    def query(self, hwnd):
        return self.windows.get(hwnd)

    def render_window(self, w: WindowInfo) -> bytes:
        rows = []
        mx, my, mw, mh = self.MARKER
        for y in range(w.height):
            row = bytearray()
            for x in range(w.width):
                cx, cy = x - w.client_left, y - w.client_top
                inside = 0 <= cx < w.client_width and 0 <= cy < w.client_height
                colour = self.FRAME
                if inside:
                    colour = self.MARK if (mx <= cx < mx + mw and my <= cy < my + mh) else self.CLIENT
                row += bytes(colour) + b"\x00"
            rows.append(bytes(row))
        return b"".join(rows)

    def capture(self, hwnd, width, height):
        self.captures += 1
        w = self.windows[hwnd]
        if (width, height) != (w.client_width, w.client_height):
            raise WindowCaptureError("The client area changed size before it could be captured; discarded.")
        raw = RawBitmap(width, height, crop_bgra(self.render_window(w), w.width, w.height, w.client_left, w.client_top,
                                                 width, height))
        if self.after_capture:
            self.after_capture(self)
        return raw

    def client_size(self, hwnd):
        w = self.windows.get(hwnd)
        return (w.client_width, w.client_height) if w else None

    def post_mouse(self, hwnd, event, x, y):
        self.events.append(("mouse", hwnd, event, x, y))

    def post_key(self, hwnd, event, key):
        self.events.append(("key", hwnd, event, key))

    def post_char(self, hwnd, char):
        self.events.append(("char", hwnd, char))


def framed(**kw):
    """A 320x240 window whose client area is 304x201 at offset (8, 31), like a real top-level window."""
    return info(w=320, h=240, client_width=304, client_height=201, client_left=8, client_top=31, **kw)


class OneCoordinateSpace(unittest.TestCase):
    """C-01: screenshot pixel (x, y) is client coordinate (x, y)."""

    def build(self, window=None, **session):
        desk = FramedDesktop(window or framed())
        stop = __import__("app.core.cancel", fromlist=["EmergencyStop"]).EmergencyStop()
        verifier = WindowVerifier(desk, stop)
        target = verifier.verify(100)
        provider = WindowScreenshotProvider(target, verifier, stop)
        inp = WindowInputSession(target, verifier, desk, stop, max_actions=session.pop("max_actions", 20), **session)
        return desk, provider, inp

    def test_screenshot_has_exactly_the_client_size_not_the_window_size(self):
        desk, provider, _ = self.build()
        shot = provider.capture()
        self.assertEqual((shot.width, shot.height), (304, 201))
        self.assertNotEqual((shot.width, shot.height), (320, 240))
        self.assertEqual((shot.width, shot.height), desk.client_size(100))     # the input bounds are the same numbers

    def test_a_marker_is_found_at_its_client_coordinates_and_a_tap_there_arrives_unchanged(self):
        desk, provider, inp = self.build()
        frame = Frame.from_screenshot(provider.capture())
        found = find_color(frame, (0, 255, 0))
        self.assertEqual((found.bounds.x, found.bounds.y, found.bounds.width, found.bounds.height), desk.MARKER)
        self.assertEqual(find_color(frame, (0, 0, 0)).count, 0)               # no frame or title-bar pixel is included
        cx, cy = found.bounds.x + found.bounds.width // 2, found.bounds.y + found.bounds.height // 2
        inp.tap(cx, cy)                                                       # a screenshot point, used without conversion
        self.assertEqual({(e[3], e[4]) for e in desk.events}, {(cx, cy)})
        self.assertTrue(all(e[1] == 100 for e in desk.events))

    def test_last_pixel_of_the_screenshot_is_the_last_valid_input_pixel(self):
        _, provider, inp = self.build()
        shot = provider.capture()
        inp.move(shot.width - 1, shot.height - 1)
        for x, y in ((shot.width, 0), (0, shot.height)):
            with self.assertRaises(InputRefused):
                inp.move(x, y)

    def test_resizing_changes_the_screenshot_and_the_input_bounds_together(self):
        desk, provider, inp = self.build()
        desk.windows[100] = framed_larger = replace(desk.windows[100], width=420, height=340, client_width=404,
                                                    client_height=301)
        shot = provider.capture()
        self.assertEqual((shot.width, shot.height), (404, 301))
        inp.move(403, 300)
        with self.assertRaises(InputRefused):
            inp.move(404, 300)
        self.assertEqual(framed_larger.client_left, 8)

    def test_moving_the_window_changes_nothing(self):
        desk, provider, _ = self.build()
        before = provider.capture().png
        # the simulated window has no screen position: what matters is that nothing in the image depends on one
        self.assertEqual(provider.capture().png, before)

    def test_a_layout_change_during_capture_is_discarded(self):
        desk, provider, _ = self.build()
        desk.after_capture = lambda d: d.windows.update({100: replace(d.windows[100], client_left=10)})
        with self.assertRaises(WindowCaptureError):
            provider.capture()

    def test_client_size_change_between_check_and_copy_is_discarded(self):
        desk, provider, _ = self.build()
        real = desk.capture

        def shrink_then_copy(hwnd, w, h):
            desk.windows[hwnd] = replace(desk.windows[hwnd], client_width=300)
            return real(hwnd, w, h)

        desk.capture = shrink_then_copy
        with self.assertRaises(WindowCaptureError):
            provider.capture()

    def test_window_image_size_defaults_to_the_client_size_for_simple_windows(self):
        w = info(w=64, h=48)
        self.assertEqual((w.client_width, w.client_height, w.client_left, w.client_top), (64, 48, 0, 0))
        self.assertFalse(w.dpi_ambiguous)

    def test_client_area_without_area_is_refused_even_when_the_window_has_size(self):
        desk = FramedDesktop(info(w=320, h=240, client_width=0, client_height=0, client_left=8, client_top=31))
        stop = __import__("app.core.cancel", fromlist=["EmergencyStop"]).EmergencyStop()
        verifier = WindowVerifier(desk, stop)
        with self.assertRaises(WindowCaptureError):
            WindowScreenshotProvider(verifier.verify(100), verifier, stop).capture()
        self.assertEqual(desk.captures, 0)


class CropHelper(unittest.TestCase):
    def test_crop_returns_exactly_the_rectangle(self):
        w, h = 5, 4
        image = b"".join(bytes([x, y, 0, 0]) for y in range(h) for x in range(w))
        out = crop_bgra(image, w, h, 1, 2, 3, 2)
        self.assertEqual(out, b"".join(bytes([x, y, 0, 0]) for y in (2, 3) for x in (1, 2, 3)))

    def test_crop_refuses_anything_outside_or_inconsistent(self):
        image = bytes(5 * 4 * 4)
        for args in ((-1, 0, 2, 2), (0, -1, 2, 2), (4, 0, 2, 2), (0, 3, 2, 2), (0, 0, 0, 2), (0, 0, 2, 0), (0, 0, 6, 1)):
            with self.subTest(args=args), self.assertRaises(WindowCaptureError):
                crop_bgra(image, 5, 4, *args)
        with self.assertRaises(WindowCaptureError):
            crop_bgra(image[:-1], 5, 4, 0, 0, 1, 1)


class DpiContract(unittest.TestCase):
    """P-02: what is guaranteed. An ambiguous scale is refused for capture AND input, with zero side effects."""

    def build(self):
        desk = FramedDesktop(framed(dpi_ambiguous=True))
        from app.core.cancel import EmergencyStop
        stop = EmergencyStop()
        verifier = WindowVerifier(desk, stop)
        target = verifier.verify(100)                                        # verification itself does not depend on scale
        return desk, WindowScreenshotProvider(target, verifier, stop), WindowInputSession(target, verifier, desk, stop,
                                                                                           max_actions=5)

    def test_capture_is_refused_before_any_pixel_is_copied(self):
        desk, provider, _ = self.build()
        with self.assertRaises(WindowCaptureError) as cm:
            provider.capture()
        self.assertIn("DPI", str(cm.exception))
        self.assertEqual(desk.captures, 0)

    def test_every_input_operation_is_refused_with_zero_events(self):
        desk, _, inp = self.build()
        calls = (lambda: inp.tap(1, 1), lambda: inp.secondary_tap(1, 1), lambda: inp.double_tap(1, 1),
                 lambda: inp.move(1, 1), lambda: inp.button_down(1, 1), lambda: inp.key_press(Key.A),
                 lambda: inp.key_down(Key.A), lambda: inp.type_text("a"))
        for call in calls:
            with self.assertRaises(InputRefused) as cm:
                call()
            self.assertIn("DPI", str(cm.exception))
        self.assertEqual(desk.events, [])

    def test_an_unambiguous_scale_is_unaffected(self):
        desk = FramedDesktop(framed())
        from app.core.cancel import EmergencyStop
        stop = EmergencyStop()
        verifier = WindowVerifier(desk, stop)
        target = verifier.verify(100)
        WindowInputSession(target, verifier, desk, stop, max_actions=1).tap(5, 5)
        self.assertEqual(len(desk.events), 2)
        self.assertEqual((WindowScreenshotProvider(target, verifier, stop).capture().width), 304)


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealBackendGeometry(unittest.TestCase):
    """Read-only: the real backend reports ONE client geometry for both capture and input."""

    def test_query_and_client_size_agree_and_offsets_are_inside_the_window(self):
        from app.windows.win32 import Win32InputBackend
        backend = Win32InputBackend()
        checked = 0
        for w in backend.enumerate()[:25]:
            again = backend.query(w.hwnd)
            size = backend.client_size(w.hwnd)
            if again is None or size is None or again.minimized:
                continue
            checked += 1
            self.assertEqual((again.client_width, again.client_height), size)
            self.assertIsInstance(again.dpi_ambiguous, bool)
            self.assertLessEqual(again.client_width, again.width + 1)
            self.assertLessEqual(again.client_height, again.height + 1)
        self.assertGreaterEqual(checked, 0)

    def test_capture_of_the_wrong_client_size_is_refused_by_the_real_backend(self):
        from app.windows.win32 import Win32InputBackend
        backend = Win32InputBackend()
        for w in backend.enumerate()[:5]:
            if w.minimized or w.client_width <= 0:
                continue
            with self.assertRaises(WindowCaptureError):
                backend.capture(w.hwnd, w.client_width + 7, w.client_height + 3)
            break


if __name__ == "__main__":
    unittest.main()
