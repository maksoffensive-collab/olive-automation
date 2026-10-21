"""Win32 implementation of `WindowBackend` (ctypes, standard library only). The ONLY module with Windows API calls.

Read-only by construction: it lists windows, reads their attributes and copies their pixels with `PrintWindow`.
It never sends input, changes focus or position, closes, or launches anything.

Coordinate contract (shared by screenshots and input): pixel (0, 0) is the top-left pixel of the window's CLIENT
area; the frame, title bar and borders are not part of it. A capture renders the whole window with `PrintWindow`
and keeps only the client area, so screenshot pixel (x, y) is exactly client coordinate (x, y). Sizes and offsets are
read with a per-monitor-aware thread DPI context that is restored after every call (the process-wide DPI setting is
not changed), i.e. they are physical pixels. That is only a meaningful scale for the target if the target itself is
DPI-aware for its monitor; for a DPI-unaware (or mismatched system-aware) window on a scaled monitor the scale is
ambiguous, `WindowInfo.dpi_ambiguous` is set, and capture and input are refused. Not verified beyond 96 dpi.

`Win32InputBackend` adds the input primitives of this provider. Every MOUSE and KEYBOARD event is delivered as
real, hardware-level input (`SendInput`) -- universally, for every window, not gated by an app's UI technology
or window class (see `Win32InputBackend`'s own docstring for the two successive, explicit user decisions that
got here: mouse first, once a per-app-class allowlist proved unable to cover "any Windows program" the way a
real click, indistinguishable from a person's own, naturally does; keyboard once posted key messages proved
unable to reach a program that reads the keyboard directly or needs a modifier combination). A MOUSE event's
real receiving window is found with `WindowFromPoint`, the OS's own hit-test, immediately before every send,
after the target is raised to the top of the Z order without being activated (`SetWindowPos` with
`SWP_NOACTIVATE`); if anything other than the verified window or a genuine descendant of it (`IsChild`-checked)
is found there, the click is refused (`InputDeliveryError`) rather than ever sent to whatever else is on screen
-- with a real click there is no safe way to address an occluded window at all. A KEY or character event needs
the target to hold real system focus, not just be on screen, so the verified window is brought to the foreground
first if it is not already (`SetForegroundWindow` -- the one API in this module that changes what has focus or
is the foreground window, used only here, and re-checked immediately afterwards: refused, rather than ever sent
to whatever else has focus, if Windows did not actually let it become the foreground window). This module never
addresses input by moving the real pointer independently of a click or by targeting the "active"/"foreground"
window as a blind fallback -- every real event is still computed from the SAME verified window's own geometry
or handle, never a global one. The one exception presses nothing: letting go at the end of a live run
(`release_primary`, `release_secondary`, `release_key`, the person's decision of 2026-10-20) sends a button-up
where the pointer is and a key-up to whatever has the focus, for what that run's session itself pressed.

THE WHOLE DESKTOP ("Full Windows", on the person's explicit decision of 2026-10-19): `desktop_mouse`,
`desktop_key` and `desktop_char` send the same real events through the same three senders, at a screen point or to
whatever holds the keyboard focus -- with no window to verify. They are used only by `DesktopInputSession`
(app/windows/desktop.py), which refuses a changed screen layout, a point on this application's own windows and keys
while this application has the focus, and runs only while the person's Esc or own mouse stops it. Every event this
module sends carries `_OWN_INPUT` in its extra information, so the observation hooks can tell this application's
input from a person's -- which matters over remote control, where Windows flags the person's input as injected too.

`Win32ObservationBackend` is the recorder's read-only observation layer: low-level mouse and keyboard hooks that only
report what the user does. Its hook procedures always pass every event on unchanged (never block, alter or
inject anything), and it changes no focus, window or desktop state.
"""
from __future__ import annotations

import contextlib
import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes

from app.providers.input import Key
from app.windows import keymap
from app.windows.backend import (DESKTOP_HWND, DesktopCapture, DoubleClickSettings, InputDeliveryError, KeyEvent, KeyObservation,
                                 MouseEvent,
                                 MouseObservation, MouseObservationKind, RawBitmap, WindowBackend, WindowCaptureError,
                                 WindowInfo, WindowInputBackend, WindowObservationBackend, crop_bgra)

