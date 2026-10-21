"""Macro files, version 2: conditions and decisions on disk, v1 untouched, the file treated as untrusted input.

Every test writes into its own temporary folder; the user's `data/macros` is only ever READ (and its bytes and
modification times are checked to be unchanged afterwards). Execution goes through the same `Rig` as
tests/test_decide.py -- the real `PlaybackEngine` and preflight -- and, for serial isolation, the real
`InputSession` over a real `MappingService` with only the device link faked.
"""
import ast
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import fields
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped
from app.decide import (ConditionalMacro, Condition, Decision, FlowState, RunMacro, TapMatch, WaitUntil,
                        load_macro_file, migrate_v1, run_steps, save_macro_file)
from app.decide import format as v2
from app.decide.format import MacroFormatError, asset_dir_for, asset_name, conditional_from_json, \
    conditional_to_json
from app.observe import ColorRegionQuery, PixelQuery, TemplateQuery, TextLine, TextLinesQuery, TextQuery
from app.playback.format import macro_from_json, macro_to_json
from app.playback.macro import ActionOp, Macro
from app.providers.recording import Recording
from app.vision.bitmap import Bitmap, encode_png
from app.vision.roi import Roi
from tests.test_decide import V1_MACRO, Rig, a_macro, ctx
from tests.test_ldplayer_playback import rig as ld_rig
from tests.test_observe import BLUE, RED, WHITE, FakeOcr, block, mark, scene, three_marks
from tests.test_playback import PROV, TID, down, up

ROOT = Path(__file__).resolve().parents[1]
BLANK = Bitmap.solid(200, 100, WHITE)


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="macro-v2-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name="daily"):
        return self.dir / f"{name}.macro.json"

    def write(self, text, name="daily", assets=None):
        p = self.path(name)
        p.write_text(text, encoding="utf-8", newline="\n")
        if assets:
            d = asset_dir_for(p)
            d.mkdir(exist_ok=True)
            for n, data in assets.items():
                (d / n).write_bytes(data)
        return p


def full_macro(template=None):
    """Every step, branch and condition kind the format stores."""
    template = template or mark()
    return ConditionalMacro("daily collect", (
        RunMacro(a_macro(3, 4)),
        WaitUntil(Condition.pixel((30, 20), RED, tolerance=3, label="red square"), timeout_s=20, interval_s=0.5),
        Decision(Condition.template(template, region=Roi(0, 0, 60, 30), min_confidence=0.99),
                 then=TapMatch(offset=(2, -1))),
        Decision(Condition.color(RED, region=Roi(25, 15, 20, 20), min_ratio=0.2), then=RunMacro(a_macro(7, 8))),
        Decision(Condition.text("Collect", region=Roi(0, 0, 200, 100), language="en", expected=False),
                 then=None, otherwise=TapMatch(ActionOp.DOUBLE_TAP, (0, 5))),
    ))


def v2_doc(*steps, name="m"):
    return json.dumps({"schema": "automation-macro", "version": 2, "name": name, "steps": list(steps)})


def pixel_cond(**over):
    c = {"type": "pixel", "point": [30, 20], "color": [255, 0, 0], "tolerance": 0, "expected": True, "label": ""}
    c.update(over)
    return c


def tap_if(cond=None, **over):
    s = {"type": "if", "condition": cond or pixel_cond(), "then": {"type": "tap_match", "op": "tap",
                                                                   "offset": [0, 0]}, "else": None}
    s.update(over)
    return s


def wait(cond=None, **over):
    s = {"type": "wait_until", "condition": cond or pixel_cond(), "timeout_s": 5, "interval_s": 0.05}
    s.update(over)
    return s


