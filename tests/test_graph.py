"""Version-4 scripts: blocks joined by arrows (the person's design of 2026-10-25) -- the drawing's rules, its file, and
its runs on one target.

Fakes only: the generic target, input and screens of tests.test_decide (nothing is sent anywhere).
"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from app.decide import Condition
from app.decide.format import MacroFormatError
from app.decide.graph import (AGAIN, ALL, AT_MATCH, DONE, DOUBLE, DRAG, FOR, FOUND, HOLD, MISSING, NEXT, RIGHT, TIMES,
                              UNTIL, CheckNode, ClickNode, Edge, GraphError, MacroNode, Part, RepeatNode, Script,
                              StartNode, StopNode, WaitNode)
from app.decide.graph_format import (save_script_file, script_from_json, script_to_json, summarize_script,
                                     validate_script)
from app.vision.bitmap import Bitmap

PICTURE = Bitmap(6, 4, bytes((i * 37) % 256 for i in range(72)))


def text(*words, region=None):
    from app.vision.roi import Roi
    return Part(Condition.text_lines([(w, True) for w in words], region=Roi(*region) if region else None))


def script(*nodes, edges=(), name="Test"):
    return Script(name, (StartNode("s"),) + tuple(nodes), tuple(Edge(*e) for e in edges))


class TheDrawingsRules(unittest.TestCase):
    def test_a_loop_goes_back_only_through_a_repeat_circle(self):
        with self.assertRaisesRegex(GraphError, "repeat circle"):
            script(WaitNode("a"), WaitNode("b"), edges=(("s", NEXT, "a"), ("a", NEXT, "b"), ("b", NEXT, "a")))
        made = script(WaitNode("a"), RepeatNode("r"), edges=(("s", NEXT, "a"), ("a", NEXT, "r"), ("r", AGAIN, "a")))
        self.assertEqual(made.edge_from("r", AGAIN).target, "a")

    def test_each_output_leads_to_one_block_and_only_outputs_the_block_has(self):
        with self.assertRaisesRegex(GraphError, "One output"):
            script(WaitNode("a"), WaitNode("b"), edges=(("s", NEXT, "a"), ("s", NEXT, "b")))
        with self.assertRaisesRegex(GraphError, "no output"):
            script(WaitNode("a"), edges=(("s", FOUND, "a"),))
        with self.assertRaisesRegex(GraphError, "no output"):                 # until stopped by hand: no "done"
            script(RepeatNode("r"), WaitNode("a"), edges=(("s", NEXT, "r"), ("r", DONE, "a")))
        with self.assertRaisesRegex(GraphError, "no output"):
            script(StopNode("x"), WaitNode("a"), edges=(("s", NEXT, "x"), ("x", NEXT, "a")))

    def test_one_start_and_nothing_back_into_it(self):
        with self.assertRaisesRegex(GraphError, "exactly one start"):
            Script("Two", (StartNode("s"), StartNode("t")))
        with self.assertRaisesRegex(GraphError, "back to the start"):
            script(RepeatNode("r"), edges=(("s", NEXT, "r"), ("r", AGAIN, "s")))
        with self.assertRaisesRegex(GraphError, "same id"):
            script(WaitNode("a"), WaitNode("a"))

    def test_an_if_looks_for_one_kind_and_a_mixed_block_for_several(self):
        pixel = Part(Condition.pixel((1, 1), (1, 2, 3)))
        picture = Part(Condition.template(PICTURE))
        with self.assertRaisesRegex(GraphError, "mixed"):
            CheckNode("c", parts=(pixel, picture))
        with self.assertRaisesRegex(GraphError, "one pixel or one picture"):
            CheckNode("c", parts=(picture, picture))
        self.assertEqual(len(CheckNode("c", parts=(text("OK"), text("Готово"))).parts), 2)   # texts, each its area
        self.assertEqual(CheckNode("c", parts=(pixel, picture, text("OK")), mixed=True, mode=ALL).mode, ALL)
        with self.assertRaisesRegex(GraphError, "IS there"):
            Part(Condition.pixel((1, 1), (1, 2, 3), expected=False))

    def test_the_settings_of_blocks_are_checked(self):
        for bad in (lambda: WaitNode("w", seconds=0), lambda: RepeatNode("r", mode=UNTIL, until="25:00"),
                    lambda: RepeatNode("r", mode=TIMES, times=0), lambda: RepeatNode("r", mode=FOR, seconds=0),
                    lambda: ClickNode("k", how=DRAG, where=AT_MATCH), lambda: ClickNode("k", hold_s=0),
                    lambda: StopNode("x", message="a\nb"), lambda: StartNode("bad id!")):
            with self.assertRaises(GraphError):
                bad()
        self.assertFalse(ClickNode("k").ready)
        self.assertTrue(ClickNode("k", point=(1, 2)).ready and ClickNode("k", where=AT_MATCH).ready)
        self.assertFalse(ClickNode("k", how=DRAG, point=(1, 2)).ready)


def everything():
    return script(
        CheckNode("c", at=(100, 40), parts=(Part(Condition.template(PICTURE), (640, 480)),)),
        CheckNode("m", parts=(Part(Condition.pixel((3, 4), (10, 20, 30), tolerance=5), (640, 480)),
                              text("OK", "Готово", region=(1, 2, 30, 40))), mixed=True, mode=ALL),
        RepeatNode("r", mode=UNTIL, until="18:30"), RepeatNode("q", mode=TIMES, times=3),
        ClickNode("k", how=DOUBLE, where=AT_MATCH), ClickNode("d", how=DRAG, point=(1, 2), to=(30, 40), size=(640, 480)),
        ClickNode("h", how=HOLD, point=(5, 5), hold_s=2.5), WaitNode("w", seconds=1.5), MacroNode("p", macro="daily.macro.json"),
        StopNode("x", success=False, message="Кнопки нет"),
        edges=(("s", NEXT, "c"), ("c", FOUND, "k"), ("c", MISSING, "r"), ("r", AGAIN, "c"), ("r", DONE, "x"),
               ("k", NEXT, "m"), ("m", FOUND, "d"), ("m", MISSING, "q"), ("q", AGAIN, "m"), ("d", NEXT, "h"),
               ("h", NEXT, "w"), ("w", NEXT, "p")), name="Всё сразу")


class TheFile(unittest.TestCase):
    def test_z_the_file_module_evaluates_imports_and_executes_nothing(self):
        """The same trust boundary as version 2's (tests.test_macro_v2.TrustBoundary): a file is data."""
        import ast
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app" / "decide" / "graph_format.py")
                         .read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"eval", "exec", "compile", "__import__", "getattr", "setattr",
                                                "globals", "locals", "vars", "open", "input"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"import_module", "system", "popen", "Popen", "run", "call",
                                                  "check_output", "loads", "load", "startfile", "exec_module"})
        imports = ({n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
                   | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names})
        for bad in ("pickle", "marshal", "importlib", "subprocess", "shelve", "yaml", "ctypes", "app.adb",
                    "app.ldplayer", "app.windows", "app.gui", "app.bootstrap", "app.core.process", "app.ocr"):
            self.assertFalse(any(m == bad or m.startswith(bad + ".") for m in imports), bad)

    def test_a_script_is_read_back_exactly_as_it_was_written(self):
        made = everything()
        validate_script(made)
        text_, assets = script_to_json(made)
        self.assertEqual(json.loads(text_)["version"], 4)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_script_file(made, Path(tmp) / "all.macro.json")
            self.assertEqual(sorted(p.name for p in (Path(tmp) / "all.assets").iterdir()), sorted(assets))
            self.assertEqual(script_from_json(path.read_text(encoding="utf-8"), Path(tmp) / "all.assets"), made)
        self.assertEqual(summarize_script(json.loads(text_)), ("Всё сразу", 11, tuple(assets)))

    def test_anything_unknown_or_missing_is_refused(self):
        data = json.loads(script_to_json(everything())[0])
        for change in (lambda d: d["nodes"][0].update(extra=1), lambda d: d["nodes"][1].pop("parts"),
                       lambda d: d["nodes"][1].update(type="python"), lambda d: d.update(version=5),
                       lambda d: d["edges"].append({"from": "s", "out": "next", "to": "nowhere"}),
                       lambda d: d["edges"][0].update(out=1)):
            broken = json.loads(json.dumps(data))
            change(broken)
            with self.subTest(change=change), self.assertRaises(MacroFormatError):
                script_from_json(json.dumps(broken), None)



