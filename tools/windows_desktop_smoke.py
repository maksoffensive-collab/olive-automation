r"""Live check of FULL WINDOWS (the whole desktop, the person's decision of 2026-10-19), aimed only at a window this
script creates IN A CHILD PROCESS -- a run on the whole desktop never acts on Olive's own windows, so the target must
be another program's -- kept on top of every other window at a known place.

  0. The whole desktop captured (the person's real screen, read and dropped: never saved): the picture has the
     screen layout's size, and the test window's green rectangle is in it exactly where the window is.
  1. A click recorded on the desktop: a dry run sends nothing; a live run clicks the test window at that point.
  2. The person takes over by moving the mouse: a run of six clicks is stopped at once after the second, by a move
     made the way a person's hand (or a remote-control tool such as AnyDesk) makes it -- injected, WITHOUT Olive's
     own mark -- so nothing more is clicked.
  3. Esc in the middle of a drag with Shift held: the run stops, and lets go of exactly the button and Shift (they
     are up afterwards, and the window got their release) -- nothing stays pressed.

Before every live run each point the run will touch is checked to be on the test window (`WindowFromPoint`); if one
is not, that part is skipped and nothing is sent. Esc is pressed only while the test window has the keyboard focus.

REAL INPUT anywhere on the screen by construction (Full Windows): the pointer MOVES and the test window comes to the
front; keep your hands off the mouse and keyboard for the minute it takes (moving the mouse would stop the runs --
which is what part 2 checks). Run only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_desktop_smoke.py
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MARKER = (0.25, 0.40, 0.10, 0.08)
CHILD = r"""
import ctypes, ctypes.wintypes as wt, json, sys, time
sys.path.insert(0, sys.argv[1])
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
from windows_click_smoke import TestWindow
w = TestWindow(marker=tuple(json.loads(sys.argv[2])))
w.start()
u = ctypes.windll.user32
u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
u.SetWindowPos(w.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)     # HWND_TOPMOST, no size/move/activate
time.sleep(0.5)
origin, rect = wt.POINT(0, 0), wt.RECT()
u.ClientToScreen(w.hwnd, ctypes.byref(origin))
u.GetClientRect(w.hwnd, ctypes.byref(rect))
print(json.dumps({"hwnd": w.hwnd, "origin": [origin.x, origin.y], "size": [rect.right, rect.bottom]}), flush=True)
for line in sys.stdin:
    if line.strip() == "dump":
        print(json.dumps([list(m) + [k] for m, k in zip(w.messages, w.marks)]), flush=True)
    else:
        break
