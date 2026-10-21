"""Unit tests for the read-only .record parser, serializer and structural comparison.

All data here is synthetic (see helpers.py). Real-file tests are in test_record_real_files.py.
"""
import json
import unittest

from app.macros.analysis import analyze, format_report
from app.macros.compare import structural_diff
from app.macros.model import MacroFormatError
from app.macros.parser import parse_bytes
from app.macros.serializer import serialize
from tests import helpers


class ParseBasics(unittest.TestCase):
    def test_parses_simple_macro(self):
        macro = parse_bytes(helpers.to_bytes(helpers.simple_macro()))
        self.assertEqual(len(macro.operations), 1 + 4 + 2)
        self.assertEqual(macro.operations[0].operation_id, "ImeClipboard")
        self.assertEqual(macro.operations[0].params, {"text": "DrawPen"})
        first_touch = macro.operations[1]
        self.assertEqual(first_touch.timing, 1000)
        self.assertEqual((first_touch.points[0].x, first_touch.points[0].y, first_touch.points[0].state),
                         (480, 270, 1))
        self.assertEqual(macro.operations[2].points, [])
        self.assertEqual(macro.record_info.resolution, (960, 540))
        self.assertIsNone(macro.merged_records)

    def test_parses_merged_macro(self):
        macro = parse_bytes(helpers.to_bytes(helpers.merged_macro()))
        self.assertIsNone(macro.operations)
        self.assertEqual(len(macro.merged_records), 2)
        self.assertEqual(macro.merged_records[1].operations[0].timing, 50)
        self.assertEqual(macro.record_info.resolution, (0, 0))

    def test_detects_text_format(self):
        macro = parse_bytes(helpers.to_bytes(helpers.simple_macro()))
        self.assertEqual((macro.fmt.newline, macro.fmt.indent, macro.fmt.trailing_newline, macro.fmt.bom),
                         ("\r\n", 4, False, False))
        macro = parse_bytes(helpers.to_bytes(helpers.simple_macro(), newline="\n", trailing_newline=True,
                                             bom=True, indent=2))
        self.assertEqual((macro.fmt.newline, macro.fmt.indent, macro.fmt.trailing_newline, macro.fmt.bom),
                         ("\n", 2, True, True))

    def test_records_source_hash_and_size(self):
        data = helpers.to_bytes(helpers.simple_macro())
        macro = parse_bytes(data)
        self.assertEqual(macro.source_size, len(data))
        self.assertEqual(len(macro.source_sha256), 64)


