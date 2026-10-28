"""Condition and decision: observe -> is it true? -> which prepared branch? -> the EXISTING action path.

Deterministic synthetic images only (the fakes of tests/test_observe.py). Actions run through the real
`PlaybackEngine` and its real preflight; the LDPlayer cases go further and run through the real `InputSession`
over a real `MappingService`, with only the device link faked, so "which serial received the tap" is the
production answer and not a stand-in's.

The properties these tests hold down:
  * a condition answers true/false and keeps the observation behind it; "false" is never an exception;
  * WAIT UNTIL is bounded, polls fresh frames, and ends promptly on the emergency stop or the run's own cancel;
    timeout is a result, a failure to look is an error;
  * a decision only picks one of its prepared branches, and the chosen action reaches input ONLY through
    `create_playback` -> preflight -> the provider's session -> budget -> stop;
  * a match seen on one target can never become a click on another, and N targets share nothing;
  * the macro file format did not change: old files load, round-trip byte-for-byte, and run as branches.
"""
import ast
import threading
import time
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, TargetNotVerified
from app.decide import (ELSE, THEN, Cancelled, Condition, ConditionResult, Decision, FlowState, InvalidCondition,
                        InvalidDecision, RunMacro, TapMatch, WaitStatus, WaitUntil, act, decide, evaluate,
                        run_steps, tap_macro, wait_until)
from app.observe import (InvalidQuery, ObservationContext, ObservationUnavailable, OcrUnavailable, PixelQuery,
                         TextQuery)
from app.playback import format as macro_format
from app.playback.engine import PlaybackEngine, PlaybackRefused, PlaybackState
from app.playback.format import macro_from_json, macro_to_json
from app.playback.macro import ActionOp, Macro
from app.providers.recording import Recording
from app.providers.target import Capability as C, VerificationState
from app.vision.bitmap import Bitmap
from app.vision.roi import Roi
from tests.test_ldplayer_playback import rig
from tests.test_observe import (BLUE, RED, WHITE, FakeOcr, FakeScreenshots, block, mark, scene, three_marks)
from tests.test_playback import ALL, PROV, TID, FakeInput, FakeTarget, down, up

ROOT = Path(__file__).resolve().parents[1] / "app"
BLANK = Bitmap.solid(200, 100, WHITE)


def ctx(frames=None, *, target_id=TID, provider_id=PROV, ocr=None, stop=None, fail=None):
    provider = FakeScreenshots(target_id=target_id, provider_id=provider_id, frames=frames, fail=fail)
    return ObservationContext(provider, stop=stop, ocr=ocr), provider


class Rig:
    """One generic target: its context, a call-recording input, and the provider-style playback factory."""

    def __init__(self, frames=None, *, caps=ALL, target_id=TID, ocr=None, stop=None, fail=None, on_call=None):
        self.stop = stop or EmergencyStop()
        self.target = FakeTarget(caps=caps, target_id=target_id)
        self.context, self.screens = ctx(frames, target_id=target_id, ocr=ocr, stop=self.stop, fail=fail)
        self.input = FakeInput(target_id=target_id)
        self.input.on_call = on_call
        self.built: list[Macro] = []

    def create_playback(self, macro, target, *, dry_run, allow_different_target):
        """What a provider's `create_playback` does: bind the macro to the target's own input session."""
        self.built.append(macro)
        return PlaybackEngine(macro, target, self.input, self.stop, dry_run=dry_run,
                              allow_different_target=allow_different_target)

    def run(self, *steps, cancel=None, dry_run=False):
        return run_steps(steps, target=self.target, context=self.context, create_playback=self.create_playback,
                         stop=self.stop, cancel=cancel, dry_run=dry_run)


def a_macro(x=5, y=6, prov=PROV, tid=TID):
    return Macro(Recording(prov, tid, (down(0, x, y), up(10, x, y))), "prepared")