# ---- runs ---------------------------------------------------------------------------------------------------
from tests.test_decide import Rig, a_macro                                          # noqa: E402
from tests.test_observe import RED, WHITE                                           # noqa: E402

RED_AT = Part(Condition.pixel((30, 20), RED))                                       # the scene: red at (30, 20)
WHITE_AT_RED = Part(Condition.pixel((30, 20), WHITE))                               # never true there


class Clock:
    def __init__(self):
        self.now, self.wall = 0.0, dt.datetime(2026, 10, 25, 18, 0)

    def __call__(self):
        return self.now

    def tick(self, seconds):
        self.now += seconds
        self.wall += dt.timedelta(seconds=seconds)


def run(made, rig=None, *, macros=None, dry_run=False, clock=None, on_event=None):
    from app.decide.graph_run import run_graph
    rig = rig or Rig()
    kwargs = {} if clock is None else {"clock": clock, "wall": lambda: clock.wall}
    report = run_graph(made, target=rig.target, context=rig.context, macros=macros or {},
                       create_playback=rig.create_playback, stop=rig.stop, dry_run=dry_run, on_event=on_event,
                       **kwargs)
    return report, rig


class SaidInOtherLanguages(unittest.TestCase):
    """A provider's own words in other languages travel with a run that stopped (2026-10-27): the report's params
    carry `message_<code>`, which the screens show in the person's language."""

    def test_a_screen_that_could_not_be_read_keeps_the_providers_russian_words(self):
        from app.core.errors import TargetNotVerified
        gone = TargetNotVerified("The device is gone.", reason_code="device_gone").saying(ru="Устройства больше нет.")
        report, rig = run(script(CheckNode("c", parts=(RED_AT,)), edges=(("s", NEXT, "c"),)), Rig(fail=gone))
        self.assertEqual((report.state, report.reason), ("failed", "observation_unavailable"))
        self.assertEqual(report.params.get("message_ru"), "Устройства больше нет.")
        self.assertEqual(rig.input.calls, [])

    def test_a_click_the_provider_refused_keeps_its_russian_words(self):
        from app.core.errors import TargetNotVerified

        def gone(*_args):
            raise TargetNotVerified("The device is gone.", reason_code="device_gone").saying(ru="Устройства нет.")
        report, _ = run(script(ClickNode("k", point=(5, 5), size=(200, 100)), edges=(("s", NEXT, "k"),)),
                        Rig(on_call=gone))
        self.assertEqual((report.state, report.reason), ("failed", "device_gone"), report.message)
        self.assertEqual(report.params.get("message_ru"), "Устройства нет.")


