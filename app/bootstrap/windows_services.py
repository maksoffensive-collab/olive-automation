"""`AutomationServices` for the Windows provider: an adapter over the existing `app.windows` objects. No new logic.

    discover        one synthetic "Full Windows" entry (see FullWindowsTarget), always first, followed by
                    WindowBackend.enumerate()  (own process's windows and untitled windows are left out)
    verify/recheck  the Full Windows ref verifies in the CURRENT screen layout and re-checks that it has not
                    changed; every other ref goes through WindowVerifier.verify / recheck exactly as before
    capture         WindowScreenshotProvider (a window); DesktopScreenshotProvider (Full Windows: the whole virtual
                    screen, every monitor)
    create_recorder WindowRecorder (a window) or DesktopRecorder (Full Windows), each with a fresh observation backend
    create_playback create_window_playback (WindowInputSession: handle-addressed messages only) or, for Full
                    Windows, create_desktop_playback (DesktopInputSession, watched by DesktopRunGuard)
    guard_run       for Full Windows, the run-wide watch for the person taking over (Esc, a click, the mouse moved)
    bring_to_front  a run's window restored, raised and (at the start) made the main window; sends no input
    escape_watch    Esc anywhere stops a live run (EscapeWatch) -- not needed by Full Windows, which has its own

Full Windows takes input on the person's explicit decision of 2026-10-19; see app/windows/desktop.py for every
safeguard.

Window handles stay in here: the GUI receives opaque `TargetEntry.ref` tokens that are only valid for the most
recent discovery. Nothing in this module sends input itself.
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import SafetyViolation, TargetNotVerified
from app.gui.services import TargetEntry
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.providers.base import Screenshot
from app.providers.recording import Recorder
from app.providers.target import Target
from app.windows.backend import WindowInfo, WindowInputBackend, WindowObservationBackend
from app.windows.ids import PROVIDER_ID
from app.windows.desktop import DesktopRunGuard
from app.windows.escape import EscapeWatch
from app.windows.playback import create_desktop_playback, create_window_playback
from app.windows.provider import DesktopScreenshotProvider, WindowScreenshotProvider
from app.windows.recorder import DesktopRecorder, WindowRecorder
from app.windows.target import FULL_DESKTOP_ID, FullWindowsTarget, WindowTarget, WindowVerifier, screen_layout

FULL_WINDOWS_REF = FULL_DESKTOP_ID
def _full_windows_entry(layout: tuple[int, int, int, int] | None) -> TargetEntry:
    size = f"{layout[2]} x {layout[3]}" if layout else ""
    return TargetEntry(FULL_WINDOWS_REF, "Full Windows", "", "desktop", size, PROVIDER_ID, "Full Windows",
                       whole_screen=True)


def _entry(info: WindowInfo, ref: str) -> TargetEntry:
    size = "minimized" if info.minimized else f"{info.width} x {info.height}"
    return TargetEntry(ref, info.title, info.process_name or "(unknown program)", info.class_name, size,
                       PROVIDER_ID, "Open Windows")


class WindowsServices:
    def __init__(self, backend: WindowInputBackend, observer_factory: Callable[[], WindowObservationBackend],
                 stop: EmergencyStop | None = None, *, own_pid: int | None = None,
                 ignore_injected_input: bool = False) -> None:
        self._backend = backend
        self._observer_factory = observer_factory
        self._stop = stop or EmergencyStop()
        self._verifier = WindowVerifier(backend, self._stop)
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._refs: dict[str, WindowInfo] = {}          # ref -> the identity snapshot that was listed
        # WindowRecorder's OWN default (see app/windows/recorder.py) is to ignore injected input, because a
        # general-purpose recorder cannot tell "another automation tool" apart from "a legitimate remote input
        # driver" and errs conservative. But this platform's real, ordinary deployment is a Windows host operated
        # over a remote-control tool (RDP, AnyDesk, ...), and every one of those delivers clicks and keystrokes
        # through the same user-mode API a script would use to fabricate input -- which Windows itself flags as
        # injected, indistinguishably from genuinely synthetic input. With the recorder's own default, Record ->
        # do things over remote control -> Stop silently produces a zero-event recording every single time, which
        # is exactly what "the recorded macro never appears" turned out to be (confirmed live: real remote-style
        # input against a real window, observed being discarded event by event). So the Windows provider itself
        # -- not the general-purpose recorder class -- opts to accept injected input by default.
        self._ignore_injected_input = ignore_injected_input

    @classmethod
    def create_default(cls, stop: EmergencyStop | None = None) -> "WindowsServices":
        from app.windows.win32 import Win32InputBackend, Win32ObservationBackend
        return cls(Win32InputBackend(), Win32ObservationBackend, stop)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def stop(self) -> EmergencyStop:
        return self._stop

    def discover(self) -> list[TargetEntry]:
        infos = [w for w in self._backend.enumerate() if w.pid != self._own_pid and w.title.strip()]
        infos.sort(key=lambda w: (w.process_name.lower(), w.title.lower(), w.hwnd))
        self._refs = {f"w{w.hwnd}": w for w in infos}
        try:
            layout = self._layout()[0]
        except TargetNotVerified:
            layout = None
        return [_full_windows_entry(layout)] + [_entry(w, f"w{w.hwnd}") for w in infos]

    def _layout(self):
        """(virtual screen, monitors) as they are now."""
        try:
            return screen_layout(self._backend)
        except Exception as exc:
            raise TargetNotVerified(f"The screen layout cannot be read: {exc}", reason_code="no_layout") from None

    def verify(self, entry: TargetEntry) -> Target:
        if entry.ref == FULL_WINDOWS_REF:
            layout, monitors = self._layout()
            return FullWindowsTarget(time.monotonic(), layout, monitors)    # the whole desktop, in THIS layout
        listed = self._refs.get(entry.ref)
        if listed is None:
            raise TargetNotVerified("That window is not part of the current list. Refresh and select it again.",
                                    reason_code="invalid_handle")
        # a selection is a specific identity (handle + process instance + executable + class), not just a handle
        return self._verifier.verify(listed.hwnd, expected=listed)

    def _window_target(self, target: object) -> WindowTarget:
        if not isinstance(target, WindowTarget):
            raise SafetyViolation("The Windows services can only act on a verified window target.")
        return target

    def recheck(self, target: Target) -> TargetEntry:
        if isinstance(target, FullWindowsTarget):
            if self._layout() != target.layout_key:
                raise TargetNotVerified("The screen layout changed (monitors or resolution) since Full Windows was "
                                        "chosen; choose it again.", reason_code="layout_changed")
            return _full_windows_entry(tuple(target.layout))
        window = self._window_target(target)
        return _entry(self._verifier.recheck(window), f"w{window.hwnd}")

    def capture(self, target: Target) -> Screenshot:
        if isinstance(target, FullWindowsTarget):
            return DesktopScreenshotProvider(target, self._backend, self._stop).capture()
        return WindowScreenshotProvider(self._window_target(target), self._verifier, self._stop).capture()

    def create_recorder(self, target: Target) -> Recorder:
        if isinstance(target, FullWindowsTarget):
            return DesktopRecorder(target, self._backend, self._observer_factory(), self._stop,
                                   ignore_injected=self._ignore_injected_input, own_pid=self._own_pid)
        return WindowRecorder(self._window_target(target), self._verifier, self._observer_factory(), self._stop,
                              ignore_injected=self._ignore_injected_input)

    def create_playback(self, macro: Macro, target: Target, *, dry_run: bool,
                        allow_different_target: bool) -> PlaybackEngine:
        if isinstance(target, FullWindowsTarget):
            return create_desktop_playback(macro, target, self._backend, self._stop,
                                           observer=None if dry_run else self._observer_factory(), dry_run=dry_run,
                                           allow_different_target=allow_different_target, own_pid=self._own_pid)
        return create_window_playback(macro, self._window_target(target), self._verifier, self._backend, self._stop,
                                      dry_run=dry_run, allow_different_target=allow_different_target)

    def guard_run(self, target: Target):
        """For a LIVE run on Full Windows: the person taking over (Esc, a click, the mouse moved by hand) triggers the
        emergency stop for as long as the run lasts, its waits included. A window needs nothing."""
        if isinstance(target, FullWindowsTarget):
            return DesktopRunGuard(self._observer_factory(), self._stop)
        return contextlib.nullcontext()

    def bring_to_front(self, target: Target, *, activate: bool = False) -> bool:
        """A run's window made seen (the person's decision of 2026-10-25): restored when minimised and raised to the
        front -- with `activate` (the start of a run) also made the main window. Only the verified window, checked
        again first; Full Windows has no window of its own. Sends no input. True when it was minimised or covered."""
        if not isinstance(target, WindowTarget):
            return False
        self._verifier.recheck(target)
        return self._backend.raise_window(target.hwnd, activate=activate)

    def screen_rect(self, target: Target) -> tuple[int, int, int, int] | None:
        """Where the target's picture lies on this computer's screen (left, top, width, height): a window's client
        area, the whole desktop's layout -- for a picker that lays the picture exactly over it. Read only."""
        if isinstance(target, FullWindowsTarget):
            return tuple(target.layout)
        if not isinstance(target, WindowTarget):
            return None
        self._verifier.recheck(target)
        return self._backend.client_screen_rect(target.hwnd)

    def escape_watch(self, entries) -> EscapeWatch | None:
        """Esc anywhere stops a LIVE run (the person's decision of 2026-10-25): the watch for a batch, unless every
        one of its targets is the whole desktop (Full Windows watches Esc, clicks and the mouse itself)."""
        if all(entry.whole_screen for entry in entries):
            return None
        return EscapeWatch(self._observer_factory(), self._stop)
