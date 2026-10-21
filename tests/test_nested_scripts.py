"""Scripts whose branches hold blocks, a start delay, blocks placed by hand -- the person's requests of 2026-10-19.

  * a branch is a list of blocks (an IF among them, at most MAX_NESTING deep); a click on the match can only open
    a branch, on the side whose observation has a match, so it clicks right after the look;
  * a chosen branch runs its blocks in order, then the run continues after the IF; a WAIT that times out inside a
    branch ends the run there; the run counts the top-level steps it finished;
  * the start delay is bounded (10 minutes), ended at once by the emergency stop or a cancel, skipped by a dry run,
    and the caller's watch (`while_acting`) begins only when the steps do -- after a delay the target is checked
    again first;
  * files: version 3 holds all of it, strictly; a script version 2 can hold is still written as version 2, byte for
    byte; every script valid before still loads;
  * the editor's model addresses blocks by path, keeps places through every edit, and offers only what may go where;
  * in the library a version-3 script keeps its start delay and places when it is renamed or copied, is counted in
    steps, and a file nested far too deep is refused where the rule is broken -- it never hides the other macros.

Fakes only (the synthetic screen of tests.test_observe, the call-recording input of tests.test_playback).
"""
import dataclasses
import json
import threading
import time
import unittest
from contextlib import contextmanager

from app.decide import (ELSE, MAX_BLOCKS, MAX_NESTING, MAX_START_DELAY_S, MAX_STEPS, THEN, Condition, Decision,
                        FlowState, InvalidDecision, RunMacro, TapMatch, WaitUntil, count_blocks, walk)
from app.decide.format import (ConditionalMacro, MacroFormatError, conditional_from_json, conditional_to_json,
                               needs_version_3, summarize_macro_text)
from app.decide.position import MAX_POSITION
from app.gui import flow_edit as fe
from app.gui.macro_edit import EditError
from app.gui.view import fmt
from app.playback.engine import PlaybackRefused
from app.playback.preflight import Issue
from tests import test_gui as gui_tests
from tests.test_decide import Rig, a_macro
from tests.test_observe import RED

FOUND = Condition.pixel((30, 20), RED)            # true on the synthetic screen
MISSING = Condition.pixel((0, 0), RED)            # false on it


def wait(seconds=0.2, condition=FOUND, at=None):
    return WaitUntil(condition, seconds, 0.05, at)


# ---- the blocks ----------------------------------------------------------------------------------------------
class Blocks(unittest.TestCase):
    def test_a_branch_is_a_list_of_blocks_and_one_block_is_still_a_branch(self):
        macro = RunMacro(a_macro())
        self.assertEqual(Decision(FOUND, macro).then, (macro,))
        self.assertEqual(Decision(FOUND, [TapMatch(), macro]).then, (TapMatch(), macro))
        self.assertEqual(Decision(FOUND).then, ())
        self.assertEqual(Decision(FOUND, macro), Decision(FOUND, (macro,)))

    def test_a_click_on_the_match_opens_only_the_branch_with_a_match_and_only_first(self):
        Decision(FOUND, (TapMatch(), wait()))
        Decision(FOUND.negated(), otherwise=(TapMatch(),))
        for bad in (lambda: Decision(FOUND, (wait(), TapMatch())),        # after something else: refused
                    lambda: Decision(FOUND, otherwise=(TapMatch(),)),     # no match on that side
                    lambda: Decision(FOUND, ("click",)),
                    lambda: Decision(FOUND, (Decision(MISSING, otherwise=(TapMatch(),)),))):
            with self.assertRaises(InvalidDecision):
                bad()

    def test_ifs_nest_at_most_max_nesting_deep(self):
        deep = Decision(FOUND, (TapMatch(),))
        for _ in range(MAX_NESTING - 1):
            deep = Decision(FOUND, (deep,))
        with self.assertRaises(InvalidDecision):
            Decision(FOUND, (deep,))
        self.assertEqual(count_blocks((deep,)), MAX_NESTING + 1)

    def test_a_place_is_two_whole_numbers_on_the_scene_or_nothing(self):
        self.assertEqual(wait(at=[10, 20]).at, (10, 20))
        for bad in ((-1, 0), (1.5, 2), (1, 2, 3), "10,20", (True, 3), (0, 100_001)):
            with self.subTest(bad=bad), self.assertRaises(InvalidDecision):
                wait(at=bad)

    def test_walk_gives_every_block_in_run_order_with_its_path(self):
        script = (wait(), Decision(FOUND, (TapMatch(), Decision(MISSING, (wait(),))), (RunMacro(a_macro()),)))
        self.assertEqual([p for p, _ in walk(script)],
                         [(0,), (1,), (1, THEN, 0), (1, THEN, 1), (1, THEN, 1, THEN, 0), (1, ELSE, 0)])