# ---- condition ------------------------------------------------------------------------------------
class Conditions(unittest.TestCase):
    def test_pixel_condition_true(self):
        c, _ = ctx()
        result = evaluate(Condition.pixel((30, 20), RED), c)
        self.assertIsInstance(result, ConditionResult)
        self.assertTrue(result.satisfied)
        self.assertTrue(result.found)
        self.assertEqual((result.center.x, result.center.y), (30, 20))

    def test_pixel_condition_false_is_an_answer_that_keeps_the_measurement(self):
        c, _ = ctx()
        result = evaluate(Condition.pixel((0, 0), RED), c)
        self.assertFalse(result.satisfied)
        self.assertEqual(result.diagnostics()["detail"]["actual"], WHITE)
        self.assertIsNone(result.center)

    def test_color_region_condition(self):
        c, _ = ctx()
        self.assertTrue(evaluate(Condition.color(RED, region=Roi(30, 20, 10, 10), min_ratio=0.9), c).satisfied)
        self.assertFalse(evaluate(Condition.color(RED, region=Roi(0, 0, 10, 10)), c).satisfied)

    def test_template_condition_reports_where(self):
        c, _ = ctx([three_marks()])
        result = evaluate(Condition.template(mark(), min_confidence=0.99, region=Roi(0, 0, 60, 30)), c)
        self.assertTrue(result.satisfied)
        self.assertEqual((result.observation.bounds.x, result.observation.bounds.y), (10, 10))
        self.assertEqual((result.center.x, result.center.y), (14, 14))

    def test_text_condition(self):
        c, _ = ctx(ocr=FakeOcr([block("Collect reward", 40, 60, 80, 12)]))
        yes = evaluate(Condition.text("collect"), c)
        no = evaluate(Condition.text("Upgrade"), c)
        self.assertTrue(yes.satisfied)
        self.assertEqual((yes.center.x, yes.center.y), (80, 66))
        self.assertFalse(no.satisfied)

    def test_expected_false_is_satisfied_when_absent_and_still_reports_what_is_there(self):
        c, _ = ctx()
        gone = evaluate(Condition.pixel((0, 0), RED, expected=False), c)
        still_there = evaluate(Condition.pixel((30, 20), RED, expected=False), c)
        self.assertTrue(gone.satisfied)
        self.assertFalse(gone.found)
        self.assertFalse(still_there.satisfied)
        self.assertTrue(still_there.found, "a negative condition must not lie about what it saw")
        self.assertEqual(Condition.pixel((1, 1), RED).negated(), Condition.pixel((1, 1), RED, expected=False))

    def test_diagnostics_are_plain_data(self):
        c, _ = ctx()
        d = evaluate(Condition.pixel((30, 20), RED, label="red square"), c).diagnostics()
        self.assertEqual(d["condition"], "red square")
        self.assertEqual(d["target"], TID)
        for value in d.values():
            self.assertFalse(callable(value))

    def test_one_screenshot_per_evaluation_unless_reuse_is_asked_for(self):
        c, screens = ctx()
        evaluate(Condition.pixel((1, 1), WHITE), c)
        evaluate(Condition.pixel((1, 1), WHITE), c)
        self.assertEqual(screens.captures, 2)
        evaluate(Condition.pixel((1, 1), WHITE), c, max_age_s=60)
        self.assertEqual(screens.captures, 2)


class InvalidConditions(unittest.TestCase):
    def test_a_condition_needs_an_observation_query(self):
        for bad in ("find the button", None, 42, lambda: True, Roi(0, 0, 1, 1)):
            with self.assertRaises(InvalidCondition):
                Condition(bad)

    def test_malformed_queries_fail_where_they_are_written(self):
        with self.assertRaises(InvalidQuery):
            Condition.pixel((1, 1), (300, 0, 0))
        with self.assertRaises(InvalidQuery):
            Condition.text("x", match="regex")
        with self.assertRaises(InvalidQuery):
            Condition.template("button.png")

    def test_bad_label(self):
        with self.assertRaises(InvalidCondition):
            Condition(PixelQuery((1, 1), RED), "x" * 101)
        with self.assertRaises(InvalidCondition):
            Condition(PixelQuery((1, 1), RED), "line\nbreak")

    def test_a_query_that_does_not_fit_the_frame_is_an_error_not_a_false(self):
        c, _ = ctx()
        with self.assertRaises(InvalidQuery):
            evaluate(Condition.pixel((5000, 5), RED), c)

    def test_a_result_cannot_be_paired_with_another_expectation(self):
        c, _ = ctx()
        observation = c.observe(PixelQuery((1, 1), RED))
        with self.assertRaises(InvalidCondition):
            ConditionResult(Condition.pixel((1, 1), RED, expected=False), observation)


