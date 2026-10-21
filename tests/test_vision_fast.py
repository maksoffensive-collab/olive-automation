"""Fast picture search with OpenCV (optional; the person's approval of 2026-10-20): the exact matcher's answer --
the same places, confidences and order, the same refusals -- found much faster; and everything still works, the same
way, without OpenCV.

Synthetic pictures only (no screen is read); the matcher is pure computation.
"""
import random
import subprocess
import sys
import textwrap
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
        """85.8 s for the exact matcher on such a picture (docs/OBSERVATION.md)."""
        scene = noise(1280, 720, 7)
        pattern = cut(scene, 900, 500, 48, 32)
        started = time.perf_counter()
        with mock.patch.object(tm, "_exact", side_effect=AssertionError("the slow search was used")):
            found = tm.match_template(frame(scene), pattern)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual([(m.x, m.y, m.confidence) for m in found], [(900, 500, 1.0)])


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