# ---- A-D: version 1 -----------------------------------------------------------------------------------
class Version1(Folder):
    def test_a_v1_loads_as_the_same_macro_as_before(self):
        loaded = load_macro_file(self.write(V1_MACRO))
        self.assertIsInstance(loaded, Macro)
        self.assertEqual(loaded, macro_from_json(V1_MACRO))

    def test_b_v1_saves_as_v1_never_upgraded_silently(self):
        p = save_macro_file(macro_from_json(V1_MACRO), self.path())
        self.assertEqual(p.read_text(encoding="utf-8"), V1_MACRO)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["version"], 1)
        self.assertFalse(asset_dir_for(p).exists())

    def test_c_reading_never_rewrites_a_file(self):
        p = self.write(V1_MACRO)
        os.utime(p, (1_000_000_000, 1_000_000_000))
        before = (p.read_bytes(), p.stat().st_mtime_ns)
        for _ in range(3):
            load_macro_file(p)
        self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), before)
        self.assertEqual(sorted(x.name for x in self.dir.iterdir()), [p.name], "reading created a file")

    def test_c_the_users_own_macros_load_unchanged_and_round_trip_byte_for_byte(self):
        folder = ROOT / "data" / "macros"
        files = sorted(folder.rglob("*.macro.json")) if folder.is_dir() else []
        if not files:
            self.skipTest("no local macros")
        for path in files:
            with self.subTest(path.name):
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                loaded = load_macro_file(path)
                if isinstance(loaded, ConditionalMacro):    # a script the application saved: its own serializer
                    text = conditional_to_json(loaded)[0]
                else:
                    self.assertIsInstance(loaded, Macro)
                    text = macro_to_json(loaded)
                self.assertEqual(text.encode("utf-8"), before[0])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_v1_executes_as_before(self):
        r = Rig()
        m = load_macro_file(self.write(V1_MACRO))
        report = r.run(RunMacro(m))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (11, 12))])

    def test_d_migration_is_explicit_and_keeps_the_v1_file(self):
        old = self.write(V1_MACRO, "old")
        migrated = migrate_v1(load_macro_file(old))
        self.assertEqual(migrated.steps, (RunMacro(macro_from_json(V1_MACRO)),))
        new = save_macro_file(migrated, self.path("new"))
        self.assertEqual(old.read_text(encoding="utf-8"), V1_MACRO, "migration touched the v1 file")
        self.assertEqual(json.loads(new.read_text(encoding="utf-8"))["version"], 2)
        self.assertEqual(load_macro_file(new), migrated)
        a, b = Rig(), Rig()
        a.run(RunMacro(macro_from_json(V1_MACRO)))
        b.run(*load_macro_file(new).steps)
        self.assertEqual([c[:2] for c in a.input.calls], [c[:2] for c in b.input.calls])

    def test_migration_refuses_what_is_not_a_v1_macro(self):
        with self.assertRaises(MacroFormatError):
            migrate_v1(full_macro())


