"""C-05 (Stage 8): the `.record` parse -> serialize contract is a CANONICAL SEMANTIC round trip, not byte preservation.

Contract under test:
  * parse -> serialize -> parse keeps the complete supported semantic content and every value's TYPE
    (`1` stays an int, `1.0` a float, `true` a bool, unknown keys and their values survive);
  * formatting (indentation, line endings, whitespace, trailing newlines, number spelling, escapes, empty-list layout)
    may be normalized; byte-exact output is NOT promised;
  * key order is semantically irrelevant, but the serializer deterministically keeps the SOURCE order;
  * serializing again is a fixed point (canonical output is stable);
  * non-finite numbers (NaN, +-Infinity, and numbers that overflow to infinity such as 1e400) are rejected when
    parsing, and can never be serialized as `Infinity`/`NaN`.
"""
import json
import unittest
from pathlib import Path

from app.macros.model import Macro, MacroFormatError, Operation, Point, RecordInfo
from app.macros.parser import parse_bytes
from app.macros.serializer import serialize
from tests import helpers
from tests.test_record_real_files import real_files

DOCS = Path(__file__).resolve().parents[1] / "docs"


def semantic_diff(a, b, path="$"):
    """Differences in MEANING: same keys and values, same value types (1, 1.0 and True differ), lists in order;
    dict key order does not matter."""
    if type(a) is not type(b):
        return [f"{path}: type {type(a).__name__} != {type(b).__name__}"]
    if isinstance(a, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(a)} != {sorted(b)}"]
        return [d for k in a for d in semantic_diff(a[k], b[k], f"{path}.{k}")]
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in semantic_diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def strict_loads(data: bytes):
    def refuse(name):
        raise AssertionError(f"non-JSON constant {name!r} in the output")
    return json.loads(data.decode("utf-8-sig"), parse_constant=refuse)


class Semantic(unittest.TestCase):
    def assertSemanticRoundTrip(self, data: bytes, expected=None):
        """data -> parse -> serialize -> parse: same meaning as `expected` (default: the source itself)."""
        source = expected if expected is not None else strict_loads(data)
        out = serialize(parse_bytes(data))
        self.assertEqual(semantic_diff(source, strict_loads(out)), [], "meaning changed by serialize")
        again = parse_bytes(out)
        self.assertEqual([i for i in again.issues if i.severity != "info"], [], "canonical output must parse cleanly")
        self.assertEqual(semantic_diff(source, strict_loads(serialize(again))), [], "meaning changed by the second trip")
        self.assertEqual(serialize(again), out, "canonical output must be a fixed point")
        return out