class ProviderFailure(unittest.TestCase):
    def test_a_capture_failure_is_an_error_never_a_false(self):
        c, _ = ctx(fail=TargetNotVerified("gone", reason_code="gone"))
        with self.assertRaises(ObservationUnavailable):
            evaluate(Condition.pixel((1, 1), RED), c)

    def test_no_text_engine_is_its_own_error(self):
        c, _ = ctx()
        with self.assertRaises(OcrUnavailable):
            evaluate(Condition.text("Collect"), c)
        c, _ = ctx(ocr=FakeOcr(fail=RuntimeError("engine crashed")))
        with self.assertRaises(OcrUnavailable):
            evaluate(Condition.text("Collect"), c)

    def test_a_run_reports_a_capture_failure_as_failed_and_sends_nothing(self):
        r = Rig(fail=RuntimeError("device vanished"))
        report = r.run(Decision(Condition.pixel((1, 1), RED), then=TapMatch()))
        self.assertIs(report.state, FlowState.FAILED)
        self.assertEqual(report.reason, "observation_unavailable")
        self.assertEqual(r.input.calls, [])

    def test_a_providers_russian_words_travel_with_a_failed_run(self):
        """2026-10-27: the provider's own words in other languages reach the report (`params`), for the screens."""
        gone = TargetNotVerified("The device is gone.", reason_code="device_gone").saying(ru="Устройства больше нет.")
        report = Rig(fail=gone).run(Decision(Condition.pixel((1, 1), RED), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "observation_unavailable"))
        self.assertEqual(report.params, {"message_ru": "Устройства больше нет."})

    def test_a_wait_does_not_turn_a_capture_failure_into_a_timeout(self):
        r = Rig(fail=RuntimeError("device vanished"))
        report = r.run(WaitUntil(Condition.pixel((1, 1), RED), timeout_s=5, interval_s=0.05))
        self.assertIs(report.state, FlowState.FAILED)
        self.assertEqual(report.reason, "observation_unavailable")

    def test_a_stop_while_text_is_being_read_ends_the_run_as_cancelled_not_failed(self):
        r = Rig(ocr=FakeOcr(fail=EmergencyStopped("stop while reading")))
        report = r.run(WaitUntil(Condition.text("Collect"), timeout_s=5, interval_s=0.05))
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))

    def test_a_run_reports_a_missing_text_engine(self):
        r = Rig()
        report = r.run(Decision(Condition.text("Collect"), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_unavailable"))
        self.assertEqual(r.input.calls, [])


# ---- WAIT UNTIL -------------------------------------------------------------------------------------
class WaitUntilTests(unittest.TestCase):
    def test_already_true_costs_one_screenshot_and_no_delay(self):
        c, screens = ctx()
        started = time.monotonic()
        result = wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5), c)
        self.assertIs(result.status, WaitStatus.SATISFIED)
        self.assertEqual((result.attempts, screens.captures), (1, 1))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_polls_fresh_frames_until_the_screen_changes(self):
        c, screens = ctx([BLANK, BLANK, scene()])
        result = wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5, interval_s=0.05), c)
        self.assertTrue(result.satisfied)
        self.assertEqual((result.attempts, screens.captures), (3, 3))

    def test_not_found_while_polling_is_normal_and_timeout_is_a_result(self):
        c, _ = ctx([BLANK])
        started = time.monotonic()
        result = wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=0.3, interval_s=0.05), c)
        elapsed = time.monotonic() - started
        self.assertIs(result.status, WaitStatus.TIMED_OUT)
        self.assertGreater(result.attempts, 1)
        self.assertEqual(result.last.diagnostics()["detail"]["actual"], WHITE)
        self.assertLess(elapsed, 2.0, "the wait overran its own bound")

    def test_waiting_for_something_to_disappear(self):
        c, _ = ctx([scene(), scene(), BLANK])
        result = wait_until(WaitUntil(Condition.pixel((30, 20), RED, expected=False), timeout_s=5,
                                      interval_s=0.05), c)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.attempts, 3)

    def test_a_wait_without_a_bound_cannot_be_written(self):
        cond = Condition.pixel((1, 1), RED)
        for bad in (0, -1, None, "forever", 10_000, True, float("inf")):
            with self.assertRaises(InvalidCondition):
                WaitUntil(cond, timeout_s=bad)
        for bad in (0, -0.5, None, True):
            with self.assertRaises(InvalidCondition):
                WaitUntil(cond, timeout_s=1, interval_s=bad)
        with self.assertRaises(TypeError):
            WaitUntil(cond)                                   # the timeout is not optional
        self.assertEqual(WaitUntil(cond, timeout_s=1, interval_s=0.001).effective_interval_s, 0.05)

    def test_the_emergency_stop_ends_a_wait_at_once(self):
        stop = EmergencyStop()
        c, _ = ctx([BLANK], stop=stop)
        threading.Timer(0.15, lambda: stop.trigger("test")).start()
        started = time.monotonic()
        with self.assertRaises(EmergencyStopped):
            wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=60, interval_s=5), c, stop=stop)
        self.assertLess(time.monotonic() - started, 2.0, "the stop waited for the interval")

    def test_cancel_ends_a_wait_at_once_and_is_not_the_emergency_stop(self):
        stop, cancel = EmergencyStop(), threading.Event()
        c, _ = ctx([BLANK], stop=stop)
        threading.Timer(0.15, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=60, interval_s=5), c, stop=stop,
                       cancel=cancel)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse(stop.is_set(), "cancelling one run must not trigger the platform's stop")

    def test_a_stop_already_set_takes_no_screenshot(self):
        stop = EmergencyStop()
        stop.trigger("test")
        c, screens = ctx(stop=stop)
        with self.assertRaises(EmergencyStopped):
            wait_until(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5), c, stop=stop)
        self.assertEqual(screens.captures, 0)

    def test_run_reports_timeout_and_skips_the_steps_written_after_it(self):
        r = Rig([BLANK])
        report = r.run(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=0.2, interval_s=0.05),
                       Decision(Condition.pixel((0, 0), WHITE), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.TIMED_OUT, "timeout"))
        self.assertEqual(len(report.steps), 1)
        self.assertEqual(r.input.calls, [])

    def test_run_waits_then_acts(self):
        r = Rig([BLANK, scene()])
        report = r.run(WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5, interval_s=0.05),
                       Decision(Condition.color(RED, region=Roi(25, 15, 20, 20), min_ratio=0.2), then=TapMatch()))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual(r.input.names(), ["tap"])


