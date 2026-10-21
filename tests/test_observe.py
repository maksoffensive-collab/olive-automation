"""Observation: capture once, ask questions about the image, never act on the answer.

Everything here runs on deterministic synthetic images built in code -- never a real game screenshot -- so a
failure means the observation logic changed, not that a game did. The one place a real machine is involved is
the Windows OCR engine, and that test skips itself where the engine is absent rather than failing.

The properties these tests exist to hold down:
  * "not found" is a RESULT, not an exception, and it still carries what WAS measured;
  * a failure to LOOK (no image, bad query, no engine, stop triggered) is an exception, and a different one for
    each cause;
  * one screenshot can answer many queries, and observing twice does not silently cost two captures;
  * two targets observing at once share no image, no cache and no lock;
  * waits are bounded and end the moment the emergency stop is triggered;
  * nothing in the package can send input, and it does not know which provider produced its pixels.
"""
import threading
import time
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped
from app.observe import (COLOR_REGION, EXACT, PIXEL, TEMPLATE, TEXT, ColorRegionQuery, InvalidQuery,
                         ObservationContext, ObservationError, ObservationResult, ObservationTimeout,
                         ObservationUnavailable, OcrUnavailable, PixelQuery, TemplateQuery, TextQuery,
                         observe)
from app.ocr.provider import OcrResult, OcrTextBlock
from app.providers.base import Screenshot, ScreenshotProvider, TargetIdentity
from app.vision.bitmap import Bitmap, decode_png, encode_png
from app.vision.frame import Frame
from app.vision.roi import Roi

WHITE = (255, 255, 255)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
NEAR_RED = (250, 6, 4)


def scene() -> Bitmap:
    """200x100 white, a red 10x10 square at (30,20), a blue 6x4 block at (150,70)."""
    return Bitmap.solid(200, 100, WHITE).with_rect(30, 20, 10, 10, RED).with_rect(150, 70, 6, 4, BLUE)


def three_marks() -> Bitmap:
    """The same 8x8 mark at three places, for multi-match and ordering tests."""
    bm = Bitmap.solid(120, 60, WHITE)
    for x, y in ((10, 10), (60, 10), (35, 40)):
        bm = bm.with_rect(x, y, 8, 8, RED).with_rect(x + 2, y + 2, 4, 4, BLUE)
    return bm


def mark() -> Bitmap:
    return Bitmap.solid(8, 8, RED).with_rect(2, 2, 4, 4, BLUE)


class FakeScreenshots(ScreenshotProvider):
    """A screenshot provider over images supplied by the test. It mints real `Screenshot`s through the base
    class, so everything downstream runs the production path."""

    def __init__(self, target_id="t-1", provider_id="fake-provider", frames=None, fail=None, delay=0.0):
        self._identity = TargetIdentity(provider_id, target_id, "Display Name Nobody Should Route By")
        self._frames = list(frames) if frames else [scene()]
        self._fail = fail
        self.captures = 0
        self.delay = delay
        self.seen_threads = set()

    @property
    def identity(self):
        return self._identity

    def capture(self):
        self.seen_threads.add(threading.get_ident())
        if self._fail is not None:
            raise self._fail
        if self.delay:
            time.sleep(self.delay)
        bm = self._frames[min(self.captures, len(self._frames) - 1)]
        self.captures += 1
        return self._issue_screenshot(encode_png(bm), bm.width, bm.height, detail="fake")


class FakeOcr:
    """An OCR provider that returns what the test told it to, without subclassing anything."""

    def __init__(self, blocks=(), fail=None):
        self.blocks = tuple(blocks)
        self.fail = fail
        self.calls = []
        self.languages = []

    def read_text(self, frame, *, roi=None, language="", cancel=None):
        self.calls.append(roi)
        self.languages.append(language)
        if self.fail is not None:
            raise self.fail
        return OcrResult(self.blocks)


def block(text, x=0, y=0, w=20, h=10, conf=1.0):
    return OcrTextBlock(text, Roi(x, y, w, h), conf)


def context(**kw):
    ocr = kw.pop("ocr", None)
    stop = kw.pop("stop", None)
    provider = FakeScreenshots(**kw)
    return ObservationContext(provider, stop=stop, ocr=ocr), provider


def frame_of(bm: Bitmap, target_id="t-1") -> Frame:
    return Frame.from_screenshot(FakeScreenshots(target_id=target_id, frames=[bm]).capture())


