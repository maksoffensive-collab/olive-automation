r"""Real-Windows timing experiment for the recorder and the playback engine (audit finding C-14). Read-only.

It measures, on THIS machine and with the code paths the application really uses:

  A  the clock APIs: implementation and resolution reported by Python, the system timer resolution;
  B  the granularity actually observed (distinct values, smallest / largest step) for each clock, at rest, with the
     system timer raised to 1 ms (`timeBeginPeriod`), and under CPU load;
  C  the timestamps the Win32 observation layer puts on events (`Win32ObservationBackend._mouse_observation` /
     `_key_observation`, the very functions the low-level hooks call);
  D  what the real `WindowRecorder` records for events separated by known real intervals: events are stamped by the
     real stamping function and delivered to the real recorder; the recorded `delay_ms` is compared with the interval
     measured on the performance counter;
  E  two consequences: double-click classification near the system threshold, and how many of a stream of 5 ms
     pointer moves survive the recorder's throttle;
  F  the real `PlaybackEngine` (real clock, real sleep) scheduling 10 ms delays.

No input is sent to or read from any application (the hardware events themselves are simulated, because real input
must not be injected); no window is touched.

    .venv\Scripts\python.exe tools\timing_check.py
"""
from __future__ import annotations

import ctypes
import statistics
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sample(clock, seconds: float = 0.5) -> list[int]:
    """Distinct values a clock returns while polled in a tight loop (converted to ns)."""
    seen, end = set(), time.perf_counter() + seconds
    while time.perf_counter() < end:
        seen.add(clock())
    return sorted(seen)


def describe(label: str, values: list[int], unit_ns: int = 1) -> None:
    gaps = [(b - a) * unit_ns / 1e6 for a, b in zip(values, values[1:])]
    print(f"  {label:<34} distinct={len(values):>8}  smallest step={min(gaps):9.4f} ms  "
          f"largest={max(gaps):9.3f} ms  median={statistics.median(gaps):9.4f} ms")


def spin(ms: float) -> None:
    end = time.perf_counter_ns() + int(ms * 1e6)
    while time.perf_counter_ns() < end:
        pass


