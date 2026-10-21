r"""Measurement of a LARGE macro through the real playback path, aimed only at a window this script creates.

This is the evidence behind `MAX_SESSION_ACTIONS` (Stage 8, C-03). It uses the real code path end to end:
`Macro` -> `create_window_playback` -> `WindowInputSession` -> `Win32InputBackend`, aimed at a window created here,
in this process, and at no other program. Every run prints one line of measurements.

REAL INPUT in `live` mode: delivery is real, hardware-level `SendInput` (see `app.windows.win32`), not messages posted
to a handle. A live run MOVES THE REAL MOUSE POINTER over this window for every action (thousands of times for a
large macro), brings the window to the front and gives it keyboard focus; do not use the mouse or keyboard while it
runs. `build` and `dry` send nothing. Run `live` only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_large_macro_check.py --actions 20000 --delay-ms 0 --mode live

Modes:
  build  build the macro, preflight it, serialize/parse it (GUI-thread costs; no window, no input)
  dry    dry run through the real session (must deliver nothing, must not use budget)
  live   live run; checks that the window received exactly the expected messages in order, memory, throughput
         and how quickly the window still answers (`SendMessageTimeout(WM_NULL)`) while the run is in progress
  stall  live run against a window of a CHILD process (also created by this script) whose thread never reads its
         queue: the OS queue limit is reached and the run must FAIL cleanly instead of hanging or reporting
         COMPLETED. (A stalled window in the SAME process would block the identity re-check itself, an artefact
         of the experiment, so the stalled window lives in a child process.)
  stop   live run; an emergency stop is raised part-way; measures the delay until the run ends and that nothing
         more is delivered afterwards
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import statistics
import sys
import threading
import time
import tracemalloc
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_click_smoke import TestWindow  # noqa: E402

from app.core.cancel import EmergencyStop  # noqa: E402
from app.playback.format import macro_from_json, macro_to_json  # noqa: E402
from app.playback.macro import Macro  # noqa: E402
from app.playback.preflight import preflight  # noqa: E402
from app.providers.recording import RecordedEvent as E, RecordedEventKind as K, Recording  # noqa: E402
from app.windows.playback import create_window_playback  # noqa: E402
from app.windows.target import WindowVerifier  # noqa: E402
from app.windows.win32 import Win32InputBackend  # noqa: E402


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def memory_mb() -> tuple[float, float]:
    """(current, peak) working set of this process in MB."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    p = ctypes.WinDLL("psapi", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    p.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    c = _PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    p.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1e6, c.PeakWorkingSetSize / 1e6


def ping_ms(hwnd: int, timeout_ms: int = 2000) -> float | None:
    """Round trip of a WM_NULL sent to the test window (None if it did not answer): is the window still responsive?"""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT,
                                      wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t()
    t0 = time.perf_counter()
    ok = u.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, timeout_ms, ctypes.byref(result))    # SMTO_ABORTIFHUNG
    return (time.perf_counter() - t0) * 1000 if ok else None


def moves(n: int, delay_ms: int) -> tuple[E, ...]:
    return tuple(E(K.POINTER_MOVE, 0 if i == 0 else delay_ms, 5 + i % 200, 5 + i % 150) for i in range(n))


def expected(n: int) -> list[tuple[str, int, int]]:
    return [("move", 5 + i % 200, 5 + i % 150) for i in range(n)]


class Prober(threading.Thread):
    """While a run is in progress: ask the window every 50 ms whether it answers, and time this process's own
    scheduling (a sleep(10 ms) that comes back late shows the interpreter/GUI thread being starved)."""

    def __init__(self, hwnd: int | None) -> None:
        super().__init__(daemon=True)
        self.hwnd, self.done = hwnd, threading.Event()
        self.pings: list[float | None] = []
        self.lag_ms: list[float] = []

    def run(self) -> None:
        while not self.done.is_set():
            t0 = time.perf_counter()
            time.sleep(0.01)
            self.lag_ms.append((time.perf_counter() - t0) * 1000 - 10)
            if self.hwnd and len(self.lag_ms) % 5 == 0:
                self.pings.append(ping_ms(self.hwnd))

    def summary(self) -> str:
        answered = [p for p in self.pings if p is not None]
        ping = (f"window answered {len(answered)}/{len(self.pings)} pings, median {statistics.median(answered):.2f} ms, "
                f"max {max(answered):.1f} ms" if answered else f"pings {len(self.pings)} answered 0")
        lag = f"thread lag median {statistics.median(self.lag_ms):.2f} ms, max {max(self.lag_ms):.1f} ms" if self.lag_ms else ""
        return f"{ping}; {lag}"


def serve_blocked_window() -> int:
    """Child process for the stall mode: own window whose thread blocks on its first message and never returns."""
    class Blocking(list):
        def append(self, item):
            threading.Event().wait(3600)

    win = TestWindow()
    win.messages = Blocking()
    win.start()
    print(win.hwnd, flush=True)
    time.sleep(3600)
    return 0


