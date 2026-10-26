"""Fast picture search with OpenCV (optional; the person's approval of 2026-10-20): the standard-library search's
answer -- the same places, confidences and order, the same refusals -- found much faster, well under a second for a
whole screen also when the screen is smooth and dark (2026-10-21: the first fast search took minutes there); and
everything still works, the same way, without OpenCV.

Synthetic pictures only (no screen is read); the matcher is pure computation.
"""
import random
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.providers.base import ScreenshotProvider, TargetIdentity
from app.vision import template as tm
from app.vision.bitmap import Bitmap, VisionError, encode_png
from app.vision.frame import Frame
from app.vision.roi import Roi

ROOT = Path(__file__).resolve().parents[1]
FAST = tm.fast_matcher()


class _Pictures(ScreenshotProvider):
    identity = TargetIdentity("synthetic", "t1")

    def __init__(self, bitmap: Bitmap) -> None:
        self._bitmap = bitmap

    def capture(self):
        return self._issue_screenshot(encode_png(self._bitmap), self._bitmap.width, self._bitmap.height)


def frame(bitmap: Bitmap) -> Frame:
    return Frame.from_screenshot(_Pictures(bitmap).capture())


def noise(width: int, height: int, seed: int, *, levels: int = 256) -> Bitmap:
    rnd = random.Random(seed)
    step = 256 // levels
    return Bitmap(width, height, bytes(rnd.randrange(levels) * step for _ in range(width * height * 3)))


def smooth(width: int, height: int, seed: int, *, base: int, amp: int) -> Bitmap:
    """A picture like a real screen rather than noise: colours that change slowly (a 40-pixel grid of random colours
    around `base`, at most `amp` up or down, enlarged by the fast search's own resizing -- so only with OpenCV)."""
    rnd = random.Random(seed)
    gw, gh = width // 40 + 2, height // 40 + 2
    grid = bytes(max(0, min(255, base + rnd.randrange(-amp, amp + 1))) for _ in range(gw * gh * 3))
    return Bitmap(width, height, FAST.resize(grid, gw, gh, width, height))


def cut(bitmap: Bitmap, x: int, y: int, w: int, h: int) -> Bitmap:
    rows = [bitmap.rgb[((y + r) * bitmap.width + x) * 3:((y + r) * bitmap.width + x + w) * 3] for r in range(h)]
    return Bitmap(w, h, b"".join(rows))


def paste(scene: Bitmap, part: Bitmap, x: int, y: int, *, brighter: int = 0) -> Bitmap:
    """`part` put into `scene` at (x, y), each value raised by `brighter` (a copy that is close, not exact)."""
    data = bytearray(scene.rgb)
    for r in range(part.height):
        row = bytes(min(255, v + brighter) for v in part.rgb[r * part.width * 3:(r + 1) * part.width * 3])
        i = ((y + r) * scene.width + x) * 3
        data[i:i + len(row)] = row
    return Bitmap(scene.width, scene.height, bytes(data))


def answer(matcher, *args, **kwargs):
    try:
        return [(m.x, m.y, m.width, m.height, round(m.confidence, 12)) for m in matcher(*args, **kwargs)]
    except VisionError as exc:
        return ("refused", str(exc))