# ---- A / B / S: the screenshot contract ------------------------------------------------------------
class ScreenshotContract(unittest.TestCase):
    def test_a_context_needs_a_real_screenshot_provider_not_a_handle_or_a_name(self):
        for bad in ("emulator-5554", 1234, None, object(), b"png"):
            with self.assertRaises(ObservationUnavailable):
                ObservationContext(bad)

    def test_the_fake_provider_produces_genuine_minted_screenshots(self):
        provider = FakeScreenshots()
        shot = provider.capture()
        self.assertIsInstance(shot, Screenshot)
        self.assertEqual((shot.width, shot.height), (200, 100))
        self.assertEqual((shot.source.provider_id, shot.source.target_id), ("fake-provider", "t-1"))
        self.assertEqual(decode_png(shot.png), scene())

    def test_a_result_carries_the_target_identity_and_never_the_display_name(self):
        ctx, provider = context()
        result = ctx.observe(PixelQuery((30, 20), RED))
        self.assertEqual(result.target_id, "t-1")
        self.assertEqual(result.source.provider_id, "fake-provider")
        self.assertNotIn("Display Name", repr(result.source))

    def test_a_capture_failure_is_an_infrastructure_error_never_a_not_found_result(self):
        from app.core.errors import TargetNotVerified
        ctx, _ = context(fail=TargetNotVerified("the device is gone", reason_code="stale_entry"))
        with self.assertRaises(ObservationUnavailable) as caught:
            ctx.observe(PixelQuery((1, 1), RED))
        self.assertIn("t-1", str(caught.exception))

    def test_a_background_capable_provider_is_observed_through_the_same_contract(self):
        """S. Observation asks a provider for an image and nothing else -- no window, no focus, no activation.
        The contract has no parameter that could request any of those, which is what makes a background ADB
        capture and a desktop capture interchangeable here."""
        import inspect

        from app.observe import context as context_module
        source = inspect.getsource(context_module)
        for forbidden in ("foreground", "activate", "SetForegroundWindow", "minimi", "focus", "cursor"):
            self.assertNotIn(forbidden.lower(), source.lower(), forbidden)
        self.assertEqual([p for p in inspect.signature(ScreenshotProvider.capture).parameters if p != "self"], [])

    def test_the_image_is_decoded_once_and_reused_within_its_age(self):
        ctx, provider = context()
        ctx.observe(PixelQuery((30, 20), RED))
        ctx.observe(PixelQuery((30, 20), RED), max_age_s=5.0)
        self.assertEqual(provider.captures, 1)
        ctx.observe(PixelQuery((30, 20), RED))          # default is always fresh
        self.assertEqual(provider.captures, 2)
        ctx.forget()
        ctx.observe(PixelQuery((30, 20), RED), max_age_s=5.0)
        self.assertEqual(provider.captures, 3)


# ---- C / D: pixel ----------------------------------------------------------------------------------
class PixelObservation(unittest.TestCase):
    def test_an_exact_pixel_match_reports_where_and_what_it_saw(self):
        ctx, _ = context()
        result = ctx.observe(PixelQuery((30, 20), RED))
        self.assertTrue(result.found)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.kind, PIXEL)
        self.assertEqual(result.detail["actual"], RED)
        self.assertEqual(result.detail["distance"], 0)
        self.assertEqual(result.bounds, Roi(30, 20, 1, 1))
        self.assertEqual((result.center.x, result.center.y), (30, 20))
        self.assertEqual(result.confidence, 1.0)

    def test_a_pixel_that_does_not_match_is_a_result_not_an_error_and_keeps_the_measurement(self):
        ctx, _ = context()
        result = ctx.observe(PixelQuery((0, 0), RED))
        self.assertFalse(result.found)
        self.assertEqual(result.matches, ())
        self.assertEqual(result.detail["actual"], WHITE)      # what WAS there is not thrown away
        self.assertEqual(result.detail["distance"], 255)
        self.assertEqual(result.confidence, 0.0)
        self.assertIsNone(result.best)

    def test_tolerance_is_per_channel_and_inclusive(self):
        ctx, _ = context(frames=[Bitmap.solid(10, 10, NEAR_RED)])
        self.assertFalse(ctx.observe(PixelQuery((1, 1), RED, tolerance=4)).found)   # blue differs by 5
        ctx2, _ = context(frames=[Bitmap.solid(10, 10, NEAR_RED)])
        hit = ctx2.observe(PixelQuery((1, 1), RED, tolerance=6))
        self.assertTrue(hit.found)
        self.assertEqual(hit.detail["distance"], 6)

    def test_a_negative_query_is_satisfied_by_absence_and_still_reports_what_is_there(self):
        ctx, _ = context()
        result = ctx.observe(PixelQuery((0, 0), RED, expected=False))
        self.assertFalse(result.found)                  # the red is genuinely not there
        self.assertTrue(result.satisfied)               # which is what was asked for
        self.assertEqual(result.detail["actual"], WHITE)