_GA_ROOT, _GA_ROOTOWNER = 2, 3
_DWMWA_CLOAKED = 13
_PW_RENDERFULLCONTENT = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DPI_PER_MONITOR_AWARE_V2 = -4
_MAX_SIDE = 16384
_MAX_TITLE = 1024


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class Win32Backend(WindowBackend):
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowCaptureError("The Win32 backend is only available on Windows.")
        H = wintypes.HWND
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        d = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, H, wintypes.LPARAM)
        self._monitor_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.HDC,
                                                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def sig(fn, res, *args):
            fn.restype, fn.argtypes = res, list(args)
            return fn

        self._EnumWindows = sig(u.EnumWindows, wintypes.BOOL, self._enum_proc, wintypes.LPARAM)
        self._EnumChildWindows = sig(u.EnumChildWindows, wintypes.BOOL, H, self._enum_proc, wintypes.LPARAM)
        self._IsChild = sig(u.IsChild, wintypes.BOOL, H, H)
        self._ScreenToClientBase = sig(u.ScreenToClient, wintypes.BOOL, H, ctypes.POINTER(wintypes.POINT))
        self._IsWindow = sig(u.IsWindow, wintypes.BOOL, H)
        self._IsWindowVisible = sig(u.IsWindowVisible, wintypes.BOOL, H)
        self._IsIconic = sig(u.IsIconic, wintypes.BOOL, H)
        self._IsWindowEnabled = sig(u.IsWindowEnabled, wintypes.BOOL, H)
        self._GetAncestor = sig(u.GetAncestor, H, H, wintypes.UINT)
        self._GetTextW = sig(u.GetWindowTextW, ctypes.c_int, H, wintypes.LPWSTR, ctypes.c_int)
        self._GetClassW = sig(u.GetClassNameW, ctypes.c_int, H, wintypes.LPWSTR, ctypes.c_int)
        self._GetPid = sig(u.GetWindowThreadProcessId, wintypes.DWORD, H, ctypes.POINTER(wintypes.DWORD))
        self._GetRect = sig(u.GetWindowRect, wintypes.BOOL, H, ctypes.POINTER(wintypes.RECT))
        self._GetClientRectBase = sig(u.GetClientRect, wintypes.BOOL, H, ctypes.POINTER(wintypes.RECT))
        self._ClientToScreenBase = sig(u.ClientToScreen, wintypes.BOOL, H, ctypes.POINTER(wintypes.POINT))
        self._MonitorFromWindow = sig(u.MonitorFromWindow, ctypes.c_void_p, H, wintypes.DWORD)
        self._EnumDisplayMonitors = sig(u.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC,
                                        ctypes.POINTER(wintypes.RECT), self._monitor_proc, wintypes.LPARAM)
        self._GetConsoleWindow = sig(k.GetConsoleWindow, H)
        self._GetDpiForSystem = getattr(u, "GetDpiForSystem", None)
        if self._GetDpiForSystem is not None:
            sig(self._GetDpiForSystem, wintypes.UINT)
        self._GetWindowDpiCtx = getattr(u, "GetWindowDpiAwarenessContext", None)
        self._GetAwarenessFromCtx = getattr(u, "GetAwarenessFromDpiAwarenessContext", None)
        if self._GetWindowDpiCtx is not None and self._GetAwarenessFromCtx is not None:
            sig(self._GetWindowDpiCtx, ctypes.c_void_p, H)
            sig(self._GetAwarenessFromCtx, ctypes.c_int, ctypes.c_void_p)
        try:
            self._GetDpiForMonitor = ctypes.WinDLL("shcore").GetDpiForMonitor
            sig(self._GetDpiForMonitor, ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.UINT))
        except (OSError, AttributeError):
            self._GetDpiForMonitor = None
        self._GetWindowDC = sig(u.GetWindowDC, wintypes.HDC, H)
        self._ReleaseDC = sig(u.ReleaseDC, ctypes.c_int, H, wintypes.HDC)
        self._PrintWindow = sig(u.PrintWindow, wintypes.BOOL, H, wintypes.HDC, wintypes.UINT)
        self._SetThreadDpi = getattr(u, "SetThreadDpiAwarenessContext", None)
        if self._SetThreadDpi is not None:
            sig(self._SetThreadDpi, ctypes.c_void_p, ctypes.c_void_p)
        self._CreateCompatibleDC = sig(g.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
        self._CreateCompatibleBitmap = sig(g.CreateCompatibleBitmap, wintypes.HBITMAP, wintypes.HDC, ctypes.c_int,
                                           ctypes.c_int)
        self._SelectObject = sig(g.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
        self._DeleteObject = sig(g.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
        self._DeleteDC = sig(g.DeleteDC, wintypes.BOOL, wintypes.HDC)
        self._GetDIBits = sig(g.GetDIBits, ctypes.c_int, wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT)
        self._OpenProcess = sig(k.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._CloseHandle = sig(k.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
        self._GetProcessTimes = sig(k.GetProcessTimes, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME))
        self._QueryImageName = sig(k.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self._DwmGetWindowAttribute = sig(d.DwmGetWindowAttribute, ctypes.c_long, H, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD)
        self._GetDC = sig(u.GetDC, wintypes.HDC, H)
        self._ScreenMetric = sig(u.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
        self._BitBlt = sig(g.BitBlt, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)

    @contextlib.contextmanager
    def _dpi_aware(self):
        previous = None
        if self._SetThreadDpi is not None:
            previous = self._SetThreadDpi(ctypes.c_void_p(_DPI_PER_MONITOR_AWARE_V2))
        try:
            yield
        finally:
            if previous:
                self._SetThreadDpi(ctypes.c_void_p(previous))

    # ---- queries -----------------------------------------------------------
    def _process_info(self, pid: int) -> tuple[int, str]:
        """(creation time in 100 ns ticks, executable file name); (0, "") when it cannot be read."""
        handle = self._OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 0, ""
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            start = 0
            if self._GetProcessTimes(handle, created, exited, kernel, user):
                start = (created.dwHighDateTime << 32) | created.dwLowDateTime
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            name = ""
            if self._QueryImageName(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
            return start, name
        finally:
            self._CloseHandle(handle)

    def _dpi_ambiguous(self, hwnd: int) -> bool:
        """True if the window's coordinate scale cannot be established (see the module docstring). At 96 dpi
        nothing is ambiguous. Any failure to read the scale on a scaled system counts as ambiguous (fail closed)."""
        try:
            monitor_dpi = system_dpi = 96
            if self._GetDpiForSystem is not None:
                system_dpi = int(self._GetDpiForSystem())
                monitor_dpi = system_dpi
            if self._GetDpiForMonitor is not None:
                x, y = wintypes.UINT(0), wintypes.UINT(0)
                monitor = self._MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
                if monitor and self._GetDpiForMonitor(monitor, 0, ctypes.byref(x), ctypes.byref(y)) == 0 and x.value:
                    monitor_dpi = int(x.value)
            if monitor_dpi == 96 and system_dpi == 96:
                return False
            if self._GetWindowDpiCtx is None or self._GetAwarenessFromCtx is None:
                return True
            awareness = int(self._GetAwarenessFromCtx(self._GetWindowDpiCtx(hwnd)))
            if awareness == 2:                                       # per-monitor aware: real pixels of its monitor
                return False
            if awareness == 1:                                       # system aware: real pixels only at system scale
                return monitor_dpi != system_dpi
            return True                                              # unaware / unknown on a scaled monitor
        except Exception:
            return True

    def _client_geometry(self, hwnd: int, window_rect) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the client area; the offsets are relative to the window rectangle."""
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        if not self._GetClientRectBase(hwnd, ctypes.byref(rect)) or not self._ClientToScreenBase(hwnd, ctypes.byref(origin)):
            return 0, 0, 0, 0
        return (int(origin.x) - int(window_rect.left), int(origin.y) - int(window_rect.top),
                max(0, int(rect.right - rect.left)), max(0, int(rect.bottom - rect.top)))

    def _info(self, hwnd: int) -> WindowInfo | None:
        if not self._IsWindow(hwnd):
            return None
        title_buf = ctypes.create_unicode_buffer(_MAX_TITLE)
        self._GetTextW(hwnd, title_buf, _MAX_TITLE)
        class_buf = ctypes.create_unicode_buffer(256)
        self._GetClassW(hwnd, class_buf, 256)
        pid = wintypes.DWORD(0)
        self._GetPid(hwnd, ctypes.byref(pid))
        start, name = self._process_info(pid.value) if pid.value else (0, "")
        rect = wintypes.RECT()
        ok = self._GetRect(hwnd, ctypes.byref(rect))
        cloaked = wintypes.DWORD(0)
        if self._DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0:
            cloaked = wintypes.DWORD(0)
        root = self._GetAncestor(hwnd, _GA_ROOT)
        cl, ct, cw, ch = self._client_geometry(hwnd, rect) if ok else (0, 0, 0, 0)
        return WindowInfo(
            hwnd=int(hwnd), title=title_buf.value, class_name=class_buf.value, pid=int(pid.value), process_start=start,
            process_name=name, top_level=(root is not None and int(root) == int(hwnd)),
            visible=bool(self._IsWindowVisible(hwnd)), minimized=bool(self._IsIconic(hwnd)),
            cloaked=bool(cloaked.value), width=max(0, rect.right - rect.left) if ok else 0,
            height=max(0, rect.bottom - rect.top) if ok else 0, enabled=bool(self._IsWindowEnabled(hwnd)),
            client_width=cw, client_height=ch, client_left=cl, client_top=ct, dpi_ambiguous=self._dpi_ambiguous(hwnd))

    def query(self, hwnd: int) -> WindowInfo | None:
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or not 0 < hwnd <= 0xFFFFFFFF:
            return None
        with self._dpi_aware():
            return self._info(hwnd)

    def enumerate(self) -> list[WindowInfo]:
        found: list[int] = []

        def callback(hwnd, _lparam):
            if hwnd and self._IsWindowVisible(hwnd):
                found.append(int(hwnd))
            return True

        with self._dpi_aware():
            self._EnumWindows(self._enum_proc(callback), 0)
            infos = [self._info(h) for h in found]
        return [i for i in infos if i and i.top_level and not i.cloaked and i.width > 0 and i.height > 0]

    def virtual_screen(self) -> tuple[int, int, int, int]:
        with self._dpi_aware():
            return (int(self._ScreenMetric(_SM_XVIRTUALSCREEN)), int(self._ScreenMetric(_SM_YVIRTUALSCREEN)),
                    int(self._ScreenMetric(_SM_CXVIRTUALSCREEN)), int(self._ScreenMetric(_SM_CYVIRTUALSCREEN)))

    def owner_pid(self, hwnd: int) -> int | None:
        if not _valid_handle(hwnd) or not self._IsWindow(hwnd):
            return None
        pid = wintypes.DWORD(0)
        self._GetPid(hwnd, ctypes.byref(pid))
        return int(pid.value) or None

    def monitors(self) -> tuple[tuple[int, int, int, int], ...]:
        found: list[tuple[int, int, int, int]] = []

        def callback(_monitor, _dc, rect, _data):
            r = rect.contents
            found.append((int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)))
            return True

        with self._dpi_aware():
            listed = self._EnumDisplayMonitors(None, None, self._monitor_proc(callback), 0)
        if not listed or not found:
            raise WindowCaptureError("The monitors cannot be listed.")
        return tuple(sorted(found))

    def console_windows(self) -> tuple[int, ...]:
        """The console window itself (the classic console host) and the window that owns it (a terminal such as
        Windows Terminal sets itself as the owner of its hidden console window -- [ASSUMPTION], by observation of
        recent Windows versions, not by documentation)."""
        console = self._GetConsoleWindow()
        if not console:
            return ()
        owner = self._GetAncestor(console, _GA_ROOTOWNER)
        return tuple(sorted({int(console)} | ({int(owner)} if owner else set())))

    # ---- pixels ------------------------------------------------------------
    def capture_desktop(self, max_side: int) -> DesktopCapture:
        """Every monitor in one picture: the virtual screen, copied from the screen's own drawing surface (read
        only -- nothing is moved, focused or clicked). Physical pixels: the thread is per-monitor DPI aware while it
        reads the size and the pixels. A desktop wider or taller than `max_side` is refused before any device
        context, bitmap or buffer is taken: three 4K monitors side by side would cost hundreds of megabytes only to
        be discarded."""
        with self._dpi_aware():
            left, top = int(self._ScreenMetric(_SM_XVIRTUALSCREEN)), int(self._ScreenMetric(_SM_YVIRTUALSCREEN))
            width, height = int(self._ScreenMetric(_SM_CXVIRTUALSCREEN)), int(self._ScreenMetric(_SM_CYVIRTUALSCREEN))
            limit = min(max_side, _MAX_SIDE)
            if not (0 < width <= limit and 0 < height <= limit):
                raise WindowCaptureError(f"The desktop size ({width} x {height}) is not supported for capture: at "
                                         f"most {limit} pixels a side.")
            screen_dc = self._GetDC(None)
            if not screen_dc:
                raise WindowCaptureError("The desktop's drawing surface is not available.")
            mem_dc = bitmap = old = None
            try:
                mem_dc = self._CreateCompatibleDC(screen_dc)
                bitmap = self._CreateCompatibleBitmap(screen_dc, width, height)
                if not mem_dc or not bitmap:
                    raise WindowCaptureError("Could not allocate an off-screen bitmap.")
                old = self._SelectObject(mem_dc, bitmap)
                if not old:
                    raise WindowCaptureError("Could not prepare the off-screen bitmap.")
                copied = self._BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, _SRCCOPY | _CAPTUREBLT)
                self._SelectObject(mem_dc, old)          # GetDIBits reads a bitmap that is in no device context
                old = None
                if not copied:
                    raise WindowCaptureError("The desktop could not be copied.")
                info = _BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height          # negative: top-down
                info.bmiHeader.biPlanes, info.bmiHeader.biBitCount, info.bmiHeader.biCompression = 1, 32, 0
                buf = ctypes.create_string_buffer(width * height * 4)
                if not self._GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(info), 0):
                    raise WindowCaptureError("Could not read the desktop's pixels.")
                return DesktopCapture(left, top, RawBitmap(width, height, buf.raw))
            finally:
                if old:
                    self._SelectObject(mem_dc, old)
                if bitmap:
                    self._DeleteObject(bitmap)
                if mem_dc:
                    self._DeleteDC(mem_dc)
                self._ReleaseDC(None, screen_dc)

    def capture(self, hwnd: int, width: int, height: int) -> RawBitmap:
        """The CLIENT area (currently `width` x `height`) as pixels: pixel (x, y) is client coordinate (x, y)."""
        if not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
            raise WindowCaptureError("Unsupported window size.")
        with self._dpi_aware():
            window_rect = wintypes.RECT()
            if not self._IsWindow(hwnd) or not self._GetRect(hwnd, ctypes.byref(window_rect)):
                raise WindowCaptureError("The window rectangle is not available.")
            left, top, client_w, client_h = self._client_geometry(hwnd, window_rect)
            if (client_w, client_h) != (width, height):
                raise WindowCaptureError("The client area changed size before it could be captured; discarded.")
            width_w = max(0, int(window_rect.right - window_rect.left))
            height_w = max(0, int(window_rect.bottom - window_rect.top))
            if not (0 < width_w <= _MAX_SIDE and 0 < height_w <= _MAX_SIDE):
                raise WindowCaptureError("Unsupported window size.")
            window_dc = self._GetWindowDC(hwnd)
            if not window_dc:
                raise WindowCaptureError("The window's drawing surface is not available.")
            mem_dc = bitmap = old = None
            try:
                mem_dc = self._CreateCompatibleDC(window_dc)
                bitmap = self._CreateCompatibleBitmap(window_dc, width_w, height_w)
                if not mem_dc or not bitmap:
                    raise WindowCaptureError("Could not allocate an off-screen bitmap.")
                old = self._SelectObject(mem_dc, bitmap)
                if not self._PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT):
                    raise WindowCaptureError("The window refused to render itself (PrintWindow failed).")
                info = _BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                info.bmiHeader.biWidth, info.bmiHeader.biHeight = width_w, -height_w  # negative: top-down
                info.bmiHeader.biPlanes, info.bmiHeader.biBitCount, info.bmiHeader.biCompression = 1, 32, 0
                buf = ctypes.create_string_buffer(width_w * height_w * 4)
                if not self._GetDIBits(mem_dc, bitmap, 0, height_w, buf, ctypes.byref(info), 0):
                    raise WindowCaptureError("Could not read the rendered pixels.")
                # the whole window was rendered (frame and title bar included); only the client area is kept
                return RawBitmap(width, height, crop_bgra(buf.raw, width_w, height_w, left, top, width, height))
            finally:
                if old:
                    self._SelectObject(mem_dc, old)
                if bitmap:
                    self._DeleteObject(bitmap)
                if mem_dc:
                    self._DeleteDC(mem_dc)
                self._ReleaseDC(hwnd, window_dc)


_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_MOUSEEVENTF_MOVE, _MOUSEEVENTF_ABSOLUTE, _MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_SRCCOPY, _CAPTUREBLT = 0x00CC0020, 0x40000000
_OWN_INPUT = 0x4F4C5631   # "OLV1" in every event's extra information: this application's input, never a person's
_DOUBLE_CLICK_MARGIN_S = 0.032   # Windows stamps input in ticks of about 16 ms: a second press is kept two ticks clear


def _own_input(extra_info: int) -> bool:
    """Whether an observed event carries this application's own mark (see `_OWN_INPUT`)."""
    return extra_info == _OWN_INPUT
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
_HWND_TOP = 0


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
               ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
               ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


# One physical click on a real, hardware-level XAML button is a down-then-up pair; a double click is simply two
# such pairs sent close together and left for the OS/app to recognise as a double click, exactly like a real
# user's second click -- there is no separate "double-click" hardware event to fabricate. So LEFT_DOUBLE -- the
# double-click message that FOLLOWS a press and a release (`MouseEvent.LEFT_DOUBLE`; the session sends that pair
# first) -- is the second press alone, which Windows itself makes the double click. (Sent as a whole double click
# there, a double tap was three clicks and a stray release: seen live on 2026-10-20.)
_REAL_CLICK_FLAGS: dict[MouseEvent, tuple[int, ...]] = {
    MouseEvent.MOVE: (_MOUSEEVENTF_MOVE,),
    MouseEvent.MOVE_LEFT_HELD: (_MOUSEEVENTF_MOVE,),
    MouseEvent.LEFT_DOWN: (_MOUSEEVENTF_LEFTDOWN,),
    MouseEvent.LEFT_UP: (_MOUSEEVENTF_LEFTUP,),
    MouseEvent.LEFT_DOUBLE: (_MOUSEEVENTF_LEFTDOWN,),
    MouseEvent.RIGHT_DOWN: (_MOUSEEVENTF_RIGHTDOWN,),
    MouseEvent.RIGHT_UP: (_MOUSEEVENTF_RIGHTUP,),
}


def _valid_handle(hwnd: object) -> bool:
    return isinstance(hwnd, int) and not isinstance(hwnd, bool) and 0 < hwnd <= 0xFFFFFFFF


class Win32InputBackend(Win32Backend, WindowInputBackend):
    """Every MOUSE and KEYBOARD event is delivered as real, hardware-level input (`SendInput`), universally --
    not gated by an app's window class or UI technology, on two successive explicit user decisions: mouse first
    (after a narrower, `CoreWindow`-only version was found insufficient -- many applications' real, clickable
    content has no reliable, generically-recognisable signature to gate on, and a real click is the one delivery
    mechanism confirmed to work for every UI technology tried), then keyboard too (after being told a program
    that reads raw keyboard input, or needs a modifier combination, still could not be reached by a posted
    message no matter which window received it).

    MOUSE: `WindowFromPoint` -- the OS's own, exact hit-test, not a per-app-class guess -- finds the real window
    under the point (`_resolve_real_click_target`), refusing (`InputDeliveryError`) rather than ever proceeding
    if it is not the verified window or a genuine descendant of it (`IsChild`-checked): with a real click there
    is no safe way to address an occluded window, since the click physically lands wherever the OS says it does.
    The window is first raised to the top of the Z order without being activated (`SetWindowPos` with
    `SWP_NOACTIVATE`) so an ordinarily-covered target is not refused just because something else happened to be
    on top a moment earlier.

    KEYBOARD: unlike a click, real key/character input is delivered to whichever window currently holds SYSTEM
    FOCUS, not to wherever it is on screen, so `_ensure_real_focus` brings the verified window to the foreground
    if it is not already the foreground window (or does not already own it) before every key or character send --
    the one place this module calls an API that changes which window is the foreground/active window, gated
    immediately afterwards by re-checking that it actually took effect: Windows itself restricts which process
    can steal foreground focus, and if it did not work, the send is refused (`InputDeliveryError`) rather than
    ever going to whatever else currently has focus. Which SPECIFIC control within the foregrounded window ends
    up with keyboard focus is not, and cannot be, chosen here -- it follows the same real, OS-level focus a
    preceding real click already gave that control, exactly as it would for a person clicking then typing."""

    def __init__(self) -> None:
        super().__init__()
        u = ctypes.WinDLL("user32", use_last_error=True)
        H = wintypes.HWND
        self._GetClientRect = u.GetClientRect
        self._GetClientRect.restype, self._GetClientRect.argtypes = wintypes.BOOL, [H, ctypes.POINTER(wintypes.RECT)]
        self._MapVirtualKey = u.MapVirtualKeyW
        self._MapVirtualKey.restype, self._MapVirtualKey.argtypes = wintypes.UINT, [wintypes.UINT, wintypes.UINT]
        self._SendInput = u.SendInput
        self._SendInput.restype, self._SendInput.argtypes = wintypes.UINT, [wintypes.UINT, ctypes.POINTER(_INPUT),
                                                                              ctypes.c_int]
        self._WindowFromPoint = u.WindowFromPoint
        self._WindowFromPoint.restype, self._WindowFromPoint.argtypes = H, [wintypes.POINT]
        self._SetWindowPos = u.SetWindowPos
        self._SetWindowPos.restype = wintypes.BOOL
        self._SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self._GetSystemMetrics = u.GetSystemMetrics
        self._GetSystemMetrics.restype, self._GetSystemMetrics.argtypes = ctypes.c_int, [ctypes.c_int]
        self._GetForegroundWindow = u.GetForegroundWindow
        self._GetForegroundWindow.restype = H
        self._SetForegroundWindow = u.SetForegroundWindow
        self._SetForegroundWindow.restype, self._SetForegroundWindow.argtypes = wintypes.BOOL, [H]
        self._GetDoubleClickTime = u.GetDoubleClickTime
        self._GetDoubleClickTime.restype, self._GetDoubleClickTime.argtypes = wintypes.UINT, []
        self._clock = time.perf_counter                 # a fine clock (Python 3.12's monotonic moves in ~16 ms ticks)
        self._left_press: tuple[float, int | None, int | None] | None = None   # when and where the last primary
        #                                                                         press was sent: a double click's first

    def client_size(self, hwnd: int) -> tuple[int, int] | None:
        if not _valid_handle(hwnd):
            return None
        with self._dpi_aware():
            rect = wintypes.RECT()
            if not self._IsWindow(hwnd) or not self._GetClientRect(hwnd, ctypes.byref(rect)):
                return None
            return rect.right - rect.left, rect.bottom - rect.top

    # ---- real click resolution and delivery (see class docstring) ---------------------------------------------
    def _resolve_real_click_target(self, top_hwnd: int, x: int, y: int) -> tuple[int, int, int]:
        """The real window a click at (x, y) -- `top_hwnd`'s own client coordinates -- would actually reach, and
        the screen point it would reach it at. Found exactly the way Windows itself finds it: a real hit-test
        (`WindowFromPoint`) at the real screen point, after raising `top_hwnd` to the front of the Z order
        without activating it (`SetWindowPos` + `SWP_NOACTIVATE`) so an ordinarily-covered target is not missed
        just because something else happened to be on top a moment earlier. This replaced an earlier, hand-rolled
        `EnumChildWindows`-based approximation that had to be individually tuned, one application at a time, to
        recognise specific "real content" and "internal routing proxy" window classes -- correct only for the
        apps it happened to be tested against. A real hit-test is the OS's own, exact answer and needs no such
        list: it is correct for any application's UI technology by construction. Raises `InputDeliveryError`,
        rather than ever returning a wrong answer, if the real hit is neither `top_hwnd` itself nor a genuine
        descendant of it (`IsChild`-checked) -- with a REAL click there is no safe way to address an occluded
        window at all, so anything else found there must refuse the send, never silently redirect to it."""
        point = wintypes.POINT(x, y)
        if not self._ClientToScreenBase(top_hwnd, ctypes.byref(point)):
            raise InputDeliveryError("Could not resolve the click to a screen position; nothing sent.")
        screen_x, screen_y = point.x, point.y
        self._SetWindowPos(top_hwnd, _HWND_TOP, 0, 0, 0, 0, _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
        hit = self._WindowFromPoint(wintypes.POINT(screen_x, screen_y))
        if not hit or (hit != top_hwnd and not self._IsChild(top_hwnd, hit)):
            raise InputDeliveryError("Another window currently covers the target at that point; nothing sent.")
        return hit, screen_x, screen_y

    def _ensure_real_focus(self, top_hwnd: int) -> None:
        """Real key/character input, unlike a real click, is delivered to whichever window currently holds
        SYSTEM FOCUS, not to wherever it is on screen -- so `top_hwnd` must actually become the foreground
        window first, if it is not already (the ordinary case right after a real click on it, since a real
        click already activates the window it lands on, exactly like a person's own click would). Refuses
        (`InputDeliveryError`), rather than ever sending a keystroke to whatever else currently has focus, if
        this does not succeed: Windows itself restricts which process may steal foreground focus, and no
        workaround for that restriction (e.g. attaching input queues) is attempted here."""
        foreground = self._GetForegroundWindow()
        if foreground == top_hwnd or self._IsChild(top_hwnd, foreground):
            return
        self._SetForegroundWindow(top_hwnd)
        foreground = self._GetForegroundWindow()
        if foreground != top_hwnd and not self._IsChild(top_hwnd, foreground):
            raise InputDeliveryError("Could not bring the target window to the foreground; nothing sent.")

    def _send_real_click(self, screen_x: int | None, screen_y: int | None, event: MouseEvent) -> None:
        """Sends `event` as a real, hardware-level click at an already-verified screen point (see
        `_resolve_real_click_target`, which must run first and is the only place this module ever refuses an
        occluded target) -- or, with no point (None, None), where the pointer already is, with no move at all: only
        a release at the end of a run does that (`release_primary`). The only place a real screen coordinate or
        hardware-level pointer input call exists in this module."""
        flags = _REAL_CLICK_FLAGS[event]
        if event is MouseEvent.LEFT_DOUBLE:
            self._second_press_in_time(screen_x, screen_y)
        if screen_x is None or screen_y is None:
            inputs = (_INPUT * len(flags))()
            for i, flag in enumerate(flags):
                inputs[i].type = _INPUT_MOUSE
                inputs[i].mi = _MOUSEINPUT(0, 0, 0, flag, 0, _OWN_INPUT)      # no MOVE, no ABSOLUTE: right here
        else:
            vx, vy = self._GetSystemMetrics(_SM_XVIRTUALSCREEN), self._GetSystemMetrics(_SM_YVIRTUALSCREEN)
            vw, vh = self._GetSystemMetrics(_SM_CXVIRTUALSCREEN), self._GetSystemMetrics(_SM_CYVIRTUALSCREEN)
            if vw <= 0 or vh <= 0:
                raise InputDeliveryError("Could not read the screen geometry; nothing sent.")
            abs_x = round((screen_x - vx) * 65535 / max(vw - 1, 1))
            abs_y = round((screen_y - vy) * 65535 / max(vh - 1, 1))
            inputs = (_INPUT * (len(flags) + 1))()
            inputs[0].type = _INPUT_MOUSE
            inputs[0].mi = _MOUSEINPUT(abs_x, abs_y, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                                       | _MOUSEEVENTF_VIRTUALDESK, 0, _OWN_INPUT)
            for i, flag in enumerate(flags, start=1):
                inputs[i].type = _INPUT_MOUSE
                inputs[i].mi = _MOUSEINPUT(abs_x, abs_y, 0, flag | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                                           0, _OWN_INPUT)
        sent = self._SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            raise InputDeliveryError(f"The system accepted only {sent} of {len(inputs)} click events; nothing "
                                     "further sent.")
        if event is MouseEvent.LEFT_DOWN:
            self._left_press = (self._clock(), screen_x, screen_y)
        elif event is MouseEvent.LEFT_DOUBLE:
            self._left_press = None                       # a third press makes no double click

    def _second_press_in_time(self, screen_x: int | None, screen_y: int | None) -> None:
        """Windows makes the double click only when its second press follows the first within this computer's
        double-click time (read now: it can be changed) and inside its double-click rectangle around the first;
        between the two the window is raised and checked again, which a busy target can stretch, and it may even move.
        Otherwise it would be a second single click reported as a double click -- so it is not sent, and the double
        tap ends cut off (2 of 4) instead. Measured from the first PRESS, kept two of Windows' own ticks clear."""
        first = self._left_press
        if first is None:
            raise InputDeliveryError("A double click's second press with no first press before it; nothing sent.")
        pressed, x0, y0 = first
        limit, late = int(self._GetDoubleClickTime()) / 1000, self._clock() - pressed
        if late >= limit - _DOUBLE_CLICK_MARGIN_S:
            raise InputDeliveryError(f"The double click's second press would come {late * 1000:.0f} ms after the "
                                     f"first, too close to or past this computer's double-click time "
                                     f"({limit * 1000:.0f} ms): it would be two single clicks, so it is not sent.")
        cx, cy = self._GetSystemMetrics(_SM_CXDOUBLECLK), self._GetSystemMetrics(_SM_CYDOUBLECLK)
        if None in (screen_x, screen_y, x0, y0) or abs(screen_x - x0) * 2 > cx or abs(screen_y - y0) * 2 > cy:
            raise InputDeliveryError("The double click's second press would land away from the first (did the window "
                                     "move?): it would be two single clicks, so it is not sent.")

    def _send_real_key(self, vk: int, scan: int, *, extended: bool, up: bool) -> None:
        """Sends one real, hardware-level key event. `_ensure_real_focus` must run first."""
        flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if extended else 0)
        inputs = (_INPUT * 1)()
        inputs[0].type = _INPUT_KEYBOARD
        inputs[0].ki = _KEYBDINPUT(vk, scan, flags, 0, _OWN_INPUT)
        sent = self._SendInput(1, inputs, ctypes.sizeof(_INPUT))
        if sent != 1:
            raise InputDeliveryError("The system did not accept the key event; nothing sent.")

    def _send_real_char(self, char: str) -> None:
        """Sends one real, hardware-level Unicode character as a down/up pair (`KEYEVENTF_UNICODE`, the standard
        way to inject an arbitrary character without a keyboard-layout mapping). `_ensure_real_focus` must run
        first."""
        code = ord(char)
        inputs = (_INPUT * 2)()
        inputs[0].type = inputs[1].type = _INPUT_KEYBOARD
        inputs[0].ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE, 0, _OWN_INPUT)
        inputs[1].ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, _OWN_INPUT)
        sent = self._SendInput(2, inputs, ctypes.sizeof(_INPUT))
        if sent != 2:
            raise InputDeliveryError(f"The system accepted only {sent} of 2 character events; nothing further "
                                     "sent.")

    def post_mouse(self, hwnd: int, event: MouseEvent, x: int, y: int) -> None:
        if not _valid_handle(hwnd):
            raise InputDeliveryError("Invalid window identifier; nothing sent.")
        if not isinstance(event, MouseEvent):
            raise InputDeliveryError("Unknown pointer event; nothing sent.")
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0x7FFF for v in (x, y)):
            raise InputDeliveryError("Pointer coordinates out of the message range; nothing sent.")
        _, screen_x, screen_y = self._resolve_real_click_target(hwnd, x, y)
        self._send_real_click(screen_x, screen_y, event)

    def post_key(self, hwnd: int, event: KeyEvent, key: Key) -> None:
        if not _valid_handle(hwnd):
            raise InputDeliveryError("Invalid window identifier; nothing sent.")
        if not isinstance(event, KeyEvent):
            raise InputDeliveryError("Unknown key event; nothing sent.")
        try:
            vk = keymap.virtual_key(key)
        except ValueError as exc:
            raise InputDeliveryError(f"{exc}; nothing sent.") from None
        self._ensure_real_focus(hwnd)
        scan = self._MapVirtualKey(vk, 0) & 0xFF
        self._send_real_key(vk, scan, extended=keymap.is_extended(key), up=event is KeyEvent.UP)

    def post_char(self, hwnd: int, char: str) -> None:
        if not _valid_handle(hwnd):
            raise InputDeliveryError("Invalid window identifier; nothing sent.")
        if not isinstance(char, str) or len(char) != 1 or not char.isprintable() or ord(char) > 0xFFFF                 or 0xD800 <= ord(char) <= 0xDFFF:
            raise InputDeliveryError("Only a single printable character from the basic multilingual plane can be "
                                     "sent; nothing sent.")
        self._ensure_real_focus(hwnd)
        self._send_real_char(char)

    # ---- the whole desktop (Full Windows): gated by DesktopInputSession, see the module docstring --------------
    def window_at(self, screen_x: int, screen_y: int) -> int | None:
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (screen_x, screen_y)):
            return None
        with self._dpi_aware():
            hit = self._WindowFromPoint(wintypes.POINT(screen_x, screen_y))
            root = self._GetAncestor(hit, _GA_ROOT) if hit else None
        return int(root) if root else None

    def foreground_window(self) -> int | None:
        fore = self._GetForegroundWindow()
        root = self._GetAncestor(fore, _GA_ROOT) if fore else None
        return int(root) if root else None

    def desktop_mouse(self, event: MouseEvent, screen_x: int, screen_y: int) -> None:
        if not isinstance(event, MouseEvent):
            raise InputDeliveryError("Unknown pointer event; nothing sent.")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (screen_x, screen_y)):
            raise InputDeliveryError("Screen coordinates must be integers; nothing sent.")
        with self._dpi_aware():
            self._send_real_click(screen_x, screen_y, event)

    def desktop_key(self, event: KeyEvent, key: Key) -> None:
        if not isinstance(event, KeyEvent):
            raise InputDeliveryError("Unknown key event; nothing sent.")
        try:
            vk = keymap.virtual_key(key)
        except ValueError as exc:
            raise InputDeliveryError(f"{exc}; nothing sent.") from None
        scan = self._MapVirtualKey(vk, 0) & 0xFF
        self._send_real_key(vk, scan, extended=keymap.is_extended(key), up=event is KeyEvent.UP)

    def desktop_char(self, char: str) -> None:
        if not isinstance(char, str) or len(char) != 1 or not char.isprintable() or ord(char) > 0xFFFF \
                or 0xD800 <= ord(char) <= 0xDFFF:
            raise InputDeliveryError("Only a single printable character from the basic multilingual plane can be "
                                     "sent; nothing sent.")
        self._send_real_char(char)

    # ---- letting go at the end of a live run: see WindowInputSession.release_held --------------------------------
    def release_primary(self) -> None:
        self._send_real_click(None, None, MouseEvent.LEFT_UP)      # where the pointer is now: no move

    def release_secondary(self) -> None:
        self._send_real_click(None, None, MouseEvent.RIGHT_UP)     # where the pointer is now: no move

    def release_key(self, key: Key) -> None:
        try:
            vk = keymap.virtual_key(key)
        except ValueError as exc:
            raise InputDeliveryError(f"{exc}; nothing sent.") from None
        scan = self._MapVirtualKey(vk, 0) & 0xFF
        self._send_real_key(vk, scan, extended=keymap.is_extended(key), up=True)    # no focus change: a release


_WH_KEYBOARD_LL, _WH_MOUSE_LL = 13, 14
_WM_QUIT = 0x0012
_HC_ACTION = 0
_LLKHF_INJECTED, _LLMHF_INJECTED = 0x10, 0x01
_GWL_STYLE, _ES_PASSWORD = -16, 0x20
_SM_CXDOUBLECLK, _SM_CYDOUBLECLK = 36, 37
_IDLE_TICK_S = 0.1
_HOOK_MOUSE_KINDS = {0x0200: MouseObservationKind.MOVE, 0x0201: MouseObservationKind.PRIMARY_DOWN,
                     0x0202: MouseObservationKind.PRIMARY_UP, 0x0204: MouseObservationKind.SECONDARY_DOWN,
                     0x0205: MouseObservationKind.SECONDARY_UP}
_HOOK_KEY_DOWN, _HOOK_KEY_UP = (0x0100, 0x0104), (0x0101, 0x0105)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


class Win32ObservationBackend(WindowObservationBackend):
    """Low-level hooks on a dedicated thread; observations are queued and delivered by a second thread so a slow
    consumer can never stall the system's input. The hook procedures only read, enqueue and call the next hook."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowCaptureError("The Win32 backend is only available on Windows.")
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, LP, WP = wintypes.HWND, wintypes.LPARAM, wintypes.WPARAM
        self._hook_proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, WP, LP)

        def sig(fn, res, *args):
            fn.restype, fn.argtypes = res, list(args)
            return fn

        self._SetHook = sig(u.SetWindowsHookExW, ctypes.c_void_p, ctypes.c_int, self._hook_proc_type,
                            wintypes.HMODULE, wintypes.DWORD)
        self._Unhook = sig(u.UnhookWindowsHookEx, wintypes.BOOL, ctypes.c_void_p)
        self._CallNext = sig(u.CallNextHookEx, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_int, WP, LP)
        self._GetMessage = sig(u.GetMessageW, ctypes.c_int, ctypes.POINTER(wintypes.MSG), H, wintypes.UINT,
                               wintypes.UINT)
        self._PeekMessage = sig(u.PeekMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), H, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT)
        self._PostThreadMessage = sig(u.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD, wintypes.UINT, WP, LP)
        self._WindowFromPoint = sig(u.WindowFromPoint, H, wintypes.POINT)
        self._GetAncestor = sig(u.GetAncestor, H, H, wintypes.UINT)
        self._ClientToScreen = sig(u.ClientToScreen, wintypes.BOOL, H, ctypes.POINTER(wintypes.POINT))
        self._GetClientRect = sig(u.GetClientRect, wintypes.BOOL, H, ctypes.POINTER(wintypes.RECT))
        self._IsWindow = sig(u.IsWindow, wintypes.BOOL, H)
        self._GetForeground = sig(u.GetForegroundWindow, H)
        self._GetThreadId = sig(u.GetWindowThreadProcessId, wintypes.DWORD, H, ctypes.POINTER(wintypes.DWORD))
        self._GetGuiThreadInfo = sig(u.GetGUIThreadInfo, wintypes.BOOL, wintypes.DWORD,
                                     ctypes.POINTER(_GUITHREADINFO))
        self._GetClassW = sig(u.GetClassNameW, ctypes.c_int, H, wintypes.LPWSTR, ctypes.c_int)
        self._GetWindowLong = sig(u.GetWindowLongW, ctypes.c_long, H, ctypes.c_int)
        self._GetDoubleClickTime = sig(u.GetDoubleClickTime, wintypes.UINT)
        self._GetSystemMetrics = sig(u.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
        self._SetThreadDpi = getattr(u, "SetThreadDpiAwarenessContext", None)
        if self._SetThreadDpi is not None:
            sig(self._SetThreadDpi, ctypes.c_void_p, ctypes.c_void_p)
        self._GetModuleHandle = sig(k.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
        self._GetCurrentThreadId = sig(k.GetCurrentThreadId, wintypes.DWORD)
        self._lock = threading.Lock()
        self._hook_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._hook_tid = 0
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._active = False
        self._mouse_proc = self._key_proc = None

    def double_click_settings(self) -> DoubleClickSettings:
        return DoubleClickSettings(int(self._GetDoubleClickTime()), int(self._GetSystemMetrics(_SM_CXDOUBLECLK)),
                                   int(self._GetSystemMetrics(_SM_CYDOUBLECLK)))

    # ---- lifecycle -----------------------------------------------------------
    def start(self, hwnd: int | None, on_mouse, on_key, on_idle) -> None:
        with self._lock:
            if self._active:
                raise WindowCaptureError("Observation is already running.")
            if hwnd is not DESKTOP_HWND and not _valid_handle(hwnd):
                raise WindowCaptureError("Invalid window identifier.")
            self._queue = queue.SimpleQueue()
            ready, failure = threading.Event(), []
            self._active = True
            self._hook_thread = threading.Thread(target=self._run_hooks, args=(hwnd, ready, failure), daemon=True,
                                                 name="observe-hooks")
            self._dispatch_thread = threading.Thread(target=self._run_dispatch, args=(on_mouse, on_key, on_idle),
                                                     daemon=True, name="observe-dispatch")
            self._hook_thread.start()
            ready.wait(5.0)
            if failure or not ready.is_set():
                self._active = False
                if self._hook_tid:
                    self._PostThreadMessage(self._hook_tid, _WM_QUIT, 0, 0)
                raise WindowCaptureError(failure[0] if failure else "Timed out installing the input hooks.")
            self._dispatch_thread.start()

    def stop(self) -> bool:
        """Stop observing. Returns True once the hook and dispatch threads have actually ended (or there was
        nothing to stop); False if either failed to terminate within its join timeout, in which case its OS
        resources (in particular the low-level hook) may still be active. Never raises for that reason alone: a
        slow-to-exit thread is surfaced to the caller, not turned into a failure of whatever `stop()` is part of."""
        with self._lock:
            if not self._active:
                return True
            self._active = False
            hook_thread, dispatch, tid = self._hook_thread, self._dispatch_thread, self._hook_tid
            self._queue.put(None)                                   # ends the dispatcher
        if tid:
            self._PostThreadMessage(tid, _WM_QUIT, 0, 0)            # ends the hook thread's message loop
        me = threading.current_thread()
        clean = True
        for t in (hook_thread, dispatch):
            if t is None or t is me:                                # a callback stopping itself cannot join itself
                continue
            if t.is_alive():
                t.join(3.0)
            if t.is_alive():
                clean = False
        return clean

    # ---- hook thread -----------------------------------------------------------
    def _run_hooks(self, hwnd: int | None, ready: threading.Event, failure: list) -> None:
        if self._SetThreadDpi is not None:
            self._SetThreadDpi(ctypes.c_void_p(_DPI_PER_MONITOR_AWARE_V2))    # lives as long as this thread
        msg = wintypes.MSG()
        self._PeekMessage(ctypes.byref(msg), None, 0, 0, 0)                 # creates this thread's message queue
        self._hook_tid = int(self._GetCurrentThreadId())
        put = self._queue.put

        def mouse_proc(n_code, w_param, l_param):
            try:
                kind = _HOOK_MOUSE_KINDS.get(w_param) if n_code == _HC_ACTION else None
                if kind is not None:
                    info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    put(self._mouse_observation(hwnd, kind, info.pt.x, info.pt.y, bool(info.flags & _LLMHF_INJECTED),
                                                _own_input(info.dwExtraInfo)))
            except Exception:
                pass                                                        # observation must never disturb input
            return self._CallNext(None, n_code, w_param, l_param)

        def key_proc(n_code, w_param, l_param):
            try:
                if n_code == _HC_ACTION and (w_param in _HOOK_KEY_DOWN or w_param in _HOOK_KEY_UP):
                    info = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    put(self._key_observation(hwnd, w_param in _HOOK_KEY_DOWN, int(info.vkCode),
                                              bool(info.flags & _LLKHF_INJECTED), _own_input(info.dwExtraInfo)))
            except Exception:
                pass
            return self._CallNext(None, n_code, w_param, l_param)

        self._mouse_proc, self._key_proc = self._hook_proc_type(mouse_proc), self._hook_proc_type(key_proc)
        module = self._GetModuleHandle(None)
        mouse_hook = self._SetHook(_WH_MOUSE_LL, self._mouse_proc, module, 0)
        key_hook = self._SetHook(_WH_KEYBOARD_LL, self._key_proc, module, 0)
        if not mouse_hook or not key_hook:
            failure.append("The operating system refused to install the input observation hooks.")
            for h in (mouse_hook, key_hook):
                if h:
                    self._Unhook(h)
            ready.set()
            return
        ready.set()
        try:
            while self._GetMessage(ctypes.byref(msg), None, 0, 0) > 0:       # WM_QUIT (or an error) ends it
                pass
        finally:
            self._Unhook(mouse_hook)
            self._Unhook(key_hook)

    def _mouse_observation(self, hwnd: int | None, kind: MouseObservationKind, x: int, y: int,
                           injected: bool, own: bool = False) -> MouseObservation:
        t = time.perf_counter_ns()          # same clock as the recording session; monotonic() steps 15.6 ms
        hit = self._WindowFromPoint(wintypes.POINT(x, y))
        root = self._GetAncestor(hit, _GA_ROOT) if hit else None
        origin = size = None
        if hwnd is not DESKTOP_HWND and self._IsWindow(hwnd):
            corner = wintypes.POINT(0, 0)
            rect = wintypes.RECT()
            if self._ClientToScreen(hwnd, ctypes.byref(corner)) and self._GetClientRect(hwnd, ctypes.byref(rect)):
                origin = (int(corner.x), int(corner.y))
                size = (int(rect.right - rect.left), int(rect.bottom - rect.top))
        return MouseObservation(kind, int(x), int(y), t, int(root) if root else None, origin, size, injected, own)

    def _key_observation(self, hwnd: int | None, is_down: bool, vk: int, injected: bool, own: bool = False) -> KeyObservation:
        t = time.perf_counter_ns()          # same clock as the recording session; monotonic() steps 15.6 ms
        fore = self._GetForeground()
        root = self._GetAncestor(fore, _GA_ROOT) if fore else None
        secure = False
        if root and (hwnd is DESKTOP_HWND or int(root) == hwnd):        # the whole desktop: any focused window
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if self._GetGuiThreadInfo(self._GetThreadId(fore, None), ctypes.byref(info)) and info.hwndFocus:
                name = ctypes.create_unicode_buffer(64)
                self._GetClassW(info.hwndFocus, name, 64)
                lowered = name.value.lower()
                if lowered == "edit" or lowered.startswith("richedit"):
                    secure = bool(self._GetWindowLong(info.hwndFocus, _GWL_STYLE) & _ES_PASSWORD)
        return KeyObservation(is_down, vk, t, int(root) if root else None, secure, injected, own)

    # ---- dispatcher thread ---------------------------------------------------------
    def _run_dispatch(self, on_mouse, on_key, on_idle) -> None:
        while True:
            try:
                item = self._queue.get(timeout=_IDLE_TICK_S)
            except queue.Empty:
                if not self._active:
                    return
                on_idle()
                continue
            if item is None or not self._active:
                return
            (on_mouse if isinstance(item, MouseObservation) else on_key)(item)