@unittest.skipIf(FAST is None, "OpenCV is not installed")
class SameAnswerFaster(unittest.TestCase):
    def test_the_fast_search_gives_exactly_the_exact_matchers_answer(self):
        """Pictures with the template once, many times, slightly changed, nowhere; every threshold, region and count."""
        cases = 0
        for seed in range(6):
            scene = noise(90, 60, seed, levels=4 if seed % 2 else 256)       # coarse pictures repeat themselves
            pattern = cut(scene, 20 + seed, 10 + seed, 9, 7)
            if seed in (2, 3):              # a close copy elsewhere: within reach of the lower thresholds only
                scene = paste(scene, pattern, 60, 35, brighter=10)
            for min_confidence in (1.0, 0.97, 0.9, 0.8):
                for roi in (None, Roi(5, 5, 70, 45)):
                    for max_results in (1, 3):
                        with self.subTest(seed=seed, min_confidence=min_confidence, roi=roi, max_results=max_results):
                            args = (frame(scene), pattern)
                            kwargs = dict(min_confidence=min_confidence, roi=roi, max_results=max_results)
                            self.assertEqual(answer(tm.match_template, *args, **kwargs),
                                             answer(tm.match_template_exact, *args, **kwargs))
                            cases += 1
        self.assertEqual(cases, 6 * 4 * 2 * 2)

    def test_a_flat_template_is_refused_the_same_way(self):
        scene, flat = Bitmap.solid(120, 80, (10, 20, 30)), Bitmap.solid(6, 6, (10, 20, 30))   # 8625 places
        self.assertEqual(answer(tm.match_template, frame(scene), flat),
                         answer(tm.match_template_exact, frame(scene), flat))
        self.assertEqual(answer(tm.match_template, frame(scene), flat)[0], "refused")

    def test_the_whole_screen_is_searched_in_well_under_a_second(self):
        """85.8 s for the standard-library search on such a picture (docs/OBSERVATION.md)."""
        scene = noise(1280, 720, 7)
        pattern = cut(scene, 900, 500, 48, 32)
        started = time.perf_counter()
        with mock.patch.object(tm, "_exact", side_effect=AssertionError("the slow search was used")):
            found = tm.match_template(frame(scene), pattern)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual([(m.x, m.y, m.confidence) for m in found], [(900, 500, 1.0)])

    def test_a_smooth_dark_full_hd_screen_is_searched_in_under_a_second(self):
        """2026-10-21: on a screen like a real one -- smooth, dark, little contrast -- the first fast search ruled out
        no place at all and measured all 1.6 million of them: 290 s for a 150 x 150 picture that was not there."""
        scene = smooth(1920, 1080, 8, base=60, amp=25)
        absent = smooth(150, 150, 9, base=60, amp=25)
        present = cut(scene, 1200, 700, 150, 150)
        with mock.patch.object(tm, "_exact", side_effect=AssertionError("the slow search was used")):
            for pattern, expected in ((absent, []), (present, [(1200, 700, 1.0)])):
                with self.subTest(expected=expected):
                    started = time.perf_counter()
                    found = tm.match_template(frame(scene), pattern)
                    self.assertLess(time.perf_counter() - started, 1.0)
                    self.assertEqual([(m.x, m.y, m.confidence) for m in found], expected)

    def test_a_large_area_is_searched_in_strips_the_same_way_and_the_stop_is_heard_between_them(self):
        from app.core.errors import EmergencyStopped
        from app.vision import fast
        scene = noise(200, 150, 5)
        pattern = cut(scene, 120, 100, 9, 7)
        whole = answer(tm.match_template, frame(scene), pattern, min_confidence=0.3, max_results=5)
        strips = []
        with mock.patch.object(fast, "STRIP_PIXELS", 200 * 8):           # 8 rows of places a strip: 18 strips
            found = tm.search_template(frame(scene), pattern, min_confidence=0.3, max_results=5,
                                       check=lambda: strips.append(1))
            self.assertEqual([(m.x, m.y, m.width, m.height, round(m.confidence, 12)) for m in found.matches], whole)
            self.assertEqual(len(strips), 18 + 1)                            # and one piece measured

            counting = threading.Lock()                                    # strips are looked at by several threads

            def stop():
                with counting:
                    if len(strips) == 24:
                        raise EmergencyStopped("test")
                    strips.append(1)
            with self.assertRaises(EmergencyStopped):
                tm.search_template(frame(scene), pattern, check=stop)
        self.assertEqual(len(strips), 24)                                    # ended before its sixth strip

    def test_a_picture_not_found_says_where_it_came_closest(self):
        from app.observe import ObservationContext, TemplateQuery
        scene = noise(80, 60, 3)
        brighter = Bitmap(9, 7, bytes(min(255, v + 10) for v in cut(scene, 30, 20, 9, 7).rgb))
        result = ObservationContext(_Pictures(scene)).observe(TemplateQuery(brighter, min_confidence=0.99))
        self.assertFalse(result.found)
        best = result.detail["best"]
        self.assertEqual((best["x"], best["y"], best["width"], best["height"]), (30, 20, 9, 7))
        self.assertTrue(0.9 < best["confidence"] < 0.99, best)

    def test_any_size_on_a_full_hd_screen_takes_about_a_second_at_most(self):
        scene = smooth(1920, 1080, 10, base=60, amp=40)
        pattern = cut(scene, 500, 300, 120, 80)
        bigger = Bitmap(168, 112, FAST.resize(pattern.rgb, 120, 80, 168, 112))       # the same, 140% as large
        data = bytearray(scene.rgb)
        for r in range(112):
            i = ((800 + r) * 1920 + 1500) * 3
            data[i:i + 168 * 3] = bigger.rgb[r * 168 * 3:(r + 1) * 168 * 3]
        scene = Bitmap(1920, 1080, bytes(data))
        started = time.perf_counter()
        found = tm.search_template(frame(scene), pattern, any_size=True, min_side=4, roi=Roi(1000, 500, 920, 580))
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertEqual([(m.x, m.y, m.width, m.height) for m in found.matches], [(1500, 800, 168, 112)])
        self.assertEqual(found.scale, 1.4)
        self.assertGreater(found.matches[0].confidence, 0.99)


