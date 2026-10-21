"""Tests for retry policy, playback engine (dry-run sink only) and orchestrator. Synthetic data only."""
import threading
import time
import unittest

from app.automation.orchestrator import Mode, Status, run_on_targets
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, NoTargetSelected, SafetyViolation, TargetNotVerified
from app.macros.parser import parse_bytes
from app.macros.playback import CoordinateTransform, DryRunSink, PlaybackRefused, build_steps, play
from app.recovery.retry import RetriesExhausted, RetryPolicy, run_with_retry
from tests import helpers
from tests.test_mapping import make_service, two_instance_world
from tests.test_target_contract import WindowStyleTarget


def targets():
    svc = make_service(two_instance_world())
    return [svc.verify("ld1"), svc.verify("ld2")]


def macro(ops):
    return parse_bytes(helpers.to_bytes({"operations": ops, "recordInfo": helpers.record_info()}))


def back(t):
    return {"timing": t, "operationId": "AndroidBack"}


class RetryTests(unittest.TestCase):
    P = RetryPolicy(max_attempts=3, delay_s=0)

    def test_bounds_enforced(self):
        for n in (0, 11, -1):
            with self.assertRaises(ValueError):
                RetryPolicy(max_attempts=n)

    def test_succeeds_after_transient_failures(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("flaky")
            return "ok"
        self.assertEqual(run_with_retry(fn, self.P, EmergencyStop()), "ok")
        self.assertEqual(len(calls), 3)

    def test_exhaustion_is_bounded(self):
        calls = []

        def fn():
            calls.append(1)
            raise OSError("down")
        with self.assertRaises(RetriesExhausted) as cm:
            run_with_retry(fn, self.P, EmergencyStop())
        self.assertEqual((len(calls), cm.exception.attempts), (3, 3))

    def test_permanent_and_safety_errors_not_retried(self):
        calls = []

        def perm():
            calls.append(1)
            raise KeyError("x")
        with self.assertRaises(KeyError):
            run_with_retry(perm, self.P, EmergencyStop(), is_transient=lambda e: False)

        def unsafe():
            calls.append(1)
            raise SafetyViolation("no")
        with self.assertRaises(SafetyViolation):
            run_with_retry(unsafe, self.P, EmergencyStop())
        self.assertEqual(len(calls), 2)

    def test_stop_blocks_and_interrupts_backoff(self):
        stop = EmergencyStop()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            run_with_retry(lambda: 1, self.P, stop)
        stop.reset()
        threading.Timer(0.1, stop.trigger).start()
        t0 = time.monotonic()
        with self.assertRaises(EmergencyStopped):
            run_with_retry(lambda: 1 / 0, RetryPolicy(3, delay_s=20), stop)
        self.assertLess(time.monotonic() - t0, 5)

    def test_backoff_capped(self):
        p = RetryPolicy(5, delay_s=1, backoff=10, max_delay_s=5)
        self.assertEqual([p.delay_before_retry(i) for i in (1, 2, 3)], [1, 5, 5])


class PlaybackTests(unittest.TestCase):
    T = CoordinateTransform(0.05, 0.05)

    def test_requires_verified_target(self):
        with self.assertRaises(TargetNotVerified):
            play("emulator-5554", macro(helpers.tap(0, 1, 1)), DryRunSink(), EmergencyStop(), transform=self.T)

    def test_refuses_touch_without_transform(self):
        with self.assertRaises(PlaybackRefused):
            build_steps(macro(helpers.tap(0, 100, 100)), None)

    def test_non_touch_macro_needs_no_transform(self):
        self.assertEqual(len(build_steps(macro([back(0)]), None)), 1)

    def test_refuses_invalid_and_merged(self):
        with self.assertRaises(PlaybackRefused):
            build_steps(macro([back(10), back(5)]), None)
        merged = parse_bytes(helpers.to_bytes({"recordInfo": helpers.record_info(0, 0), "mergedRecords": [
            {"operations": [], "recordInfo": helpers.record_info()}]}))
        with self.assertRaises(PlaybackRefused):
            build_steps(merged, None)

    def test_dry_run_sends_to_intended_target_in_order_with_transform(self):
        t1, _ = targets()
        sink = DryRunSink()
        res = play(t1, macro(helpers.tap(0, 9600, 5400)), sink, EmergencyStop(), transform=self.T, speed=1000)
        self.assertTrue(res.completed)
        self.assertEqual({s for s, _ in sink.steps}, {t1.adb_serial})
        self.assertEqual(sink.steps[0][1].points[0][1:3], (480, 270))
        times = [s.at_ms for _, s in sink.steps]
        self.assertEqual(times, sorted(times))

    def test_timing_is_respected(self):
        t1, _ = targets()
        t0 = time.monotonic()
        play(t1, macro([back(0), back(300)]), DryRunSink(), EmergencyStop())
        self.assertGreaterEqual(time.monotonic() - t0, 0.29)

    def test_stop_mid_playback(self):
        t1, _ = targets()
        stop = EmergencyStop()
        threading.Timer(0.1, stop.trigger).start()
        res = play(t1, macro([back(0), back(30000)]), DryRunSink(), stop)
        self.assertTrue(res.stopped and not res.completed)
        self.assertEqual(res.sent, 1)

    def test_active_stop_refuses_start(self):
        t1, _ = targets()
        stop = EmergencyStop()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            play(t1, macro([back(0)]), DryRunSink(), stop)

    def test_input_macro_not_mutated(self):
        m = macro(helpers.tap(0, 100, 100))
        build_steps(m, self.T)
        self.assertEqual(m.operations[0].points[0].x, 100)


class OrchestratorTests(unittest.TestCase):
    def test_empty_and_bare_targets_refused(self):
        with self.assertRaises(NoTargetSelected):
            run_on_targets([], lambda t: 1, EmergencyStop())
        with self.assertRaises(TargetNotVerified):
            run_on_targets(["emulator-5554"], lambda t: 1, EmergencyStop())

    def test_duplicates_refused(self):
        t1, _ = targets()
        with self.assertRaises(TargetNotVerified):
            run_on_targets([t1, t1], lambda t: 1, EmergencyStop())

    def test_each_job_gets_its_own_target(self):
        for mode in Mode:
            seen = {}
            r = run_on_targets(targets(), lambda t: seen.setdefault(t.logical_id, t.adb_serial), EmergencyStop(),
                               mode=mode, stagger_s=0.01)
            self.assertTrue(r.all_completed, mode)
            self.assertEqual(seen, {"ld1": "emulator-5554", "ld2": "emulator-5556"})

    def test_one_failure_does_not_stop_others(self):
        def job(t):
            if t.logical_id == "ld1":
                raise RuntimeError("boom")
            return "ok"
        r = run_on_targets(targets(), job, EmergencyStop(), mode=Mode.SIMULTANEOUS)
        self.assertEqual(r.outcomes["ld1"].status, Status.FAILED)
        self.assertEqual(r.outcomes["ld2"].status, Status.COMPLETED)

    def test_emergency_stop_halts_all(self):
        stop = EmergencyStop()

        def job(t):
            stop.trigger()
            stop.check()
        r = run_on_targets(targets(), job, stop, mode=Mode.SEQUENTIAL)
        self.assertEqual({o.status for o in r.outcomes.values()}, {Status.STOPPED})

    def test_stagger_interrupted_by_stop(self):
        stop = EmergencyStop()
        threading.Timer(0.1, stop.trigger).start()
        t0 = time.monotonic()
        r = run_on_targets(targets(), lambda t: 1, stop, mode=Mode.STAGGERED, stagger_s=20)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(r.outcomes["ld2"].status, Status.STOPPED)

    def test_worker_bound(self):
        with self.assertRaises(ValueError):
            run_on_targets(targets(), lambda t: 1, EmergencyStop(), max_workers=1000)

    def test_accepts_any_verified_target_not_only_ldplayers(self):
        """A-01: run_on_targets is typed on the generic Target contract, not the LDPlayer VerifiedTarget. A
        provider that has nothing to do with LDPlayer (WindowStyleTarget, a stand-in for a future desktop/window
        provider) must be accepted on its own, and mixed with a real VerifiedTarget in the same run."""
        w1, w2 = WindowStyleTarget("win-a"), WindowStyleTarget("win-b")
        seen = {}
        r = run_on_targets([w1, w2], lambda t: seen.setdefault(t.target_id, t.provider_id), EmergencyStop())
        self.assertTrue(r.all_completed)
        self.assertEqual(seen, {"win-a": "desktop-window", "win-b": "desktop-window"})

        ld1, _ = targets()
        r = run_on_targets([ld1, w1], lambda t: t.provider_id, EmergencyStop())
        self.assertEqual({o.status for o in r.outcomes.values()}, {Status.COMPLETED})
        self.assertEqual(set(r.outcomes), {ld1.target_id, w1.target_id})

    def test_an_unverified_target_is_refused_even_if_it_is_not_ldplayer_shaped(self):
        from app.providers.target import VerificationState
        unverified = WindowStyleTarget("win-a", state=VerificationState.UNVERIFIED)
        with self.assertRaises(TargetNotVerified):
            run_on_targets([unverified], lambda t: 1, EmergencyStop())


if __name__ == "__main__":
    unittest.main()
