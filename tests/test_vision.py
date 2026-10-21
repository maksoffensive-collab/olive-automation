"""Tests for the local vision layer. All images are synthetic; frames are produced through the LDPlayer adapter
(VerifiedTarget -> AdbSession -> ScreenshotProvider) with a fake ADB so the provenance rule is exercised too."""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from app.ldplayer.provider import LDPlayerScreenshotProvider
from app.core.cancel import EmergencyStop
from app.core.errors import SafetyViolation
from app.macros.record_coords import (AssumedRecordCanvas, CoordinateSpaceUnresolved, RecordPoint,
                                      record_to_pixel)
from app.providers.base import FrameSource, Screenshot, ScreenshotProvider
from app.vision.bitmap import Bitmap, VisionError, decode_png, load_png
from app.vision.change import compare_frames
from app.vision.color import find_color
from app.vision.coords import (CoordinateError, NormalizedPoint, PixelPoint, normalized_to_pixel,
                               pixel_to_normalized, rescale_pixel)
from app.vision.frame import Frame
from app.vision.roi import Roi, resolve_roi
from app.vision.template import match_template
from tests.test_adb_session import SessionAdb
from tests.test_mapping import make_service, two_instance_world

RED, GREEN, BLUE, WHITE, BLACK = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(bm: Bitmap, ctype=2, ftype=0) -> bytes:
    """Test-only PNG encoder supporting colour types 0/2/6 and all five filter types."""
    bpp = {0: 1, 2: 3, 6: 4}[ctype]
    rows = []
    for y in range(bm.height):
        px = bm.rgb[y * bm.width * 3:(y + 1) * bm.width * 3]
        if ctype == 2:
            rows.append(bytearray(px))
        elif ctype == 6:
            r = bytearray(bm.width * 4)
            r[0::4], r[1::4], r[2::4], r[3::4] = px[0::3], px[1::3], px[2::3], bytes([255]) * bm.width
            rows.append(r)
        else:
            rows.append(bytearray(px[0::3]))
    raw, prev = bytearray(), bytearray(len(rows[0]))
    for cur in rows:
        out = bytearray(len(cur))
        for i in range(len(cur)):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ftype == 0:
                pred = 0
            elif ftype == 1:
                pred = a
            elif ftype == 2:
                pred = b
            elif ftype == 3:
                pred = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out[i] = (cur[i] - pred) & 255
        raw += bytes([ftype]) + out
        prev = cur
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", bm.width, bm.height, 8, ctype, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b""))


class _SizedAdb(SessionAdb):
    def __init__(self, w, bm):
        super().__init__(w)
        self.bm = bm

    def shell_query(self, serial, *cmd):
        if cmd == ("wm", "size"):
            return f"Physical size: {self.bm.width}x{self.bm.height}"
        return super().shell_query(serial, *cmd)

    def screencap_png(self, serial, *, grant):
        return encode_png(self.bm)


def shot_of(bm: Bitmap, ld="ld1") -> Screenshot:
    w = two_instance_world()
    svc = make_service(w)
    adb = _SizedAdb(w, bm)
    svc._adb = adb
    stop = EmergencyStop()
    return LDPlayerScreenshotProvider(svc.verify(ld), adb, svc, stop).capture()


def frame_of(bm: Bitmap, ld="ld1") -> Frame:
    return Frame.from_screenshot(shot_of(bm, ld))


def scene() -> Bitmap:
    """200x100 white image with a red 10x10 square at (30,20) and a blue 6x4 rect at (150,70)."""
    return Bitmap.solid(200, 100, WHITE).with_rect(30, 20, 10, 10, RED).with_rect(150, 70, 6, 4, BLUE)


