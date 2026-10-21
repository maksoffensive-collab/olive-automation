r"""Controlled real-input smoke test: every Windows input operation, aimed at a window created by this script itself.

It opens a small dedicated window (created here, in this process, not activated), verifies it through the Windows
provider, runs a dry run of every operation (must deliver nothing), then performs each operation once through
`WindowInputSession` and checks that the window received EXACTLY the expected messages, in order, with the expected
client coordinates / key codes / characters. It also checks that out-of-bounds and invalid requests are refused
with nothing delivered, then closes its own window. It aims at no other program.

REAL INPUT: delivery is real, hardware-level `SendInput` (see `app.windows.win32`), not messages posted to a handle.
It MOVES THE REAL MOUSE POINTER over this window, brings the window to the front and gives it keyboard focus; do not
use the mouse or keyboard while it runs. Run it only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_click_smoke.py
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.errors import PlatformError  # noqa: E402
from app.providers.input import Key  # noqa: E402
from app.windows.input import InputRefused, WindowInputSession  # noqa: E402
from app.windows.target import WindowVerifier  # noqa: E402
from app.windows.win32 import _OWN_INPUT, Win32InputBackend  # noqa: E402

WM_DESTROY, WM_CLOSE, WM_PAINT, WM_ERASEBKGND, WM_PRINTCLIENT = 0x0002, 0x0010, 0x000F, 0x0014, 0x0318
CS_VREDRAW, CS_HREDRAW, CS_DBLCLKS = 0x0001, 0x0002, 0x0008
RED, GREEN = 0x0000FF, 0x00FF00                       # COLORREF is 0x00BBGGRR
WM_MOUSEFIRST, WM_MOUSELAST = 0x0200, 0x020E
KEY_MESSAGES = {0x0100: "keydown", 0x0101: "keyup", 0x0102: "char", 0x0104: "syskeydown", 0x0105: "syskeyup"}
MOUSE_MESSAGES = {0x0200: "move", 0x0201: "left_down", 0x0202: "left_up", 0x0203: "left_double", 0x0204: "right_down",
                  0x0205: "right_up"}
WS_OVERLAPPEDWINDOW, SW_SHOWNOACTIVATE = 0x00CF0000, 4
CLASS_NAME = "LDPlatformClickSmokeWindow"
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class TestWindow:
    """A window on its own thread that records every mouse and key message it receives, and whether each carried
    this application's own input mark (`marks`): real input is hardware-level, so the window also gets what the
    person's hand does over it and what Windows itself makes (a pointer move when a window appears under it, the
    move of the pointer to the point of a click) -- `own()` keeps what Olive sent. A pointer MOVE message carries
    no mark (Windows makes it from where the pointer is when the window looks), so moves are told by their place.

    `translate` turns key presses into characters, as an ordinary window's message loop does; `double_clicks` asks
    Windows for double-click messages (CS_DBLCLKS); `marker` paints the client area red with a green rectangle at
    (x, y, w, h) FRACTIONS of the client size, so the picture follows the window's size like a game's does."""

    def __init__(self, *, translate: bool = False, double_clicks: bool = False,
                 marker: tuple[float, float, float, float] | None = None) -> None:
        self.hwnd: int | None = None
        self.messages: list[tuple[str, int, int]] = []
        self.marks: list[bool] = []
        self.translate, self.double_clicks, self.marker = translate, double_clicks, marker
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._error: str | None = None

    def own(self, start: int = 0, *, moves: bool = False) -> list[tuple[str, int, int]]:
        """The messages from `start` on that carried Olive's own mark; pointer moves only when asked for."""
        pairs = zip(self.messages[start:], self.marks[start:])
        return [m for m, own in pairs if own and (moves or m[0] != "move")]

    def marker_rect(self, width: int, height: int) -> tuple[int, int, int, int]:
        """The green rectangle (x, y, w, h) as painted at a client size of `width` x `height`."""
        fx, fy, fw, fh = self.marker
        return int(fx * width), int(fy * height), max(1, int(fw * width)), max(1, int(fh * height))

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10) or self._error or not self.hwnd:
            raise RuntimeError(f"test window could not be created: {self._error}")

    def _run(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u.DefWindowProcW.restype, u.DefWindowProcW.argtypes = LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                                                       wintypes.LPARAM]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HANDLE,
                                      wintypes.HINSTANCE, wintypes.LPVOID]
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.GetMessageExtraInfo.restype = wintypes.LPARAM
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.BeginPaint.restype, u.BeginPaint.argtypes = wintypes.HDC, [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE]
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        g.CreateSolidBrush.restype, g.CreateSolidBrush.argtypes = wintypes.HANDLE, [wintypes.DWORD]
        k.GetModuleHandleW.restype = wintypes.HINSTANCE
        red, green = (g.CreateSolidBrush(RED), g.CreateSolidBrush(GREEN)) if self.marker else (None, None)

        def paint(hwnd, hdc) -> None:
            client = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(client))
            u.FillRect(hdc, ctypes.byref(client), red)
            x, y, w, h = self.marker_rect(client.right, client.bottom)
            u.FillRect(hdc, ctypes.byref(wintypes.RECT(x, y, x + w, y + h)), green)

        def proc(hwnd, msg, wparam, lparam):
            if WM_MOUSEFIRST <= msg <= WM_MOUSELAST or msg in KEY_MESSAGES:
                self.marks.append((u.GetMessageExtraInfo() & 0xFFFFFFFF) == _OWN_INPUT)
            if WM_MOUSEFIRST <= msg <= WM_MOUSELAST:
                self.messages.append((MOUSE_MESSAGES.get(msg, f"mouse-{msg:#x}"), lparam & 0xFFFF, (lparam >> 16) & 0xFFFF))
            elif msg in KEY_MESSAGES:
                self.messages.append((KEY_MESSAGES[msg], int(wparam), int(lparam) & 0xFFFFFFFF))
            elif msg == WM_PAINT and self.marker:
                ps = PAINTSTRUCT()
                hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                paint(hwnd, hdc)
                u.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            elif msg == WM_PRINTCLIENT and self.marker:
                paint(hwnd, wparam)
                return 0
            elif msg == WM_ERASEBKGND and self.marker:
                return 1
            elif msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)  # keep a reference alive
        cls = WNDCLASSW()
        name = CLASS_NAME + ("Dbl" if self.double_clicks else "") + ("Painted" if self.marker else "")
        cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self._proc, k.GetModuleHandleW(None), name
        cls.style = (CS_DBLCLKS if self.double_clicks else 0) | (CS_HREDRAW | CS_VREDRAW if self.marker else 0)
        u.RegisterClassW(ctypes.byref(cls))
        hwnd = u.CreateWindowExW(0, name, "Automation platform click test (safe to ignore)", WS_OVERLAPPEDWINDOW,
                                 200, 200, 320, 240, None, None, cls.hInstance, None)
        if not hwnd:
            self._error = f"CreateWindowExW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        self.hwnd = int(hwnd)
        u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self._ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if self.translate:
                u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.hwnd:
            u = ctypes.WinDLL("user32")
            u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)   # closes ONLY the window this script created
            self._thread.join(5)


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # consistent real pixels
    except Exception:
        pass
    win = TestWindow(translate=True, double_clicks=True)
    win.start()
    time.sleep(0.3)
    ok = False
    try:
        stop = EmergencyStop()
        backend = Win32InputBackend()
        verifier = WindowVerifier(backend, stop)
        target = verifier.verify(win.hwnd)
        size = backend.client_size(win.hwnd)
        print(f"VERIFIED dedicated window: {target.provider_id}:{target.target_id}; client area {size[0]}x{size[1]}")
        x, y = 40, 30

        def operations(s):
            return [("tap", lambda: s.tap(x, y)), ("secondary_tap", lambda: s.secondary_tap(x, y)),
                    ("double_tap", lambda: s.double_tap(x, y)), ("move", lambda: s.move(x + 1, y + 1)),
                    ("button_down", lambda: s.button_down(x + 2, y + 2)), ("button_up", lambda: s.button_up(x + 3, y + 3)),
                    ("key_press", lambda: s.key_press(Key.A)), ("key_down", lambda: s.key_down(Key.SHIFT)),
                    ("key_up", lambda: s.key_up(Key.SHIFT)), ("type_text", lambda: s.type_text("Hi"))]

        dry = WindowInputSession(target, verifier, backend, stop, max_actions=1, dry_run=True)
        for name, run in operations(dry):
            run()
        time.sleep(0.4)
        print(f"DRY RUN: {len(dry.intended)} intended actions, e.g. {dry.intended[0].describe()}; "
              f"window received {len(win.own(moves=True))} message(s) from Olive "
              f"({len(win.messages)} in all: the rest is the pointer's own moves over it)")
        assert win.own(moves=True) == [], "dry run must deliver nothing"

        live = WindowInputSession(target, verifier, backend, stop, max_actions=20)
        expected = {
            "tap": [("left_down", x, y), ("left_up", x, y)],
            "secondary_tap": [("right_down", x, y), ("right_up", x, y)],
            "double_tap": [("left_down", x, y), ("left_up", x, y), ("left_double", x, y), ("left_up", x, y)],
            "move": [("move", x + 1, y + 1)],
            "button_down": [("left_down", x + 2, y + 2)],
            "button_up": [("left_up", x + 3, y + 3)],
            "key_press": [("keydown", 0x41, None), ("keyup", 0x41, None)],
            "key_down": [("keydown", 0x10, None)],
            "key_up": [("keyup", 0x10, None)],
            "type_text": [("char", ord("H"), None), ("char", ord("i"), None)],
        }
        # Real input: the pointer travels to each point (Olive's own move, then the click) and a typed character
        # arrives as a key press the window's loop turns into WM_CHAR -- so moves are compared only for the move
        # itself, and characters only for typed text.
        for name, run in operations(live):
            before = len(win.messages)
            run()
            time.sleep(0.25)
            want = expected[name]
            if name == "move":                   # Windows makes a move message without the event's mark
                got = [m for m in win.messages[before:] if m[0] == "move"]
                good = want[0] in [m[:3] for m in got]
            elif name == "type_text":
                got = [m for m in win.messages[before:] if m[0] == "char"]
                good = [(g[0], g[1]) for g in got] == [(w[0], w[1]) for w in want]
            else:
                got = [m for m in win.own(before) if m[0] != "char"]
                good = len(got) == len(want) and all(g[0] == w[0] and g[1] == w[1] and (w[2] is None or g[2] == w[2])
                                                     for g, w in zip(got, want))
            print(f"  {name:<14} {'OK' if good else 'MISMATCH'}: window received {[(g[0], g[1]) for g in got]}")
            assert good, f"{name}: expected {want}, got {got}"
        print(f"LIVE: {live.actions_sent} actions, {len(win.messages)} messages in total")

        total = len(win.messages)
        refusals = 0
        for label, call in (("out-of-bounds tap", lambda: live.tap(size[0], 0)),
                            ("out-of-bounds move", lambda: live.move(0, -1)),
                            ("float coordinate", lambda: live.tap(1.5, 1)),
                            ("unknown key", lambda: live.key_press("a")),
                            ("control character text", lambda: live.type_text("a\nb")),
                            ("release without press", lambda: live.button_up(1, 1))):
            try:
                call()
                raise AssertionError(f"{label} was not refused")
            except InputRefused as exc:
                refusals += 1
                print(f"  refused ({label}): {exc}")
        time.sleep(0.3)
        assert win.own(total, moves=True) == [], "refused requests must deliver nothing"
        ok = refusals == 6
    except (PlatformError, AssertionError, RuntimeError) as exc:
        print(f"SMOKE TEST FAILED: {type(exc).__name__}: {exc}")
    finally:
        win.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