# ---- E: colour region ------------------------------------------------------------------------------
class ColorRegionObservation(unittest.TestCase):
    def test_the_ratio_is_measured_against_the_region_not_the_frame(self):
        ctx, _ = context()
        # the red square is 100 px inside a 20x20 = 400 px region -> 0.25
        result = ctx.observe(ColorRegionQuery(RED, region=Roi(25, 15, 20, 20), min_ratio=0.2))
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.detail["ratio"], 0.25)
        self.assertEqual(result.detail["count"], 100)
        self.assertEqual(result.detail["total"], 400)
        self.assertEqual(result.kind, COLOR_REGION)

    def test_a_ratio_below_the_minimum_is_not_found_but_still_reports_the_ratio(self):
        ctx, _ = context()
        result = ctx.observe(ColorRegionQuery(RED, region=Roi(25, 15, 20, 20), min_ratio=0.5))
        self.assertFalse(result.found)
        self.assertAlmostEqual(result.detail["ratio"], 0.25)   # the measurement survives the verdict

    def test_the_whole_frame_is_the_default_region(self):
        ctx, _ = context()
        result = ctx.observe(ColorRegionQuery(WHITE, min_ratio=0.9))
        self.assertTrue(result.found)
        self.assertEqual(result.detail["total"], 200 * 100)

    def test_the_bounding_box_of_the_matching_pixels_is_returned(self):
        ctx, _ = context()
        result = ctx.observe(ColorRegionQuery(RED, min_ratio=0.001))
        self.assertEqual(result.bounds, Roi(30, 20, 10, 10))


# ---- F / G / H / I: template -----------------------------------------------------------------------
class TemplateObservation(unittest.TestCase):
    def test_a_template_present_in_the_frame_is_found_with_its_position(self):
        ctx, _ = context(frames=[three_marks()])
        result = ctx.observe(TemplateQuery(mark(), min_confidence=0.99))
        self.assertTrue(result.found)
        self.assertEqual(result.kind, TEMPLATE)
        self.assertEqual(result.bounds, Roi(10, 10, 8, 8))
        self.assertEqual((result.center.x, result.center.y), (14, 14))
        self.assertEqual(result.confidence, 1.0)

    def test_a_template_that_is_not_there_is_not_found_and_is_not_an_error(self):
        other = Bitmap.solid(8, 8, BLUE).with_rect(1, 1, 2, 2, (7, 200, 7))
        ctx, _ = context(frames=[three_marks()])
        result = ctx.observe(TemplateQuery(other, min_confidence=0.99))
        self.assertFalse(result.found)
        self.assertEqual(result.matches, ())
        self.assertEqual(result.confidence, 0.0)

    def test_every_occurrence_is_returned_when_more_than_one_is_asked_for(self):
        ctx, _ = context(frames=[three_marks()])
        result = ctx.observe(TemplateQuery(mark(), min_confidence=0.99, max_results=5))
        self.assertTrue(result.found)
        self.assertEqual(len(result.matches), 3)
        self.assertEqual({(m.bounds.x, m.bounds.y) for m in result.matches}, {(10, 10), (60, 10), (35, 40)})

    def test_the_order_of_several_matches_is_deterministic(self):
        """I. Equal confidence must not leave the order to chance: it is confidence, then y, then x."""
        ctx, _ = context(frames=[three_marks()])
        runs = [ctx.observe(TemplateQuery(mark(), min_confidence=0.99, max_results=5)) for _ in range(3)]
        orders = {tuple((m.bounds.x, m.bounds.y) for m in r.matches) for r in runs}
        self.assertEqual(len(orders), 1, f"ordering varied between runs: {orders}")
        self.assertEqual(orders.pop(), ((10, 10), (60, 10), (35, 40)))

    def test_a_region_limits_the_search(self):
        ctx, _ = context(frames=[three_marks()])
        result = ctx.observe(TemplateQuery(mark(), min_confidence=0.99, region=Roi(30, 35, 40, 20),
                                           max_results=5))
        self.assertEqual([(m.bounds.x, m.bounds.y) for m in result.matches], [(35, 40)])