# ---- E-G: version 2 -----------------------------------------------------------------------------------
class Version2(Folder):
    def test_e_f_g_save_load_round_trip_and_fixed_point(self):
        original = full_macro()
        p = save_macro_file(original, self.path())
        loaded = load_macro_file(p)
        self.assertIsInstance(loaded, ConditionalMacro)
        self.assertEqual(loaded, original, "a stored macro must come back as exactly the objects it was")
        first = p.read_bytes()
        save_macro_file(loaded, p, overwrite=True)
        self.assertEqual(p.read_bytes(), first, "save(load(x)) must be byte-identical")
        self.assertEqual(sorted(x.name for x in asset_dir_for(p).iterdir()), [asset_name(mark())])

    def test_the_format_is_deterministic_sorted_ascii_json(self):
        text, assets = conditional_to_json(full_macro())
        self.assertEqual(text, conditional_to_json(full_macro())[0])
        self.assertTrue(text.isascii() and text.endswith("}\n"))
        data = json.loads(text)
        self.assertEqual((data["schema"], data["version"]), ("automation-macro", 2))
        self.assertEqual(list(data), sorted(data))
        self.assertNotIn("rgb", text, "image data must never be embedded in the JSON")
        self.assertEqual(list(assets), [asset_name(mark())])

    def test_the_same_template_is_stored_once_and_read_once(self):
        m = ConditionalMacro("twice", (Decision(Condition.template(mark()), then=TapMatch()),
                                       Decision(Condition.template(mark(), min_confidence=0.9), then=TapMatch())))
        loaded = load_macro_file(save_macro_file(m, self.path()))
        self.assertIs(loaded.steps[0].condition.query.template, loaded.steps[1].condition.query.template)
        self.assertEqual(len(list(asset_dir_for(self.path()).iterdir())), 1)

    def test_every_query_field_is_stored(self):
        """Adding a field to a query must not be silently lost by persistence."""
        stored = {PixelQuery: {"point", "color", "tolerance", "expected"},
                  ColorRegionQuery: {"color", "tolerance", "region", "min_ratio", "expected"},
                  TemplateQuery: {"template", "min_confidence", "region", "max_results", "expected", "any_size"},
                  TextQuery: {"text", "match", "ignore_case", "region", "language", "min_confidence",
                              "max_results", "expected"},
                  TextLinesQuery: {"lines", "ignore_case", "region", "language", "min_confidence", "expected"},
                  TextLine: {"text", "present"}}
        for query, names in stored.items():
            self.assertEqual({f.name for f in fields(query)}, names, query.__name__)
        # ...and the file format asks for exactly those keys (plus "type" and the label)
        from app.decide.format import _CONDITION_KEYS
        self.assertEqual(set(_CONDITION_KEYS["text_lines"]) | {"expected"},
                         {f.name for f in fields(TextLinesQuery)})

    def test_saving_refuses_to_overwrite_unless_asked(self):
        p = save_macro_file(full_macro(), self.path())
        with self.assertRaises(MacroFormatError):
            save_macro_file(full_macro(), p)
        save_macro_file(full_macro(), p, overwrite=True)
        self.assertEqual([x.name for x in self.dir.iterdir() if x.suffix == ".tmp"], [])


# ---- H-Q: every kind, loaded from a file, evaluated and executed ------------------------------------------
class LoadedBehaviour(Folder):
    def load(self, macro):
        return load_macro_file(save_macro_file(macro, self.path(), overwrite=True))

    def test_h_pixel(self):
        r = Rig()
        m = self.load(ConditionalMacro("p", (Decision(Condition.pixel((30, 20), RED), then=TapMatch()),)))
        self.assertIs(r.run(*m.steps).state, FlowState.COMPLETED)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (30, 20))])

    def test_i_color(self):
        r = Rig()
        m = self.load(ConditionalMacro("c", (Decision(Condition.color(RED, region=Roi(30, 20, 10, 10),
                                                                      min_ratio=0.9), then=TapMatch()),)))
        r.run(*m.steps)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (35, 25))])     # centre of the red area

    def test_j_template(self):
        r = Rig([three_marks()])
        m = self.load(ConditionalMacro("t", (Decision(Condition.template(mark(), region=Roi(50, 0, 40, 30),
                                                                         min_confidence=0.99),
                                                      then=TapMatch(offset=(2, -1))),)))
        r.run(*m.steps)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (66, 13))])

    def test_k_text(self):
        r = Rig(ocr=FakeOcr([block("Collect reward", 40, 60, 80, 12)]))
        m = self.load(ConditionalMacro("x", (Decision(Condition.text("collect"), then=TapMatch()),)))
        r.run(*m.steps)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (80, 66))])

    def test_l_expected_false(self):
        r = Rig()
        m = self.load(ConditionalMacro("n", (Decision(Condition.pixel((30, 20), RED, expected=False),
                                                      otherwise=TapMatch()),)))
        self.assertFalse(m.steps[0].condition.expected)
        report = r.run(*m.steps)
        self.assertEqual(report.steps[0].branch, "else")
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (30, 20))])

    def test_m_if_then_without_else_does_nothing_when_false(self):
        r = Rig()
        m = self.load(ConditionalMacro("n", (Decision(Condition.pixel((0, 0), RED), then=TapMatch()),)))
        self.assertIs(r.run(*m.steps).state, FlowState.COMPLETED)
        self.assertEqual(r.input.calls, [])

    def test_n_if_then_else(self):
        m = self.load(ConditionalMacro("ite", (Decision(Condition.pixel((30, 20), RED), then=RunMacro(a_macro(1, 1)),
                                                        otherwise=RunMacro(a_macro(2, 2))),)))
        yes, no = Rig(), Rig([BLANK])
        yes.run(*m.steps)
        no.run(*m.steps)
        self.assertEqual([c[:2] for c in yes.input.calls], [("tap", (1, 1))])
        self.assertEqual([c[:2] for c in no.input.calls], [("tap", (2, 2))])

    def test_o_wait_until(self):
        m = self.load(ConditionalMacro("w", (WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5, interval_s=0.05),
                                             Decision(Condition.pixel((30, 20), RED), then=TapMatch()))))
        r = Rig([BLANK, BLANK, scene()])
        self.assertIs(r.run(*m.steps).state, FlowState.COMPLETED)
        self.assertEqual(r.screens.captures, 4)
        timeout = self.load(ConditionalMacro("w2", (WaitUntil(Condition.pixel((30, 20), RED), timeout_s=0.2,
                                                              interval_s=0.05),)))
        self.assertIs(Rig([BLANK]).run(*timeout.steps).state, FlowState.TIMED_OUT)

    def test_p_run_macro_branch_and_step(self):
        m = self.load(ConditionalMacro("r", (RunMacro(a_macro(5, 5)),
                                             Decision(Condition.pixel((30, 20), RED), then=RunMacro(a_macro(6, 6))))))
        r = Rig()
        r.run(*m.steps)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (5, 5)), ("tap", (6, 6))])
        self.assertEqual(m.steps[0].macro.name, "prepared")

    def test_q_tap_match_branch_ops(self):
        for op in (ActionOp.TAP, ActionOp.DOUBLE_TAP, ActionOp.SECONDARY_TAP):
            m = self.load(ConditionalMacro("q", (Decision(Condition.pixel((30, 20), RED), then=TapMatch(op, (1, 2))),)))
            r = Rig()
            r.run(*m.steps)
            self.assertEqual([c[:2] for c in r.input.calls], [(op.value, (31, 22))])
            os.remove(self.path())