def main() -> int:
    if "--serve-blocked-window" in sys.argv:
        return serve_blocked_window()
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", type=int, required=True)
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--mode", choices=("build", "dry", "live", "stall", "stop"), default="live")
    a = ap.parse_args()
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    n, d = a.actions, a.delay_ms
    print(f"== mode={a.mode} actions={n} delay_ms={d}")
    gc.collect()
    base_ws, _ = memory_mb()

    if a.mode == "build":
        t = time.perf_counter()
        m = Macro(Recording("windows", "win-x", moves(n, d)), "big")
        t_build = time.perf_counter() - t
        t = time.perf_counter()
        actions = len(m.actions)
        t_compile = time.perf_counter() - t
        t = time.perf_counter()
        report = preflight(m, None)          # target refusal is expected; the per-action scan is what is timed
        t_pre = time.perf_counter() - t
        t = time.perf_counter()
        text = macro_to_json(m)
        t_json = time.perf_counter() - t
        t = time.perf_counter()
        back = macro_from_json(text)
        t_load = time.perf_counter() - t
        ws, peak = memory_mb()
        print(f"build macro {t_build:.2f}s (compiles {actions} actions), preflight {t_pre:.2f}s, "
              f"to_json {t_json:.2f}s ({len(text) / 1e6:.1f} MB), from_json {t_load:.2f}s "
              f"(equal={back.recording.events == m.recording.events}); working set {base_ws:.0f} -> {ws:.0f} MB, "
              f"peak {peak:.0f} MB; preflight issues {len(report.issues)}")
        return 0

    child = None
    if a.mode == "stall":
        import subprocess
        child = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--serve-blocked-window"],
                                 stdout=subprocess.PIPE, text=True)
        win = type("ChildWindow", (), {"hwnd": int(child.stdout.readline()), "messages": [],
                                       "close": lambda self: child.kill()})()
    else:
        win = TestWindow()
        win.start()
    time.sleep(0.3)
    failures: list[str] = []
    try:
        stop = EmergencyStop()
        backend = Win32InputBackend()
        verifier = WindowVerifier(backend, stop)
        target = verifier.verify(win.hwnd)
        m = Macro(Recording(target.provider_id, target.target_id, moves(n, d)), "big")
        engine = create_window_playback(m, target, verifier, backend, stop, dry_run=(a.mode == "dry"))
        issues = [i.code for i in engine.preflight().errors]
        print(f"preflight errors: {issues}")
        if issues:
            print("REFUSED before any input; window received", len(win.messages))
            return 0
        if a.mode == "dry":
            tracemalloc.start()
        prober = Prober(win.hwnd if a.mode in ("live", "stop") else None)
        prober.start()
        t0 = time.perf_counter()
        if a.mode == "stop":
            engine.start_in_background()
            time.sleep(min(1.0, max(0.2, n * max(d, 1) / 4000)))
            t_stop = time.perf_counter()
            stop.trigger("measurement")
            engine.wait(30)
            latency = (time.perf_counter() - t_stop) * 1000
            report = engine.report()
            delivered_at_end = len(win.messages)
            time.sleep(0.5)
            print(f"stop: state={report.state.value} reason={report.cancel_reason}, ended {latency:.1f} ms after the stop, "
                  f"completed {report.completed_count}/{n}, delivered after end: {len(win.messages) - delivered_at_end}")
            if report.state.value != "cancelled" or len(win.messages) != delivered_at_end:
                failures.append("stop must cancel and deliver nothing afterwards")
        else:
            report = engine.run()
        elapsed = time.perf_counter() - t0
        prober.done.set()
        prober.join(2)
        ws, peak = memory_mb()
        if a.mode == "dry":
            _, tm_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(f"dry: {report.state.value} {report.completed_count}/{report.actions_total} in {elapsed:.2f}s, "
                  f"messages received {len(win.messages)}, budget used {engine._input.actions_sent}, "
                  f"intended {len(engine._input.intended)}, python peak {tm_peak / 1e6:.0f} MB, "
                  f"working set {base_ws:.0f} -> {ws:.0f} MB (peak {peak:.0f})")
            if win.messages or engine._input.actions_sent:
                failures.append("dry run must deliver nothing and use no budget")
        elif a.mode == "live":
            last, idle = -1, 0
            while idle < 10 and len(win.messages) < n:      # let the window drain its queue (up to ~2 s idle)
                time.sleep(0.2)
                idle = idle + 1 if len(win.messages) == last else 0
                last = len(win.messages)
            got = list(win.messages)
            exact = got == expected(n)
            print(f"live: {report.state.value} {report.completed_count}/{report.actions_total} in {elapsed:.2f}s "
                  f"({report.completed_count / elapsed:.0f} actions/s), window received {len(got)}, exact and in order: {exact}, "
                  f"budget used {engine._input.actions_sent}, working set {base_ws:.0f} -> {ws:.0f} MB (peak {peak:.0f})")
            print("responsiveness:", prober.summary())
            if not exact or report.state.value != "completed":
                failures.append("live run must complete and deliver every action exactly once, in order")
        elif a.mode == "stall":
            f = report.failure
            print(f"stall: {report.state.value} after {report.completed_count}/{n} in {elapsed:.2f}s; "
                  f"failure={None if f is None else (f.code, f.message[:120], f.action_index)}")
            if report.state.value != "failed":
                failures.append("a window that stops reading its queue must make the run FAIL, not complete")
    finally:
        win.close()
    print("RESULT:", "PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