# ---- J / K / L: OCR --------------------------------------------------------------------------------
class TextObservation(unittest.TestCase):
    def test_a_fake_ocr_provider_satisfies_the_existing_contract_without_subclassing(self):
        from app.ocr.provider import OcrProvider
        self.assertIsInstance(FakeOcr(), OcrProvider)

    def test_text_is_found_by_what_it_says_never_by_which_engine_read_it(self):
        ocr = FakeOcr([block("Collect", 10, 20, 60, 14), block("Settings", 10, 50, 70, 14)])
        ctx, _ = context(ocr=ocr)
        result = ctx.observe(TextQuery("Collect"))
        self.assertTrue(result.found)
        self.assertEqual(result.kind, TEXT)
        best, center = result.best, result.center
        assert best is not None and center is not None
        self.assertEqual(best.detail["text"], "Collect")
        self.assertEqual(result.bounds, Roi(10, 20, 60, 14))
        self.assertEqual((center.x, center.y), (40, 27))

    def test_a_substring_matches_by_default_and_exact_does_not(self):
        ocr = FakeOcr([block("Collect all")])
        ctx, _ = context(ocr=ocr)
        self.assertTrue(ctx.observe(TextQuery("Collect")).found)
        self.assertFalse(ctx.observe(TextQuery("Collect", match=EXACT)).found)
        self.assertTrue(ctx.observe(TextQuery("Collect all", match=EXACT)).found)

    def test_case_is_ignored_unless_the_query_says_otherwise(self):
        ocr = FakeOcr([block("COLLECT")])
        ctx, _ = context(ocr=ocr)
        self.assertTrue(ctx.observe(TextQuery("collect")).found)
        self.assertFalse(ctx.observe(TextQuery("collect", ignore_case=False)).found)

    def test_absent_text_is_a_normal_result_that_still_says_how_much_was_read(self):
        ocr = FakeOcr([block("Settings"), block("Shop")])
        ctx, _ = context(ocr=ocr)
        result = ctx.observe(TextQuery("Collect"))
        self.assertFalse(result.found)
        self.assertEqual(result.detail["blocks_read"], 2)

    def test_asking_for_text_with_no_engine_configured_is_an_engine_error_not_a_not_found(self):
        ctx, _ = context()                               # no ocr
        with self.assertRaises(OcrUnavailable):
            ctx.observe(TextQuery("Collect"))
        self.assertFalse(ctx.has_ocr)

    def test_an_engine_that_fails_is_an_engine_error_not_an_empty_reading(self):
        ctx, _ = context(ocr=FakeOcr(fail=RuntimeError("engine crashed")))
        with self.assertRaises(OcrUnavailable):
            ctx.observe(TextQuery("Collect"))

    def test_the_region_is_handed_to_the_engine_so_it_can_read_less(self):
        ocr = FakeOcr([block("Collect")])
        ctx, _ = context(ocr=ocr)
        ctx.observe(TextQuery("Collect", region=Roi(0, 0, 50, 40)))
        self.assertEqual(ocr.calls, [Roi(0, 0, 50, 40)])

    def test_an_empty_query_asks_only_whether_there_is_any_text(self):
        ctx, _ = context(ocr=FakeOcr([block("anything")]))
        self.assertTrue(ctx.observe(TextQuery("")).found)
        ctx2, _ = context(ocr=FakeOcr([]))
        self.assertFalse(ctx2.observe(TextQuery("")).found)


class StopDuringTextReading(unittest.TestCase):
    """An emergency stop that lands while the OCR engine is reading is a STOP, not "no engine". Found by the
    live condition/decision smoke: the stop killed the engine's process, the empty answer was reported as an
    engine failure, and the observer then wrapped even a raised stop as `OcrUnavailable`."""

    def test_the_observer_lets_a_stop_raised_by_the_engine_through_unchanged(self):
        ctx, _ = context(ocr=FakeOcr(fail=EmergencyStopped("stop while reading")))
        with self.assertRaises(EmergencyStopped):
            ctx.observe(TextQuery("Collect"))

    def test_the_engine_reports_a_run_the_stop_killed_as_a_stop(self):
        from app.core.process import CommandResult
        from app.ocr.winrt import WindowsOcrProvider

        class KilledByStop:
            def run(self, args, *, timeout, cwd=None, cancel=None):
                return CommandResult(tuple(args), None, b"", b"", duration_ms=5, cancelled=True)

        engine = WindowsOcrProvider(KilledByStop(), work_dir=Path(__file__).resolve().parent / "_ocr_tmp")
        try:
            with self.assertRaises(EmergencyStopped):
                engine.read_text(frame_of(scene()))
        finally:
            import shutil
            shutil.rmtree(Path(__file__).resolve().parent / "_ocr_tmp", ignore_errors=True)

    def test_an_engine_that_merely_failed_is_still_ocr_unavailable(self):
        ctx, _ = context(ocr=FakeOcr(fail=RuntimeError("engine crashed")))
        with self.assertRaises(OcrUnavailable):
            ctx.observe(TextQuery("Collect"))