# ---- IF / THEN / ELSE -------------------------------------------------------------------------------
class Decisions(unittest.TestCase):
    def test_then_when_true_else_when_false(self):
        c, _ = ctx()
        d = Decision(Condition.pixel((30, 20), RED), then=RunMacro(a_macro(1, 1)), otherwise=RunMacro(a_macro(2, 2)))
        taken = decide(d, evaluate(d.condition, c))
        self.assertEqual(taken.branch, THEN)
        self.assertEqual(taken.macro.actions[0].x, 1)
        d2 = Decision(Condition.pixel((0, 0), RED), then=RunMacro(a_macro(1, 1)), otherwise=RunMacro(a_macro(2, 2)))
        taken = decide(d2, evaluate(d2.condition, c))
        self.assertEqual(taken.branch, ELSE)
        self.assertEqual(taken.macro.actions[0].x, 2)

    def test_a_missing_branch_decides_to_do_nothing(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((0, 0), RED), then=TapMatch()))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual(report.steps[0].branch, ELSE)
        self.assertIsNone(report.steps[0].action)
        self.assertEqual(r.input.calls, [])
        self.assertEqual(r.built, [], "no engine may even be built when nothing is to be done")

    def test_decide_is_pure(self):
        c, screens = ctx()
        d = Decision(Condition.pixel((30, 20), RED), then=TapMatch())
        result = evaluate(d.condition, c)
        captures = screens.captures
        self.assertEqual(decide(d, result), decide(d, result))
        self.assertEqual(screens.captures, captures)

    def test_tap_match_becomes_one_action_at_the_match_center_on_the_observed_target(self):
        c, _ = ctx([three_marks()])
        d = Decision(Condition.template(mark(), min_confidence=0.99, region=Roi(50, 0, 40, 30)),
                     then=TapMatch(offset=(2, -1)))
        taken = decide(d, evaluate(d.condition, c))
        (action,) = taken.macro.actions
        self.assertEqual((action.op, action.x, action.y), (ActionOp.TAP, 66, 13))
        self.assertEqual((taken.macro.target.provider_id, taken.macro.target.target_id), (PROV, TID))

    def test_double_and_secondary_taps(self):
        c, _ = ctx()
        obs = evaluate(Condition.pixel((30, 20), RED), c).observation
        for op in (ActionOp.TAP, ActionOp.DOUBLE_TAP, ActionOp.SECONDARY_TAP):
            self.assertEqual([a.op for a in tap_macro(obs, TapMatch(op)).actions], [op])

    def test_else_of_a_negative_condition_clicks_what_is_still_there(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED, expected=False), otherwise=TapMatch()))
        self.assertEqual(report.steps[0].branch, ELSE)
        self.assertEqual(r.input.calls[0][:2], ("tap", (30, 20)))

    def test_invalid_decisions_are_refused_where_they_are_written(self):
        cond = Condition.pixel((1, 1), RED)
        self.assertEqual((Decision(cond).then, Decision(cond).otherwise), ((), ()))   # empty: looks, does nothing
        with self.assertRaises(InvalidDecision):
            Decision(cond.negated(), then=TapMatch())               # nothing to click when absent
        with self.assertRaises(InvalidDecision):
            Decision(cond, otherwise=TapMatch())
        with self.assertRaises(InvalidDecision):
            Decision(cond, then="tap it")
        with self.assertRaises(InvalidDecision):
            Decision("red", then=TapMatch())
        with self.assertRaises(InvalidDecision):
            TapMatch(ActionOp.TYPE_TEXT)
        with self.assertRaises(InvalidDecision):
            TapMatch(offset=(1.5, 0))
        with self.assertRaises(InvalidDecision):
            RunMacro("macro.json")

    def test_a_result_of_another_condition_is_refused(self):
        c, _ = ctx()
        d = Decision(Condition.pixel((30, 20), RED), then=TapMatch())
        with self.assertRaises(InvalidDecision):
            decide(d, evaluate(Condition.pixel((31, 20), RED), c))

    def test_malformed_step_lists_are_refused_before_anything_is_observed(self):
        r = Rig()
        for steps in ((), ("tap",), (Decision(Condition.pixel((1, 1), RED), then=TapMatch()),) * 101):
            with self.assertRaises(InvalidDecision):
                r.run(*steps)
        self.assertEqual(r.screens.captures, 0)