# ---- R-S: template assets -------------------------------------------------------------------------------
class TemplateAssets(Folder):
    def doc_with(self, name):
        return v2_doc(tap_if({"type": "template", "asset": name, "region": None, "min_confidence": 0.95,
                              "max_results": 1, "expected": True, "label": ""}))

    def test_r_missing_template(self):
        name = asset_name(mark())
        with self.assertRaisesRegex(MacroFormatError, "missing"):
            load_macro_file(self.write(self.doc_with(name)))
        with self.assertRaisesRegex(MacroFormatError, "no asset folder"):
            conditional_from_json(self.doc_with(name))

    def test_s_invalid_templates(self):
        name = asset_name(mark())
        good = encode_png(mark())
        big = Bitmap.solid(3000, 1, RED)
        cases = {
            "not a png": b"GIF89a not an image",
            "corrupt": good[:30] + bytes([good[30] ^ 0xFF]) + good[31:],
            "truncated": good[:40],
            "oversized": encode_png(big),
            "wrong pixels": encode_png(Bitmap.solid(8, 8, BLUE)),
        }
        for case, data in cases.items():
            with self.subTest(case):
                p = self.write(self.doc_with(name), assets={name: data})
                with self.assertRaises(MacroFormatError) as caught:
                    load_macro_file(p)
                self.assertIn(name, str(caught.exception))
                shutil.rmtree(asset_dir_for(p))

    def test_an_asset_larger_than_the_limit_is_refused_before_it_is_read(self):
        name = asset_name(mark())
        p = self.write(self.doc_with(name), assets={name: b"\x89PNG" + b"\0" * (v2.MAX_ASSET_BYTES + 1)})
        with self.assertRaisesRegex(MacroFormatError, "larger than"):
            load_macro_file(p)

    def test_a_link_out_of_the_asset_folder_is_refused(self):
        name = asset_name(mark())
        outside = self.dir / "outside.png"
        outside.write_bytes(encode_png(mark()))
        p = self.write(self.doc_with(name))
        asset_dir_for(p).mkdir()
        try:
            os.symlink(outside, asset_dir_for(p) / name)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symbolic links")
        with self.assertRaisesRegex(MacroFormatError, "inside"):
            load_macro_file(p)

    def test_x_y_only_the_content_named_file_pattern_is_accepted(self):
        good = asset_name(mark())
        bad_names = ["../" + good, "..\\" + good, "sub/" + good, "sub\\" + good, "C:/Windows/win.ini",
                     "C:\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\" + good, "file:///etc/passwd",
                     good.upper(), good[:-4] + ".bmp", good + ".exe", "template-0123.png", "", ".", "..",
                     good + "\0", "con.png", 42, None, ["a"], {"path": good}]
        for bad in bad_names:
            with self.subTest(bad=bad):
                p = self.write(self.doc_with(bad), assets={good: encode_png(mark())})
                with self.assertRaisesRegex(MacroFormatError, "not allowed"):
                    load_macro_file(p)

    def test_asset_folder_belongs_to_one_macro_file(self):
        self.assertEqual(asset_dir_for(Path("x/daily.macro.json")), Path("x/daily.assets"))
        for bad in ("daily.json", ".macro.json", "daily.macro.json.txt"):
            with self.assertRaises(MacroFormatError):
                asset_dir_for(Path(bad))