w.close()
"""

from app.bootstrap.windows_services import FULL_WINDOWS_REF, WindowsServices  # noqa: E402
from app.playback.engine import PlaybackState  # noqa: E402
from app.playback.macro import Macro  # noqa: E402
from app.providers.input import Key  # noqa: E402
from app.providers.recording import PointerButton, RecordedEvent as E, RecordedEventKind as K, Recording  # noqa: E402
from app.vision.frame import Frame  # noqa: E402

LEFT = PointerButton.PRIMARY
VK_SHIFT, VK_ESCAPE, VK_LBUTTON = 0x10, 0x1B, 0x01
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK, KEYEVENTF_KEYUP = 0x0001, 0x8000, 0x4000, 0x0002
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
GA_ROOT = 2


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _UNION)]


u = ctypes.WinDLL("user32", use_last_error=True)
u.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
u.WindowFromPoint.restype, u.WindowFromPoint.argtypes = wintypes.HWND, [wintypes.POINT]
u.GetAncestor.restype, u.GetAncestor.argtypes = wintypes.HWND, [wintypes.HWND, wintypes.UINT]
u.GetForegroundWindow.restype = wintypes.HWND
u.GetAsyncKeyState.restype, u.GetAsyncKeyState.argtypes = ctypes.c_short, [ctypes.c_int]


def as_a_person_moves(screen_x: int, screen_y: int) -> None:
    """The pointer moved to (screen_x, screen_y) the way a person's hand or a remote-control tool moves it: injected
    input WITHOUT Olive's mark (dwExtraInfo 0). Only ever to a point on this script's own test window."""
    vx, vy = u.GetSystemMetrics(SM_XVIRTUALSCREEN), u.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw, vh = u.GetSystemMetrics(SM_CXVIRTUALSCREEN), u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    event = INPUT(INPUT_MOUSE)
    event.u.mi = MOUSEINPUT(round((screen_x - vx) * 65535 / max(1, vw - 1)), round((screen_y - vy) * 65535 / max(1, vh - 1)),
                            0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, None)
    u.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def as_a_person_presses_esc() -> None:
    events = (INPUT * 2)(INPUT(INPUT_KEYBOARD), INPUT(INPUT_KEYBOARD))
    events[0].u.ki = KEYBDINPUT(VK_ESCAPE, 0, 0, 0, None)
    events[1].u.ki = KEYBDINPUT(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0, None)
    u.SendInput(2, events, ctypes.sizeof(INPUT))


def down(vk: int) -> bool:
    return bool(u.GetAsyncKeyState(vk) & 0x8000)


class Child:
    """The test window in its own process: where it is, and what it received (with Olive's mark or not)."""

    def __init__(self) -> None:
        self.process = subprocess.Popen([sys.executable, "-c", CHILD, str(Path(__file__).resolve().parent),
                                         json.dumps(MARKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        info = json.loads(self.process.stdout.readline())
        self.hwnd, self.origin, self.size = info["hwnd"], tuple(info["origin"]), tuple(info["size"])

    def messages(self) -> list[list]:
        self.process.stdin.write("dump\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def own(self, start: int = 0, *, moves: bool = False) -> list[tuple]:
        return [tuple(m[:3]) for m in self.messages()[start:] if m[3] and (moves or m[0] != "move")]

    def on_it(self, screen_x: int, screen_y: int) -> bool:
        hit = u.WindowFromPoint(wintypes.POINT(screen_x, screen_y))
        return bool(hit) and int(u.GetAncestor(hit, GA_ROOT) or 0) == self.hwnd

    def close(self) -> None:
        try:
            self.process.stdin.write("quit\n")
            self.process.stdin.flush()
            self.process.wait(10)
        except Exception:
            self.process.kill()


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
        if not ok:
            failures.append(what)

    child = Child()
    try:
        services = WindowsServices.create_default()           # its own process's windows are never acted on
        target = services.verify(next(e for e in services.discover() if e.ref == FULL_WINDOWS_REF))
        left, top, width, height = target.layout
        ox, oy = child.origin
        print(f"VERIFIED Full Windows {target.target_id}; the test window (pid {child.process.pid}) client at "
              f"({ox}, {oy}), {child.size[0]}x{child.size[1]}")

        def picture_point(cx: int, cy: int) -> tuple[int, int]:
            """A client point of the test window as a pixel of the Full Windows picture."""
            return ox + cx - left, oy + cy - top

        # ---- 0. the whole desktop captured, read and dropped -------------------------------------------------------
        print("0. the whole desktop captured")
        frame = Frame.from_screenshot(services.capture(target))
        rx, ry, rw, rh = (int(MARKER[0] * child.size[0]), int(MARKER[1] * child.size[1]),
                          int(MARKER[2] * child.size[0]), int(MARKER[3] * child.size[1]))
        px, py = picture_point(rx + rw // 2, ry + rh // 2)
        check((frame.width, frame.height) == (width, height) and frame.bitmap.pixel(px, py) == (0, 255, 0),
              f"{frame.width}x{frame.height} (the layout: {width}x{height}); the test window's green rectangle at "
              f"({px}, {py}): {frame.bitmap.pixel(px, py)}")
        del frame                                             # the person's screen is not kept

        # ---- 1. a click ---------------------------------------------------------------------------------------------
        print("1. a click recorded on the desktop")
        a = (60, 50)
        click = Macro(Recording(target.provider_id, target.target_id,
                                (E(K.BUTTON_DOWN, 0, *picture_point(*a), LEFT, 1),
                                 E(K.BUTTON_UP, 40, *picture_point(*a), LEFT, 1))), "desktop click")
        if not child.on_it(ox + a[0], oy + a[1]):
            check(False, "the point is not on the test window: nothing sent")
        else:
            start = len(child.messages())
            report = services.create_playback(click, target, dry_run=True, allow_different_target=False).run()
            time.sleep(0.3)
            check(report.state is PlaybackState.COMPLETED and child.own(start, moves=True) == [],
                  f"dry run: {report.state.value}, nothing sent")
            report = services.create_playback(click, target, dry_run=False, allow_different_target=False).run()
            time.sleep(0.3)
            got = child.own(start)
            check(report.state is PlaybackState.COMPLETED and got == [("left_down", *a), ("left_up", *a)],
                  f"live: {report.state.value}, the test window got {got}")
        services.stop.reset()

        # ---- 2. the person takes over by moving the mouse --------------------------------------------------------
        print("2. the mouse moved by hand stops a run")
        events = []
        for i in range(6):
            events += [E(K.BUTTON_DOWN, 0 if i == 0 else 600, *picture_point(*a), LEFT, 1),
                       E(K.BUTTON_UP, 40, *picture_point(*a), LEFT, 1)]
        six = Macro(Recording(target.provider_id, target.target_id, tuple(events)), "six clicks")
        hand = (a[0] + 60, a[1] + 20)                         # 63 px away: more than a bump of the desk (24 px)
        if not (child.on_it(ox + a[0], oy + a[1]) and child.on_it(ox + hand[0], oy + hand[1])):
            check(False, "a point is not on the test window: nothing sent")
        else:
            start = len(child.messages())
            engine = services.create_playback(six, target, dry_run=False, allow_different_target=False)
            engine.start_in_background()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len([m for m in child.own(start) if m[0] == "left_down"]) < 2:
                time.sleep(0.05)
            as_a_person_moves(ox + hand[0], oy + hand[1])
            engine.wait(10)
            report = engine.report()
            time.sleep(0.3)
            downs = len([m for m in child.own(start) if m[0] == "left_down"])
            check(report.state is PlaybackState.CANCELLED and services.stop.is_set() and 2 <= downs < 6,
                  f"{report.state.value} after {downs} of 6 clicks; the stop: {services.stop.reason!r}")
        services.stop.reset()

        # ---- 3. Esc in the middle of a drag with Shift held: nothing stays pressed -------------------------------
        print("3. Esc in the middle of a drag with Shift held")
        b = (a[0] + 150, a[1] + 60)
        steps = [(a[0] + (b[0] - a[0]) * i // 10, a[1] + (b[1] - a[1]) * i // 10) for i in range(1, 11)]
        events = [E(K.BUTTON_DOWN, 0, *picture_point(*a), LEFT, 1), E(K.BUTTON_UP, 40, *picture_point(*a), LEFT, 1),
                  E(K.KEY_DOWN, 300, key=Key.SHIFT), E(K.BUTTON_DOWN, 200, *picture_point(*a), LEFT, 1)]
        events += [E(K.POINTER_MOVE, 300, *picture_point(*p)) for p in steps]
        events += [E(K.BUTTON_UP, 100, *picture_point(*b), LEFT, 1), E(K.KEY_UP, 50, key=Key.SHIFT)]
        drag = Macro(Recording(target.provider_id, target.target_id, tuple(events)), "shift drag")
        if not all(child.on_it(ox + p[0], oy + p[1]) for p in [a, b] + steps):
            check(False, "a point of the drag is not on the test window: nothing sent")
        elif down(VK_SHIFT) or down(VK_LBUTTON):
            check(False, "Shift or the left button is already down (by hand?): nothing sent")
        else:
            start = len(child.messages())
            engine = services.create_playback(drag, target, dry_run=False, allow_different_target=False)
            engine.start_in_background()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len([m for m in child.own(start) if m[0] == "left_down"]) < 2:
                time.sleep(0.05)
            time.sleep(0.5)                                   # well inside the drag: a few moves made
            focused = int(u.GetForegroundWindow() or 0) == child.hwnd
            if focused:
                as_a_person_presses_esc()
            else:
                services.stop.trigger("the test window does not have the focus: Esc is not pressed anywhere else")
            engine.wait(10)
            report = engine.report()
            time.sleep(0.4)
            got = child.own(start)
            after_esc = got[[i for i, m in enumerate(got) if m[0] == "left_down"][-1] + 1:] if got else []
            check(focused, "the test window had the keyboard focus, so Esc went to it")
            check(report.state is PlaybackState.CANCELLED and services.stop.is_set(),
                  f"{report.state.value}; the stop: {services.stop.reason!r}")
            check(("left_up" in [m[0] for m in after_esc]) and ("keyup", VK_SHIFT) in [m[:2] for m in after_esc],
                  f"let go: {list(report.released_at_end)}; the window got {[m[:2] for m in after_esc]}")
            check(not down(VK_SHIFT) and not down(VK_LBUTTON), "Shift and the left button are up afterwards")
        services.stop.reset()
    except Exception as exc:                      # a failure of the check itself: said, never hidden
        check(False, f"the check could not go on: {type(exc).__name__}: {exc}")
    finally:
        child.close()
    print("PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