# ---- the action path --------------------------------------------------------------------------------
class ActionRouting(unittest.TestCase):
    def test_the_chosen_action_goes_through_the_providers_playback_factory(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual(len(r.built), 1, "exactly one macro went through create_playback")
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (30, 20))])
        step = report.steps[0]
        self.assertIs(step.action.state, PlaybackState.COMPLETED)
        self.assertEqual(step.diagnostics()["action"]["steps"], ["tap (30, 20)"])

    def test_a_prepared_macro_runs_unchanged(self):
        r = Rig()
        prepared = a_macro(7, 8)
        r.run(Decision(Condition.pixel((30, 20), RED), then=RunMacro(prepared)))
        self.assertIs(r.built[0], prepared)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (7, 8))])

    def test_dry_run_is_passed_through_and_sends_nothing(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()), dry_run=True)
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertTrue(report.steps[0].action.dry_run)
        self.assertEqual(r.input.calls, [])


class ActionSafety(unittest.TestCase):
    def test_preflight_refuses_an_operation_the_target_does_not_declare_and_nothing_is_sent(self):
        r = Rig(caps=(C.TAP,))
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch(ActionOp.DOUBLE_TAP)))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "action_refused"))
        self.assertIn("unsupported_capability", [i.code for i in report.issues])
        self.assertEqual(r.input.calls, [])

    def test_a_prepared_macro_for_another_target_is_refused_by_preflight(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=RunMacro(a_macro(tid="someone-else"))))
        self.assertEqual(report.reason, "action_refused")
        self.assertIn("target_mismatch", [i.code for i in report.issues])
        self.assertEqual(r.input.calls, [])

    def test_a_match_seen_on_one_target_can_never_become_a_click_on_another(self):
        seen_on_a, _ = ctx(target_id="A")
        choice = decide(Decision(Condition.pixel((30, 20), RED), then=TapMatch()),
                        evaluate(Condition.pixel((30, 20), RED), seen_on_a))
        b = Rig(target_id="B")
        with self.assertRaises(PlaybackRefused) as refused:
            act(choice.macro, b.target, b.create_playback, stop=b.stop)
        self.assertIn("target_mismatch", [i.code for i in refused.exception.issues])
        self.assertEqual(b.input.calls, [])

    def test_a_run_refuses_to_observe_one_target_and_act_on_another(self):
        a = Rig(target_id="A")
        b = Rig(target_id="B")
        with self.assertRaises(SafetyViolation):
            run_steps([Decision(Condition.pixel((30, 20), RED), then=TapMatch())], target=b.target,
                      context=a.context, create_playback=b.create_playback, stop=b.stop)
        self.assertEqual(a.screens.captures + b.screens.captures, 0)

    def test_an_unverified_target_is_refused_before_anything(self):
        r = Rig()
        r.target = FakeTarget(state=VerificationState.UNVERIFIED)
        with self.assertRaises(TargetNotVerified):
            r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertEqual(r.screens.captures, 0)

    def test_negative_coordinates_from_an_offset_are_refused_by_preflight(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch(offset=(-100, 0))))
        self.assertIn("invalid_coordinates", [i.code for i in report.issues])
        self.assertEqual(r.input.calls, [])

    def test_a_failing_action_ends_the_run_as_failed(self):
        r = Rig()
        r.input.raises[1] = SafetyViolation("the window went away")
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()),
                       Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "action_failed"))
        self.assertEqual(len(report.steps), 1, "no step may run after a failed action")

    def test_the_emergency_stop_before_a_run_sends_nothing(self):
        r = Rig()
        r.stop.trigger("test")
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))
        self.assertEqual((r.input.calls, r.screens.captures), ([], 0))

    def test_the_emergency_stop_during_a_prepared_macro_stops_its_remaining_actions(self):
        stop = EmergencyStop()
        r = Rig(stop=stop, on_call=lambda inp, name: stop.trigger("pressed"))
        three = Macro(Recording(PROV, TID, (down(0, 1, 1), up(5, 1, 1), down(10, 2, 2), up(5, 2, 2),
                                            down(10, 3, 3), up(5, 3, 3))))
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=RunMacro(three)))
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))
        self.assertEqual(len(r.input.calls), 1)

    def test_the_emergency_stop_between_observation_and_action_sends_nothing(self):
        stop = EmergencyStop()
        r = Rig(stop=stop)
        original = r.context.observe

        def observe_then_stop(*args, **kwargs):
            result = original(*args, **kwargs)
            stop.trigger("pressed right after looking")
            return result

        r.context.observe = observe_then_stop
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertEqual(report.reason, "emergency_stop")
        self.assertEqual(r.input.calls, [])

    def test_cancel_stops_a_running_action_without_touching_the_emergency_stop(self):
        cancel = threading.Event()
        r = Rig(on_call=lambda inp, name: cancel.set())
        slow = Macro(Recording(PROV, TID, (down(0, 1, 1), up(5, 1, 1), down(300, 2, 2), up(5, 2, 2))))
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=RunMacro(slow)), cancel=cancel)
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "cancelled"))
        self.assertEqual(len(r.input.calls), 1)
        self.assertFalse(r.stop.is_set())

    def test_each_action_gets_a_fresh_engine_so_the_budget_is_exactly_its_own(self):
        r = Rig()
        steps = [Decision(Condition.pixel((30, 20), RED), then=TapMatch())] * 3
        report = r.run(*steps)
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([len(m.actions) for m in r.built], [1, 1, 1])
        self.assertEqual(len(r.input.calls), 3)