class RealOcrEngine(unittest.TestCase):
    """The one integration test in this file: the actual Windows OCR engine, through the whole observation path.

    Everything else here uses synthetic images and a fake engine, because unit tests must not depend on a
    machine. But "OCR works" is a claim about a real engine, so it is checked against the real one -- and
    skipped, never failed, where that engine or the text renderer is unavailable."""

    @classmethod
    def setUpClass(cls):
        from app.core.cancel import EmergencyStop
        from app.core.process import CommandRunner
        from app.ocr.winrt import WindowsOcrProvider

        cls.tmp = __import__("tempfile").TemporaryDirectory()
        cls.runner = CommandRunner(EmergencyStop())
        cls.ocr = WindowsOcrProvider(cls.runner, Path(cls.tmp.name))
        if not cls.ocr.available():
            raise unittest.SkipTest("Windows OCR engine is not available on this machine")
        image = cls._render("Collect", Path(cls.tmp.name) / "sample.png")
        if image is None:
            raise unittest.SkipTest("could not render a text image to read")
        cls.image: Path = image

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _render(cls, text, path):
        script = (
            "Add-Type -AssemblyName System.Drawing;"
            "$b = New-Object System.Drawing.Bitmap(420,120);"
            "$g = [System.Drawing.Graphics]::FromImage($b);"
            "$g.Clear([System.Drawing.Color]::White);"
            "$g.TextRenderingHint = 'AntiAliasGridFit';"
            "$f = New-Object System.Drawing.Font('Segoe UI', 30);"
            f"$g.DrawString('{text}', $f, [System.Drawing.Brushes]::Black, 25, 30);"
            f"$g.Dispose(); $b.Save('{path}'); $b.Dispose()")
        result = cls.runner.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                timeout=60)
        return path if path.is_file() and not getattr(result, "timed_out", False) else None

    def frame(self):
        return frame_of(decode_png(self.image.read_bytes()), "real-1")

    def test_the_engine_satisfies_the_existing_contract_and_reads_real_text(self):
        from app.ocr.provider import OcrProvider
        self.assertIsInstance(self.ocr, OcrProvider)
        result = self.ocr.read_text(self.frame())
        self.assertIn("collect", result.text.casefold())
        self.assertTrue(all(b.bounds.width > 0 and b.bounds.height > 0 for b in result.blocks))

    def test_text_is_found_through_the_observation_layer_with_a_usable_centre(self):
        context = ObservationContext(
            FakeScreenshots(target_id="real-1", frames=[decode_png(self.image.read_bytes())]), ocr=self.ocr)
        result = context.observe(TextQuery("Collect"))
        self.assertTrue(result.found, f"engine read: {result.detail}")
        self.assertEqual(result.kind, TEXT)
        centre = result.center
        assert centre is not None
        self.assertTrue(0 < centre.x < 420 and 0 < centre.y < 120, (centre.x, centre.y))
        self.assertFalse(context.observe(TextQuery("Nonexistent")).found)

    def test_an_engine_failure_is_reported_as_an_engine_error(self):
        from app.ocr.winrt import WindowsOcrProvider
        broken = WindowsOcrProvider(self.runner, Path(self.tmp.name), powershell="no-such-shell.exe")
        context = ObservationContext(FakeScreenshots(), ocr=broken)
        with self.assertRaises(OcrUnavailable):
            context.observe(TextQuery("Collect"))
        self.assertFalse(broken.available())


# ---- M / N: invalid queries ------------------------------------------------------------------------
class InvalidQueries(unittest.TestCase):
    def test_a_region_outside_the_frame_is_refused_as_a_query_mistake(self):
        ctx, _ = context()
        for bad in (Roi(190, 90, 50, 50), Roi(0, 0, 500, 10), Roi(199, 99, 2, 2)):
            with self.assertRaises(InvalidQuery):
                ctx.observe(ColorRegionQuery(RED, region=bad))

    def test_a_point_outside_the_frame_is_refused(self):
        ctx, _ = context()
        for bad in ((200, 10), (10, 100), (999, 999)):
            with self.assertRaises(InvalidQuery):
                ctx.observe(PixelQuery(bad, RED))

    def test_a_template_bigger_than_the_search_area_is_refused(self):
        ctx, _ = context()
        with self.assertRaises(InvalidQuery):
            ctx.observe(TemplateQuery(Bitmap.solid(300, 300, RED)))
        with self.assertRaises(InvalidQuery):
            ctx.observe(TemplateQuery(mark(), region=Roi(0, 0, 5, 5)))

    def test_a_featureless_template_is_refused_rather_than_matched_everywhere(self):
        ctx, _ = context()
        with self.assertRaises(InvalidQuery):
            ctx.observe(TemplateQuery(Bitmap.solid(4, 4, WHITE), min_confidence=0.9, max_results=9))

    def test_malformed_queries_are_rejected_where_they_are_written(self):
        with self.assertRaises(InvalidQuery):
            PixelQuery((1, 1), (300, 0, 0))
        with self.assertRaises(InvalidQuery):
            PixelQuery((1, 1), RED, tolerance=-1)
        with self.assertRaises(InvalidQuery):
            PixelQuery((-1, 0), RED)
        with self.assertRaises(InvalidQuery):
            ColorRegionQuery(RED, min_ratio=0)
        with self.assertRaises(InvalidQuery):
            ColorRegionQuery(RED, min_ratio=1.5)
        with self.assertRaises(InvalidQuery):
            TemplateQuery("not-a-bitmap")
        with self.assertRaises(InvalidQuery):
            TemplateQuery(mark(), max_results=0)
        with self.assertRaises(InvalidQuery):
            TextQuery("x", match="fuzzy")
        with self.assertRaises(InvalidQuery):
            TextQuery(123)  # pyright: ignore[reportArgumentType]  (deliberately not a string)

    def test_observe_refuses_anything_that_is_not_a_frame_or_not_a_query(self):
        with self.assertRaises(InvalidQuery):
            observe("emulator-5554", PixelQuery((1, 1), RED))
        with self.assertRaises(InvalidQuery):
            observe(frame_of(scene()), "find the button")


