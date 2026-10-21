"""Live check of condition -> decision -> the existing action path on real LDPlayer instances.

    python tools/decide_smoke.py --index 9 --index 10 --probe     look only: screenshot + OCR, print the plan
    python tools/decide_smoke.py --index 9 --index 10             run every phase (sends ONE tap per target
                                                                   per phase, onto empty home-screen wallpaper)
    ... --from-file <folder>                                       the same, but every plan is first saved as a
                                                                   v2 macro file (+ template asset), loaded back,
                                                                   and only the LOADED steps are run

Per target, in parallel, through the real `LDPlayerServices`:
  1. screenshot                 ObservationContext.capture()
  2. condition                  a pixel condition that is true and one that is false
  3. WAIT UNTIL                 the text label is on the screen (real OCR), bounded
  4. template/text observation  the label itself, cropped from this target's own screen, found again by template
  5. decision -> action         IF the template is found THEN TapMatch(offset) -> create_playback -> preflight ->
                                InputSession -> budget -> stop. The offset puts the tap on empty wallpaper below
                                the label, so the click is real and changes nothing.
  6. serial                     every adb command is recorded; each tap must carry only its own target's serial
  7/8. minimised, then covered  both phases run with the instance windows minimised, then restored without
                                activation and covered by a topmost window this tool owns
  9. foreground / cursor        read before and after each run; they must not change
  10. emergency stop            both targets wait on a condition that never holds; the stop must end both waits
                                promptly and no tap may follow

The tool leaves window state as it found it. Starting or stopping instances and editing their configuration is
NOT done here (see the stage report for how the second instance was prepared and restored).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bootstrap.ldplayer_services import LDPlayerServices  # noqa: E402
from app.config.settings import load_settings  # noqa: E402
from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.net import listening_port_owners  # noqa: E402
from app.core.process import CommandRunner  # noqa: E402
from app.decide import (ConditionalMacro, Condition, Decision, FlowState, TapMatch, WaitUntil, evaluate,  # noqa: E402
                        load_macro_file, run_steps, save_macro_file)
from app.decide.format import asset_dir_for  # noqa: E402
from app.adb.client import AdbClient  # noqa: E402
from app.ldplayer.config_reader import read_instance_config  # noqa: E402
from app.ldplayer.console import LDConsole  # noqa: E402
from app.observe import ObservationContext  # noqa: E402
from app.ocr.winrt import WindowsOcrProvider  # noqa: E402
from app.providers.base import ScreenshotProvider  # noqa: E402
from app.vision.roi import Roi  # noqa: E402

LABEL = "LDStore"
TAP_OFFSET = (0, 150)
user32 = ctypes.windll.user32
SW_SHOWMINNOACTIVE, SW_SHOWNOACTIVATE = 7, 4


class RecordingRunner:
    """The real CommandRunner, plus a thread-safe log of every command line (to prove which serial got what)."""

    def __init__(self, inner: CommandRunner) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, args, **kwargs):
        with self._lock:
            self.calls.append((threading.current_thread().name, tuple(str(a) for a in args)))
        return self._inner.run(args, **kwargs)

    def taps(self) -> list[tuple[str, tuple[str, ...]]]:
        with self._lock:
            return [(t, a) for t, a in self.calls if "input" in a and "tap" in a]


class ServiceScreens(ScreenshotProvider):
    """A ScreenshotProvider over the services' own capture of one verified target (nothing re-implemented)."""

    def __init__(self, services, target) -> None:
        self._services, self._target = services, target

    @property
    def identity(self):
        return self._target.identity

    def capture(self):
        return self._services.capture(self._target)


def build(stop: EmergencyStop):
    settings = load_settings()
    runner = RecordingRunner(CommandRunner(stop))
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s)
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s)
    services = LDPlayerServices(console, adb, lambda i: read_instance_config(settings.ldplayer_dir, i),
                                lambda: listening_port_owners(runner), stop)
    return services, console, runner


def foreground_and_cursor():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return user32.GetForegroundWindow(), (pt.x, pt.y)


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def owner_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def plan_for(context: ObservationContext, ocr):
    """Find the label on this target's own screen; derive the template and the harmless tap point from it."""
    frame = context.capture()
    hit = evaluate(Condition.text(LABEL), context, max_age_s=60)
    if not hit.satisfied:
        raise SystemExit(f"{context.target_id}: '{LABEL}' is not on the screen; put the home screen up first.")
    b = hit.observation.bounds
    template = frame.bitmap.crop(b.x, b.y, b.width, b.height)
    region = Roi(max(0, b.x - 20), max(0, b.y - 20), min(frame.width - max(0, b.x - 20), b.width + 40),
                 min(frame.height - max(0, b.y - 20), b.height + 40))
    center = hit.center
    tap = (center.x + TAP_OFFSET[0], center.y + TAP_OFFSET[1])
    return {"frame": frame, "label_bounds": b, "template": template, "region": region, "tap": tap}


