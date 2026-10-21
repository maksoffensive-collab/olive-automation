"""Malformed external data at file-input boundaries (fourth bug-fix stage).

  C-11  a .record point whose id/state is a list or object crashed `analyze` / `validate` (unhashable dict key);
  C-16  numeric settings were silently coerced (2.9 -> 2, true -> 1);
  C-06  the PNG decoder allocated the whole inflated stream before checking it against the declared size.
Deterministic; temporary files only.
"""
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zlib
from pathlib import Path

from app.config.settings import load_settings
from app.core.errors import ConfigError
from app.macros.analysis import analyze, format_report
from app.macros.parser import parse_bytes
from app.macros.playback import PlaybackRefused, build_steps
from app.macros.validator import ERROR, has_errors, validate
from app.vision.bitmap import Bitmap, VisionError, decode_png
from tests.test_vision import encode_png

ROOT = Path(__file__).resolve().parents[1]


def record(points_json: str, extra_ops: str = "") -> bytes:
    return ('{"operations": [{"timing": 0, "operationId": "PutMultiTouch", "points": %s}%s],'
            ' "recordInfo": {"resolutionWidth": 960, "resolutionHeight": 540}}' % (points_json, extra_ops)).encode()


class UnhashablePointFields(unittest.TestCase):
    """C-11."""

    BAD_IDS = ('[{"id": [1], "x": 1, "y": 2, "state": 1}]', '[{"id": {"a": 1}, "x": 1, "y": 2, "state": 0}]')
    BAD_STATES = ('[{"id": 1, "x": 1, "y": 2, "state": [1]}]', '[{"id": 1, "x": 1, "y": 2, "state": {"s": 1}}]')

    def test_analysis_reports_instead_of_crashing(self):
        for points in self.BAD_IDS + self.BAD_STATES:
            with self.subTest(points=points):
                report = analyze(parse_bytes(record(points)))
                self.assertEqual(report.total_points, 1)
                self.assertEqual(sum(report.point_ids.values()), 1)
                self.assertEqual(sum(report.point_states.values()), 1)
                self.assertIn("Points: total=1", format_report(report))

    def test_validator_reports_an_error_finding_instead_of_crashing(self):
        for points in self.BAD_IDS:
            with self.subTest(points=points):
                findings = validate(parse_bytes(record(points)))
                self.assertTrue(has_errors(findings))
                self.assertIn("point_id_invalid", {f.code for f in findings if f.severity == ERROR})

    def test_an_unhashable_state_is_an_unknown_state_warning(self):
        for points in self.BAD_STATES:
            with self.subTest(points=points):
                findings = validate(parse_bytes(record(points)))
                self.assertIn("state_unknown", {f.code for f in findings})

    def test_merged_entries_are_covered_too(self):
        data = ('{"recordInfo": {}, "mergedRecords": [{"operations": [{"timing": 0, "operationId": "PutMultiTouch", '
                '"points": %s}], "recordInfo": {}}]}' % self.BAD_IDS[0]).encode()
        macro = parse_bytes(data)
        analyze(macro)
        self.assertIn("point_id_invalid", {f.code for f in validate(macro)})

    def test_playback_refuses_instead_of_raising_a_type_error(self):
        for points in self.BAD_IDS:
            with self.subTest(points=points):
                with self.assertRaises(PlaybackRefused):
                    build_steps(parse_bytes(record(points)), None)

    def test_valid_files_report_exactly_as_before(self):
        good = parse_bytes(record('[{"id": 1, "x": 1, "y": 2, "state": 1}]',
                                  ', {"timing": 4, "operationId": "PutMultiTouch", "points": [{"id": 1, "x": 1, "y": 2, "state": 0}]}'))
        report = analyze(good)
        self.assertEqual(dict(report.point_ids), {1: 2})
        self.assertEqual(dict(report.point_states), {1: 1, 0: 1})
        self.assertFalse(has_errors(validate(good)))

    def test_a_string_id_that_looks_like_the_marker_does_not_collide_with_an_unhashable_one(self):
        both = record('[{"id": [1], "x": 1, "y": 2, "state": 1}, {"id": "[1]", "x": 1, "y": 2, "state": 1}]')
        report = analyze(parse_bytes(both))
        self.assertEqual(len(report.point_ids), 2)

    def test_the_inspect_tool_reports_every_file_and_does_not_abort_the_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.record").write_bytes(record(self.BAD_IDS[0]))
            (Path(tmp) / "b.record").write_bytes(record('[{"id": 1, "x": 1, "y": 2, "state": 1}]'))
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "inspect_record.py"), tmp], cwd=ROOT,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("2 file(s), 0 failure(s)", done.stdout)