# ---- O / P: bounded waiting and cancellation -------------------------------------------------------
class WaitingAndCancellation(unittest.TestCase):
    def test_a_wait_that_is_already_true_returns_at_once_with_one_capture(self):
        ctx, provider = context()
        result = ctx.wait_until(PixelQuery((30, 20), RED), timeout_s=5)
        self.assertTrue(result.satisfied)
        self.assertEqual(provider.captures, 1)

    def test_a_wait_ends_when_the_screen_changes(self):
        blank = Bitmap.solid(200, 100, WHITE)
        ctx, provider = context(frames=[blank, blank, scene()])
        result = ctx.wait_until(PixelQuery((30, 20), RED), timeout_s=5, interval_s=0.05)
        self.assertTrue(result.satisfied)
        self.assertEqual(provider.captures, 3)

    def test_a_wait_that_never_comes_true_times_out_and_reports_what_it_last_saw(self):
        ctx, _ = context(frames=[Bitmap.solid(200, 100, WHITE)])
        started = time.monotonic()
        with self.assertRaises(ObservationTimeout) as caught:
            ctx.wait_until(PixelQuery((30, 20), RED), timeout_s=0.4, interval_s=0.05)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "the wait overran its own timeout")
        self.assertGreaterEqual(caught.exception.attempts, 1)
        self.assertIsInstance(caught.exception.last, ObservationResult)
        self.assertEqual(caught.exception.last.detail["actual"], WHITE)

    def test_a_wait_without_a_bound_is_refused(self):
        ctx, _ = context()
        for bad in (0, -1, None, "forever", 10_000):
            with self.assertRaises(ObservationError):
                ctx.wait_until(PixelQuery((1, 1), RED), timeout_s=bad)

    def test_the_emergency_stop_refuses_a_new_observation_immediately(self):
        stop = EmergencyStop()
        ctx, provider = context(stop=stop)
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            ctx.observe(PixelQuery((1, 1), RED))
        self.assertEqual(provider.captures, 0, "a capture was attempted after the emergency stop")

    def test_the_emergency_stop_interrupts_a_wait_instead_of_letting_it_run_its_timeout(self):
        stop = EmergencyStop()
        ctx, _ = context(frames=[Bitmap.solid(200, 100, WHITE)], stop=stop)
        threading.Timer(0.15, lambda: stop.trigger("stop during wait")).start()
        started = time.monotonic()
        with self.assertRaises(EmergencyStopped):
            ctx.wait_until(PixelQuery((30, 20), RED), timeout_s=30, interval_s=0.05)
        self.assertLess(time.monotonic() - started, 5.0, "the stop did not end the wait promptly")

    def test_cancellation_is_not_reported_as_a_capture_failure(self):
        stop = EmergencyStop()
        ctx, _ = context(stop=stop)
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            ctx.capture()                                # EmergencyStopped, never ObservationUnavailable


