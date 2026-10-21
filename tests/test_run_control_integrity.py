"""Run-control outcome integrity (third bug-fix stage).

  C-04  an emergency stop that interrupts a multi-message input operation must end the run as CANCELLED
        ("emergency_stop"), not as a provider FAILURE; the partial delivery stays visible;
  C-07  a status callback (for example a UI) that raises must not abort the run, skip other targets, or change what
        a target's outcome says.
Deterministic: simulated desktop / fake targets only.
"""
import threading
import unittest

from app.automation.orchestrator import Mode, Status, run_on_targets
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError
from app.playback.engine import PlaybackState as S
from app.playback.macro import Macro
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.windows.playback import create_window_playback
from tests.test_gui import Base
from tests.test_phase7_10_11 import targets
from tests.test_playback import Clock, SAMPLE, rig
from tests.test_windows_input import FakeInput as FakeWindowBackend
from tests.test_windows_provider import info, setup


class StopInsideAMultiMessageOperation(unittest.TestCase):
    """C-04, through the real Windows input session on a simulated desktop."""

    def build(self, *events):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        backend = FakeWindowBackend(desk, (60, 40))
        clock = Clock()
        macro = Macro(Recording(target.provider_id, target.target_id, tuple(events)))
        eng = create_window_playback(macro, target, verifier, backend, stop, clock=clock, sleep=clock.sleep)
        return eng, backend, stop

    def test_stop_between_the_characters_of_a_text_is_a_cancel(self):
        eng, backend, stop = self.build(E(K.TEXT, 0, text="Hello"))
        backend.on_event = lambda b: stop.trigger("panic") if len(b.events) == 2 else None
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason), (S.CANCELLED, "emergency_stop"))
        self.assertEqual(len(backend.events), 2)                              # nothing after the stop was sent
        self.assertEqual(rep.completed_count, 0)                              # the interrupted action did not complete
        self.assertEqual((rep.failure.delivered, rep.failure.total, rep.failure.action_index), (2, 5, 0))

    def test_stop_between_the_clicks_of_a_double_tap_is_a_cancel(self):
        eng, backend, stop = self.build(E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1), E(K.BUTTON_UP, 10, 5, 5, B.PRIMARY, 1),
                                        E(K.BUTTON_DOWN, 20, 5, 5, B.PRIMARY, 2), E(K.BUTTON_UP, 10, 5, 5, B.PRIMARY, 2))
        backend.on_event = lambda b: stop.trigger("panic") if len(b.events) == 2 else None
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason), (S.CANCELLED, "emergency_stop"))
        self.assertEqual((rep.failure.delivered, rep.failure.total), (2, 4))
        self.assertEqual(len(backend.events), 2)

    def test_a_later_action_is_not_started_after_the_interrupted_one(self):
        eng, backend, stop = self.build(E(K.TEXT, 0, text="abc"), E(K.POINTER_MOVE, 5, 3, 3))
        backend.on_event = lambda b: stop.trigger("panic") if len(b.events) == 1 else None
        rep = eng.run()
        self.assertEqual(rep.state, S.CANCELLED)
        self.assertEqual([e[0] for e in backend.events], ["char"])

    def test_a_partial_delivery_that_is_not_caused_by_the_stop_is_still_a_failure(self):
        eng, backend, stop = self.build(E(K.TEXT, 0, text="Hello"))
        backend.fail_at = 3                                                   # the OS refuses the third message
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.cancel_reason), (S.FAILED, "PartialInputDelivery", None))
        self.assertEqual((rep.failure.delivered, rep.failure.total), (2, 5))

    def test_a_window_that_vanishes_mid_operation_is_still_a_failure(self):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        backend = FakeWindowBackend(desk, (60, 40))
        backend.on_event = lambda b: desk.windows.pop(100, None)
        macro = Macro(Recording(target.provider_id, target.target_id, (E(K.TEXT, 0, text="abc"),)))
        rep = create_window_playback(macro, target, verifier, backend, stop).run()
        self.assertEqual(rep.state, S.FAILED)
        self.assertFalse(stop.is_set())

    def test_stop_set_after_an_unrelated_failure_does_not_relabel_it(self):
        eng, backend, stop = self.build(E(K.TEXT, 0, text="Hello"))
        backend.fail_at = 3
        backend.on_event = None
        rep = eng.run()
        stop.trigger("late")                                                  # a stop AFTER the run ended changes nothing
        self.assertEqual(eng.report().state, S.FAILED)
        self.assertEqual(rep.state, S.FAILED)


class StopCauseChain(unittest.TestCase):
    """C-04 at the generic engine: only an EXPLICIT cause chain (`raise ... from stop_error`) counts."""

    def test_an_error_caused_by_the_stop_error_cancels_and_keeps_partial_delivery(self):
        eng, inp, *_ = rig(SAMPLE())
        err = PlatformError("stopped after 1 of 2")
        err.delivered, err.total = 1, 2
        err.__cause__ = EmergencyStopped("Emergency stop is active")
        inp.raises[2] = err
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason), (S.CANCELLED, "emergency_stop"))
        self.assertEqual((rep.failure.delivered, rep.failure.total, rep.failure.action_index), (1, 2, 1))
        self.assertEqual(rep.completed_count, 1)

    def test_a_deeper_cause_chain_is_followed(self):
        eng, inp, *_ = rig(SAMPLE())
        middle = PlatformError("middle")
        middle.__cause__ = EmergencyStopped("stop")
        outer = PlatformError("outer")
        outer.__cause__ = middle
        inp.raises[1] = outer
        self.assertEqual(eng.run().state, S.CANCELLED)

    def test_an_error_merely_raised_while_handling_a_stop_is_not_a_cancel(self):
        eng, inp, *_ = rig(SAMPLE())
        try:
            try:
                raise EmergencyStopped("stop")
            except EmergencyStopped:
                raise PlatformError("unrelated")                              # implicit __context__ only, no __cause__
        except PlatformError as exc:
            inp.raises[1] = exc
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code), (S.FAILED, "PlatformError"))

    def test_the_direct_stop_error_behaviour_is_unchanged(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.raises[2] = EmergencyStopped("Emergency stop is active")
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason, rep.failure), (S.CANCELLED, "emergency_stop", None))

    def test_an_ordinary_failure_is_unchanged(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.raises[2] = RuntimeError("boom")
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.failure.message), (S.FAILED, "RuntimeError", "boom"))