class ProvenanceTests(unittest.TestCase):
    def test_frame_only_from_pipeline_screenshot(self):
        with self.assertRaises(SafetyViolation):
            Screenshot(b"x", 1, 1, FrameSource("ldplayer-adb", "ld1"))  # cannot be forged
        with self.assertRaises(SafetyViolation):
            Frame(FrameSource("ldplayer-adb", "ld1"), scene())  # direct construction is refused too
        with self.assertRaises(VisionError):
            Frame.from_screenshot("emulator-5554")
        with self.assertRaises(VisionError):
            Frame.from_screenshot(b"png bytes")

    def test_frame_keeps_logical_id_and_native_size(self):
        f = frame_of(scene(), "ld2")
        self.assertEqual((f.target_id, f.source.provider_id, f.width, f.height), ("ld2", "ldplayer-adb", 200, 100))
        self.assertEqual(f.bitmap.pixel(30, 20), RED)

    def test_vision_functions_reject_non_frames(self):
        bm = scene()
        with self.assertRaises(VisionError):
            find_color("emulator-5554", RED)
        with self.assertRaises(VisionError):
            find_color(bm, RED)
        with self.assertRaises(VisionError):
            match_template(bm, bm)
        with self.assertRaises(VisionError):
            compare_frames(bm, bm)

    def test_vision_package_has_no_adb_process_input_or_network_access(self):
        import app.vision as v
        root = Path(v.__file__).parent
        text = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
        for banned in ("subprocess", "app.core.process", "InputSession", "input_tap", "AdbClient", "shell_query",
                       "requests", "urllib", "socket", "http", "app.adb", "app.ldplayer", "app.instances",
                       "app.macros", "adb", "ldplayer", ".record"):
            self.assertNotIn(banned, text.lower() if banned.islower() else text, banned)


class DecoderTests(unittest.TestCase):
    def test_all_filters_and_colour_types_round_trip(self):
        bm = Bitmap(7, 5, bytes((x * 37 + y * 11 + c * 5) % 256 for y in range(5) for x in range(7) for c in range(3)))
        for ftype in range(5):
            self.assertEqual(decode_png(encode_png(bm, 2, ftype)), bm, f"filter {ftype}")
            self.assertEqual(decode_png(encode_png(bm, 6, ftype)), bm, f"rgba filter {ftype}")
        grey = Bitmap(4, 3, bytes(v for y in range(3) for x in range(4) for v in [(x + y * 4) * 20] * 3))
        for ftype in range(5):
            self.assertEqual(decode_png(encode_png(grey, 0, ftype)), grey)

    def test_rejects_bad_input(self):
        good = encode_png(scene())
        corrupt = bytearray(good)
        corrupt[40] ^= 0xFF
        for bad in (b"", b"nope", good[:-20], bytes(corrupt)):
            with self.assertRaises(VisionError):
                decode_png(bad)

    def test_rejects_interlaced_and_16_bit(self):
        for depth, interlace in ((8, 1), (16, 0)):
            ihdr = struct.pack(">IIBBBBB", 2, 2, depth, 2, 0, 0, interlace)
            data = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"\x00" * 20))
                    + _chunk(b"IEND", b""))
            with self.assertRaises(VisionError):
                decode_png(data)

    def test_load_png_missing_and_invalid_and_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VisionError):
                load_png(Path(tmp) / "missing.png")
            bad = Path(tmp) / "bad.png"
            bad.write_bytes(b"not an image")
            with self.assertRaises(VisionError):
                load_png(bad)
            with self.assertRaises(VisionError):
                load_png(tmp)  # a directory
            ok = Path(tmp) / "ok.png"
            ok.write_bytes(encode_png(scene()))
            self.assertEqual(load_png(ok), scene())

    def test_bitmap_validation(self):
        for args in ((0, 1, b""), (1, 1, b"ab"), (-1, 1, b"abc")):
            with self.assertRaises(VisionError):
                Bitmap(*args)
        with self.assertRaises(VisionError):
            scene().pixel(200, 0)