# ---- Q / R: several targets at once ----------------------------------------------------------------
class ConcurrentObservation(unittest.TestCase):
    def test_two_targets_observe_their_own_screens_with_no_shared_image(self):
        red_scene = Bitmap.solid(40, 40, WHITE).with_rect(5, 5, 10, 10, RED)
        blue_scene = Bitmap.solid(40, 40, WHITE).with_rect(5, 5, 10, 10, BLUE)
        a = ObservationContext(FakeScreenshots(target_id="ld1", frames=[red_scene]))
        b = ObservationContext(FakeScreenshots(target_id="ld2", frames=[blue_scene]))
        ra, rb = a.observe(PixelQuery((6, 6), RED)), b.observe(PixelQuery((6, 6), RED))
        self.assertTrue(ra.found)
        self.assertFalse(rb.found)
        self.assertEqual(rb.detail["actual"], BLUE)
        self.assertEqual((ra.target_id, rb.target_id), ("ld1", "ld2"))

    def test_many_targets_observing_in_parallel_never_see_each_others_pixels(self):
        """Q. Each target's colour is unique, so any cross-talk shows up as a wrong answer."""
        count = 8
        colours = [(10 + i * 20, 40, 200 - i * 10) for i in range(count)]
        contexts = [ObservationContext(FakeScreenshots(target_id=f"ld{i}",
                                                       frames=[Bitmap.solid(30, 30, colours[i])], delay=0.01))
                    for i in range(count)]
        results, errors = {}, []
        barrier = threading.Barrier(count)

        def work(i):
            try:
                barrier.wait(5)
                own = contexts[i].observe(PixelQuery((5, 5), colours[i]))
                others = [contexts[i].observe(PixelQuery((5, 5), colours[j])) for j in range(count) if j != i]
                results[i] = (own.found, any(o.found for o in others), own.detail["actual"])
            except BaseException as exc:                 # a thread's failure must not hang the test
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), count)
        for i, (own_found, saw_another, actual) in results.items():
            self.assertTrue(own_found, f"target {i} could not see its own colour")
            self.assertFalse(saw_another, f"target {i} saw another target's colour")
            self.assertEqual(actual, colours[i])

    def test_the_package_holds_no_module_level_mutable_state(self):
        """R. A cache that is not target-scoped would make targets share pixels, so there must not be one."""
        import app.observe as pkg
        root = Path(pkg.__file__).parent
        for path in sorted(root.glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith((" ", "\t", "#", "@", "class ", "def ")) or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name, value = name.strip(), value.strip()
                if not name.isidentifier() or name.startswith("__"):
                    continue
                self.assertFalse(value.startswith(("[", "{", "dict(", "list(", "set(")),
                                 f"{path.name}: module-level mutable {name}")

    def test_each_context_has_its_own_lock_and_its_own_frame(self):
        a, _ = context(target_id="ld1")
        b, _ = context(target_id="ld2")
        self.assertIsNot(a._lock, b._lock)
        a.observe(PixelQuery((1, 1), WHITE))
        self.assertIsNone(b._frame)

    def test_captures_are_counted_per_target(self):
        a, pa = context(target_id="ld1")
        b, pb = context(target_id="ld2")
        a.observe_all([PixelQuery((1, 1), WHITE), ColorRegionQuery(RED, min_ratio=0.001)])
        self.assertEqual((a.captures, b.captures), (1, 0))
        self.assertEqual((pa.captures, pb.captures), (1, 0))


# ---- one screenshot, many questions ----------------------------------------------------------------
class ScreenshotReuse(unittest.TestCase):
    def test_many_queries_cost_one_capture_and_describe_the_same_moment(self):
        ctx, provider = context(ocr=FakeOcr([block("Collect", 10, 20)]))
        results = ctx.observe_all([
            PixelQuery((30, 20), RED),
            ColorRegionQuery(RED, region=Roi(25, 15, 20, 20), min_ratio=0.2),
            TemplateQuery(Bitmap.solid(4, 4, RED), min_confidence=0.99, region=Roi(28, 18, 14, 14)),
            TextQuery("Collect"),
        ])
        self.assertEqual(provider.captures, 1, "observe_all captured more than once")
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r.found for r in results))
        self.assertEqual({r.captured_at for r in results}, {results[0].captured_at})
        self.assertEqual([r.kind for r in results], [PIXEL, COLOR_REGION, TEMPLATE, TEXT])

    def test_an_empty_query_list_is_refused_rather_than_capturing_for_nothing(self):
        ctx, provider = context()
        with self.assertRaises(ObservationError):
            ctx.observe_all([])
        self.assertEqual(provider.captures, 0)

    def test_the_age_of_a_reused_frame_is_visible_in_the_result(self):
        ctx, _ = context()
        ctx.observe(PixelQuery((1, 1), WHITE))
        time.sleep(0.05)
        later = ctx.observe(PixelQuery((1, 1), WHITE), max_age_s=10)
        self.assertGreater(later.age_s, 0.0)
        self.assertGreaterEqual(later.observed_at, later.captured_at)


# ---- result model ----------------------------------------------------------------------------------
class ResultModel(unittest.TestCase):
    def test_every_kind_answers_in_the_same_shape(self):
        ctx, _ = context(frames=[three_marks()], ocr=FakeOcr([block("Collect", 10, 20)]))
        for query in (PixelQuery((10, 10), RED), ColorRegionQuery(RED, min_ratio=0.001),
                      TemplateQuery(mark(), min_confidence=0.99), TextQuery("Collect")):
            with self.subTest(query=type(query).__name__):
                r = ctx.observe(query)
                for attribute in ("found", "satisfied", "confidence", "bounds", "center", "matches",
                                  "captured_at", "observed_at", "detail", "source", "target_id", "age_s"):
                    self.assertTrue(hasattr(r, attribute), attribute)
                self.assertIsInstance(r.found, bool)
                self.assertTrue(0.0 <= r.confidence <= 1.0)

    def test_detail_cannot_carry_anything_executable(self):
        from app.observe.result import Match
        with self.assertRaises(ValueError):
            Match(Roi(0, 0, 1, 1), 1.0, {"act": lambda: None})
        with self.assertRaises(ValueError):
            Match(Roi(0, 0, 1, 1), 1.0, {"session": object()})

    def test_a_result_cannot_claim_a_find_with_nothing_to_point_at(self):
        from app.providers.base import FrameSource
        with self.assertRaises(ValueError):
            ObservationResult(kind=PIXEL, found=True, source=FrameSource("p", "t"),
                              captured_at=0.0, observed_at=0.0, matches=())

    def test_detail_is_read_only(self):
        ctx, _ = context()
        result = ctx.observe(PixelQuery((1, 1), WHITE))
        with self.assertRaises(TypeError):
            result.detail["actual"] = (0, 0, 0)