def flow_for(plan):
    label = Condition.text(LABEL, label=f"text {LABEL}")
    found = Condition.template(plan["template"], region=plan["region"], min_confidence=0.95,
                               label=f"template {LABEL}")
    return [WaitUntil(label, timeout_s=20, interval_s=0.5), Decision(found, then=TapMatch(offset=TAP_OFFSET))]


def run_phase(name, services, runner, targets, contexts, plans, stop):
    print(f"\n== phase: {name}")
    before_taps = len(runner.taps())
    fg0, cur0 = foreground_and_cursor()
    reports, errors = {}, []

    def work(target):
        try:
            ctx = contexts[target.target_id]
            shot = ctx.capture()
            pixel = plans[target.target_id]["frame"].bitmap.pixel(20, 20)
            true_c = evaluate(Condition.pixel((20, 20), pixel, tolerance=40), ctx, max_age_s=60)
            false_c = evaluate(Condition.pixel((20, 20), tuple(255 - v for v in pixel), tolerance=10), ctx,
                               max_age_s=60)
            started = time.monotonic()
            steps = plans[target.target_id].get("steps") or flow_for(plans[target.target_id])
            report = run_steps(steps, target=target, context=ctx,
                               create_playback=services.create_playback, stop=stop)
            reports[target.target_id] = (report, time.monotonic() - started, shot.width, shot.height,
                                         true_c.satisfied, false_c.satisfied)
        except BaseException as exc:
            errors.append((target.target_id, exc))

    threads = [threading.Thread(target=work, args=(t,), name=f"flow-{t.target_id}") for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    fg1, cur1 = foreground_and_cursor()
    ok = not errors
    for tid, exc in errors:
        print(f"  {tid}: ERROR {type(exc).__name__}: {exc}")
    for t in targets:
        if t.target_id not in reports:
            continue
        report, took, w, h, pt, pf = reports[t.target_id]
        wait = report.steps[0].wait if report.steps else None
        dec = report.steps[1] if len(report.steps) > 1 else None
        print(f"  {t.target_id} ({t.adb_serial}): screenshot {w}x{h}; pixel true={pt} false-condition={pf}; "
              f"state={report.state.value} in {took:.1f}s")
        if wait:
            print(f"    WAIT UNTIL text: {wait.status.value}, attempts={wait.attempts}, {wait.waited_s:.1f}s")
        if dec:
            d = dec.condition.diagnostics()
            print(f"    IF template: satisfied={d['satisfied']} conf={d['confidence']} center={d['center']} "
                  f"-> branch {dec.branch}; action {dec.action.state.value if dec.action else None} "
                  f"{[s.description for s in dec.action.steps] if dec.action else ''}")
        ok &= report.state is FlowState.COMPLETED and pt and not pf
    new = runner.taps()[before_taps:]
    for t in targets:
        mine = [a for _, a in new if t.adb_serial in a]
        others = [a for _, a in new if t.adb_serial in a and plans[t.target_id]["tap"] != (int(a[-2]), int(a[-1]))]
        print(f"    adb taps to {t.adb_serial}: {[a[-2:] for a in mine]} (expected {plans[t.target_id]['tap']})")
        ok &= len(mine) == 1 and not others
    serials = {t.adb_serial for t in targets}
    stray = [a for _, a in new if not any(s in a for s in serials)]
    ok &= not stray and len(new) == len(targets)
    print(f"  foreground unchanged: {fg0 == fg1}; cursor unchanged: {cur0 == cur1} {cur1}")
    ok &= fg0 == fg1 and cur0 == cur1
    print(f"  phase {name}: {'PASS' if ok else 'FAIL'}")
    return ok


def emergency_phase(services, runner, targets, contexts, stop, stop_steps=None):
    print("\n== phase: emergency stop")
    before_taps = len(runner.taps())
    never = Condition.text("text-that-is-never-on-this-screen-1234", label="never")
    out = {}

    def work(target):
        started = time.monotonic()
        report = run_steps(stop_steps or [WaitUntil(never, timeout_s=60, interval_s=0.5),
                                          Decision(Condition.text(LABEL), then=TapMatch(offset=TAP_OFFSET))],
                           target=target, context=contexts[target.target_id],
                           create_playback=services.create_playback, stop=stop)
        out[target.target_id] = (report, time.monotonic() - started)

    threads = [threading.Thread(target=work, args=(t,), name=f"stop-{t.target_id}") for t in targets]
    for t in threads:
        t.start()
    time.sleep(2.0)
    triggered = time.monotonic()
    stop.trigger("smoke test")
    for t in threads:
        t.join(60)
    ended = time.monotonic() - triggered
    ok = True
    for t in targets:
        report, took = out[t.target_id]
        print(f"  {t.target_id}: state={report.state.value} reason={report.reason} after {took:.1f}s")
        ok &= report.state is FlowState.CANCELLED and report.reason == "emergency_stop"
    new = runner.taps()[before_taps:]
    print(f"  all runs ended {ended:.2f}s after the stop; taps sent: {len(new)}")
    ok &= ended < 5.0 and not new
    stop.reset()
    print(f"  phase emergency stop: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, action="append", required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--save", default="")
    parser.add_argument("--from-file", default="", help="folder: save each plan as a v2 macro there, load it back, "
                                                         "and run only what was loaded")
    args = parser.parse_args()
    stop = EmergencyStop()
    services, console, runner = build(stop)
    entries = {int(e.size.split(",")[0].split()[1]): e for e in services.discover()}
    targets = [services.verify(entries[i]) for i in args.index]
    windows = {i.index: i for i in console.list_instances()}
    ocr = WindowsOcrProvider(CommandRunner(stop))
    contexts = {t.target_id: ObservationContext(ServiceScreens(services, t), stop=stop, ocr=ocr) for t in targets}
    print("targets:", [(t.target_id, t.adb_serial) for t in targets])
    plans = {t.target_id: plan_for(contexts[t.target_id], ocr) for t in targets}
    for t in targets:
        p = plans[t.target_id]
        print(f"  {t.target_id}: label at {p['label_bounds']}, template {p['template'].width}x"
              f"{p['template'].height}, planned tap {p['tap']}")
        if args.save:
            from app.vision.bitmap import encode_png
            Path(args.save, f"{t.target_id}.png").write_bytes(encode_png(p["frame"].bitmap))
    stop_steps = None
    if args.from_file:
        folder = Path(args.from_file)
        folder.mkdir(parents=True, exist_ok=True)
        for t in targets:
            built = ConditionalMacro(f"smoke {t.target_id}", tuple(flow_for(plans[t.target_id])))
            path = save_macro_file(built, folder / f"smoke_{t.target_id}.macro.json", overwrite=True)
            loaded = load_macro_file(path)
            assert loaded == built, "the loaded macro differs from the saved one"
            plans[t.target_id]["steps"] = loaded.steps
            print(f"  {t.target_id}: v2 file {path.name} ({path.stat().st_size} bytes), assets "
                  f"{sorted(a.name for a in asset_dir_for(path).iterdir())}; loaded == saved")
        never = ConditionalMacro("stop test", (
            WaitUntil(Condition.text("text-that-is-never-on-this-screen-1234", label="never"), timeout_s=60,
                      interval_s=0.5),
            Decision(Condition.text(LABEL), then=TapMatch(offset=TAP_OFFSET))))
        stop_steps = load_macro_file(save_macro_file(never, folder / "stop_test.macro.json", overwrite=True)).steps
        print(f"  emergency-stop macro loaded from stop_test.macro.json: {len(stop_steps)} steps")
    if args.probe:
        return 0

    hwnds = [windows[i].top_hwnd for i in args.index]
    was_iconic = {h: bool(user32.IsIconic(h)) for h in hwnds}
    results = []
    try:
        for h in hwnds:
            user32.ShowWindow(h, SW_SHOWMINNOACTIVE)
        time.sleep(0.5)
        print("\nminimised:", {h: bool(user32.IsIconic(h)) for h in hwnds})
        results.append(run_phase("minimised", services, runner, targets, contexts, plans, stop))
        results.append(all(user32.IsIconic(h) for h in hwnds))

        import tkinter as tk
        for h in hwnds:
            user32.ShowWindow(h, SW_SHOWNOACTIVATE)
        time.sleep(0.5)
        rects = [window_rect(h) for h in hwnds]
        cover = tk.Tk()
        cover.overrideredirect(True)
        cover.attributes("-topmost", True)
        left, top = min(r[0] for r in rects), min(r[1] for r in rects)
        right, bottom = max(r[2] for r in rects), max(r[3] for r in rects)
        cover.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
        cover.configure(bg="#303030")
        cover.update()
        time.sleep(0.3)
        cover.update()
        import os
        covered = {}
        for h, r in zip(hwnds, rects):
            at = user32.WindowFromPoint(wt.POINT((r[0] + r[2]) // 2, (r[1] + r[3]) // 2))
            covered[h] = owner_pid(at) == os.getpid()
        print("\ncovered by this tool's window:", covered)
        results.append(all(covered.values()))
        done = threading.Event()
        phase_ok = []
        threading.Thread(target=lambda: (phase_ok.append(run_phase("covered", services, runner, targets, contexts,
                                                                    plans, stop)), done.set())).start()
        while not done.is_set():
            cover.update()
            time.sleep(0.05)
        results.extend(phase_ok)
        cover.destroy()

        results.append(emergency_phase(services, runner, targets, contexts, stop, stop_steps))
    finally:
        for h in hwnds:
            user32.ShowWindow(h, SW_SHOWMINNOACTIVE if was_iconic[h] else SW_SHOWNOACTIVATE)
    print("\nwindow state restored:", {h: bool(user32.IsIconic(h)) == was_iconic[h] for h in hwnds})
    print("\nRESULT:", "PASS" if all(results) else f"FAIL {results}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