def grey(width: int, height: int, value) -> Bitmap:
    """A grey picture whose value at (x, y) is `value(x, y)`."""
    return Bitmap(width, height, bytes(v for y in range(height) for x in range(width) for v in [value(x, y)] * 3))


@unittest.skipIf(FAST is None, "OpenCV is not installed")
class ReviewOfTheFastSearch(unittest.TestCase):
    """The review of 2026-10-21: the fast search measured only the peaks of the SHAPE, so a place whose confidence
    (shape x colour) reached the threshold beside a better shape was never measured; and more."""

    def same(self, scene, pattern, **kwargs):
        fast = answer(tm.match_template, frame(scene), pattern, **kwargs)
        self.assertEqual(fast, answer(tm.match_template_exact, frame(scene), pattern, **kwargs))
        return fast

    def test_on_a_ramp_the_place_whose_colour_is_closest_is_found(self):
        """Every place on the band has the template's shape exactly; only the colour tells them apart."""
        pattern = grey(10, 10, lambda x, y: 100 + 5 * x)
        scene = grey(100, 50, lambda x, y: max(0, min(255, 103 + 5 * (x - 50))) if 20 <= y < 30 else 0)
        found = self.same(scene, pattern, min_confidence=0.99, max_results=3)
        self.assertEqual([m[:2] for m in found], [(49, 20)])

    def test_a_one_pixel_wide_template_finds_places_side_by_side(self):
        """Two places a pixel apart do not overlap a template one pixel wide: both are answers."""
        rnd = random.Random(4)
        column = [rnd.randrange(256) for _ in range(6 * 3)]
        scene = noise(40, 30, 6)
        data = bytearray(scene.rgb)
        for r in range(6):
            i = ((10 + r) * 40 + 10) * 3
            data[i:i + 3] = bytes(column[r * 3:r * 3 + 3])
            data[i + 3:i + 6] = bytes(min(255, v + 4) for v in column[r * 3:r * 3 + 3])   # a close copy beside it
        data[(15 * 40 + 11) * 3] ^= 0x10                                               # its shape a little less
        scene = Bitmap(40, 30, bytes(data))
        found = self.same(scene, Bitmap(1, 6, bytes(column)), min_confidence=0.9, max_results=2)
        self.assertEqual([m[:2] for m in found], [(10, 10), (11, 10)])

    def test_smooth_and_bright_low_contrast_pictures_give_the_reference_answer(self):
        """Not only noise: smooth pictures, and bright ones with little contrast (where a float32 map is least
        exact), at every threshold."""
        cases = 0
        for seed, base, amp in ((1, 60, 40), (2, 235, 20), (5, 230, 25), (3, 128, 35)):   # spreads 2.7 .. 7
            scene = smooth(90, 60, seed, base=base, amp=amp)
            for x, y in ((20, 10), (55, 31)):
                pattern = cut(scene, x, y, 12, 9)
                try:
                    tm.check_template(pattern)
                except VisionError:
                    continue                                                   # too flat to be looked for
                for min_confidence in (0.999, 0.99, 0.95, 0.8):
                    with self.subTest(seed=seed, x=x, min_confidence=min_confidence):
                        found = self.same(scene, pattern, min_confidence=min_confidence, max_results=3)
                        self.assertIn((x, y), [m[:2] for m in found])
                        cases += 1
        self.assertEqual(cases, 4 * 2 * 4)

    def test_the_map_is_close_to_the_exact_measure_also_on_bright_pictures_with_little_contrast(self):
        """OpenCV adds up in float32; on a bright picture with little contrast its raw sums lose 0.005 of the
        confidence (measured 2026-10-21). Both pictures are first less the template's average: 1e-7."""
        from app.vision import fast
        scene = smooth(120, 80, 2, base=235, amp=20)
        pattern = cut(scene, 40, 30, 24, 18)
        tsums, tsquares = tm._template_sums(pattern)
        approx = fast.score_map(fast._view(scene.rgb, 120, 80), fast._view(pattern.rgb, 24, 18))
        places = [(y, x) for y in range(0, 63, 4) for x in range(0, 97, 4)] + [(30, 40)]
        for (c, sums, q), (y, x) in zip(fast.sums(scene.rgb, 120, 80, pattern.rgb, 24, 18, places), places):
            self.assertAlmostEqual(float(approx[y, x]), tm.confidence(24 * 18, c, sums, q, tsums, tsquares), places=5)

    def test_a_tall_template_is_not_searched_strip_after_tiny_strip(self):
        """A strip is at least as tall as the template: at most twice the work of one pass."""
        from app.vision import fast
        scene = noise(120, 100, 8)
        pattern = cut(scene, 30, 20, 10, 40)
        calls = []
        real = fast.shape_map

        def counted(area, picture):
            calls.append(area.shape[0])
            return real(area, picture)
        with mock.patch.object(fast, "STRIP_PIXELS", 120 * 20), mock.patch.object(fast, "shape_map", counted):
            found = tm.match_template(frame(scene), pattern)
        self.assertEqual([(m.x, m.y) for m in found], [(30, 20)])
        self.assertLessEqual(len(calls), 2)                                   # 61 rows of places, 40 a strip
        self.assertLessEqual(sum(calls), 2 * 100)

    def test_any_size_finds_the_coloured_one_among_grey_copies_of_the_same_shape(self):
        pattern = grey(16, 12, lambda x, y: 40 if (x // 4 + y // 4) % 2 else 200)
        green = Bitmap(16, 12, bytes(v for i in range(16 * 12) for v in
                                     ((20, 160, 40) if pattern.rgb[i * 3] == 200 else (10, 40, 10))))
        scene = Bitmap.solid(300, 120, (90, 90, 90))
        for k in range(9):                                                     # nine grey copies, larger
            scene = paste(scene, Bitmap(24, 18, FAST.resize(pattern.rgb, 16, 12, 24, 18)), 5 + k * 30, 10)
        scene = paste(scene, Bitmap(24, 18, FAST.resize(green.rgb, 16, 12, 24, 18)), 140, 80)
        found = tm.search_template(frame(scene), green, any_size=True, min_side=4, min_confidence=0.9)
        self.assertEqual([(m.x, m.y, m.width, m.height) for m in found.matches], [(140, 80, 24, 18)])

    def test_the_stop_is_heard_between_the_fine_looks_of_a_search_in_any_size(self):
        from app.core.errors import EmergencyStopped
        from app.vision import fast
        scene = smooth(400, 300, 5, base=100, amp=60)
        pattern = cut(scene, 100, 100, 60, 40)
        looks, real, armed = [], fast.score_map, []

        def counted(area, picture):
            looks.append(1)
            armed.append(True)
            return real(area, picture)

        def check():
            if armed:
                raise EmergencyStopped("test")
        with mock.patch.object(fast, "score_map", counted), self.assertRaises(EmergencyStopped):
            tm.search_template(frame(scene), pattern, any_size=True, min_side=4, check=check)
        self.assertEqual(len(looks), 1)                                       # not one look more after the stop

    def test_a_fine_pattern_flat_at_a_smaller_size_does_not_hide_the_real_copy(self):
        """Review 2026-10-21: OpenCV gives a flat template 1.0 at every place; made smaller, a fine pattern turns flat,
        and those places filled every promising slot. A flat picture has no shape: 0, as the exact measure says."""
        from app.vision import fast
        self.assertFalse(fast.shape_map(fast._view(noise(30, 20, 1).rgb, 30, 20), fast._view(
            Bitmap.solid(4, 4, (9, 9, 9)).rgb, 4, 4)).any())
        pattern = grey(12, 12, lambda x, y: 0 if (x + y) % 2 else 255)
        bigger = Bitmap(17, 17, bytes(min(255, v + 3) for v in FAST.resize(pattern.rgb, 12, 12, 17, 17)))
        scene = paste(Bitmap.solid(150, 100, (127, 127, 127)), bigger, 90, 50)
        found = tm.search_template(frame(scene), pattern, any_size=True, min_side=4, min_confidence=0.9)
        self.assertEqual([(m.x, m.y, m.width, m.height) for m in found.matches], [(90, 50, 17, 17)])

    def test_many_places_near_a_low_threshold_are_measured_in_bounded_time(self):
        """Review 2026-10-21: a small picture on a smooth screen at a low threshold puts most places near it; they
        are thinned to local maxima beyond LIMIT_PLACES, so a look stays well under a second."""
        from app.vision import fast
        scene = smooth(1920, 1080, 11, base=120, amp=60)
        pattern = cut(scene, 900, 500, 8, 8)
        measured = []
        real = fast.sums

        def counted(*args, **kwargs):
            measured.append(len(args[6]))
            return real(*args, **kwargs)
        started = time.perf_counter()
        with mock.patch.object(fast, "sums", counted):
            found = tm.match_template(frame(scene), pattern, min_confidence=0.3, max_results=3)
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertLessEqual(measured[0], fast.LIMIT_PLACES + fast.CLOSEST)
        self.assertEqual((found[0].x, found[0].y, found[0].confidence), (900, 500, 1.0))

    def test_any_size_with_many_results_does_not_refuse_a_picture_it_accepted(self):
        """A fine pattern is accepted at its own size; made smaller it may look flat -- that is no refusal."""
        from app.vision import fast
        pattern = grey(20, 20, lambda x, y: 0 if (x + y) % 2 else 255)        # half its size: one grey
        scene = paste(Bitmap.solid(120, 80, (128, 128, 128)), pattern, 50, 30)
        with mock.patch.object(fast, "best_size", return_value=(0.5, 10, 10, 30, 50)):
            found = tm.search_template(frame(scene), pattern, any_size=True, min_side=4, max_results=2)
        self.assertEqual((found.matches, found.scale), ((), 0.5))

    def test_a_flat_template_is_refused_by_a_check_made_before_any_search(self):
        tm.check_template(cut(noise(20, 20, 1), 2, 2, 6, 6))
        with self.assertRaises(VisionError):
            tm.check_template(Bitmap.solid(6, 6, (10, 20, 30)))

    def test_a_script_waiting_for_a_flat_picture_is_refused_before_its_first_step_acts(self):
        """Review 2026-10-21: the refusal of a flat picture came only when the WAIT looked -- after step 1 had
        tapped. It is now made before the run's first step, with the other questions a run could never ask."""
        from app.decide import Condition, FlowState, RunMacro, WaitUntil
        from app.playback.macro import Macro
        from app.providers.recording import Recording
        from tests.test_decide import Rig
        from tests.test_playback import PROV, TID, down, up
        rig = Rig([Bitmap.solid(200, 100, (255, 255, 255)).with_rect(30, 20, 4, 4, (220, 20, 20))])
        report = rig.run(RunMacro(Macro(Recording(PROV, TID, (down(0, 5, 6), up(10, 5, 6))), "tap first")),
                         WaitUntil(Condition.template(Bitmap.solid(6, 6, (220, 20, 20))), timeout_s=5,
                                   interval_s=0.05))
        self.assertEqual((report.state, report.reason, report.steps), (FlowState.FAILED, "invalid_query", ()))
        self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_the_editor_refuses_a_flat_picture_when_it_is_picked(self):
        from app.gui import flow_edit as fe
        from app.gui.macro_edit import EditError
        form = fe.condition_form(None)
        form.kind, form.fields = fe.TEMPLATE, {"min_confidence": "0.95", "max_results": "1"}
        form.template = Bitmap.solid(8, 8, (10, 20, 30))
        with self.assertRaises(EditError) as caught:
            fe.build_condition(form)
        self.assertEqual(caught.exception.key, "err_flow_template_flat")
        form.template = cut(noise(20, 20, 1), 2, 2, 8, 8)
        self.assertEqual(fe.build_condition(form).query.template, form.template)

    def test_a_picture_that_adapting_would_make_flat_is_refused_before_anything_is_done(self):
        from app.observe import InvalidQuery, ObservationContext, TemplateQuery
        stripes = grey(16, 16, lambda x, y: 0 if x % 2 else 255)            # 50% of it: one grey
        context = ObservationContext(_Pictures(noise(40, 40, 3)), picked_on=(80, 80))
        with self.assertRaises(InvalidQuery):
            context.check_size((TemplateQuery(stripes),))

    def test_the_standard_library_search_measures_only_its_area_and_hears_the_stop_while_it_prepares(self):
        from app.core.errors import EmergencyStopped
        scene = noise(200, 150, 2)
        pattern = cut(scene, 120, 100, 9, 7)
        sizes = []
        real = tm._integral

        def made(values, width, area, *args):
            sizes.append((area.width, area.height))
            return real(values, width, area, *args)
        with mock.patch.object(tm, "_integral", made):
            found = tm.match_template_exact(frame(scene), pattern, roi=Roi(110, 90, 30, 20))
        self.assertEqual([(m.x, m.y) for m in found], [(120, 100)])
        self.assertEqual(set(sizes), {(30, 20)})
        rows = []

        def check():
            rows.append(1)
            if len(rows) == 5:
                raise EmergencyStopped("test")
        with self.assertRaises(EmergencyStopped):
            tm._search(frame(scene), pattern, 0.9, None, 1, None, check)
        self.assertEqual(len(rows), 5)

    def test_the_pieces_measured_at_a_time_stay_within_their_memory(self):
        from app.vision import fast
        scene = noise(60, 40, 4)
        pattern = cut(scene, 20, 10, 20, 15)
        places = [(y, x) for y in range(0, 25, 2) for x in range(0, 40, 3)]
        per_place = 20 * 15 * 3 * 8
        pieces = []
        with mock.patch.object(fast, "MEMORY", 3 * per_place):
            fast.sums(scene.rgb, 60, 40, pattern.rgb, 20, 15, places, check=lambda: pieces.append(1))
        self.assertEqual(len(pieces), -(-len(places) // 3))                   # three places a piece


class WithoutOpenCV(unittest.TestCase):
    def test_the_fast_search_is_used_whenever_opencv_is_installed(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest("OpenCV is not installed")
        self.assertIsNotNone(tm.fast_matcher())

    def test_everything_works_the_same_way_without_opencv(self):
        code = textwrap.dedent('''
            import sys
            sys.modules["cv2"] = None                         # as if it were not installed
            from app.vision import template as tm
            assert tm.fast_matcher() is None
            from tests.test_vision_fast import cut, frame, noise
            scene = noise(40, 30, 1)
            found = tm.match_template(frame(scene), cut(scene, 10, 12, 6, 5))
            assert [(m.x, m.y, m.confidence) for m in found] == [(10, 12, 1.0)], found
            assert "cv2" not in [m for m in sys.modules if sys.modules[m] is not None]
            print("ok")
        ''')
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ok"), out.stderr)

    def test_only_the_fast_matcher_imports_opencv_or_numpy(self):
        for path in sorted((ROOT / "app").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            uses = any(f"import {m}" in text or f"from {m}" in text for m in ("cv2", "numpy"))
            if uses:
                self.assertEqual(path.relative_to(ROOT).as_posix(), "app/vision/fast.py")


if __name__ == "__main__":
    unittest.main()