class NumericSettings(unittest.TestCase):
    """C-16."""

    def load(self, toml_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.toml"
            cfg.write_text(toml_text, encoding="utf-8")
            return load_settings(cfg, env={"SystemDrive": tmp})

    def test_a_fractional_value_for_an_integer_setting_is_refused_not_truncated(self):
        for text in ("retry_count = 2.9", "worker_limit = 3.5", "retry_count = 0.1"):
            with self.subTest(text=text), self.assertRaises(ConfigError) as cm:
                self.load(text)
            self.assertIn("whole number", str(cm.exception))

    def test_booleans_are_not_numbers(self):
        for text in ("retry_count = true", "worker_limit = false", "adb_timeout_s = true", "retry_backoff_s = false"):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.load(text)

    def test_environment_values_are_checked_the_same_way(self):
        for env in ({"LDAP_RETRY_COUNT": "2.9"}, {"LDAP_RETRY_COUNT": "true"}, {"LDAP_ADB_TIMEOUT_S": "true"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                with tempfile.TemporaryDirectory() as tmp:
                    load_settings(env={**env, "SystemDrive": tmp})

    def test_valid_values_are_unchanged(self):
        s = self.load("retry_count = 3\nworker_limit = 2\nadb_timeout_s = 7\nretry_backoff_s = 0.25")
        self.assertEqual((s.retry_count, s.worker_limit, s.adb_timeout_s, s.retry_backoff_s), (3, 2, 7.0, 0.25))
        self.assertIsInstance(s.retry_count, int)

    def test_a_whole_float_for_an_integer_setting_is_accepted_as_that_integer(self):
        s = self.load("retry_count = 3.0")
        self.assertEqual(s.retry_count, 3)
        self.assertIsInstance(s.retry_count, int)

    def test_env_strings_that_are_whole_numbers_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(env={"LDAP_RETRY_COUNT": "4", "LDAP_ADB_TIMEOUT_S": "2.5", "SystemDrive": tmp})
        self.assertEqual((s.retry_count, s.adb_timeout_s), (4, 2.5))

    def test_range_and_type_errors_keep_their_messages(self):
        with self.assertRaises(ConfigError) as cm:
            self.load("retry_count = 99")
        self.assertIn("between", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            self.load('retry_count = "x"')
        self.assertIn("must be a number", str(cm.exception))


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_with_payload(width: int, height: int, inflated: bytes, ctype: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(inflated, 1))
            + chunk(b"IEND", b""))


class PngInflatedSizeIsBounded(unittest.TestCase):
    """C-06."""

    def peak_mb(self, data: bytes) -> tuple[float, type | None]:
        tracemalloc.start()
        try:
            error = None
            try:
                decode_png(data)
            except Exception as exc:  # noqa: BLE001
                error = type(exc)
            return tracemalloc.get_traced_memory()[1] / 1e6, error
        finally:
            tracemalloc.stop()

    def test_a_small_png_with_a_huge_deflate_stream_is_rejected_without_inflating_it(self):
        bomb = png_with_payload(10, 10, bytes(120_000_000))                     # declared 10x10 = 310 bytes
        self.assertLess(len(bomb), 1_000_000)
        peak, error = self.peak_mb(bomb)
        self.assertIs(error, VisionError)
        self.assertLess(peak, 10, f"peak {peak:.0f} MB: the stream was inflated before the size check")

    def test_the_error_names_the_problem(self):
        with self.assertRaises(VisionError) as cm:
            decode_png(png_with_payload(4, 4, bytes(100_000)))
        self.assertIn("larger", str(cm.exception))

    def test_a_stream_that_is_too_short_or_truncated_is_still_refused(self):
        with self.assertRaises(VisionError):
            decode_png(png_with_payload(4, 4, bytes(10)))                       # shorter than declared
        good = png_with_payload(4, 4, b"\x00" + bytes(12) + (b"\x00" + bytes(12)) * 3)
        cut = bytearray(good)
        idat_at = good.index(b"IDAT")
        # damage the deflate stream itself and repair the chunk CRC so only the stream is wrong
        length = struct.unpack(">I", good[idat_at - 4:idat_at])[0]
        body = bytearray(good[idat_at + 4:idat_at + 4 + length])
        del body[-6:]
        rebuilt = good[:idat_at - 4] + chunk(b"IDAT", bytes(body)) + chunk(b"IEND", b"")
        del cut
        with self.assertRaises(VisionError):
            decode_png(rebuilt)

    def test_every_supported_kind_of_valid_image_still_decodes_exactly(self):
        bm = Bitmap(7, 5, bytes((x * 37 + y * 11 + c * 5) % 256 for y in range(5) for x in range(7) for c in range(3)))
        for ftype in range(5):
            self.assertEqual(decode_png(encode_png(bm, 2, ftype)), bm)
            self.assertEqual(decode_png(encode_png(bm, 6, ftype)), bm)

    def test_a_large_valid_image_is_not_refused(self):
        bm = Bitmap.solid(1280, 720, (10, 20, 30))
        self.assertEqual(decode_png(encode_png(bm)).width, 1280)

    def test_multiple_idat_chunks_are_still_joined(self):
        bm = Bitmap.solid(6, 6, (1, 2, 3))
        data = encode_png(bm)
        at = data.index(b"IDAT")
        length = struct.unpack(">I", data[at - 4:at])[0]
        body = data[at + 4:at + 4 + length]
        half = len(body) // 2
        split = (data[:at - 4] + chunk(b"IDAT", body[:half]) + chunk(b"IDAT", body[half:]) + chunk(b"IEND", b""))
        self.assertEqual(decode_png(split), bm)


if __name__ == "__main__":
    unittest.main()