class FormattingIsNormalizedMeaningIsKept(Semantic):
    def base(self, **fmt):
        return helpers.to_bytes(helpers.simple_macro(), **fmt)

    def test_indentation_and_compactness(self):
        for name, fmt in (("indent 2", dict(indent=2)), ("indent 4", dict(indent=4)), ("indent 8", dict(indent=8)),
                          ("tab", dict(indent="\t")), ("compact", dict(indent=None))):
            with self.subTest(name):
                self.assertSemanticRoundTrip(self.base(**fmt))

    def test_compact_json_with_spaces_after_separators(self):
        obj = helpers.simple_macro()
        self.assertSemanticRoundTrip(json.dumps(obj).encode("utf-8"), obj)          # ", " and ": "
        self.assertSemanticRoundTrip(json.dumps(obj, separators=(",", ":")).encode("utf-8"), obj)

    def test_line_endings(self):
        text = self.base(newline="\n")
        for name, data in (("LF", text), ("CRLF", text.replace(b"\n", b"\r\n")), ("CR only", text.replace(b"\n", b"\r")),
                           ("mixed", text.replace(b"\n", b"\r\n", 3)), ("LF + trailing newline", text + b"\n"),
                           ("CRLF + BOM", b"\xef\xbb\xbf" + text.replace(b"\n", b"\r\n"))):
            with self.subTest(name):
                self.assertSemanticRoundTrip(data, helpers.simple_macro())

    def test_extra_whitespace_anywhere_between_tokens(self):
        obj = helpers.simple_macro()
        noisy = json.dumps(obj, separators=(" ,\r\n\t ", " :\t "))
        self.assertSemanticRoundTrip((noisy + "\r\n\r\n   ").encode("utf-8"), obj)
        self.assertSemanticRoundTrip(self.base() + b"\r\n\r\n\r\n", obj)
        self.assertSemanticRoundTrip(self.base() + b"   \t ", obj)

    def test_expanded_and_irregular_layout(self):
        data = b'{\r\n    "operations": [\r\n    ],\r\n  "recordInfo": {\r\n\r\n}\r\n}'
        self.assertSemanticRoundTrip(data, {"operations": [], "recordInfo": {}})
        irregular = self.base().replace(b'    "recordInfo"', b'  "recordInfo"')
        self.assertSemanticRoundTrip(irregular, helpers.simple_macro())

    def test_equivalent_number_spellings_keep_their_type_and_value(self):
        for spelling, value in ((b"1.50", 1.5), (b"1E3", 1000.0), (b"1e3", 1000.0), (b"10E-1", 1.0), (b"0.10", 0.1),
                                (b"1.0", 1.0), (b"-0", 0), (b"12345678901234567890", 12345678901234567890)):
            with self.subTest(spelling=spelling):
                data = b'{"operations": [{"operationId": "AndroidBack", "timing": ' + spelling + b'}]}'
                out = self.assertSemanticRoundTrip(data, {"operations": [{"operationId": "AndroidBack", "timing": value}]})
                got = strict_loads(out)["operations"][0]["timing"]
                self.assertIs(type(got), type(value))

    def test_string_escapes(self):
        data = b'{"operations": [{"operationId": "ImeClipboard", "timing": 1, "text": "a@/b @u00e9 @u4e2d @n @" @@ @ud83d@ude00"}]}'.replace(b"@", b"\\")
        expected = {"operations": [{"operationId": "ImeClipboard", "timing": 1, "text": 'a/b é 中 \n " \\ 😀'}]}
        self.assertSemanticRoundTrip(data, expected)
        literal = json.dumps(expected, ensure_ascii=False).encode("utf-8")
        self.assertSemanticRoundTrip(literal, expected)

    def test_a_lone_surrogate_escape_mixed_with_literal_text_can_still_be_serialized(self):
        data = '{"operations": [{"operationId": "ImeClipboard", "timing": 1, "text": "é \\ud800"}]}'.encode("utf-8")
        out = self.assertSemanticRoundTrip(data)                       # must not raise UnicodeEncodeError
        self.assertEqual(strict_loads(out)["operations"][0]["text"], "é \ud800")

    def test_value_types_and_unknown_content_survive(self):
        obj = {"operations": [{"operationId": "Custom", "timing": 3, "flag": True, "none": None, "f": 2.0, "i": 2,
                               "list": [1, 1.0, True, None, "x", {"deep": [[], {}]}], "unknown": {"a": {"b": [3]}},
                               "points": [{"id": 1, "x": 5, "y": 6, "state": 1, "pressure": 0.5}]}],
               "recordInfo": helpers.record_info(), "futureTopLevel": {"k": [1, 2]}}
        out = self.assertSemanticRoundTrip(json.dumps(obj).encode("utf-8"), obj)
        self.assertEqual(semantic_diff(obj, strict_loads(out)), [])


class KeyOrder(Semantic):
    def test_key_order_is_semantically_irrelevant_and_the_source_order_is_kept(self):
        a = {"operations": [{"timing": 1, "operationId": "AndroidBack"}], "recordInfo": {"b": 1, "a": 2}}
        b = {"recordInfo": {"a": 2, "b": 1}, "operations": [{"operationId": "AndroidBack", "timing": 1}]}
        out_a = self.assertSemanticRoundTrip(json.dumps(a).encode("utf-8"), a)
        out_b = self.assertSemanticRoundTrip(json.dumps(b).encode("utf-8"), b)
        self.assertEqual(semantic_diff(strict_loads(out_a), strict_loads(out_b)), [])
        self.assertEqual(list(strict_loads(out_a)), ["operations", "recordInfo"])
        self.assertEqual(list(strict_loads(out_b)), ["recordInfo", "operations"])
        self.assertEqual(list(strict_loads(out_a)["operations"][0]), ["timing", "operationId"])
        self.assertEqual(list(strict_loads(out_b)["recordInfo"]), ["a", "b"])

    def test_a_macro_built_in_code_serializes_deterministically(self):
        def build():
            macro = Macro(operations=[Operation("AndroidBack", 5, params={"z": 1, "a": 2}),
                                      Operation("PutMultiTouch", 6, [Point(1, 2, 3, 1)])],
                          record_info=RecordInfo({"resolutionWidth": 960}))
            return serialize(macro)
        first = build()
        self.assertEqual(first, build())
        self.assertEqual(list(strict_loads(first)), ["operations", "recordInfo"])
        self.assertEqual(list(strict_loads(first)["operations"][0]), ["timing", "operationId", "z", "a"])
        self.assertEqual(list(strict_loads(first)["operations"][1]["points"][0]), ["id", "x", "y", "state"])


