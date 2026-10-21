"""Tests for the timing/merge engine and the macro validator."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.macros import timeline
from app.macros.compare import structural_diff
from app.macros.parser import parse_bytes, parse_file
from app.macros.serializer import serialize
from app.macros.timeline import MergeError
from app.macros.validator import ERROR, INFO, WARNING, has_errors, validate
from tests import helpers


def macro_from(ops, **info):
    return parse_bytes(helpers.to_bytes({"operations": ops, "recordInfo": helpers.record_info(**info)}))


def back(t):
    return {"timing": t, "operationId": "AndroidBack"}


def timings(m):
    return [op.timing for op in m.operations]


def codes(findings, severity=None):
    return sorted(f.code for f in findings if severity is None or f.severity == severity)


class AppendTests(unittest.TestCase):
    def test_spec_example_a_0_to_5000_then_b_0_to_1000(self):
        a, b = macro_from([back(0), back(5000)]), macro_from([back(0), back(1000)])
        self.assertEqual(timings(timeline.append(a, b)), [0, 5000, 5000, 6000])

    def test_gap(self):
        a, b = macro_from([back(0), back(5000)]), macro_from([back(0), back(1000)])
        self.assertEqual(timings(timeline.append(a, b, gap_ms=250)), [0, 5000, 5250, 6250])

    def test_lead_in_of_b_is_preserved(self):
        a, b = macro_from([back(100)]), macro_from([back(40), back(90)])
        self.assertEqual(timings(timeline.append(a, b)), [100, 140, 190])

    def test_inputs_are_not_mutated(self):
        a, b = macro_from(helpers.tap(0, 5, 5)), macro_from(helpers.tap(0, 9, 9))
        before = (serialize(a), serialize(b))
        timeline.append(a, b, 10)
        timeline.insert_at(a, b, 50)
        timeline.repeat(a, 3)
        timeline.scale_speed(a, 2)
        timeline.offset(a, 5)
        self.assertEqual((serialize(a), serialize(b)), before)

    def test_result_is_independent_copy(self):
        a, b = macro_from([back(0)]), macro_from([back(0)])
        m = timeline.append(a, b)
        m.operations[0].timing = 999
        self.assertEqual(a.operations[0].timing, 0)

    def test_metadata_comes_from_first_and_unknown_fields_survive(self):
        obj = {"operations": [back(0)], "recordInfo": helpers.record_info(), "future": {"k": 1}}
        a = parse_bytes(helpers.to_bytes(obj))
        b = macro_from([back(0)])
        m = timeline.append(a, b)
        self.assertEqual(m.extras, {"future": {"k": 1}})
        self.assertIsNone(m.source_path)

    def test_refuses_different_resolution_merged_format_and_bad_gap(self):
        a, b = macro_from([back(0)]), macro_from([back(0)], width=1280, height=720)
        with self.assertRaises(MergeError):
            timeline.append(a, b)
        merged = parse_bytes(helpers.to_bytes(helpers.merged_macro()))
        with self.assertRaises(MergeError):
            timeline.append(merged, a)
        with self.assertRaises(MergeError):
            timeline.append(a, merged)
        with self.assertRaises(MergeError):
            timeline.append(a, a, gap_ms=-1)

    def test_empty_first_macro(self):
        a, b = macro_from([]), macro_from([back(10)])
        self.assertEqual(timings(timeline.append(a, b)), [10])

    def test_result_serializes_and_reparses_identically(self):
        a, b = macro_from(helpers.tap(0, 5, 5)), macro_from(helpers.tap(0, 9, 9))
        m = timeline.append(a, b, 100)
        data = serialize(m)
        self.assertEqual(serialize(parse_bytes(data)), data)
        self.assertEqual(structural_diff(json.loads(data), json.loads(serialize(parse_bytes(data)))), [])


class OtherOperations(unittest.TestCase):
    def test_insert_interleaves_and_is_stable(self):
        a, b = macro_from([back(0), back(100), back(300)]), macro_from([back(0), back(100)])
        m = timeline.insert_at(a, b, 100)
        self.assertEqual(timings(m), [0, 100, 100, 200, 300])
        # at the equal timing 100, A's operation comes first
        a2 = macro_from([{"timing": 100, "operationId": "AndroidBack"}])
        b2 = macro_from([{"timing": 0, "operationId": "StopApp", "app": "x"}])
        merged = timeline.insert_at(a2, b2, 100)
        self.assertEqual([o.operation_id for o in merged.operations], ["AndroidBack", "StopApp"])

    def test_repeat(self):
        a = macro_from([back(0), back(1000)])
        self.assertEqual(timings(timeline.repeat(a, 3)), [0, 1000, 1000, 2000, 2000, 3000])
        self.assertEqual(timings(timeline.repeat(a, 2, gap_ms=500)), [0, 1000, 1500, 2500])
        self.assertEqual(timings(timeline.repeat(a, 1)), [0, 1000])
        with self.assertRaises(MergeError):
            timeline.repeat(a, 0)

    def test_offset(self):
        a = macro_from([back(10), back(20)])
        self.assertEqual(timings(timeline.offset(a, 5)), [15, 25])
        self.assertEqual(timings(timeline.offset(a, -10)), [0, 10])
        with self.assertRaises(MergeError):
            timeline.offset(a, -11)

    def test_scale_speed_is_explicit(self):
        a = macro_from([back(0), back(1000), back(2001)])
        self.assertEqual(timings(timeline.scale_speed(a, 2)), [0, 500, 1000])
        self.assertEqual(timings(timeline.scale_speed(a, 0.5)), [0, 2000, 4002])
        for bad in (0, -1):
            with self.assertRaises(MergeError):
                timeline.scale_speed(a, bad)

    def test_non_numeric_timing_refused(self):
        a = parse_bytes(helpers.to_bytes({"operations": [{"timing": "5", "operationId": "AndroidBack"}],
                                          "recordInfo": helpers.record_info()}))
        with self.assertRaises(MergeError):
            timeline.offset(a, 1)

    def test_preview_and_span(self):
        a = macro_from([back(10), back(50)])
        p = timeline.preview(a, "A")
        self.assertEqual((p.operations, p.first_ms, p.last_ms, p.span_ms), (2, 10, 50, 40))
        self.assertEqual(timeline.span_ms(a), 50)
        self.assertEqual(timeline.span_ms(macro_from([])), 0)


class ValidatorTests(unittest.TestCase):
    def test_clean_macro(self):
        f = validate(macro_from(helpers.tap(0, 1, 1) + [back(500)]))
        self.assertEqual([x for x in f if x.severity in (ERROR, WARNING)], [])
        self.assertFalse(has_errors(f))

    def test_negative_and_out_of_order_timing(self):
        f = validate(macro_from([back(-5), back(10), back(5)]))
        self.assertEqual(codes(f, ERROR), ["timing_negative", "timing_out_of_order"])
        self.assertTrue(has_errors(f))

    def test_invalid_timing_and_operation_id(self):
        m = parse_bytes(helpers.to_bytes({"operations": [{"timing": "x", "operationId": 5}, {"operationId": "AndroidBack"}],
                                          "recordInfo": helpers.record_info()}))
        c = codes(validate(m), ERROR)
        self.assertIn("timing_invalid", c)
        self.assertIn("operation_id_invalid", c)

    def test_missing_touch_up(self):
        ops = helpers.touch(0, 1, 1, 1)  # down, never lifted
        f = validate(macro_from(ops))
        self.assertIn("missing_touch_up", codes(f, ERROR))

    def test_touch_up_without_down(self):
        f = validate(macro_from(helpers.touch(0, 1, 1, 0)))
        self.assertIn("touch_up_without_down", codes(f, WARNING))

    def test_two_pointers_tracked_separately(self):
        ops = helpers.touch(0, 1, 1, 1, pid=1) + helpers.touch(10, 2, 2, 1, pid=2) + helpers.touch(20, 1, 1, 0, pid=1)
        f = validate(macro_from(ops))
        errs = [x for x in f if x.code == "missing_touch_up"]
        self.assertEqual(len(errs), 1)
        self.assertIn("pointer 2", errs[0].message)

    def test_unknown_state_and_bad_coordinates(self):
        ops = [{"timing": 0, "operationId": "PutMultiTouch", "points": [{"id": 1, "x": -3, "y": "a", "state": 7}]}]
        c = codes(validate(macro_from(ops)))
        for expected in ("state_unknown", "coordinate_negative", "coordinate_invalid"):
            self.assertIn(expected, c)

    def test_multitouch_is_info(self):
        ops = [{"timing": 0, "operationId": "PutMultiTouch",
                "points": [{"id": 1, "x": 1, "y": 1, "state": 1}, {"id": 2, "x": 2, "y": 2, "state": 1}]},
               {"timing": 5, "operationId": "PutMultiTouch",
                "points": [{"id": 1, "x": 1, "y": 1, "state": 0}, {"id": 2, "x": 2, "y": 2, "state": 0}]}]
        f = validate(macro_from(ops))
        self.assertIn("multitouch", codes(f, INFO))
        self.assertFalse(has_errors(f))

    def test_short_gap_warning_is_opt_in(self):
        m = macro_from([back(0), back(0), back(1)])
        self.assertNotIn("gap_very_short", codes(validate(m)))
        self.assertIn("gap_very_short", codes(validate(m, min_gap_ms=5), WARNING))

    def test_merged_file_reports_entries_and_unset_resolution(self):
        f = validate(parse_bytes(helpers.to_bytes(helpers.merged_macro())))
        self.assertEqual(codes(f, INFO), ["merged_file", "resolution_unset"])
        self.assertFalse(has_errors(f))

    def test_merged_entry_errors_are_located(self):
        obj = helpers.merged_macro()
        obj["mergedRecords"][1]["operations"] = helpers.touch(0, 1, 1, 1)
        f = validate(parse_bytes(helpers.to_bytes(obj)))
        bad = [x for x in f if x.code == "missing_touch_up"]
        self.assertTrue(bad and "mergedRecords[1]" in bad[0].path)

    def test_no_operations_and_missing_record_info(self):
        f = validate(parse_bytes(b"{}"))
        self.assertIn("no_operations", codes(f, ERROR))
        self.assertIn("no_record_info", codes(f, WARNING))

    def test_merge_result_of_valid_macros_stays_valid_and_truncation_is_caught(self):
        a, b = macro_from(helpers.tap(0, 5, 5)), macro_from(helpers.tap(0, 9, 9))
        merged = timeline.append(a, b, 50)
        self.assertFalse(has_errors(validate(merged)))
        merged.operations = merged.operations[:-2]  # cut off the final touch-up + empty op
        self.assertIn("missing_touch_up", codes(validate(merged), ERROR))


REAL_DIRS = [Path(r"C:\LDPlayer\LDPlayer14\vms\operationRecords"), Path(r"C:\LDPlayer\LDPlayer14\GEternal")]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class RealFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(p for d in REAL_DIRS if d.is_dir() for p in d.glob("*.record"))
        if not cls.files:
            raise unittest.SkipTest("no real .record files on this machine")
        cls.before = {p: _sha(p) for p in cls.files}
        cls._tmp = tempfile.TemporaryDirectory()
        cls.copies = []
        for i, src in enumerate(cls.files):
            dst = Path(cls._tmp.name) / f"{i:03d}.record"
            shutil.copyfile(src, dst)
            cls.copies.append(dst)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        for p in cls.files:
            if _sha(p) != cls.before[p]:
                raise AssertionError(f"ORIGINAL MODIFIED: {p}")

    def test_no_real_file_has_validation_errors(self):
        for dst in self.copies:
            with self.subTest(file=dst.name):
                self.assertEqual([str(f) for f in validate(parse_file(dst)) if f.severity == ERROR], [])

    def test_merge_two_real_macros(self):
        plain = []
        for dst in self.copies:
            m = parse_file(dst)
            if m.operations and m.merged_records is None and m.record_info.resolution == (960, 540):
                plain.append((dst, m))
            if len(plain) == 2:
                break
        self.assertEqual(len(plain), 2)
        (_, a), (_, b) = plain
        merged = timeline.append(a, b, gap_ms=1000)
        n_a, n_b = len(a.operations), len(b.operations)
        self.assertEqual(len(merged.operations), n_a + n_b)
        start = a.operations[-1].timing + 1000
        self.assertEqual([o.timing for o in merged.operations[n_a:]], [o.timing + start for o in b.operations])
        self.assertEqual([o.timing for o in merged.operations[:n_a]], [o.timing for o in a.operations])
        self.assertFalse(has_errors(validate(merged)))
        data = serialize(merged)
        again = parse_bytes(data)
        self.assertEqual(serialize(again), data)
        self.assertEqual(structural_diff(json.loads(data.decode("utf-8")), json.loads(serialize(again).decode("utf-8"))), [])


if __name__ == "__main__":
    unittest.main()