# ---- the real LDPlayer input path, device link faked -------------------------------------------------
class LDPlayerInputPath(unittest.TestCase):
    def ld_rig(self):
        _, svc, adb, stop, t1, max_age = rig()
        t2 = svc.verify("ld2")

        def create_playback(macro, target, *, dry_run, allow_different_target):
            from app.ldplayer.playback import create_ldplayer_playback
            return create_ldplayer_playback(macro, target, adb, svc, stop, dry_run=dry_run,
                                            allow_different_target=allow_different_target, max_age_s=max_age)
        return svc, adb, stop, (t1, t2), create_playback

    def test_a_decided_tap_reaches_the_verified_serial_through_the_real_input_session(self):
        _, adb, stop, (t1, _), create_playback = self.ld_rig()
        c, _ = ctx(provider_id=t1.provider_id, target_id=t1.target_id, stop=stop)
        report = run_steps([Decision(Condition.pixel((30, 20), RED), then=TapMatch())], target=t1, context=c,
                           create_playback=create_playback, stop=stop)
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([t[:3] for t in adb.taps], [("emulator-5554", 30, 20)])

    def test_parallel_targets_each_click_their_own_match_on_their_own_serial(self):
        _, adb, stop, targets, create_playback = self.ld_rig()
        spots = {targets[0].target_id: (30, 20), targets[1].target_id: (150, 70)}
        screens = {targets[0].target_id: Bitmap.solid(200, 100, WHITE).with_rect(30, 20, 4, 4, RED),
                   targets[1].target_id: Bitmap.solid(200, 100, WHITE).with_rect(150, 70, 4, 4, RED)}
        reports, errors = {}, []
        barrier = threading.Barrier(2)

        def work(target):
            try:
                c, _ = ctx([screens[target.target_id]], provider_id=target.provider_id,
                           target_id=target.target_id, stop=stop)
                barrier.wait(5)
                reports[target.target_id] = run_steps(
                    [WaitUntil(Condition.color(RED, min_ratio=0.0001), timeout_s=5, interval_s=0.05),
                     Decision(Condition.color(RED, min_ratio=0.0001), then=TapMatch())],
                    target=target, context=c, create_playback=create_playback, stop=stop)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(t,)) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(all(r.state is FlowState.COMPLETED for r in reports.values()))
        by_serial = {serial: (x, y) for serial, x, y, _ in adb.taps}
        self.assertEqual(len(adb.taps), 2)
        self.assertEqual(by_serial["emulator-5554"], (spots[targets[0].target_id][0] + 2,
                                                      spots[targets[0].target_id][1] + 2))
        self.assertEqual(by_serial["emulator-5556"], (spots[targets[1].target_id][0] + 2,
                                                      spots[targets[1].target_id][1] + 2))