class StopInsideAnOperationThroughTheGui(Base):
    def test_the_user_sees_an_emergency_stop_not_a_playback_failure(self):
        self.pick()
        events = (E(K.TEXT, 0, text="Hello"),)
        self.c.macro = Macro(Recording("windows", self.c.target.target_id, events), "txt")
        self.world.on_event = lambda w: self.stop.trigger("panic") if len(w.events) == 2 else None
        self.assertTrue(self.c.play())
        self.finish_playback()
        self.assertEqual(self.c.last_report.state, S.CANCELLED)
        self.assertEqual(self.c.status.category, "emergency_stop")
        self.assertTrue(self.c.emergency_active)
        self.assertEqual(len(self.world.events), 2)


class StatusCallbacks(unittest.TestCase):
    """C-07."""

    def run_mode(self, mode, callback, job=None, stop=None):
        ran = []

        def default_job(t):
            ran.append(t.logical_id)
            return t.logical_id

        report = run_on_targets(targets(), job or default_job, stop or EmergencyStop(), mode=mode, stagger_s=0.01,
                                on_status=callback)
        return report, ran

    def test_a_callback_that_raises_never_aborts_the_run(self):
        def failing(logical_id, status):
            raise RuntimeError("UI is gone")

        for mode in Mode:
            with self.subTest(mode=mode):
                report, ran = self.run_mode(mode, failing)
                self.assertEqual(sorted(ran), ["ld1", "ld2"])
                self.assertTrue(report.all_completed)
                self.assertTrue(all(o.status is Status.COMPLETED and o.error is None for o in report.outcomes.values()))

    def test_a_callback_failure_on_running_does_not_leave_the_status_running(self):
        def on_running(logical_id, status):
            if status is Status.RUNNING:
                raise RuntimeError("boom")

        report, ran = self.run_mode(Mode.SEQUENTIAL, on_running)
        self.assertEqual(sorted(ran), ["ld1", "ld2"])
        self.assertNotIn(Status.RUNNING, {o.status for o in report.outcomes.values()})

    def test_a_callback_failure_on_completed_does_not_turn_success_into_failure(self):
        def on_done(logical_id, status):
            if status is Status.COMPLETED:
                raise RuntimeError("boom")

        report, _ = self.run_mode(Mode.SEQUENTIAL, on_done)
        self.assertTrue(report.all_completed)
        self.assertTrue(all(o.error is None for o in report.outcomes.values()))

    def test_a_real_job_failure_is_still_a_failure_and_others_still_run(self):
        def failing(logical_id, status):
            raise RuntimeError("UI is gone")

        def job(t):
            if t.logical_id == "ld1":
                raise ValueError("job broke")
            return "ok"

        report, _ = self.run_mode(Mode.SIMULTANEOUS, failing, job)
        self.assertEqual(report.outcomes["ld1"].status, Status.FAILED)
        self.assertEqual(report.outcomes["ld1"].error, "ValueError: job broke")
        self.assertEqual(report.outcomes["ld2"].status, Status.COMPLETED)

    def test_the_emergency_stop_still_stops_everything_when_the_callback_fails(self):
        stop = EmergencyStop()

        def failing(logical_id, status):
            raise RuntimeError("UI is gone")

        def job(t):
            stop.trigger()
            stop.check()

        report, _ = self.run_mode(Mode.SEQUENTIAL, failing, job, stop)
        self.assertEqual({o.status for o in report.outcomes.values()}, {Status.STOPPED})

    def test_callback_failures_are_reported_not_hidden(self):
        def failing(logical_id, status):
            raise RuntimeError("UI is gone")

        report, _ = self.run_mode(Mode.SEQUENTIAL, failing)
        self.assertGreaterEqual(len(report.notification_errors), 2)
        self.assertIn("RuntimeError: UI is gone", report.notification_errors[0])

    def test_a_working_callback_sees_every_status_in_order_and_no_errors(self):
        seen = []
        report, _ = self.run_mode(Mode.SEQUENTIAL, lambda i, s: seen.append((i, s)))
        self.assertEqual(seen, [("ld1", Status.RUNNING), ("ld1", Status.COMPLETED),
                                ("ld2", Status.RUNNING), ("ld2", Status.COMPLETED)])
        self.assertEqual(report.notification_errors, [])

    def test_callbacks_from_worker_threads_do_not_lose_errors(self):
        lock = threading.Lock()
        calls = []

        def failing(logical_id, status):
            with lock:
                calls.append(status)
            raise RuntimeError("x")

        report, _ = self.run_mode(Mode.SIMULTANEOUS, failing)
        self.assertEqual(len(report.notification_errors), len(calls))


if __name__ == "__main__":
    unittest.main()