# ---- T-W: strict validation ----------------------------------------------------------------------------
class Validation(Folder):
    def refused(self, text, pattern=None):
        with self.assertRaises(MacroFormatError) as caught:
            load_macro_file(self.write(text))
        if pattern:
            self.assertRegex(str(caught.exception), pattern)

    def test_the_valid_baseline_loads(self):
        load_macro_file(self.write(v2_doc(wait(), tap_if())))

    def test_t_invalid_timeout(self):
        for bad in (0, -1, 600.5, 1e9, True, "20", None, [5]):
            with self.subTest(bad=bad):
                self.refused(v2_doc(wait(timeout_s=bad)))
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                self.refused(v2_doc(wait()).replace('"timeout_s": 5', f'"timeout_s": {token}'), "finite")
        load_macro_file(self.write(v2_doc(wait(timeout_s=600))))

    def test_u_invalid_polling_interval(self):
        for bad in (0, 0.049, -1, 0.001, True, "0.5", None):
            with self.subTest(bad=bad):
                self.refused(v2_doc(wait(interval_s=bad)))
        self.refused(v2_doc(wait()).replace('"interval_s": 0.05', '"interval_s": NaN'), "finite")

    def test_invalid_coordinates_and_regions(self):
        for over in ({"point": [-1, 5]}, {"point": [1.5, 2]}, {"point": [1]}, {"point": [100_001, 1]},
                     {"point": "30,20"}, {"color": [256, 0, 0]}, {"color": [1, 2]}, {"tolerance": 300}):
            with self.subTest(over=over):
                self.refused(v2_doc(tap_if(pixel_cond(**over))))
        color = {"type": "color", "color": [255, 0, 0], "tolerance": 0, "min_ratio": 0.5, "expected": True,
                 "label": ""}
        for region in ({"x": -1, "y": 0, "width": 5, "height": 5}, {"x": 0, "y": 0, "width": 0, "height": 5},
                       {"x": 0, "y": 0, "width": 5}, {"x": 0, "y": 0, "width": 5, "height": 5, "z": 1},
                       [0, 0, 5, 5], {"x": "0", "y": 0, "width": 5, "height": 5}):
            with self.subTest(region=region):
                self.refused(v2_doc(tap_if(dict(color, region=region))))
        for offset in ([0], [0.5, 0], [20_000, 0], "0,0"):
            with self.subTest(offset=offset):
                self.refused(v2_doc(tap_if(then={"type": "tap_match", "op": "tap", "offset": offset})))

    def test_v_malformed(self):
        base = json.loads(v2_doc(tap_if()))
        cases = ["", "not json", "[]", "null", '{"schema": "automation-macro"}',
                 v2_doc(tap_if()).replace('"version": 2', '"version": 2, "version": 2'),
                 json.dumps(dict(base, steps=[])), json.dumps(dict(base, steps=[tap_if()] * 101)),
                 json.dumps(dict(base, steps={})), json.dumps(dict(base, extra=1)),
                 json.dumps({k: v for k, v in base.items() if k != "name"}),
                 json.dumps(dict(base, name=5)), json.dumps(dict(base, name="x" * 101)),
                 json.dumps(dict(base, name="line\nbreak")),
                 v2_doc(tap_if(pixel_cond(expected="yes"))), v2_doc(tap_if(pixel_cond(label=None))),
                 v2_doc({k: v for k, v in tap_if().items() if k != "else"}),
                 v2_doc(tap_if(pixel_cond(expected=False))),               # THEN click with nothing to click
                 v2_doc({"type": "run_macro", "name": "x", "recording": {"events": []}}),
                 v2_doc({k: v for k, v in pixel_cond().items() if k != "tolerance"}),
                 "[" * 100_000]
        for text in cases:
            with self.subTest(text=text[:60]):
                self.refused(text)

    def test_v_an_if_with_both_branches_empty_is_read_it_looks_and_does_nothing(self):
        """Since 2026-10-19 (nested branches): the editor places an IF first and fills its branches block by block,
        so an empty one is a state a script passes through. It cannot act -- nothing to run, nothing to click."""
        macro = conditional_from_json(v2_doc(tap_if(then=None, **{"else": None})))
        self.assertEqual((macro.steps[0].then, macro.steps[0].otherwise), ((), ()))

    def test_w_unknown_types_and_versions(self):
        for step in ({"type": "exec", "code": "print(1)"}, {"type": "loop", "steps": []}, {"type": "if"},
                     {"type": None}, {"type": "IF", "condition": pixel_cond(), "then": None, "else": None},
                     "wait_until", 7, None):
            with self.subTest(step=step):
                self.refused(v2_doc(step), "step")
        for kind in ("script", "python", "eval", "ai", "Pixel", None):
            with self.subTest(condition=kind):
                self.refused(v2_doc(tap_if(pixel_cond(type=kind))), "condition type")
        for branch in ({"type": "python", "code": "import os"}, {"type": "adb", "command": "reboot"},
                       {"type": "tap", "x": 1, "y": 1}, "tap_match", []):
            with self.subTest(branch=branch):
                self.refused(v2_doc(tap_if(then=branch)), "branch")
        self.refused(v2_doc(tap_if(then={"type": "tap_match", "op": "key_press", "offset": [0, 0]})))
        self.refused(v2_doc(tap_if(then={"type": "tap_match", "op": "type_text", "offset": [0, 0]})))
        for schema, version in (("automation-macro", 3), ("automation-macro", "2"), ("automation-macro", True),
                                ("automation-macro", 2.0), ("automation-macro", 0), ("other", 2),
                                ("automation-recording", 1)):
            with self.subTest(schema=schema, version=version):
                doc = json.loads(v2_doc(tap_if()))
                doc.update(schema=schema, version=version)
                self.refused(json.dumps(doc))

    def test_a_v1_file_is_still_judged_by_the_v1_rules(self):
        self.refused(V1_MACRO.replace('"name": "old macro"', '"name": "old macro", "steps": []'))