# ---- parallel targets, no shared state ---------------------------------------------------------------
class ParallelTargets(unittest.TestCase):
    def test_many_targets_decide_and_act_in_parallel_without_cross_talk(self):
        count = 8
        stop = EmergencyStop()
        rigs = []
        for i in range(count):
            bm = Bitmap.solid(120, 60, WHITE).with_rect(10 + i * 10, 5 + i * 5, 4, 4, RED)
            rigs.append(Rig([bm], target_id=f"t{i}", stop=stop))
        reports, errors = {}, []
        barrier = threading.Barrier(count)

        def work(i):
            try:
                barrier.wait(5)
                reports[i] = rigs[i].run(Decision(Condition.color(RED, min_ratio=0.0001), then=TapMatch()))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        for i, r in enumerate(rigs):
            self.assertIs(reports[i].state, FlowState.COMPLETED)
            self.assertEqual(reports[i].target_id, f"t{i}")
            self.assertEqual([c[:2] for c in r.input.calls], [("tap", (12 + i * 10, 7 + i * 5))],
                             f"target {i} clicked something that was not on its own screen")

    def test_cancelling_one_target_leaves_the_other_running(self):
        stop = EmergencyStop()
        a, b = Rig([BLANK], target_id="A", stop=stop), Rig([BLANK, BLANK, scene()], target_id="B", stop=stop)
        cancel_a = threading.Event()
        out = {}
        wait = WaitUntil(Condition.pixel((30, 20), RED), timeout_s=5, interval_s=0.05)
        ta = threading.Thread(target=lambda: out.setdefault("A", a.run(wait, cancel=cancel_a)))
        tb = threading.Thread(target=lambda: out.setdefault("B", b.run(wait, Decision(wait.condition,
                                                                                     then=TapMatch()))))
        ta.start()
        tb.start()
        cancel_a.set()
        ta.join(10)
        tb.join(10)
        self.assertEqual((out["A"].state, out["A"].reason), (FlowState.CANCELLED, "cancelled"))
        self.assertIs(out["B"].state, FlowState.COMPLETED)
        self.assertEqual(b.input.names(), ["tap"])
        self.assertEqual(a.input.calls, [])

    def test_the_emergency_stop_reaches_every_target(self):
        stop = EmergencyStop()
        rigs = [Rig([BLANK], target_id=f"t{i}", stop=stop) for i in range(3)]
        out = {}
        wait = WaitUntil(Condition.pixel((30, 20), RED), timeout_s=30, interval_s=0.05)
        threads = [threading.Thread(target=lambda i=i: out.setdefault(i, rigs[i].run(wait))) for i in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.15)
        stop.trigger("test")
        for t in threads:
            t.join(10)
        self.assertEqual({r.reason for r in out.values()}, {"emergency_stop"})

    def test_the_package_holds_no_module_level_mutable_state(self):
        for path in sorted((ROOT / "decide").glob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith((" ", "\t", "#", "@", "class ", "def ")) or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name, value = name.strip(), value.strip()
                if not name.isidentifier() or name.startswith("__"):
                    continue
                self.assertFalse(value.startswith(("[", "{", "dict(", "list(", "set(")),
                                 f"{path.name}: module-level mutable {name}")


# ---- the macro format did not change -----------------------------------------------------------------
V1_MACRO = """{
  "name": "old macro",
  "recording": {
    "end_reason": "stopped",
    "events": [
      {
        "button": "primary",
        "click_count": 1,
        "delay_ms": 0,
        "kind": "button_down",
        "x": 11,
        "y": 12
      },
      {
        "button": "primary",
        "click_count": 1,
        "delay_ms": 40,
        "kind": "button_up",
        "x": 11,
        "y": 12
      }
    ],
    "schema": "automation-recording",
    "skipped": {},
    "target": {
      "provider_id": "fake",
      "target_id": "t-1"
    },
    "version": 1
  },
  "schema": "automation-macro",
  "version": 1
}
"""


class MacroFormatUnchanged(unittest.TestCase):
    def test_the_schema_and_version_are_still_the_originals(self):
        self.assertEqual((macro_format.SCHEMA, macro_format.VERSION), ("automation-macro", 1))

    def test_an_old_macro_still_loads_and_round_trips_byte_for_byte(self):
        m = macro_from_json(V1_MACRO)
        self.assertEqual(macro_to_json(m), V1_MACRO)
        self.assertEqual([(a.op, a.x, a.y) for a in m.actions], [(ActionOp.TAP, 11, 12)])

    def test_an_old_macro_runs_as_a_prepared_branch(self):
        r = Rig()
        report = r.run(Decision(Condition.pixel((30, 20), RED), then=RunMacro(macro_from_json(V1_MACRO))))
        self.assertIs(report.state, FlowState.COMPLETED)
        self.assertEqual([c[:2] for c in r.input.calls], [("tap", (11, 12))])

    def test_a_decided_tap_is_an_ordinary_macro_that_round_trips_through_the_existing_format(self):
        c, _ = ctx()
        m = tap_macro(evaluate(Condition.pixel((30, 20), RED), c).observation, TapMatch())
        text = macro_to_json(m)
        self.assertEqual(macro_to_json(macro_from_json(text)), text)
        self.assertEqual(macro_from_json(text).actions, m.actions)

    def test_the_users_own_macros_still_round_trip_byte_for_byte(self):
        folder = ROOT.parent / "data" / "macros"
        files = sorted(folder.rglob("*.macro.json")) if folder.is_dir() else []
        if not files:
            self.skipTest("no local macros")
        import json
        for path in files:
            with self.subTest(path.name):
                raw = path.read_bytes().decode("utf-8")
                if json.loads(raw).get("version") != 1:
                    continue        # a script (format 2), saved by the application: tests.test_macro_v2 covers those
                self.assertEqual(macro_to_json(macro_from_json(raw)), raw)

    def test_the_format_rejects_a_condition_key_rather_than_ignoring_it(self):
        """No new step type was added: a file claiming one is not silently accepted as a plain macro."""
        import json
        data = json.loads(V1_MACRO)
        data["condition"] = {"kind": "pixel"}
        with self.assertRaises(macro_format.MacroFormatError):
            macro_from_json(json.dumps(data))


# ---- architecture ------------------------------------------------------------------------------------
def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


class Architecture(unittest.TestCase):
    FILES = sorted((ROOT / "decide").glob("*.py"))

    def test_decide_depends_only_on_neutral_layers(self):
        allowed = ("app.core", "app.providers", "app.vision", "app.observe", "app.playback.macro",
                   "app.playback.engine", "app.playback.preflight", "app.playback.format", "app.decide")
        for path in self.FILES:
            for module in _imports(path):
                if module.startswith("app."):
                    self.assertTrue(module.startswith(allowed), f"{path.name} imports {module}")

    def test_decide_never_names_a_provider_a_technology_or_an_engine(self):
        from tests.test_observe import Architecture as ObserveArchitecture
        names = ObserveArchitecture().code_names
        banned = ("ldplayer", "adb", "leidian", "emulator", "serial", "hwnd", "win32", "ctypes", "subprocess",
                  "tesseract", "opencv", "winrt", "powershell", "app.gui", "app.bootstrap", "app.windows",
                  "app.instances", "app.macros", "app.ocr", "app.ai")
        for path in self.FILES:
            for name in names(path):
                for word in banned:
                    self.assertNotIn(word, name.lower(), f"{path.name}: code uses {name!r}")

    def test_nothing_in_decide_calls_an_input_operation_directly(self):
        """The only road to input is `create_playback`; no module may call a session operation itself."""
        operations = {"tap", "double_tap", "secondary_tap", "move", "button_down", "button_up", "key_press",
                      "key_down", "key_up", "type_text", "send_keyevent", "input_tap"}
        for path in self.FILES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, operations, f"{path.name} calls .{node.func.attr}()")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"PlaybackEngine", "InputSession", "WindowInputSession"},
                                     f"{path.name} builds {node.func.id} itself")

    def test_only_run_py_can_reach_playback_execution(self):
        for path in self.FILES:
            if path.name in ("run.py", "__init__.py"):
                continue
            for module in _imports(path):
                self.assertFalse(module.startswith(("app.playback.engine", "app.playback.preflight")),
                                 f"{path.name} imports {module}")

    def test_condition_and_wait_cannot_act_at_all(self):
        for name in ("condition.py", "wait.py", "errors.py"):
            modules = _imports(ROOT / "decide" / name)
            self.assertFalse({m for m in modules if m.startswith("app.playback")}, name)

    def test_lower_layers_do_not_depend_on_decide(self):
        for package in ("core", "providers", "vision", "observe", "playback", "ocr", "adb", "ldplayer", "windows",
                        "instances"):
            for path in sorted((ROOT / package).rglob("*.py")):
                bad = {m for m in _imports(path) if m.startswith("app.decide")}
                self.assertEqual(bad, set(), f"{package}/{path.name} imports {bad}")

    def test_the_gui_reaches_this_layer_only_through_its_public_surface(self):
        """The GUI runs steps and stores macros through the package and its file format; it never reaches past
        them into how a run, a wait or a decision is carried out. (Before the GUI integration it did not import
        this layer at all; the dependency now points the one allowed way: GUI -> decide.)"""
        for path in sorted((ROOT / "gui").rglob("*.py")):
            modules = {m for m in _imports(path) if m.startswith("app.decide")}
            self.assertLessEqual(modules, {"app.decide", "app.decide.format"}, path.name)

    def test_this_layer_does_not_depend_on_the_gui(self):
        for path in self.FILES:
            self.assertFalse({m for m in _imports(path) if m.startswith(("app.gui", "app.bootstrap"))}, path.name)


if __name__ == "__main__":
    unittest.main()