# ---- V: architecture -------------------------------------------------------------------------------
class Architecture(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1] / "app"

    def imports_of(self, path: Path) -> set[str]:
        import ast
        out = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
        return out

    def code_names(self, path: Path) -> set[str]:
        """Every name the CODE uses: imports, identifiers, attributes and string constants -- but not
        docstrings or comments.

        The constraint being enforced is about what the code depends on, not about which words a design note
        may use to explain itself. A module that says in prose "this also works for a device reached over a
        link" has not acquired a dependency; one that writes `import app.adb` has. Scanning raw text cannot
        tell those apart, so this looks at the syntax tree."""
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
            elif isinstance(node, ast.Name):
                out.add(node.id)
            elif isinstance(node, ast.Attribute):
                out.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value not in docstrings:
                out.add(node.value)
        return out

    def test_observation_imports_only_the_neutral_layers_below_it(self):
        allowed = ("app.core", "app.providers", "app.vision", "app.ocr", "app.observe")
        for path in sorted((self.ROOT / "observe").glob("*.py")):
            for module in self.imports_of(path):
                if module.startswith("app."):
                    self.assertTrue(module.startswith(allowed), f"{path.name} imports {module}")

    def test_observation_never_names_a_provider_a_technology_or_an_engine(self):
        banned = ("ldplayer", "adb", "leidian", "emulator", "serial", "hwnd", "win32", "ctypes",
                  "subprocess", "tesseract", "opencv", "cv2", "numpy", "pillow", "powershell",
                  "app.adb", "app.ldplayer", "app.windows", "app.gui", "app.instances", "app.macros")
        for path in sorted((self.ROOT / "observe").glob("*.py")):
            for name in self.code_names(path):
                for word in banned:
                    self.assertNotIn(word, name.lower(), f"{path.name}: code uses {name!r}")

    def test_observation_cannot_send_input(self):
        banned = ("inputsession", "input_tap", "key_press", "send_key", "type_text", "playbackengine",
                  "app.playback", "click")
        for path in sorted((self.ROOT / "observe").glob("*.py")):
            for name in self.code_names(path):
                for word in banned:
                    self.assertNotIn(word, name.lower(), f"{path.name}: code uses {name!r}")
            self.assertNotIn("tap", {n.lower() for n in self.code_names(path)}, path.name)

    def test_the_vision_layer_still_knows_nothing_about_observation_or_ocr(self):
        """Dependency direction: vision is below observation and must not reach up to it."""
        for path in sorted((self.ROOT / "vision").glob("*.py")):
            for module in self.imports_of(path):
                self.assertFalse(module.startswith(("app.observe", "app.ocr", "app.ai")),
                                 f"{path.name} imports {module}")

    def test_the_ocr_contract_still_knows_nothing_about_any_engine(self):
        text = (self.ROOT / "ocr" / "provider.py").read_text(encoding="utf-8")
        for banned in ("tesseract", "winrt", "powershell", "subprocess", "easyocr", "paddle"):
            self.assertNotIn(banned.lower(), text.lower(), banned)

    def test_the_ocr_engine_does_not_know_where_the_image_came_from(self):
        """An OCR engine reads a Frame. Whether those pixels arrived over a device link or from a desktop
        window is not its business and must not appear in its code."""
        path = self.ROOT / "ocr" / "winrt.py"
        for name in self.code_names(path):
            for word in ("ldplayer", "adb", "serial", "hwnd", "app.windows", "app.ldplayer", "app.adb",
                         "app.gui", "app.instances"):
                self.assertNotIn(word, name.lower(), f"code uses {name!r}")
        for module in self.imports_of(path):
            if module.startswith("app."):
                self.assertTrue(module.startswith(("app.core", "app.vision", "app.ocr")), module)

    def test_the_core_still_does_not_depend_on_observation_ocr_or_ai(self):
        """Observation is optional: the platform must run with none of it imported."""
        for package in ("core", "providers", "playback", "automation", "vision"):
            for path in sorted((self.ROOT / package).rglob("*.py")):
                bad = {m for m in self.imports_of(path)
                       if m.startswith(("app.observe", "app.ocr", "app.ai"))}
                self.assertEqual(bad, set(), f"{package}/{path.name} imports {bad}")

    def test_the_gui_uses_observation_through_its_contract_and_never_names_ocr(self):
        """The GUI asks questions through `ObservationContext` and hands on the text reader it was given. It imports
        neither how a query is evaluated nor any OCR module: the reader's TYPE (the OCR contract) and "can a text
        minimum be applied?" come from the observation API, which owns them. It depends on that contract through
        this API -- never on an engine; only the composition root chooses one (`tests.test_ai_ocr`)."""
        for path in sorted((self.ROOT / "gui").rglob("*.py")):
            modules = {m for m in self.imports_of(path) if m.startswith(("app.observe", "app.ocr", "app.ai"))}
            self.assertLessEqual(modules, {"app.observe"}, path.name)

    def test_the_composition_root_is_the_one_place_that_chooses_an_ocr_engine(self):
        for path in sorted((self.ROOT / "bootstrap").rglob("*.py")):
            modules = {m for m in self.imports_of(path) if m.startswith(("app.observe", "app.ocr", "app.ai"))}
            self.assertLessEqual(modules, {"app.ocr.winrt"}, path.name)


if __name__ == "__main__":
    unittest.main()