class ActualRecordStructures(Semantic):
    def test_synthetic_simple_and_merged_files(self):
        for name, obj in (("simple", helpers.simple_macro()), ("merged", helpers.merged_macro())):
            with self.subTest(name):
                self.assertSemanticRoundTrip(helpers.to_bytes(obj), obj)

    def test_every_real_file_round_trips_semantically_when_present(self):
        files = real_files()
        if not files:
            self.skipTest("no real .record files found on this machine")
        for path in files:
            with self.subTest(path.name):
                self.assertSemanticRoundTrip(path.read_bytes())


class NonFiniteNumbersAreRejected(unittest.TestCase):
    PLACES = (
        b'{"operations": [{"operationId": "AndroidBack", "timing": %s}]}',
        b'{"operations": [{"operationId": "PutMultiTouch", "timing": 1, "points": [{"id": 1, "x": %s, "y": 2, "state": 1}]}]}',
        b'{"operations": [], "recordInfo": {"resolutionWidth": %s}}',
        b'{"operations": [], "futureKey": {"nested": [%s]}}',
        b'{"mergedRecords": [{"operations": [{"operationId": "AndroidBack", "timing": 1, "extra": %s}], "recordInfo": {}}]}',
    )

    def test_literals_and_overflowing_numbers_are_refused_everywhere(self):
        for token in (b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-1e400", b"1E999", b"1.5e309"):
            for template in self.PLACES:
                with self.subTest(token=token, template=template[:40]):
                    with self.assertRaises(MacroFormatError):
                        parse_bytes(template % token)

    def test_large_but_finite_numbers_are_accepted(self):
        data = b'{"operations": [{"operationId": "AndroidBack", "timing": 1.7976931348623157e308}]}'
        self.assertEqual(parse_bytes(data).operations[0].timing, 1.7976931348623157e308)

    def test_an_absurdly_long_integer_is_a_format_error_not_a_crash(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": [{"operationId": "AndroidBack", "timing": 1' + b"0" * 5000 + b"}]}")

    def test_absurdly_deep_nesting_is_a_format_error_not_a_crash(self):
        with self.assertRaises(MacroFormatError):
            parse_bytes(b'{"operations": ' + b"[" * 100000 + b"]" * 100000 + b"}")

    def test_serialize_can_never_emit_a_non_finite_number(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            builders = (
                lambda v: Macro(operations=[Operation("AndroidBack", v)]),
                lambda v: Macro(operations=[Operation("AndroidBack", 1, params={"x": [v]})]),
                lambda v: Macro(operations=[Operation("PutMultiTouch", 1, [Point(1, v, 2, 1)])]),
                lambda v: Macro(record_info=RecordInfo({"resolutionWidth": v})),
                lambda v: Macro(extras={"future": {"k": v}}),
            )
            for i, build in enumerate(builders):
                with self.subTest(value=bad, place=i):
                    with self.assertRaises(MacroFormatError):
                        serialize(build(bad))

    def test_everything_serialize_writes_parses_again(self):
        macro = Macro(operations=[Operation("AndroidBack", 1.5)], record_info=RecordInfo({"a": 1e308}))
        self.assertEqual(parse_bytes(serialize(macro)).operations[0].timing, 1.5)


class ContractIsDocumented(unittest.TestCase):
    def test_docs_and_serializer_say_byte_exact_is_not_guaranteed(self):
        text = (DOCS / "MACRO_FORMAT.md").read_text(encoding="utf-8")
        self.assertIn("Byte-exact round trip is NOT guaranteed", text)
        self.assertIn("canonical", text)
        from app.macros import serializer
        self.assertIn("NOT guaranteed", serializer.__doc__)


if __name__ == "__main__":
    unittest.main()