class RoiTests(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(resolve_roi(None, 200, 100), Roi(0, 0, 200, 100))
        self.assertEqual(resolve_roi(Roi(190, 90, 10, 10), 200, 100), Roi(190, 90, 10, 10))  # exactly at the edge
        for bad in (Roi(191, 0, 10, 10), Roi(0, 91, 10, 10), Roi(0, 0, 201, 1), Roi(0, 0, 1, 101)):
            with self.assertRaises(VisionError):
                resolve_roi(bad, 200, 100)
        for args in ((-1, 0, 5, 5), (0, -1, 5, 5), (0, 0, 0, 5), (0, 0, 5, 0), (0.5, 0, 5, 5), (True, 0, 5, 5)):
            with self.assertRaises(VisionError):
                Roi(*args)
        with self.assertRaises(VisionError):
            resolve_roi((0, 0, 5, 5), 200, 100)

    def test_contains(self):
        r = Roi(10, 10, 5, 5)
        self.assertTrue(r.contains(10, 10) and r.contains(14, 14))
        self.assertFalse(r.contains(15, 14) or r.contains(9, 10))


class ColorTests(unittest.TestCase):
    def setUp(self):
        self.f = frame_of(scene())

    def test_exact_match_count_bounds_points(self):
        m = find_color(self.f, RED)
        self.assertEqual((m.found, m.count), (True, 100))
        self.assertEqual(m.bounds, Roi(30, 20, 10, 10))
        self.assertEqual(m.points[0], (30, 20))
        self.assertEqual(m.points[-1], (39, 29))

    def test_no_match(self):
        m = find_color(self.f, (12, 200, 34))
        self.assertEqual((m.found, m.count, m.bounds, m.points), (False, 0, None, ()))

    def test_tolerance_is_per_channel_and_inclusive(self):
        near = Bitmap.solid(20, 20, (100, 100, 100)).with_rect(2, 2, 4, 4, (110, 95, 100))
        f = frame_of(near)
        self.assertEqual(find_color(f, (100, 100, 100), tolerance=0).count, 400 - 16)
        self.assertEqual(find_color(f, (100, 100, 100), tolerance=9).count, 400 - 16)   # dR=10 > 9
        self.assertEqual(find_color(f, (100, 100, 100), tolerance=10).count, 400)      # inclusive
        self.assertEqual(find_color(f, (110, 95, 100), tolerance=0).count, 16)

    def test_roi_restricts_and_reports_image_coordinates(self):
        m = find_color(self.f, RED, roi=Roi(35, 25, 20, 20))
        self.assertEqual(m.count, 25)
        self.assertEqual(m.bounds, Roi(35, 25, 5, 5))
        self.assertEqual(find_color(self.f, RED, roi=Roi(100, 0, 100, 50)).count, 0)

    def test_min_pixels_and_max_points(self):
        self.assertFalse(find_color(self.f, RED, min_pixels=101).found)
        self.assertTrue(find_color(self.f, RED, min_pixels=100).found)
        m = find_color(self.f, RED, max_points=3)
        self.assertEqual((m.count, len(m.points)), (100, 3))

    def test_validation(self):
        for kw in ({"tolerance": -1}, {"tolerance": 256}, {"tolerance": 1.5}, {"tolerance": True},
                   {"min_pixels": 0}, {"max_points": -1}, {"roi": Roi(0, 0, 500, 5)}):
            with self.assertRaises(VisionError):
                find_color(self.f, RED, **kw)
        for c in ((1, 2), (1, 2, 256), (1, 2, -1), (1.0, 2, 3), (True, 0, 0)):
            with self.assertRaises(VisionError):
                find_color(self.f, c)

    def test_deterministic(self):
        self.assertEqual(find_color(self.f, BLUE, tolerance=3), find_color(self.f, BLUE, tolerance=3))


class TemplateTests(unittest.TestCase):
    def setUp(self):
        base = Bitmap.solid(120, 80, (240, 240, 240))
        self.tpl = Bitmap.solid(12, 9, BLACK).with_rect(3, 2, 5, 4, RED).with_rect(0, 0, 3, 3, GREEN)
        for (ox, oy) in ((20, 10), (90, 60)):  # two copies of the template
            base = base.with_rect(ox, oy, 12, 9, BLACK).with_rect(ox + 3, oy + 2, 5, 4, RED).with_rect(ox, oy, 3, 3, GREEN)
        self.base = base
        self.f = frame_of(base)

    def test_exact_match_confidence_and_position(self):
        (m,) = match_template(self.f, self.tpl, min_confidence=0.99)
        self.assertEqual((m.x, m.y, m.width, m.height), (20, 10, 12, 9))
        self.assertEqual(m.confidence, 1.0)
        self.assertEqual(m.center, (26, 14))

    def test_finds_all_in_deterministic_order(self):
        ms = match_template(self.f, self.tpl, min_confidence=0.99, max_results=5)
        self.assertEqual([(m.x, m.y) for m in ms], [(20, 10), (90, 60)])
        self.assertEqual(ms, match_template(self.f, self.tpl, min_confidence=0.99, max_results=5))

    def test_roi_limits_search_and_uses_image_coordinates(self):
        (m,) = match_template(self.f, self.tpl, roi=Roi(60, 40, 60, 40))
        self.assertEqual((m.x, m.y), (90, 60))
        self.assertEqual(match_template(self.f, self.tpl, roi=Roi(40, 0, 40, 40)), [])

    def test_partial_confidence_and_threshold(self):
        altered = self.base.with_rect(23, 12, 5, 4, (200, 0, 0))  # first copy: red -> a slightly different red
        f = frame_of(altered)
        ms = match_template(f, self.tpl, min_confidence=0.9, max_results=2)
        self.assertEqual([(x.x, x.y) for x in ms], [(90, 60), (20, 10)])  # exact copy ranks first
        self.assertEqual(ms[0].confidence, 1.0)
        self.assertAlmostEqual(ms[1].confidence, 1 - (55 * 20) / (108 * 765), places=9)
        self.assertEqual([(x.x, x.y) for x in match_template(f, self.tpl, min_confidence=0.999, max_results=2)], [(90, 60)])
        # boundary: the altered copy scores 1 - 1100/82620 = 0.98669; the threshold must be applied exactly
        self.assertEqual(len(match_template(f, self.tpl, min_confidence=0.985, max_results=2)), 2)
        self.assertEqual(len(match_template(f, self.tpl, min_confidence=0.988, max_results=2)), 1)

    def test_no_match_returns_empty(self):
        other = Bitmap.solid(12, 9, (7, 7, 7)).with_rect(1, 1, 3, 3, (250, 250, 0))
        self.assertEqual(match_template(self.f, other, min_confidence=0.95), [])

    def test_invalid_templates_and_arguments(self):
        for tpl in (None, "template.png", b"bytes", self.f):
            with self.assertRaises(VisionError):
                match_template(self.f, tpl)
        with self.assertRaises(VisionError):
            match_template(self.f, Bitmap.solid(121, 5, BLACK))  # wider than the frame
        with self.assertRaises(VisionError):
            match_template(self.f, self.tpl, roi=Roi(0, 0, 10, 10))  # larger than the search region
        for kw in ({"min_confidence": 0}, {"min_confidence": 1.5}, {"min_confidence": True}, {"max_results": 0},
                   {"max_results": 1.5}, {"roi": Roi(100, 0, 50, 50)}):
            with self.assertRaises(VisionError):
                match_template(self.f, self.tpl, **kw)

    def test_flat_template_on_flat_image_is_refused_as_not_distinctive(self):
        f = frame_of(Bitmap.solid(120, 120, WHITE))
        with self.assertRaises(VisionError):
            match_template(f, Bitmap.solid(4, 4, WHITE), min_confidence=0.9)

    def test_template_loaded_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.png"
            p.write_bytes(encode_png(self.tpl, 6, 4))
            (m,) = match_template(self.f, load_png(p), min_confidence=0.99)
            self.assertEqual((m.x, m.y), (20, 10))


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.base = Bitmap.solid(100, 60, (50, 50, 50))
        self.a = frame_of(self.base)

    def test_identical_is_unchanged(self):
        r = compare_frames(self.a, frame_of(self.base))
        self.assertEqual((r.changed, r.changed_pixels, r.total_pixels, r.ratio, r.bounds), (False, 0, 6000, 0.0, None))

    def test_change_detected_with_bounds_and_ratio(self):
        b = frame_of(self.base.with_rect(10, 20, 30, 10, (200, 50, 50)))
        r = compare_frames(self.a, b)
        self.assertEqual((r.changed, r.changed_pixels), (True, 300))
        self.assertAlmostEqual(r.ratio, 0.05)
        self.assertEqual(r.bounds, Roi(10, 20, 30, 10))

    def test_pixel_threshold_boundary(self):
        b = frame_of(self.base.with_rect(0, 0, 10, 10, (66, 50, 50)))  # difference exactly 16
        self.assertEqual(compare_frames(self.a, b, pixel_threshold=16).changed_pixels, 0)
        self.assertEqual(compare_frames(self.a, b, pixel_threshold=15).changed_pixels, 100)

    def test_min_ratio_threshold(self):
        b = frame_of(self.base.with_rect(0, 0, 10, 6, (255, 255, 255)))  # 60 / 6000 = 1%
        self.assertTrue(compare_frames(self.a, b, min_changed_ratio=0.01).changed)
        r = compare_frames(self.a, b, min_changed_ratio=0.011)
        self.assertEqual((r.changed, r.changed_pixels), (False, 60))

    def test_roi_ignores_changes_outside(self):
        b = frame_of(self.base.with_rect(10, 20, 30, 10, (200, 50, 50)))
        self.assertFalse(compare_frames(self.a, b, roi=Roi(50, 0, 50, 60)).changed)
        r = compare_frames(self.a, b, roi=Roi(0, 20, 20, 10))
        self.assertEqual((r.changed_pixels, r.total_pixels, r.ratio), (100, 200, 0.5))
        self.assertEqual(r.bounds, Roi(10, 20, 10, 10))

    def test_refusals(self):
        with self.assertRaises(VisionError):
            compare_frames(self.a, frame_of(Bitmap.solid(90, 60, (50, 50, 50))))  # different size
        with self.assertRaises(VisionError):
            compare_frames(self.a, frame_of(self.base, "ld2"))  # different instance
        for kw in ({"pixel_threshold": -1}, {"pixel_threshold": 256}, {"pixel_threshold": 1.5},
                   {"min_changed_ratio": 0}, {"min_changed_ratio": 1.1}, {"roi": Roi(0, 0, 101, 5)}):
            with self.assertRaises(VisionError):
                compare_frames(self.a, self.a, **kw)


class RecordCoordinateTests(unittest.TestCase):
    """LDPlayer-specific; lives in app.macros.record_coords, not in the target-agnostic vision layer."""

    def test_record_points_are_a_separate_type_and_refused_by_default(self):
        self.assertNotEqual(PixelPoint(5, 5), RecordPoint(5, 5))
        with self.assertRaises(CoordinateSpaceUnresolved):
            record_to_pixel(RecordPoint(9600, 5400), (1280, 720))
        with self.assertRaises(CoordinateError):
            record_to_pixel(PixelPoint(5, 5), (1280, 720), AssumedRecordCanvas(19200, 10800))

    def test_explicit_assumed_canvas_conversion_is_opt_in_only(self):
        c = AssumedRecordCanvas(19200, 10800)
        self.assertEqual(record_to_pixel(RecordPoint(9600, 5400), (1280, 720), c), PixelPoint(640, 360))
        self.assertEqual(record_to_pixel(RecordPoint(19200, 10800), (1280, 720), c), PixelPoint(1279, 719))
        with self.assertRaises(CoordinateError):
            record_to_pixel(RecordPoint(19201, 0), (1280, 720), c)
        with self.assertRaises(CoordinateError):
            AssumedRecordCanvas(0, 10)


class CoordinateTests(unittest.TestCase):
    def test_pixel_normalized_round_trip_is_exact_for_every_pixel(self):
        size = (13, 7)
        for x in range(13):
            for y in range(7):
                p = PixelPoint(x, y)
                self.assertEqual(normalized_to_pixel(pixel_to_normalized(p, size), size), p)

    def test_edges_and_bounds(self):
        self.assertEqual(normalized_to_pixel(NormalizedPoint(1.0, 1.0), (1280, 720)), PixelPoint(1279, 719))
        self.assertEqual(normalized_to_pixel(NormalizedPoint(0.0, 0.0), (1280, 720)), PixelPoint(0, 0))
        for p in (PixelPoint(1280, 0), PixelPoint(0, 720), PixelPoint(-1, 0)):
            with self.assertRaises(CoordinateError):
                pixel_to_normalized(p, (1280, 720))
        for u, v in ((-0.1, 0), (0, 1.1), (float("nan"), 0), (True, 0)):
            with self.assertRaises(CoordinateError):
                NormalizedPoint(u, v)
        for bad in ((0, 5), (5, -1), (1.5, 5), (True, 5)):
            with self.assertRaises(CoordinateError):
                pixel_to_normalized(PixelPoint(0, 0), bad)
        with self.assertRaises(CoordinateError):
            PixelPoint(1.5, 2)

    def test_rescale_between_resolutions(self):
        self.assertEqual(rescale_pixel(PixelPoint(640, 360), (1280, 720), (960, 540)), PixelPoint(480, 270))
        self.assertEqual(rescale_pixel(PixelPoint(0, 0), (1280, 720), (1920, 1080)), PixelPoint(0, 0))
        self.assertEqual(rescale_pixel(PixelPoint(1279, 719), (1280, 720), (1920, 1080)), PixelPoint(1919, 1079))


if __name__ == "__main__":
    unittest.main()
