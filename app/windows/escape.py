"""Esc anywhere stops a run: the emergency stop on the person's own Esc, wherever the focus is, while a run is on.

The person's decision of 2026-10-25: before a run and before each look and each click Olive brings the target's
window to the front, so Olive's own window -- and its Stop button and its Esc -- end up behind it. While a run is
under way the keyboard of the whole system is therefore watched for one key, Esc, and a press of it triggers the
emergency stop. Nothing else is acted on, nothing is kept or logged, and the watch ends with the run. It only
watches: the read-only low-level KEYBOARD hook of the observation backend (no mouse hook), which can never block,
change or generate input. Keys this application sends itself (a recorded Esc, say) carry its own mark and never
count; any other Esc does -- also one typed over remote control, where Windows flags the person's keys as injected.

Not during a start delay: a run that waits to start (up to a day) starts its watch when it begins to act, so an Esc
typed meanwhile elsewhere cancels nothing, and nothing watches the keyboard while nothing is done.

Windows removes a low-level hook that answers too slowly, without a word (the hook here runs Python code, which can
wait for another thread). The watch therefore renews its hook every RENEW_S; a renewal that fails triggers the
emergency stop -- a run must not go on when Esc anywhere no longer stops it.

A run on the whole desktop (Full Windows) has its own, wider watch (`desktop.DesktopRunGuard`: Esc, a click, the
mouse moved by hand), so it does not need this one. Not seen: keys typed into a program running with higher rights
than Olive (an administrator's window) while it has the focus -- Windows hides them from every hook of a lesser
program. Olive's own Stop button and Esc in its own window still work then.
"""
from __future__ import annotations

import threading

from app.core.cancel import EmergencyStop
from app.core.errors import PlatformError
from app.windows.backend import DESKTOP_HWND, WindowObservationBackend

VK_ESCAPE = 0x1B
RENEW_S = 30.0


class EscapeWatchUnavailable(PlatformError):
    """The keyboard could not be watched, so Esc anywhere would not stop a run."""

    reason_code = "escape_watch_unavailable"


class EscapeWatch:
    """While started, the person's Esc anywhere triggers the emergency stop. `start()` raises
    `EscapeWatchUnavailable` when the watch cannot be set up; `start()` and `stop()` are idempotent and may be called
    from any thread (several targets of one run may begin to act at once)."""

    def __init__(self, observer: WindowObservationBackend, stop: EmergencyStop, *, renew_s: float = RENEW_S) -> None:
        self._observer, self._stop, self._renew_s = observer, stop, renew_s
        self._lock = threading.Lock()
        self._active = False
        self._ended = threading.Event()
        self._renewer: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        with self._lock:
            if self._active:
                return
            self._begin()
            self._active = True
            self._ended.clear()
            self._renewer = threading.Thread(target=self._renew, daemon=True, name="escape-watch-renew")
            self._renewer.start()

    def _begin(self) -> None:
        try:
            self._observer.start(DESKTOP_HWND, None, self._key, _nothing)      # the keyboard only
        except Exception as exc:
            raise EscapeWatchUnavailable(f"The keyboard cannot be watched, so Esc anywhere would not stop the run: "
                                         f"{exc}") from None

    def stop(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._ended.set()
            self._observer.stop()

    def _renew(self) -> None:
        """Every RENEW_S: the hook taken down and put up again (a hook Windows removed comes back)."""
        while not self._ended.wait(self._renew_s):
            with self._lock:
                if not self._active:
                    return
                self._observer.stop()
                try:
                    self._begin()
                except EscapeWatchUnavailable as exc:
                    self._active = False
                    self._stop.trigger(f"Esc anywhere could no longer be watched: {exc}")
                    return

    def _key(self, obs) -> None:
        if obs.is_down and obs.vk == VK_ESCAPE and not obs.own:
            self._stop.trigger("Esc was pressed")


def _nothing() -> None:
    return None