# ---- running them ---------------------------------------------------------------------------------------------
class Running(unittest.TestCase):
    def test_a_branch_runs_its_blocks_in_order_then_the_step_after_the_if(self):
        r = Rig()
        first, second, after = a_macro(1, 1), a_macro(2, 2), a_macro(3, 3)
        report = r.run(Decision(FOUND, (TapMatch(), RunMacro(first), wait(), RunMacro(second))), RunMacro(after))
        self.assertIs(report.state, FlowState.COMPLETED)
        taps = [c[1] for c in r.input.calls if c[0] == "tap"]
        self.assertEqual(taps, [(30, 20), (1, 1), (2, 2), (3, 3)])                   # the click, the branch, after
        self.assertEqual([s.path for s in report.steps],
                         [(0,), (0, THEN, 1), (0, THEN, 2), (0, THEN, 3), (1,)])
        self.assertEqual(report.completed, 2)

    def test_an_if_inside_a_branch_decides_again(self):
        r = Rig()
        report = r.run(Decision(FOUND, (Decision(MISSING, (RunMacro(a_macro(1, 1)),), (RunMacro(a_macro(2, 2)),)),)))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([c[1] for c in r.input.calls if c[0] == "tap"], [(2, 2)])    # the inner IF's ELSE

    def test_a_wait_that_times_out_in_a_branch_ends_the_run_there(self):
        r = Rig()
        report = r.run(RunMacro(a_macro(9, 9)), Decision(FOUND, (wait(0.15, MISSING), RunMacro(a_macro(1, 1)))),
                       RunMacro(a_macro(2, 2)))
        self.assertIs(report.state, FlowState.TIMED_OUT)
        self.assertEqual([c[1] for c in r.input.calls if c[0] == "tap"], [(9, 9)])  # nothing after it ran
        self.assertEqual(report.completed, 1)                                        # the step before it finished

    def test_an_empty_if_only_looks(self):
        r = Rig()
        report = r.run(Decision(FOUND), RunMacro(a_macro()))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual(len(r.built), 1)

    def test_too_many_steps_or_blocks_are_refused_before_anything_is_observed(self):
        r = Rig()
        with self.assertRaises(InvalidDecision):
            r.run(*([RunMacro(a_macro())] * (MAX_STEPS + 1)))
        nested = Decision(FOUND, (TapMatch(),) + (RunMacro(a_macro()),) * 10, (RunMacro(a_macro()),) * 10)
        with self.assertRaises(InvalidDecision):
            r.run(*([nested] * (MAX_BLOCKS // 21 + 1)))
        self.assertEqual(r.screens.captures, 0)


class StartDelay(unittest.TestCase):
    def run_with(self, rig, *steps, delay, cancel=None, dry_run=False, while_acting=None):
        from app.decide import run_steps
        return run_steps(steps, target=rig.target, context=rig.context, create_playback=rig.create_playback,
                         stop=rig.stop, cancel=cancel, dry_run=dry_run, start_delay_s=delay,
                         while_acting=while_acting)

    def test_the_run_waits_before_its_first_step_and_the_watch_begins_with_the_steps(self):
        r, events = Rig(), []

        @contextmanager
        def watch():
            events.append(("watch on", time.monotonic(), len(r.input.calls)))
            yield
            events.append(("watch off", time.monotonic(), len(r.input.calls)))
        started = time.monotonic()
        report = self.run_with(r, RunMacro(a_macro()), delay=0.3, while_acting=watch)
        self.assertIs(report.state, FlowState.COMPLETED)
        (on, at_on, calls_on), (off, _at_off, calls_off) = events
        self.assertGreaterEqual(at_on - started, 0.3)                    # after the delay ...
        self.assertEqual((calls_on, calls_off), (0, 1))                  # ... and around everything that acted

    def test_the_stop_or_a_cancel_ends_the_delay_at_once_and_nothing_is_looked_at_or_sent(self):
        for how in ("stop", "cancel"):
            with self.subTest(how=how):
                r, cancel, watched = Rig(), threading.Event(), []
                threading.Timer(0.05, r.stop.trigger if how == "stop" else cancel.set).start()
                started = time.monotonic()
                report = self.run_with(r, Decision(FOUND, (TapMatch(),)), delay=3.0, cancel=cancel,
                                       while_acting=lambda: watched.append(1))
                self.assertLess(time.monotonic() - started, 1.5)                 # not the three seconds
                self.assertIs(report.state, FlowState.CANCELLED)
                self.assertEqual(report.reason, "emergency_stop" if how == "stop" else "cancelled")
                self.assertEqual((r.screens.captures, r.input.calls, watched), (0, [], []))

    def test_a_dry_run_does_not_wait(self):
        r = Rig()
        started = time.monotonic()
        report = self.run_with(r, RunMacro(a_macro()), delay=3.0, dry_run=True)
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_delay_out_of_bounds_is_refused_before_anything(self):
        r, cancelled = Rig(), threading.Event()
        cancelled.set()                       # were a too-long delay ever waited, it would end at once, not refuse
        for bad in (-1, MAX_START_DELAY_S + 1, "3", True, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(InvalidDecision):
                self.run_with(r, RunMacro(a_macro()), delay=bad, cancel=cancelled)
        self.assertEqual(r.screens.captures, 0)

    def test_a_start_delay_may_be_up_to_24_hours_and_no_longer(self):
        """The person's decision of 2026-10-20: up to 24 hours (it was 10 minutes, the bound of every WAIT UNTIL,
        which stays). Still a bound: a typo can never hold a target for days."""
        self.assertEqual(MAX_START_DELAY_S, 24 * 60 * 60)
        script = nested_script(start_delay_s=MAX_START_DELAY_S)
        self.assertEqual(conditional_from_json(conditional_to_json(script)[0]), script)
        self.assertEqual(fe.set_start_delay(fe.new_script("New"), str(MAX_START_DELAY_S)).start_delay_s,
                         MAX_START_DELAY_S)
        for bad in (MAX_START_DELAY_S + 1, float("inf")):
            with self.subTest(bad=bad), self.assertRaises(MacroFormatError):
                nested_script(start_delay_s=bad)

    def test_a_watch_that_cannot_start_ends_the_run_with_nothing_sent(self):
        r = Rig()

        @contextmanager
        def refused():
            raise PlaybackRefused((Issue("takeover_watch_unavailable", "no hooks"),))
            yield                                                         # pragma: no cover
        report = self.run_with(r, RunMacro(a_macro()), delay=0, while_acting=refused)
        self.assertIs(report.state, FlowState.FAILED)
        self.assertEqual(report.reason, "action_refused")
        self.assertEqual(r.input.calls, [])


class FlowJobStart(unittest.TestCase):
    """The GUI's job for one target: after a start delay the target is checked again, and only then watched."""

    def services(self, rig, events, *, recheck_fails=False):
        class Services:
            stop = rig.stop

            def create_playback(self, *args, **kwargs):
                return rig.create_playback(*args, **kwargs)

            def capture(self, target):
                return rig.screens.capture()

            def recheck(self, target):
                events.append(("recheck", time.monotonic()))
                if recheck_fails:
                    from app.core.errors import TargetNotVerified
                    raise TargetNotVerified("The window was closed.", reason_code="window_gone")

            @contextmanager
            def guard_run(self, target):
                events.append(("watch", time.monotonic()))
                yield
        return Services()

    def job(self, rig, events, *, delay=0.3, dry_run=False, recheck_fails=False):
        from app.gui.flows import FlowJob
        flow = ConditionalMacro("Later", (RunMacro(a_macro()),), start_delay_s=delay)
        return FlowJob(flow, rig.target, self.services(rig, events, recheck_fails=recheck_fails), ocr=None,
                       dry_run=dry_run, allow_different_target=False)

    def test_after_the_delay_the_target_is_checked_again_and_only_then_watched(self):
        rig, events = Rig(), []
        job = self.job(rig, events)
        started = time.monotonic()
        job.start_in_background()
        time.sleep(0.05)
        self.assertGreater(job.starts_in(), 0)
        self.assertEqual(events, [])                                      # nothing is watched while it waits
        self.assertTrue(job.wait(5))
        self.assertEqual([e[0] for e in events], ["recheck", "watch"])
        self.assertGreaterEqual(events[0][1] - started, 0.3)
        self.assertEqual((job.outcome()[0], job.starts_in()), ("completed", 0))

    def test_a_target_that_changed_during_the_delay_gets_nothing(self):
        rig, events = Rig(), []
        job = self.job(rig, events, delay=0.1, recheck_fails=True)
        job.start_in_background()
        self.assertTrue(job.wait(5))
        self.assertEqual(job.outcome()[0], "failed")
        self.assertEqual((rig.input.calls, [e[0] for e in events]), ([], ["recheck"]))

    def test_a_dry_run_does_not_wait_to_start(self):
        rig, events = Rig(), []
        job = self.job(rig, events, delay=MAX_START_DELAY_S, dry_run=True)
        self.assertEqual(job.delay_s, 0)
        job.start_in_background()
        self.assertTrue(job.wait(5))
        self.assertEqual(job.outcome()[0], "completed")


# ---- files ----------------------------------------------------------------------------------------------------
def nested_script(**changes):
    values = dict(name="Daily", steps=(wait(at=(40, 120)),
                                       Decision(FOUND, (TapMatch(at=(10, 300)), Decision(MISSING, (wait(),))),
                                                (RunMacro(a_macro()),), at=(40, 220))),
                  start_delay_s=180, start_at=(60, 10), end_at=None)
    values.update(changes)
    return ConditionalMacro(**values)


def deep_file(depth: int) -> str:
    """A version-3 file whose one step is `depth` IFs inside each other (written as text: the encoder would not)."""
    data = json.loads(conditional_to_json(nested_script())[0])
    condition = json.dumps(data["steps"][1]["condition"])
    data["steps"] = ["@"]
    head = '{"type": "if", "condition": %s, "at": null, "else": [], "then": [' % condition
    return json.dumps(data).replace('"@"', head * depth + "]}" * depth)


class Files(unittest.TestCase):
    def test_version_3_holds_branches_a_start_delay_and_places_and_reads_back_exactly(self):
        script = nested_script()
        text, _ = conditional_to_json(script)
        data = json.loads(text)
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["start"], {"delay_s": 180, "at": [60, 10]})
        self.assertEqual(data["steps"][1]["then"][0], {"type": "tap_match", "op": "tap", "offset": [0, 0],
                                                      "at": [10, 300]})
        again = conditional_from_json(text)
        self.assertEqual(again, script)
        self.assertEqual(conditional_to_json(again)[0], text)                          # byte for byte
        self.assertEqual(summarize_macro_text(text).version, 3)

    def test_a_script_version_2_can_hold_is_written_as_version_2(self):
        plain = ConditionalMacro("Plain", (wait(), Decision(FOUND, TapMatch(), RunMacro(a_macro()))))
        self.assertFalse(needs_version_3(plain))
        self.assertEqual(json.loads(conditional_to_json(plain)[0])["version"], 2)
        for changed in (dict(start_delay_s=5), dict(start_at=(1, 1)), dict(end_at=(1, 1)),
                        dict(steps=(wait(at=(0, 0)),)), dict(steps=(Decision(FOUND, (TapMatch(), wait())),)),
                        dict(steps=(Decision(FOUND, (wait(),)),))):
            with self.subTest(changed=changed):
                self.assertTrue(needs_version_3(nested_script(**{"start_delay_s": 0, "start_at": None,
                                                                 "steps": plain.steps, **changed})))

    def test_every_script_valid_before_still_loads(self):
        """100 steps, each an IF with a block in both branches: the most a version-2 file could hold."""
        big = ConditionalMacro("Big", (Decision(FOUND, TapMatch(), RunMacro(a_macro())),) * MAX_STEPS)
        self.assertEqual(conditional_from_json(conditional_to_json(big)[0]), big)

    def test_a_version_3_file_that_says_anything_else_is_refused(self):
        good = json.loads(conditional_to_json(nested_script())[0])

        def changed(fn):
            data = json.loads(json.dumps(good))
            fn(data)
            return json.dumps(data)

        def tap_second(d):
            d["steps"][1]["then"] = d["steps"][1]["then"][1:] + d["steps"][1]["then"][:1]
        cases = {
            "no at": lambda d: d["steps"][0].pop("at"),
            "negative at": lambda d: d["steps"][0].update(at=[-1, 0]),
            "at not a pair": lambda d: d["steps"][0].update(at=[1]),
            "tap not first": tap_second,
            "tap as a step": lambda d: d["steps"].append({"type": "tap_match", "op": "tap", "offset": [0, 0],
                                                          "at": None}),
            "branch not a list": lambda d: d["steps"][1].update(then={"type": "tap_match"}),
            "delay too long": lambda d: d["start"].update(delay_s=MAX_START_DELAY_S + 1),
            "delay negative": lambda d: d["start"].update(delay_s=-1),
            "no end": lambda d: d.pop("end"),
            "extra key": lambda d: d.update(extra=1),
            "unknown block": lambda d: d["steps"][1]["else"].append({"type": "loop", "at": None}),
        }
        for name, fn in cases.items():
            with self.subTest(name=name), self.assertRaises(MacroFormatError):
                conditional_from_json(changed(fn))

    def test_too_deep_nesting_in_a_file_is_refused(self):
        inner = {"type": "if", "condition": json.loads(conditional_to_json(nested_script())[0])["steps"][1]["condition"],
                 "then": [], "else": [], "at": None}
        for _ in range(MAX_NESTING):
            inner = dict(inner, then=[inner])
        data = json.loads(conditional_to_json(nested_script())[0])
        data["steps"] = [inner]
        with self.assertRaises(MacroFormatError):
            conditional_from_json(json.dumps(data))

    def test_a_file_nested_far_deeper_than_allowed_is_refused_not_a_crash(self):
        """Hundreds of IFs inside each other -- still a valid JSON text -- are refused at the first one too deep,
        before the reader goes any further: they never exhaust the interpreter's stack."""
        text = deep_file(600)
        for read in (conditional_from_json, summarize_macro_text):
            with self.subTest(read=read.__name__):
                with self.assertRaises(MacroFormatError) as caught:
                    read(text)
                self.assertLess(len(str(caught.exception)), 400)          # it stopped where the rule was broken


# ---- the editor's model ----------------------------------------------------------------------------------------
class EditorModel(unittest.TestCase):
    def doc(self):
        return ConditionalMacro("S", (wait(), Decision(FOUND), RunMacro(a_macro())))

    def test_blocks_go_in_anywhere_by_path_and_come_out_again(self):
        d = fe.insert_item(self.doc(), (1, THEN, 0), TapMatch())
        d = fe.insert_item(d, (1, THEN, 1), wait())
        d = fe.insert_item(d, (1, ELSE, 0), Decision(MISSING))
        d = fe.insert_item(d, (1, ELSE, 0, THEN, 0), RunMacro(a_macro(7, 7)))
        self.assertEqual(fe.item_at(d, (1, ELSE, 0, THEN, 0)), RunMacro(a_macro(7, 7)))
        with self.assertRaises(EditError):
            fe.move_item(d, (1, THEN, 1), -1)                       # the click would no longer be first
        d = fe.delete_item(d, (1, THEN, 1))
        self.assertEqual(fe.item_at(d, (1,)).then, (TapMatch(),))
        for gone in ((5,), (1, THEN, 3), (0, THEN, 0), (1, "maybe", 0), ()):
            with self.subTest(gone=gone), self.assertRaises(EditError):
                fe.item_at(d, gone)

    def test_places_travel_with_their_blocks_and_arrange_forgets_them(self):
        d = fe.place(self.doc(), {(0,): (10, 20), (2,): (300, 400), fe.START: (5, 5)})
        self.assertTrue(fe.placed(d))
        d = fe.insert_item(d, (0,), RunMacro(a_macro()))                 # everything after it moves one along
        self.assertEqual((fe.item_at(d, (1,)).at, fe.item_at(d, (3,)).at), ((10, 20), (300, 400)))
        copy = fe.duplicate_item(d, (1,))
        self.assertEqual(fe.item_at(copy, (2,)).at, (10 + fe.COPY_SHIFT, 20 + fe.COPY_SHIFT))   # beside it
        plain = fe.arrange(d)
        self.assertFalse(fe.placed(plain))
        self.assertEqual(plain.steps, tuple(s for s in fe.arrange(d).steps))

    def test_a_copy_of_an_if_is_drawn_beside_it_with_everything_its_branches_hold(self):
        d = fe.insert_item(self.doc(), (1, THEN, 0), TapMatch())
        d = fe.insert_item(d, (1, THEN, 1), wait())
        d = fe.place(d, {(1,): (100, 100), (1, THEN, 0): (50, 250), (1, THEN, 1): (50, 370)})
        copy = fe.duplicate_item(d, (1,))
        shift = fe.COPY_SHIFT
        self.assertEqual([fe.item_at(copy, p).at for p in ((2,), (2, THEN, 0), (2, THEN, 1))],
                         [(100 + shift, 100 + shift), (50 + shift, 250 + shift), (50 + shift, 370 + shift)])
        self.assertEqual(fe.item_at(copy, (1,)), fe.item_at(d, (1,)))           # the original stays where it was

    def test_a_place_is_kept_on_the_scene(self):
        """Dragged (or copied) past an edge, a block stops at it: a place is never one the file would refuse."""
        d = fe.place(self.doc(), {(0,): (MAX_POSITION + 500, -20), fe.START: (MAX_POSITION * 2, 5),
                                  fe.END: (3.6, MAX_POSITION + 0.4)})
        self.assertEqual((fe.item_at(d, (0,)).at, d.start_at, d.end_at),
                         ((MAX_POSITION, 0), (MAX_POSITION, 5), (4, MAX_POSITION)))
        corner = fe.place(self.doc(), {(0,): (MAX_POSITION, MAX_POSITION)})
        self.assertEqual(fe.item_at(fe.duplicate_item(corner, (0,)), (1,)).at, (MAX_POSITION, MAX_POSITION))
        self.assertEqual(fe.place(fe.new_script("New"), {fe.START: (MAX_POSITION + 1, 0)}).start_at, (MAX_POSITION, 0))
        self.assertEqual(fe.on_scene((10.4, -3)), (10, 0))
        for bad in ((1,), ("x", 2), (float("inf"), 0)):
            with self.subTest(bad=bad), self.assertRaises(EditError):
                fe.on_scene(bad)

    def test_an_if_moved_by_hand_keeps_its_branches(self):
        d = fe.insert_item(self.doc(), (1, THEN, 0), TapMatch())
        moved = fe.place(d, {(1,): (100, 100), (1, THEN, 0): (50, 250)})
        self.assertEqual((fe.item_at(moved, (1,)).at, fe.item_at(moved, (1, THEN, 0)).at), ((100, 100), (50, 250)))
        self.assertEqual(fe.item_at(moved, (1,)).condition, FOUND)

    def test_only_what_may_go_somewhere_is_offered_there(self):
        d = self.doc()
        self.assertTrue(fe.can_insert(d, (1, THEN, 0), fe.TAP))
        self.assertFalse(fe.can_insert(d, (1, ELSE, 0), fe.TAP))          # no match on that side
        self.assertFalse(fe.can_insert(d, (1,), fe.TAP))                  # not a branch
        tapped = fe.insert_item(d, (1, THEN, 0), TapMatch())
        self.assertFalse(fe.can_insert(tapped, (1, THEN, 0), fe.TAP))     # one is enough
        self.assertFalse(fe.can_insert(tapped, (1, THEN, 1), fe.TAP))     # and only first
        self.assertTrue(fe.can_insert(tapped, (1, THEN, 1), fe.WAIT))
        for kind in (fe.WAIT, fe.IF, fe.RUN):                             # nothing before the click: it stays first
            self.assertFalse(fe.can_insert(tapped, (1, THEN, 0), kind))
        self.assertFalse(fe.can_insert(tapped, (9,), fe.WAIT))            # no such place
        deep = d
        path = (1,)
        for _ in range(MAX_NESTING - 1):
            deep = fe.insert_item(deep, path + (THEN, 0), Decision(FOUND))
            path = path + (THEN, 0)
        self.assertFalse(fe.can_insert(deep, path + (THEN, 0), fe.IF))
        self.assertTrue(fe.can_insert(deep, path + (THEN, 0), fe.RUN))

    def test_the_start_delay_is_set_on_a_script_or_on_one_not_started_yet(self):
        self.assertEqual(fe.set_start_delay(self.doc(), "180").start_delay_s, 180)
        empty = fe.new_script("New")
        delayed = fe.set_start_delay(empty, "90")
        self.assertEqual(delayed.start_delay_s, 90)
        first = fe.insert_item(delayed, (0,), RunMacro(a_macro()))
        self.assertEqual((type(first).__name__, first.start_delay_s), ("ConditionalMacro", 90))
        for bad in ("-1", str(MAX_START_DELAY_S + 1), "soon"):
            with self.subTest(bad=bad), self.assertRaises(EditError):
                fe.set_start_delay(self.doc(), bad)

    def test_inserting_with_room_made_is_one_edit(self):
        session = fe.FlowSession(fe.place(self.doc(), {(0,): (100, 100), (1,): (100, 220), (2,): (100, 340)}))
        session.mark_saved()
        session.apply(fe.insert_placed, (1,), RunMacro(a_macro(), at=(100, 220)), {(1,): (100, 340), (2,): (100, 460)})
        steps = session.document.steps
        self.assertEqual([s.at for s in steps], [(100, 100), (100, 220), (100, 340), (100, 460)])
        session.undo()
        self.assertFalse(session.dirty)

    def test_a_new_script_takes_its_start_settings_with_undo(self):
        session = fe.FlowSession(fe.new_script("New"))
        session.apply(fe.set_start_delay, "30")
        session.apply(fe.place, {fe.START: (20, 20)})
        self.assertEqual((session.document.start_delay_s, session.document.start_at), (30, (20, 20)))
        session.undo()
        session.undo()
        self.assertEqual(session.document.start_delay_s, 0)
        session.rename("Renamed")
        self.assertEqual(session.document.name, "Renamed")


# ---- a version-3 script in the library ----------------------------------------------------------------------------
class Words:
    """A ui whose every word is its own key: enough to see which unit a size is counted in."""

    @staticmethod
    def t(key: str) -> str:
        return key + " {count}"


class InTheLibrary(gui_tests.Base):
    """What a version-3 script keeps when the library or the controller handles it as a whole file."""

    @staticmethod
    def script():
        return nested_script(end_at=(300, 900))

    def test_renaming_a_script_keeps_its_start_delay_and_places(self):
        path = self.library.save(self.script())
        renamed = self.library.rename(path, "Renamed")
        self.assertEqual(self.library.load(renamed), dataclasses.replace(self.script(), name="Renamed"))
        self.assertEqual(json.loads(renamed.read_text(encoding="utf-8"))["version"], 3)

    def test_a_copy_keeps_its_start_delay_and_places(self):
        key = self.library.key_of(self.library.save(self.script()))
        self.c.refresh_macros()
        self.assertTrue(self.c.duplicate_macro(key, "Copy"), self.c.status)
        copy = self.library.path_for("Copy")
        self.assertEqual(self.library.load(copy), dataclasses.replace(self.script(), name="Copy"))
        self.assertEqual(json.loads(copy.read_text(encoding="utf-8"))["version"], 3)

    def test_a_version_3_script_is_counted_in_steps(self):
        self.library.save(self.script())
        (info,) = self.library.list()
        self.assertEqual((info.version, info.actions), (3, 2))
        self.assertEqual(fmt.macro_size(Words, info), "count_steps 2")

    def test_a_file_nested_far_too_deep_is_listed_as_broken_and_hides_no_other(self):
        daily, deep = self.library.save(self.script()), self.library.path_for("Deep")
        deep.write_text(deep_file(600), encoding="utf-8")
        listed = {info.path: info.valid for info in self.library.list()}
        self.assertEqual(listed, {daily: True, deep: False})


if __name__ == "__main__":
    unittest.main()