def load_threads(n: int) -> list:
    stop = threading.Event()

    def burn():
        while not stop.is_set():
            sum(range(1000))

    threads = [threading.Thread(target=burn, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    return [stop, threads]


def main() -> int:
    if sys.platform != "win32":
        print("Windows only.")
        return 2
    winmm = ctypes.WinDLL("winmm")
    ntdll = ctypes.WinDLL("ntdll")
    print("A. clocks")
    for name in ("monotonic", "perf_counter", "time"):
        info = time.get_clock_info(name)
        print(f"  time.{name:<13} implementation={info.implementation:<28} reported resolution={info.resolution * 1e3:.6f} ms")
    maximum, minimum, current = (ctypes.c_ulong() for _ in range(3))
    ntdll.NtQueryTimerResolution(ctypes.byref(maximum), ctypes.byref(minimum), ctypes.byref(current))
    print(f"  system timer (100 ns units): coarsest={maximum.value / 1e4:.3f} ms finest={minimum.value / 1e4:.3f} ms "
          f"current={current.value / 1e4:.3f} ms")
    print(f"  python {sys.version.split()[0]}")

    print("B. observed granularity (0.5 s of tight polling each)")
    describe("monotonic_ns  (at rest)", sample(time.monotonic_ns))
    describe("perf_counter_ns (at rest)", sample(time.perf_counter_ns))
    winmm.timeBeginPeriod(1)
    try:
        describe("monotonic_ns  (timer raised to 1ms)", sample(time.monotonic_ns))
        describe("perf_counter_ns (timer raised to 1ms)", sample(time.perf_counter_ns))
    finally:
        winmm.timeEndPeriod(1)
    burner = load_threads(8)
    try:
        describe("monotonic_ns  (8 busy threads)", sample(time.monotonic_ns))
        describe("perf_counter_ns (8 busy threads)", sample(time.perf_counter_ns))
    finally:
        burner[0].set()

    from app.windows.win32 import Win32ObservationBackend
    from app.windows.backend import MouseObservationKind as M
    obs_backend = Win32ObservationBackend()

    print("C. timestamps of the real Win32 stamping functions")
    describe("_mouse_observation().t_ns", sample(lambda: obs_backend._mouse_observation(0, M.MOVE, 0, 0, False).t_ns, 0.5))
    describe("_key_observation().t_ns", sample(lambda: obs_backend._key_observation(0, True, 65, False).t_ns, 0.5))

    from tests.test_recorder import MS, make
    from tests.test_windows_provider import info as window_info  # noqa: F401 (keeps the simulated desktop helpers)

    def stamped(kind, x=5, y=5):
        real = obs_backend._mouse_observation(0, kind, x, y, False)         # the real stamp (time source under test)
        return replace(real, client_origin=(0, 0), client_size=(200, 200), hit_hwnd=100)

    print("D. recorded delay_ms versus the true interval (performance counter), 20 events per gap")
    rows = []
    for gap in (2, 5, 10, 20, 50, 100):
        rec, obs, *_ = make()
        rec.start()
        true, previous = [], None
        for i in range(21):
            spin(gap)
            before = time.perf_counter_ns()
            o = stamped(M.MOVE, 1 + i, 1)
            after = time.perf_counter_ns()
            rec._on_mouse(o)
            now = (before + after) // 2
            if previous is not None:
                true.append((now - previous) / 1e6)
            previous = now
        events = rec.stop().events
        recorded = [e.delay_ms for e in events[1:]]
        n = min(len(recorded), len(true))
        errors = [r - t for r, t in zip(recorded[:n], true[:n])]
        zero = sum(1 for r, t in zip(recorded[:n], true[:n]) if r == 0 and t >= 1.5)
        rows.append((gap, len(events), statistics.mean(abs(e) for e in errors) if errors else float("nan"),
                     max(abs(e) for e in errors) if errors else float("nan"), zero,
                     sorted(set(recorded))[:6]))
    for gap, kept, mean_err, max_err, zeros, seen in rows:
        print(f"  gap {gap:>3} ms: events kept={kept:>2}/21  mean |error|={mean_err:7.3f} ms  max |error|={max_err:7.3f} ms  "
              f"recorded 0 ms for a >=1.5 ms interval: {zeros:>2}  distinct delays={seen}")

    print("E. consequences")
    from app.providers.recording import RecordedEventKind as K
    wrong, trials = 0, 40
    for i in range(trials):
        true_gap = 470 if i % 2 == 0 else 530                               # system double-click time is 500 ms
        rec, obs, *_ = make()
        rec.start()
        rec._on_mouse(stamped(M.PRIMARY_DOWN))
        rec._on_mouse(stamped(M.PRIMARY_UP))
        spin(true_gap)
        rec._on_mouse(stamped(M.PRIMARY_DOWN))
        rec._on_mouse(stamped(M.PRIMARY_UP))
        counts = [e.click_count for e in rec.stop().events if e.kind is K.BUTTON_DOWN]
        expected_double = true_gap < 500
        if (counts[-1] == 2) != expected_double:
            wrong += 1
    print(f"  double-click classification (gaps 470/530 ms vs 500 ms limit): wrong in {wrong} of {trials}")
    rec, obs, *_ = make()
    rec.start()
    t_end, sent = time.perf_counter() + 1.0, 0
    while time.perf_counter() < t_end:
        sent += 1
        rec._on_mouse(stamped(M.MOVE, sent % 150, 3))
        spin(5)
    print(f"  5 ms move stream for 1 s: {sent} moves offered, {len(rec.stop())} recorded "
          f"(throttle is documented as one per 10 ms, i.e. up to about 100 per second)")

    print("F. real PlaybackEngine with the default clock and sleep, macro of 10 ms delays")
    from app.core.cancel import EmergencyStop
    from app.playback.engine import PlaybackEngine
    from app.playback.macro import Macro
    from app.providers.recording import RecordedEvent as E, Recording
    from tests.test_playback import FakeInput, FakeTarget
    events = tuple([E(K.POINTER_MOVE, 0, 1, 1)] + [E(K.POINTER_MOVE, 10, 1 + i, 1) for i in range(1, 41)])
    fake = FakeInput()
    stamps: list[float] = []
    fake.on_call = lambda i, name: stamps.append(time.perf_counter())
    PlaybackEngine(Macro(Recording("fake", "t-1", events)), FakeTarget(), fake, EmergencyStop()).run()
    intervals = [(b - a) * 1e3 for a, b in zip(stamps, stamps[1:])]
    print(f"  40 intervals scheduled at 10 ms: mean={statistics.mean(intervals):6.2f} ms  min={min(intervals):6.2f}  "
          f"max={max(intervals):6.2f}  mean |error|={statistics.mean(abs(x - 10) for x in intervals):5.2f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