class ParseRejectsUnknownStructure(unittest.TestCase):
    def test_invalid_json(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b"{not json")

    def test_invalid_utf8(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b"\xff\xfe\x00")

    def test_top_level_not_object(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b"[1, 2, 3]")

    def test_operations_not_list(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": {"a": 1}}')

    def test_operation_not_object(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": [5]}')

    def test_points_not_list(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": [{"timing": 1, "operationId": "PutMultiTouch", "points": 3}]}')

    def test_merged_records_not_list(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"mergedRecords": {"a": 1}}')

    def test_duplicate_keys_refused(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": [], "operations": []}')

    def test_nan_refused(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": [{"timing": NaN, "operationId": "AndroidBack"}]}')

    def test_missing_fields_become_issues_not_crashes(self):
        macro = parse_bytes(b'{"operations": [{"operationId": "AndroidBack"}, {"timing": 4}]}')
        text = " ".join(i.message for i in macro.issues)
        self.assertIn("no timing", text)
        self.assertIn("no operationId", text)


class RoundTrip(unittest.TestCase):
    def assertRoundTrip(self, obj, **fmt):
        data = helpers.to_bytes(obj, **fmt)
        out = serialize(parse_bytes(data))
        self.assertEqual(out, data, "byte-exact round trip")
        self.assertEqual(
            structural_diff(json.loads(data.decode("utf-8-sig")), json.loads(out.decode("utf-8-sig"))), [])

    def test_simple(self):
        self.assertRoundTrip(helpers.simple_macro())

    def test_merged(self):
        self.assertRoundTrip(helpers.merged_macro())

    def test_lf_trailing_newline_bom_indent(self):
        self.assertRoundTrip(helpers.simple_macro(), newline="\n", trailing_newline=True, bom=True, indent=2)

    def test_non_ascii_text_preserved(self):
        macro = helpers.simple_macro()
        macro["operations"][0]["text"] = "Привет – 龍"
        self.assertRoundTrip(macro)

    def test_ensure_ascii_escapes_preserved(self):
        macro = helpers.simple_macro()
        macro["operations"][0]["text"] = "Привет"
        self.assertRoundTrip(macro, ensure_ascii=True)

    def test_key_order_preserved(self):
        obj = {"recordInfo": helpers.record_info(), "operations": helpers.tap(1, 2, 3)}
        self.assertRoundTrip(obj)
        op = {"operationId": "AndroidBack", "timing": 9}
        self.assertRoundTrip({"operations": [op], "recordInfo": helpers.record_info()})

    def test_empty_operations(self):
        self.assertRoundTrip({"operations": [], "recordInfo": helpers.record_info()})

    def test_compact_json_structurally_equal(self):
        data = helpers.to_bytes(helpers.simple_macro(), indent=None)
        out = serialize(parse_bytes(data))
        self.assertEqual(structural_diff(json.loads(data), json.loads(out)), [])


class UnknownFieldsPreserved(unittest.TestCase):
    def _obj(self):
        obj = helpers.simple_macro()
        obj["futureTopKey"] = {"nested": [1, 2, {"a": None}]}
        obj["recordInfo"]["futureInfoKey"] = "x"
        obj["operations"][1]["futureOpKey"] = [True, False]
        obj["operations"][1]["points"][0]["pressure"] = 7
        obj["operations"].append({"timing": 9999, "operationId": "BrandNewOperation", "blob": {"k": "v"}})
        return obj

    def test_unknown_fields_round_trip(self):
        data = helpers.to_bytes(self._obj())
        macro = parse_bytes(data)
        self.assertEqual(macro.extras, {"futureTopKey": {"nested": [1, 2, {"a": None}]}})
        self.assertEqual(macro.operations[1].params["futureOpKey"], [True, False])
        self.assertEqual(macro.operations[1].points[0].extras, {"pressure": 7})
        self.assertEqual(serialize(macro), data)

    def test_unknown_fields_are_reported(self):
        report = analyze(parse_bytes(helpers.to_bytes(self._obj())))
        self.assertEqual(report.unknown_top_level_keys, ["futureTopKey"])
        self.assertEqual(report.record_info_keys_unknown, ["futureInfoKey"])
        self.assertEqual(report.unknown_operation_keys, {"PutMultiTouch": ["futureOpKey"]})
        self.assertEqual(report.unknown_operation_ids, ["BrandNewOperation"])
        self.assertEqual(report.unknown_point_keys, ["pressure"])

    def test_explicit_null_survives(self):
        obj = helpers.simple_macro()
        obj["operations"][0]["text"] = None
        obj["operations"][1]["timing"] = None
        data = helpers.to_bytes(obj)
        self.assertEqual(serialize(parse_bytes(data)), data)

    def test_unknown_merged_entry_key_preserved(self):
        obj = helpers.merged_macro()
        obj["mergedRecords"][0]["extraEntryKey"] = 1
        data = helpers.to_bytes(obj)
        macro = parse_bytes(data)
        self.assertEqual(serialize(macro), data)
        self.assertEqual(analyze(macro).unknown_merged_entry_keys, ["extraEntryKey"])


class StructuralDiff(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(structural_diff({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}), [])

    def test_key_order_matters(self):
        self.assertTrue(structural_diff({"a": 1, "b": 2}, {"b": 2, "a": 1}))

    def test_types_matter(self):
        self.assertTrue(structural_diff({"a": 1}, {"a": 1.0}))
        self.assertTrue(structural_diff({"a": 1}, {"a": True}))

    def test_value_length_and_missing_keys(self):
        self.assertTrue(structural_diff([1, 2], [1, 2, 3]))
        self.assertTrue(structural_diff({"a": 1}, {"a": 1, "b": 2}))
        self.assertTrue(structural_diff({"a": 1}, {"a": 2}))


class Analysis(unittest.TestCase):
    def test_counts_and_timing(self):
        report = analyze(parse_bytes(helpers.to_bytes(helpers.simple_macro())))
        self.assertEqual(report.total_operations, 7)
        self.assertEqual(dict(report.operation_ids),
                         {"ImeClipboard": 1, "PutMultiTouch": 4, "AndroidBack": 1, "StopApp": 1})
        self.assertEqual(report.total_points, 2)
        self.assertEqual(report.ops_with_points, 2)
        self.assertEqual(report.ops_with_empty_points, 2)
        self.assertEqual(dict(report.point_states), {1: 1, 0: 1})
        self.assertEqual(report.max_points_per_op, 1)
        main = report.timelines[0]
        self.assertEqual((main.first_timing, main.last_timing, main.span), (5, 2500, 2495))
        self.assertEqual(main.decreasing_steps, 0)
        self.assertEqual(report.unknown_operation_ids, [])
        self.assertEqual(report.unknown_top_level_keys, [])

    def test_out_of_order_timing_reported(self):
        obj = {"operations": [{"timing": 10, "operationId": "AndroidBack"},
                              {"timing": 5, "operationId": "AndroidBack"}],
               "recordInfo": helpers.record_info()}
        report = analyze(parse_bytes(helpers.to_bytes(obj)))
        self.assertEqual(report.timelines[0].decreasing_steps, 1)

    def test_points_outside_resolution_counted_only_when_resolution_known(self):
        obj = {"operations": helpers.tap(1, 1060, 420), "recordInfo": helpers.record_info(960, 540)}
        report = analyze(parse_bytes(helpers.to_bytes(obj)))
        self.assertEqual((report.points_outside_recorded_resolution, report.points_checked_against_resolution),
                         (2, 2))
        obj["recordInfo"] = helpers.record_info(0, 0)
        report = analyze(parse_bytes(helpers.to_bytes(obj)))
        self.assertEqual((report.points_outside_recorded_resolution, report.points_checked_against_resolution),
                         (0, 0))

    def test_merged_timelines(self):
        report = analyze(parse_bytes(helpers.to_bytes(helpers.merged_macro())))
        self.assertEqual(report.merged_entries, 2)
        self.assertEqual(len(report.timelines), 2)
        self.assertEqual(report.total_operations, 8)

    def test_multi_point_op_reported(self):
        obj = {"operations": [{"timing": 1, "operationId": "PutMultiTouch",
                               "points": [{"id": 1, "x": 1, "y": 1, "state": 1},
                                          {"id": 2, "x": 5, "y": 5, "state": 1}]}],
               "recordInfo": helpers.record_info()}
        report = analyze(parse_bytes(helpers.to_bytes(obj)))
        self.assertEqual(report.max_points_per_op, 2)
        self.assertEqual(dict(report.point_ids), {1: 1, 2: 1})

    def test_format_report_runs(self):
        text = format_report(analyze(parse_bytes(helpers.to_bytes(helpers.simple_macro()))))
        self.assertIn("Operations: 7", text)


if __name__ == "__main__":
    unittest.main()