class Runs(unittest.TestCase):
    def test_found_leads_to_a_click_where_it_was_found(self):
        events = []
        report, rig = run(script(CheckNode("c", parts=(RED_AT,)), ClickNode("k", where=AT_MATCH),
                                 edges=(("s", NEXT, "c"), ("c", FOUND, "k"))),
                          on_event=lambda node, phase: events.append((node, phase)))
        self.assertEqual(report.state, "completed", report.message)
        self.assertEqual([c[:2] for c in rig.input.calls], [("tap", (30, 20))])
        self.assertEqual(events, [("s", "enter"), ("s", "leave"), ("c", "enter"), ("c", "leave"), ("k", "enter"),
                                  ("k", "leave")])

    def test_an_output_with_no_arrow_ends_the_run_finished(self):
        report, rig = run(script(CheckNode("c", parts=(WHITE_AT_RED,)), ClickNode("k", where=AT_MATCH),
                                 edges=(("s", NEXT, "c"), ("c", FOUND, "k"))))
        self.assertEqual((report.state, report.node), ("completed", None))
        self.assertEqual(rig.input.calls, [])

    def test_a_repeat_a_number_of_times_then_its_done_arrow(self):
        events = []
        report, rig = run(script(CheckNode("c", parts=(WHITE_AT_RED,)), RepeatNode("r", mode=TIMES, times=2),
                                 StopNode("x", success=False, message="Кнопки нет"),
                                 edges=(("s", NEXT, "c"), ("c", MISSING, "r"), ("r", AGAIN, "c"), ("r", DONE, "x"))),
                          on_event=lambda node, phase: events.append((node, phase)))
        self.assertEqual((report.state, report.node, report.reason), ("failed", "x", "stop_block_error"))
        self.assertEqual(report.params, {"message": "Кнопки нет"})
        self.assertTrue(report.stopped_by_block)
        self.assertEqual([n for n, phase in events if phase == "enter"].count("c"), 3)   # once, then twice again
        self.assertEqual(events[-1], ("x", "error"))

    def test_a_stop_that_finishes(self):
        report, _ = run(script(StopNode("x"), edges=(("s", NEXT, "x"),)))
        self.assertEqual((report.state, report.node, report.stopped_by_block), ("completed", "x", True))

    def test_until_stopped_by_hand_goes_on_until_the_emergency_stop(self):
        rig = Rig()
        passes = []

        def listen(node, phase):
            if node == "c" and phase == "enter":
                passes.append(1)
                if len(passes) == 4:
                    rig.stop.trigger("the person")
        report, _ = run(script(CheckNode("c", parts=(WHITE_AT_RED,)), RepeatNode("r"),
                               edges=(("s", NEXT, "c"), ("c", MISSING, "r"), ("r", AGAIN, "c"))), rig, on_event=listen)
        self.assertEqual((report.state, report.reason), ("cancelled", "emergency_stop"))
        self.assertEqual(len(passes), 4)

    def test_a_repeat_for_a_length_of_time_and_until_a_time_of_day(self):
        # the clock moves 3 s per pass (10 s: passes at 0, 3, 6, 9 go again, the one at 12 is done); it is 18:00:
        # "until 18:01" goes again at 18:00:00 and 18:00:30, and is done at 18:01:00
        for repeat, step, expected in ((RepeatNode("r", mode=FOR, seconds=10), 3.0, 5),
                                       (RepeatNode("r", mode=UNTIL, until="18:01"), 30.0, 3)):
            clock = Clock()
            passes = []

            def listen(node, phase, clock=clock, passes=passes, step=step):
                if node == "w" and phase == "enter":
                    passes.append(1)
                    if len(passes) > 1:
                        clock.tick(step)
                    if len(passes) > 20:
                        raise AssertionError("too many passes")
            made = script(WaitNode("w", seconds=0.05), repeat, StopNode("x"),
                          edges=(("s", NEXT, "w"), ("w", NEXT, "r"), ("r", AGAIN, "w"), ("r", DONE, "x")))
            report, _ = run(made, clock=clock, on_event=listen)
            with self.subTest(repeat=repeat.mode):
                self.assertEqual((report.state, report.node), ("completed", "x"), report.message)
                self.assertEqual(len(passes), expected)

    def test_a_click_at_a_point_follows_the_size_only_in_the_same_proportions(self):
        click = ClickNode("k", point=(15, 10), size=(100, 50))                     # the scene is 200x100
        report, rig = run(script(click, edges=(("s", NEXT, "k"),)))
        self.assertEqual(report.state, "completed", report.message)
        self.assertEqual([c[:2] for c in rig.input.calls], [("tap", (30, 20))])
        report, rig = run(script(ClickNode("k", point=(15, 10), size=(100, 40)), edges=(("s", NEXT, "k"),)))
        self.assertEqual((report.state, report.reason, report.node), ("failed", "size_proportions_changed", "k"))
        self.assertEqual(rig.input.calls, [])

    def test_the_kinds_of_click(self):
        for node, names in ((ClickNode("k", how=DOUBLE, point=(5, 5)), ["double_tap"]),
                            (ClickNode("k", how=RIGHT, point=(5, 5)), ["secondary_tap"]),
                            (ClickNode("k", how=HOLD, point=(5, 5), hold_s=1.2), ["button_down", "button_up"]),
                            (ClickNode("k", how=DRAG, point=(5, 5), to=(45, 25)),
                             ["button_down"] + ["move"] * 8 + ["button_up"])):
            report, rig = run(script(node, edges=(("s", NEXT, "k"),)))
            with self.subTest(how=node.how):
                self.assertEqual(report.state, "completed", report.message)
                self.assertEqual(rig.input.names(), names)
        report, rig = run(script(ClickNode("k", how=DRAG, point=(5, 5), to=(45, 25)), edges=(("s", NEXT, "k"),)))
        self.assertEqual(rig.input.calls[-1][:2], ("button_up", (45, 25)))

    def test_a_click_where_nothing_was_found_and_a_macro_that_is_gone_say_so(self):
        report, rig = run(script(ClickNode("k", where=AT_MATCH), edges=(("s", NEXT, "k"),)))
        self.assertEqual((report.state, report.reason, report.node), ("failed", "click_no_match", "k"))
        report, rig = run(script(MacroNode("p", macro="gone.macro.json"), edges=(("s", NEXT, "p"),)))
        self.assertEqual((report.reason, report.params), ("macro_missing", {"macro": "gone.macro.json"}))
        report, rig = run(script(MacroNode("p", macro="daily"), edges=(("s", NEXT, "p"),)),
                          macros={"daily": a_macro()})
        self.assertEqual((report.state, rig.input.names()), ("completed", ["tap"]))

    def test_a_macro_that_fails_with_its_own_reason_says_it_in_the_scripts_words(self):
        """A recorded macro whose playback names what went wrong (a Full Windows click whose window never came,
        app/windows/places.py): the script stops at that block with that reason and those names, not a general
        "the action did not go through"."""
        from app.playback.engine import PlaybackEngine

        class WindowNeverCame(Exception):
            reason_code = "place_missing"
            params = (("program", "notes.exe"), ("title", "Save as"))

        class Aiming(PlaybackEngine):
            def _aim(self, index, action):
                raise WindowNeverCame("The window never came; nothing sent.")
        rig = Rig()
        rig.create_playback = lambda macro, target, *, dry_run, allow_different_target: Aiming(
            macro, target, rig.input, rig.stop, dry_run=dry_run, allow_different_target=allow_different_target)
        report, _ = run(script(MacroNode("p", macro="daily"), edges=(("s", NEXT, "p"),)), rig,
                        macros={"daily": a_macro()})
        self.assertEqual((report.state, report.reason, report.node), ("failed", "place_missing", "p"))
        self.assertEqual(report.params, {"program": "notes.exe", "title": "Save as"})
        self.assertEqual(rig.input.calls, [])

    def test_a_mixed_block_with_all_of_its_parts(self):
        both = CheckNode("m", parts=(RED_AT, WHITE_AT_RED), mixed=True, mode=ALL)
        either = CheckNode("m", parts=(RED_AT, WHITE_AT_RED), mixed=True)
        for node, expected in ((both, MISSING), (either, FOUND)):
            made = script(node, StopNode("f"), StopNode("n", success=False),
                          edges=(("s", NEXT, "m"), ("m", FOUND, "f"), ("m", MISSING, "n")))
            report, _ = run(made)
            with self.subTest(mode=node.mode):
                self.assertEqual(report.node, "f" if expected == FOUND else "n")

    def test_a_dry_run_sends_nothing_waits_for_nothing_and_goes_round_a_loop_once(self):
        made = script(WaitNode("w", seconds=3600), CheckNode("c", parts=(WHITE_AT_RED,)), RepeatNode("r"),
                      ClickNode("k", point=(1, 1)),
                      edges=(("s", NEXT, "w"), ("w", NEXT, "c"), ("c", MISSING, "r"), ("r", AGAIN, "k"),
                             ("k", NEXT, "c")))
        import time
        started = time.monotonic()
        report, rig = run(made, dry_run=True)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(report.state, "completed", report.message)
        self.assertEqual(rig.input.calls, [])

    def test_a_click_where_it_was_found_never_clicks_what_an_earlier_check_found(self):
        """The third review (minor 2): after a "missing" answer the click still had the previous match."""
        made = script(CheckNode("c", parts=(RED_AT,)), CheckNode("d", parts=(WHITE_AT_RED,)),
                      ClickNode("k", where=AT_MATCH),
                      edges=(("s", NEXT, "c"), ("c", FOUND, "d"), ("d", MISSING, "k")))
        report, rig = run(made)
        self.assertEqual((report.state, report.reason, report.node), ("failed", "click_no_match", "k"))
        self.assertEqual(rig.input.calls, [])

    def test_a_question_that_can_never_be_asked_is_refused_by_its_code_before_anything(self):
        """The third review (minor 5): a text with no reader was refused under the class's name, in English."""
        made = script(CheckNode("c", parts=(text("OK"),)), ClickNode("k", point=(1, 1)),
                      edges=(("s", NEXT, "c"), ("c", FOUND, "k")))
        report, rig = run(made)                                       # the rig has no text reader
        self.assertEqual((report.state, report.reason), ("failed", "ocr_unavailable"))
        self.assertEqual(rig.input.calls, [])

    def test_the_blocks_reached_are_counted_once_each(self):
        """The third review (minor 6): a loop read "250 of 6"."""
        made = script(CheckNode("c", parts=(WHITE_AT_RED,)), RepeatNode("r", mode=TIMES, times=4),
                      edges=(("s", NEXT, "c"), ("c", MISSING, "r"), ("r", AGAIN, "c")))
        report, _ = run(made)
        self.assertEqual(report.visited, 3)
        self.assertGreater(report.blocks_run, 3)

    def test_a_refused_picture_search_keeps_its_code(self):
        from app.vision.roi import Roi
        picture = Bitmap(30, 20, bytes((i * 7) % 256 for i in range(1800)))
        big = Part(Condition.template(picture, region=Roi(0, 0, 10, 10)))
        report, _ = run(script(CheckNode("c", parts=(big,)), edges=(("s", NEXT, "c"),)))
        self.assertEqual((report.state, report.reason, report.node),
                         ("failed", "query_picture_larger_than_area", "c"))
        self.assertEqual(report.params, {"picture": "30×20", "area": "10×10"})


if __name__ == "__main__":
    unittest.main()
