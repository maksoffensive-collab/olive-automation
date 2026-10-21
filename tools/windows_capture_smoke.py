r"""Live check of the ONE coordinate contract between Windows screenshots and Windows input, on a window this script
creates itself (never any other program).

The script opens a small window (not activated) whose client area is painted red with one green marker at a known
CLIENT position, verifies it through the Windows provider and captures it. It then checks that

  * the screenshot has exactly the size of the client area (frame, title bar and borders are not in it), and
  * the green marker is found at exactly the client coordinates where it was painted, i.e. a screenshot pixel (x, y)
    is client coordinate (x, y),
  * a live tap at the marker's centre (taken FROM the screenshot, without conversion) is received by the window at
    those very client coordinates.

The only input is one click on this script's own window -- but it is REAL input: delivery is hardware-level
`SendInput` (see `app.windows.win32`), so the click MOVES THE REAL MOUSE POINTER and brings the window to the front.
Run it only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_capture_smoke.py
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
from app.vision.color import find_color  # noqa: E402
from app.vision.frame import Frame  # noqa: E402
from app.windows.input import WindowInputSession  # noqa: E402
from app.windows.provider import WindowScreenshotProvider  # noqa: E402
from app.windows.target import WindowVerifier  # noqa: E402
from app.windows.win32 import Win32InputBackend  # noqa: E402

WM_DESTROY, WM_CLOSE, WM_PAINT, WM_PRINTCLIENT, WM_ERASEBKGND, WM_LBUTTONDOWN = 0x0002, 0x0010, 0x000F, 0x0318, 0x0014, 0x0201
WS_OVERLAPPEDWINDOW, SW_SHOWNOACTIVATE = 0x00CF0000, 4
RED, GREEN = 0x0000FF, 0x00FF00                       # COLORREF is 0x00BBGGRR
MARKER = (30, 20, 10, 6)                              # x, y, width, height in CLIENT coordinates
CLASS_NAME = "LDPlatformCaptureSmokeWindow"
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


class PaintedWindow:
    def __init__(self) -> None:
        self.hwnd: int | None = None
        self.clicks: list[tuple[int, int]] = []
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10) or self._error or not self.hwnd:
            raise RuntimeError(f"test window could not be created: {self._error}")

    def _run(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
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
        u.UpdateWindow.argtypes = [wintypes.HWND]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.BeginPaint.restype, u.BeginPaint.argtypes = wintypes.HDC, [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE]
        g.CreateSolidBrush.restype, g.CreateSolidBrush.argtypes = wintypes.HANDLE, [wintypes.DWORD]
        k.GetModuleHandleW.restype = wintypes.HINSTANCE
        red, green = g.CreateSolidBrush(RED), g.CreateSolidBrush(GREEN)

        def paint(hwnd, hdc) -> None:
            client = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(client))
            u.FillRect(hdc, ctypes.byref(client), red)
            x, y, w, h = MARKER
            u.FillRect(hdc, ctypes.byref(wintypes.RECT(x, y, x + w, y + h)), green)

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                paint(hwnd, hdc)
                u.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_PRINTCLIENT:
                paint(hwnd, wparam)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_LBUTTONDOWN:
                self.clicks.append((lparam & 0xFFFF, (lparam >> 16) & 0xFFFF))
            elif msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)  # keep a reference alive
        cls = WNDCLASSW()
        cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self._proc, k.GetModuleHandleW(None), CLASS_NAME
        u.RegisterClassW(ctypes.byref(cls))
        hwnd = u.CreateWindowExW(0, CLASS_NAME, "Automation platform capture test (safe to ignore)", WS_OVERLAPPEDWINDOW,
                                 240, 240, 320, 240, None, None, cls.hInstance, None)
        if not hwnd:
            self._error = f"CreateWindowExW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        self.hwnd = int(hwnd)
        u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        u.UpdateWindow(hwnd)
        self._ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.DispatchMessageW(ctypes.byref(msg))

    def close(self) -> None:
        if self.hwnd:
            u = ctypes.WinDLL("user32")
            u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)          # closes ONLY the window this script created
            self._thread.join(5)


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    win = PaintedWindow()
    win.start()
    time.sleep(0.5)
    failures: list[str] = []
    try:
        stop = EmergencyStop()
        backend = Win32InputBackend()
        verifier = WindowVerifier(backend, stop)
        target = verifier.verify(win.hwnd)
        info = backend.query(win.hwnd)
        client = backend.client_size(win.hwnd)
        print(f"VERIFIED dedicated window {target.provider_id}:{target.target_id}")
        print(f"  window {info.width}x{info.height}, client {client[0]}x{client[1]} at offset "
              f"({info.client_left}, {info.client_top}), dpi ambiguous: {info.dpi_ambiguous}")
        frame = Frame.from_screenshot(WindowScreenshotProvider(target, verifier, stop).capture())
        print(f"  screenshot {frame.width}x{frame.height}")
        if (frame.width, frame.height) != client:
            failures.append(f"screenshot {frame.width}x{frame.height} is not the client size {client}")
        found = find_color(frame, (0, 255, 0), tolerance=8)
        x, y, w, h = MARKER
        print(f"  green marker painted at client {MARKER}; found in the screenshot at {found.bounds}")
        if found.bounds is None or (found.bounds.x, found.bounds.y, found.bounds.width, found.bounds.height) != MARKER:
            failures.append("the marker is not at its client coordinates in the screenshot")
        else:
            cx, cy = found.bounds.x + found.bounds.width // 2, found.bounds.y + found.bounds.height // 2
            WindowInputSession(target, verifier, backend, stop, max_actions=1).tap(cx, cy)
            time.sleep(0.3)
            print(f"  tap at screenshot point ({cx}, {cy}) was received at client {win.clicks}")
            if win.clicks != [(cx, cy)]:
                failures.append(f"the tap at ({cx}, {cy}) arrived as {win.clicks}")
    except Exception as exc:  # noqa: BLE001 - a smoke test reports everything
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        win.close()
    print("PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
