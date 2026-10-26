r"""Live check of ADAPTING TO THE SIZE (the person's decisions of 2026-10-20), aimed only at a window this script
creates, whose picture follows its size the way a game's does: red, with a green rectangle at a fixed FRACTION of
the client area (`MARKER`).

  1. A recorded macro made at 300 x 200 with "Adapt to the size": after the window is made 450 x 300 (the same
     proportions) its click lands where the same place now is (x 1.5); "as is", where it was recorded; at 600 x 200
     (other proportions) it is refused before anything is sent; minimised (no size to read), refused too.
  2. A script made at 300 x 200 -- a picture of the rectangle cut from a REAL screenshot, as the editor's picker cuts
     it, looked for in an area around it, and a click beside it: a dry run sends nothing; at 450 x 300 it finds the
     rectangle and clicks beside it at 1.5 times the offset; at 600 x 200 it is refused before it clicks (a click
     beside a match needs the same proportions; since 2026-10-25 a look alone follows any proportions).
  3. "In any size" (needs the optional fast search, OpenCV): the same picture in a script that runs as is, found
     at 450 x 300 at 150 %, and clicked in its middle.

What Olive sent is told apart from what the person's hand does over the window by Olive's own mark on its input
(`TestWindow.own`), so moving the mouse does not spoil the comparison.

REAL INPUT: the clicks are hardware-level `SendInput` on this script's own window: the pointer MOVES and the window
comes to the front. Run only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_size_smoke.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_click_smoke import TestWindow  # noqa: E402

from app.bootstrap.windows_services import WindowsServices  # noqa: E402
from app.decide import Condition, Decision, FlowState, TapMatch  # noqa: E402
from app.decide.format import ConditionalMacro  # noqa: E402
from app.gui.flows import FlowJob  # noqa: E402
from app.observe import TemplateQuery  # noqa: E402
from app.playback.engine import PlaybackRefused, PlaybackState  # noqa: E402
from app.playback.macro import Macro  # noqa: E402
from app.providers.recording import PointerButton, RecordedEvent as E, RecordedEventKind as K, Recording  # noqa: E402
from app.vision.frame import Frame  # noqa: E402
from app.vision.roi import Roi  # noqa: E402
from app.vision.template import fast_matcher  # noqa: E402
from app.windows.win32 import Win32InputBackend, Win32ObservationBackend  # noqa: E402

MARKER = (0.25, 0.40, 0.10, 0.08)          # the green rectangle: x, y, width, height as fractions of the client area
SMALL, LARGE, WIDE = (300, 200), (450, 300), (600, 200)
MARGIN = 4                                 # red around the rectangle in the picture, as a person cuts it
OFFSET = (20, 10)                          # the click beside the match, at the script's own size
SW_MINIMIZE, SW_SHOWNOACTIVATE = 6, 4
SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010

u = ctypes.WinDLL("user32", use_last_error=True)
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           wintypes.UINT]
u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


def set_client(hwnd: int, width: int, height: int) -> None:
    """Make THIS script's window's client area exactly `width` x `height` (its frame added back)."""
    for _ in range(4):
        cw, ch = client_size(hwnd)
        if (cw, ch) == (width, height):
            return
        outer = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(outer))
        u.SetWindowPos(hwnd, None, 0, 0, outer.right - outer.left + width - cw, outer.bottom - outer.top + height - ch,
                       SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.3)                                  # repainted at the new size
    raise RuntimeError(f"the test window's client area could not be made {width}x{height}: {client_size(hwnd)}")


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # real pixels, as the application
    except Exception:
        pass
    win = TestWindow(marker=MARKER)
    win.start()
    time.sleep(0.3)
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
        if not ok:
            failures.append(what)

    def clicks(start: int) -> list[tuple[str, int, int]]:
        time.sleep(0.3)
        return [m[:3] for m in win.own(start)]           # what Olive pressed, where (its moves left out)

    try:
        set_client(win.hwnd, *SMALL)
        services = WindowsServices(Win32InputBackend(), Win32ObservationBackend, own_pid=-1)   # its own window listed
        entry = next(e for e in services.discover() if e.ref == f"w{win.hwnd}")
        target = services.verify(entry)
        print(f"VERIFIED its own window {target.target_id}, client {client_size(win.hwnd)}")

        # ---- 1. a recorded macro -------------------------------------------------------------------------------
        print("1. a recorded macro made at 300x200")
        x, y = 100, 80
        recording = Recording(target.provider_id, target.target_id,
                              (E(K.BUTTON_DOWN, 0, x, y, PointerButton.PRIMARY, 1),
                               E(K.BUTTON_UP, 40, x, y, PointerButton.PRIMARY, 1)), size=SMALL)
        adapting, as_is = Macro(recording, "adapts", adapt=True), Macro(recording, "as is")

        def play(macro: Macro, *, dry: bool = False):
            start = len(win.messages)
            report = services.create_playback(macro, target, dry_run=dry, allow_different_target=False).run()
            return report, clicks(start)

        set_client(win.hwnd, *LARGE)
        report, got = play(adapting, dry=True)
        check(report.state is PlaybackState.COMPLETED and got == [], f"dry run at 450x300: {report.state.value}, "
                                                                     f"nothing sent ({got})")
        report, got = play(adapting)
        want = [("left_down", 150, 120), ("left_up", 150, 120)]
        check(report.state is PlaybackState.COMPLETED and got == want, f"adapting, at 450x300: clicked {got}")
        report, got = play(as_is)
        want = [("left_down", x, y), ("left_up", x, y)]
        check(report.state is PlaybackState.COMPLETED and got == want, f"as is, at 450x300: clicked {got}")

        for label, prepare in (("600x200, other proportions", lambda: set_client(win.hwnd, *WIDE)),
                               ("minimised", lambda: (u.ShowWindow(win.hwnd, SW_MINIMIZE), time.sleep(0.5)))):
            prepare()
            start = len(win.messages)
            try:
                services.create_playback(adapting, target, dry_run=False, allow_different_target=False).run()
                check(False, f"{label}: must be refused")
            except PlaybackRefused as exc:
                codes = [i.code for i in exc.issues]
                check(clicks(start) == [], f"{label}: refused before anything was sent {codes}")
        u.ShowWindow(win.hwnd, SW_SHOWNOACTIVATE)
        time.sleep(0.5)

        # ---- 2. a script -----------------------------------------------------------------------------------------
        print("2. a script made at 300x200: a picture of the rectangle, a click beside it")
        set_client(win.hwnd, *SMALL)
        frame = Frame.from_screenshot(services.capture(target))    # a real screenshot, as the editor's picker takes
        mx, my, mw, mh = win.marker_rect(*SMALL)
        picture = frame.bitmap.crop(mx - MARGIN, my - MARGIN, mw + 2 * MARGIN, mh + 2 * MARGIN)
        area = Roi(mx - 20, my - 20, mw + 40, mh + 40)
        script = ConditionalMacro("size smoke", (Decision(Condition(TemplateQuery(picture, region=area)),
                                                          (TapMatch(offset=OFFSET),)),), size=SMALL)
        print(f"  the picture: {picture.width}x{picture.height} cut at ({mx - MARGIN}, {my - MARGIN}); the script "
              f"adapts: {script.adapt}")

        def run(flow: ConditionalMacro, *, dry: bool = False):
            start = len(win.messages)
            job = FlowJob(flow, target, services, ocr=None, dry_run=dry, allow_different_target=False)
            job.start_in_background()
            job.wait(60)
            return job.report, clicks(start)

        set_client(win.hwnd, *LARGE)
        report, got = run(script, dry=True)
        check(report is not None and report.state is FlowState.COMPLETED and got == [],
              f"dry run at 450x300: {report and report.state.value}, nothing sent ({got})")
        report, got = run(script)
        seen = report.steps[0].condition.observation if report is not None and report.steps else None
        if seen is not None and seen.center is not None:
            cx, cy = seen.center.x + round(OFFSET[0] * 1.5), seen.center.y + round(OFFSET[1] * 1.5)
            bounds = seen.matches[0].bounds
            rx, ry, rw, rh = win.marker_rect(*LARGE)
            check(bounds.x <= rx and bounds.y <= ry and bounds.right >= rx + rw and bounds.bottom >= ry + rh,
                  f"at 450x300 the picture was found around the rectangle: {bounds}, confidence "
                  f"{seen.matches[0].confidence:.3f}")
            check(report.state is FlowState.COMPLETED and got == [("left_down", cx, cy), ("left_up", cx, cy)],
                  f"and the click beside it moved 1.5 times its offset: {got}, expected ({cx}, {cy})")
        else:
            check(False, f"at 450x300 the picture must be found: {report and (report.state.value, report.message)}")

        set_client(win.hwnd, *WIDE)
        report, got = run(script)
        check(report is not None and report.state is FlowState.FAILED and report.reason == "size_proportions_changed"
              and got == [], f"at 600x200: {report and (report.state.value, report.reason)}, nothing sent ({got})")

        # ---- 3. in any size --------------------------------------------------------------------------------------
        print("3. in any size: the same picture, the script as is")
        if fast_matcher() is None:
            print("  skipped: the optional fast search (OpenCV) is not installed")
        else:
            set_client(win.hwnd, *LARGE)
            any_size = ConditionalMacro("any size smoke", (Decision(Condition(TemplateQuery(picture, any_size=True)),
                                                                    (TapMatch(),)),), size=SMALL, adapt=False)
            report, got = run(any_size)
            seen = report.steps[0].condition.observation if report is not None and report.steps else None
            if seen is not None and seen.center is not None:
                c = seen.center
                check(report.state is FlowState.COMPLETED and got == [("left_down", c.x, c.y), ("left_up", c.x, c.y)]
                      and seen.detail.get("scale") == 1.5,
                      f"found at {seen.detail.get('scale')} of its size, clicked {got} (its middle: ({c.x}, {c.y}))")
            else:
                check(False, f"in any size it must be found: {report and (report.state.value, report.message)}")
    except Exception as exc:                     # a failure of the check itself: said, never hidden
        check(False, f"the check could not go on: {type(exc).__name__}: {exc}")
    finally:
        win.close()
    print("PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
