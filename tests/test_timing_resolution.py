"""Timing resolution (sixth bug-fix stage, audit finding C-14).

Measured on Windows with Python 3.12 (tools/timing_check.py): `time.monotonic` is `GetTickCount64()` with a fixed
15.625 ms step (33 distinct values in 0.5 s, unchanged by `timeBeginPeriod(1)` and by CPU load), so events stamped
with it were recorded with delays of 15/16 ms for real gaps of 5 or 10 ms, 93/94/110 ms for 100 ms, only about 65 of
a 200/s move stream survived a "10 ms" throttle, and the playback engine fired 10 ms delays as bursts (0 ms then
about 20 ms).

The contract these tests encode: every time source of the recorder and of the playback engine has sub-millisecond
resolution, a recorded delay is within 2.5 ms of the real interval, the move throttle is honoured at its configured
value and the engine schedules delays without bursts. The real-clock tests use short real waits.
"""
import inspect
import statistics
import sys
import time
import unittest
from dataclasses import replace

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.providers.base import TargetIdentity
from app.providers.input import Key
from app.providers.recording import RecordedEventKind as K, RecordedEvent as E, Recording, RecordingSession
from app.windows.backend import MouseObservationKind as M
from app.windows.recorder import WindowRecorder
from tests.test_playback import FakeInput, FakeTarget
from tests.test_recorder import make

IS_WINDOWS = sys.platform == "win32"


def resolution(clock, unit_ns: int = 1, seconds: float = 0.03) -> tuple[int, float]:
    """(distinct values seen, smallest positive step in ms) while polling `clock` in a tight loop."""
    seen, end = set(), time.perf_counter() + seconds
    while time.perf_counter() < end:
        seen.add(clock())
    values = sorted(seen)
    steps = [(b - a) * unit_ns / 1e6 for a, b in zip(values, values[1:])]
    return len(values), (min(steps) if steps else float("inf"))


def spin(ms: float) -> None:
    end = time.perf_counter_ns() + int(ms * 1e6)
    while time.perf_counter_ns() < end:
        pass


class TimeSourcesHaveSubMillisecondResolution(unittest.TestCase):
    def assert_fine(self, clock, unit_ns=1, what=""):
        distinct, smallest = resolution(clock, unit_ns)
        self.assertGreater(distinct, 200, f"{what}: only {distinct} distinct values in 30 ms (step {smallest} ms)")
        self.assertLess(smallest, 1.0, what)

    def test_the_default_clock_of_a_recording_session(self):
        self.assert_fine(RecordingSession()._clock, 1, "RecordingSession default clock")

    def test_the_default_clock_of_the_windows_recorder(self):
        default = inspect.signature(WindowRecorder.__init__).parameters["clock_ns"].default
        self.assert_fine(default, 1, "WindowRecorder default clock")

    def test_the_default_clock_of_the_playback_engine(self):
        default = inspect.signature(PlaybackEngine.__init__).parameters["clock"].default
        self.assert_fine(default, 1_000_000_000, "PlaybackEngine default clock")      # seconds -> ns

    @unittest.skipUnless(IS_WINDOWS, "needs Windows")
    def test_the_real_win32_event_stamps(self):
        from app.windows.win32 import Win32ObservationBackend
        backend = Win32ObservationBackend()
        self.assert_fine(lambda: backend._mouse_observation(0, M.MOVE, 0, 0, False).t_ns, 1, "mouse stamp")
        self.assert_fine(lambda: backend._key_observation(0, True, 65, False).t_ns, 1, "key stamp")


@unittest.skipUnless(IS_WINDOWS, "needs Windows")
class RecordedTimingMatchesRealTime(unittest.TestCase):
    """The real stamping function feeds the real WindowRecorder; only the hardware event itself is simulated."""

    @classmethod
    def setUpClass(cls):
        from app.windows.win32 import Win32ObservationBackend
        cls.backend = Win32ObservationBackend()

    def stamped(self, kind, x=5, y=5):
        real = self.backend._mouse_observation(0, kind, x, y, False)         # real time source
        return replace(real, client_origin=(0, 0), client_size=(500, 500), hit_hwnd=100)

    def test_recorded_delays_are_within_2_5_ms_of_the_real_intervals(self):
        for gap in (5, 10, 20, 100):
            with self.subTest(gap_ms=gap):
                rec, *_ = make(min_move_interval_ms=0)
                rec.start()
                true, previous = [], None
                for i in range(12):
                    spin(gap)
                    before = time.perf_counter_ns()
                    obs = self.stamped(M.MOVE, 1 + i, 1)
                    after = time.perf_counter_ns()
                    rec._on_mouse(obs)
                    now = (before + after) / 2
                    if previous is not None:
                        true.append((now - previous) / 1e6)
                    previous = now
                events = rec.stop().events
                self.assertEqual(len(events), 12)
                errors = [e.delay_ms - t for e, t in zip(events[1:], true)]
                self.assertLess(max(abs(x) for x in errors), 2.5, errors)

    def test_the_move_throttle_is_honoured_at_its_configured_value(self):
        rec, *_ = make(min_move_interval_ms=10)
        rec.start()
        end = time.perf_counter() + 0.4
        n = 0
        while time.perf_counter() < end:
            n += 1
            rec._on_mouse(self.stamped(M.MOVE, n % 400, 3))
            spin(4)                                                           # a move every 4 ms
        delays = [e.delay_ms for e in rec.stop().events[1:]]
        self.assertGreater(len(delays), 15)
        self.assertLessEqual(min(delays), 13, delays)                          # 12 ms steps, not 15/16 ms quanta
        self.assertLess(statistics.median(delays), 14.5, delays)
        self.assertGreaterEqual(min(delays), 10)                               # the throttle still holds


class PlaybackDelaysAreNotBursts(unittest.TestCase):
    def test_ten_millisecond_delays_are_scheduled_evenly(self):
        events = tuple([E(K.POINTER_MOVE, 0, 1, 1)] + [E(K.POINTER_MOVE, 10, 1 + i, 1) for i in range(1, 31)])
        fake = FakeInput()
        stamps = []
        fake.on_call = lambda i, name: stamps.append(time.perf_counter())
        PlaybackEngine(Macro(Recording("fake", "t-1", events)), FakeTarget(), fake, EmergencyStop()).run()
        intervals = [(b - a) * 1e3 for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(intervals), 30)
        self.assertGreater(min(intervals), 2.0, intervals)                    # no burst of near-zero intervals
        self.assertLess(statistics.mean(abs(x - 10) for x in intervals), 4.0, intervals)


class ContractsThatMustNotChange(unittest.TestCase):
    def test_an_injected_clock_still_controls_the_recording_session(self):
        class Clock:
            now = 0

            def __call__(self):
                return self.now

        clock = Clock()
        session = RecordingSession(clock_ns=clock)
        session.start(TargetIdentity("p", "t"))
        clock.now = 5_000_000_000                                              # a long wait before the first event
        session.record(K.KEY_DOWN, key=Key.A)
        clock.now += 1_500_000
        session.record(K.KEY_UP, key=Key.A)
        self.assertEqual([e.delay_ms for e in session.stop().events], [0, 2])  # first event 0; 1.5 ms rounds half up


if __name__ == "__main__":
    unittest.main()