# ---- Z / security: nothing in a file can pick a target, grant a binding, or run code ------------------
class TrustBoundary(Folder):
    def test_tap_match_cannot_carry_a_target_or_coordinates(self):
        for extra in ({"target": {"provider_id": "x", "target_id": "y"}}, {"target_id": "t-2"}, {"x": 1, "y": 1},
                      {"serial": "emulator-5556"}, {"provider": "adb"}):
            with self.subTest(extra=extra):
                branch = dict({"type": "tap_match", "op": "tap", "offset": [0, 0]}, **extra)
                with self.assertRaisesRegex(MacroFormatError, "unknown"):
                    load_macro_file(self.write(v2_doc(tap_if(then=branch))))

    def test_a_file_cannot_grant_itself_a_different_target(self):
        rec = json.loads(macro_to_json(a_macro(tid="someone-else")))["recording"]
        for key in ("allow_different_target", "target", "rebind"):
            with self.subTest(key=key):
                step = {"type": "run_macro", "name": "x", "recording": rec, key: True}
                with self.assertRaisesRegex(MacroFormatError, "unknown"):
                    load_macro_file(self.write(v2_doc(step)))
        with self.assertRaises(MacroFormatError):
            save_macro_file(ConditionalMacro("x", (RunMacro(a_macro(), allow_different_target=True),)), self.path())

    def test_a_stored_macro_for_another_target_is_refused_by_preflight_unless_the_caller_allows_it(self):
        m = load_macro_file(save_macro_file(ConditionalMacro("x", (RunMacro(a_macro(9, 9, tid="elsewhere")),)),
                                            self.path()))
        r = Rig()
        report = r.run(*m.steps)
        self.assertEqual(report.reason, "action_refused")
        self.assertIn("target_mismatch", [i.code for i in report.issues])
        self.assertEqual(r.input.calls, [])
        r2 = Rig()
        report = run_steps(m.steps, target=r2.target, context=r2.context, create_playback=r2.create_playback,
                           stop=r2.stop, allow_different_target=True)            # the caller's run-time choice
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([c[:2] for c in r2.input.calls], [("tap", (9, 9))])

    def test_a_stored_macro_of_another_provider_never_runs(self):
        m = load_macro_file(save_macro_file(ConditionalMacro("x", (RunMacro(a_macro(prov="other-provider")),)),
                                            self.path()))
        r = Rig()
        report = run_steps(m.steps, target=r.target, context=r.context, create_playback=r.create_playback,
                           stop=r.stop, allow_different_target=True)
        self.assertIn("wrong_provider", [i.code for i in report.issues])
        self.assertEqual(r.input.calls, [])

    def test_a_loaded_tap_is_stamped_with_the_target_that_observed_it(self):
        m = load_macro_file(save_macro_file(ConditionalMacro("x", (Decision(Condition.pixel((30, 20), RED),
                                                                            then=TapMatch()),)), self.path()))
        r = Rig(target_id="the-bound-one")
        r.run(*m.steps)
        self.assertEqual(r.built[0].target.target_id, "the-bound-one")

    def test_z_the_format_module_evaluates_imports_and_executes_nothing(self):
        tree = ast.parse((ROOT / "app" / "decide" / "format.py").read_text(encoding="utf-8"))
        builtins = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "globals", "locals", "vars",
                    "open", "input"}
        methods = {"import_module", "system", "popen", "Popen", "run", "call", "check_output", "loads", "load",
                   "startfile", "exec_module"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, builtins, f"format.py calls {node.func.id}()")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, methods, f"format.py calls .{node.func.attr}()")
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        for bad in ("pickle", "marshal", "importlib", "subprocess", "shelve", "yaml", "ctypes", "app.adb",
                    "app.ldplayer", "app.windows", "app.gui", "app.bootstrap", "app.core.process", "app.ocr"):
            self.assertFalse(any(m == bad or m.startswith(bad + ".") for m in imports), bad)

    def test_z_loading_calls_no_provider_and_sends_nothing(self):
        r = Rig()
        m = load_macro_file(save_macro_file(full_macro(), self.path()))
        self.assertEqual((r.screens.captures, r.input.calls, r.built), (0, [], []))
        self.assertIsInstance(m, ConditionalMacro)

    def test_a_json_payload_in_a_text_field_stays_text(self):
        doc = v2_doc(tap_if({"type": "text", "text": "__import__('os').system('calc')", "match": "contains",
                             "ignore_case": True, "region": None, "language": "", "min_confidence": 0.0,
                             "max_results": 1, "expected": True, "label": "{{7*7}}"}))
        m = load_macro_file(self.write(doc))
        self.assertEqual(m.steps[0].condition.query.text, "__import__('os').system('calc')")
        for bad in ("en; rm -rf", "../ru", "x" * 40):
            with self.subTest(language=bad):
                with self.assertRaises(MacroFormatError):
                    load_macro_file(self.write(doc.replace('"language": ""', json.dumps({"language": bad})[1:-1])))


# ---- regressions: parallel, stop, OCR cancellation, isolation --------------------------------------------
class Regressions(Folder):
    def test_a_loaded_macro_runs_on_two_targets_in_parallel_each_on_its_own_serial(self):
        _, svc, adb, stop, t1, max_age = ld_rig()
        t2 = svc.verify("ld2")
        m = load_macro_file(save_macro_file(ConditionalMacro("par", (
            WaitUntil(Condition.color(RED, min_ratio=0.0001), timeout_s=5, interval_s=0.05),
            Decision(Condition.color(RED, min_ratio=0.0001), then=TapMatch()))), self.path()))

        def create_playback(macro, target, *, dry_run, allow_different_target):
            from app.ldplayer.playback import create_ldplayer_playback
            return create_ldplayer_playback(macro, target, adb, svc, stop, dry_run=dry_run,
                                            allow_different_target=allow_different_target, max_age_s=max_age)

        screens = {t1.target_id: Bitmap.solid(200, 100, WHITE).with_rect(30, 20, 4, 4, RED),
                   t2.target_id: Bitmap.solid(200, 100, WHITE).with_rect(150, 70, 4, 4, RED)}
        out, barrier = {}, threading.Barrier(2)

        def work(target):
            c, _ = ctx([screens[target.target_id]], provider_id=target.provider_id, target_id=target.target_id,
                       stop=stop)
            barrier.wait(5)
            out[target.target_id] = run_steps(m.steps, target=target, context=c, create_playback=create_playback,
                                              stop=stop)

        threads = [threading.Thread(target=work, args=(t,)) for t in (t1, t2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertTrue(all(r.state is FlowState.COMPLETED for r in out.values()))
        self.assertEqual(sorted((s, x, y) for s, x, y, _ in adb.taps),
                         [("emulator-5554", 32, 22), ("emulator-5556", 152, 72)])

    def test_the_emergency_stop_ends_a_loaded_wait(self):
        m = load_macro_file(save_macro_file(ConditionalMacro("s", (
            WaitUntil(Condition.pixel((30, 20), RED), timeout_s=60, interval_s=5),
            Decision(Condition.pixel((0, 0), WHITE), then=TapMatch()))), self.path()))
        r = Rig([BLANK])
        threading.Timer(0.15, lambda: r.stop.trigger("test")).start()
        started = time.monotonic()
        report = r.run(*m.steps)
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(r.input.calls, [])

    def test_a_stop_while_a_loaded_text_condition_is_read_is_a_cancel(self):
        m = load_macro_file(save_macro_file(ConditionalMacro("o", (
            WaitUntil(Condition.text("Collect"), timeout_s=5, interval_s=0.05),)), self.path()))
        r = Rig(ocr=FakeOcr(fail=EmergencyStopped("stop while reading")))
        report = r.run(*m.steps)
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))

    def test_loaded_objects_are_immutable_and_shared_safely(self):
        m = load_macro_file(save_macro_file(full_macro(), self.path()))
        with self.assertRaises(Exception):
            m.steps[0].macro = None
        with self.assertRaises(Exception):
            m.steps[2].condition.query.template.rgb = b""

    def test_the_format_module_holds_no_module_level_mutable_state(self):
        for line in (ROOT / "app" / "decide" / "format.py").read_text(encoding="utf-8").splitlines():
            if line.startswith((" ", "\t", "#", "@", "class ", "def ")) or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip().isidentifier() and not name.strip().startswith("__"):
                self.assertFalse(value.strip().startswith(("[", "{", "dict(", "list(", "set(")), line)


if __name__ == "__main__":
    unittest.main()
