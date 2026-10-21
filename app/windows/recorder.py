"""Windows recorder: records what the user does in ONE explicitly chosen, verified window.

Implements the provider-neutral `Recorder` contract (app/providers/recording.py) for a `WindowTarget`. It only
OBSERVES: the operating-system layer (`WindowObservationBackend`) reports input the user already made; this module
never sends, blocks, changes or re-routes input, never activates or moves a window, and never touches any window
other than to compare it with the selected one.

Scope (what is recorded)
  * Nothing happens until `start()`, which needs the verified target the recorder was built with (capability
    RECORD, identity re-checked). `stop()` / `cancel()` / the control hotkey / an invalid target / the emergency
    stop each end it deterministically and release the observation hook; there is no other way to end it.
  * Mouse: only events whose pointer is over the selected window's client area (the top-level window under the
    pointer must be the selected one). Presses over the title bar, borders or another window are ignored. A release
    is recorded only for a press that was recorded; moves and that release are also followed while the button is
    held, even outside the window (a drag), and are then recorded at their real, unclamped client coordinates.
  * Keyboard: only while the selected window has the keyboard focus. A key-up is recorded for a recorded key-down
    even if focus moved meanwhile, so recorded keys stay balanced. Auto-repeat (a second down without an up) is
    recorded once. Modifiers are ordinary key events.
  * Injected input (from other programs, flagged by the system) is ignored by default.
  * Before recording each event the target identity is re-verified (same process instance and window class, still a
    live visible top-level window). If that fails the recording ends as INVALID_TARGET with the reason and yields no
    recording; the recorder never switches to another window. The identity is also re-checked on idle ticks.

Coordinates: client-relative integer pixels, origin at the client area's top-left, read at the moment of each
event, so moving or resizing the window is followed naturally and nothing is clamped.
Double click: a second press of the same button within the system double-click time and distance of the first press
gets `click_count` 2 (a third press starts over at 1). Moves are dropped when identical to the last recorded move or
closer than `min_move_interval_ms` (default 10 ms) to it; presses and releases carry their own coordinates.

Control hotkey (default F10): its release ends the recording; its press and release are NOT recorded unless
`record_control_key=True`. It is honoured whichever window has focus (a stop request is not a recorded action), is
never blocked (the system and the application still receive F10), and cannot start a recording (starting needs an
explicit target, so a GUI must call `start()`).

The whole desktop ("Full Windows", on the person's explicit decision of 2026-10-19): `DesktopRecorder` records
what the person does ANYWHERE on the screen except on this application's own windows -- mouse events in virtual-screen
coordinates of the layout chosen (the same pixels as the Full Windows picture), keys while a window that is not this
application's has the focus -- and ends as INVALID_TARGET if the screen layout changes while it records.

Input this application sends itself (marked; see app/windows/win32.py) is never recorded.

Sensitive data: no text is reconstructed and password contents are never read. Key events are not recorded while
the focused control is a password-style edit control (`ES_PASSWORD`); each such key-down is only counted as
"secure_input" in the recording's `skipped` map. Secure fields of other kinds (browser pages, custom or UI
automation controls) cannot be detected: do not record while typing secrets.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError, TargetNotVerified
from app.providers.base import TargetIdentity
from app.providers.input import Key
from app.providers.recording import (MAX_EVENTS, PointerButton, RecordedEventKind, RecorderState,
                                     RecorderStateError, Recording, RecordingSession)
from app.providers.target import Capability, require_capability
from app.windows import keymap
from app.windows.backend import (DESKTOP_HWND, DoubleClickSettings, KeyObservation, MouseObservation,
                                 MouseObservationKind, WindowBackend, WindowObservationBackend)
from app.windows.target import FullWindowsTarget, WindowTarget, WindowVerifier, screen_layout

_NS_PER_MS = 1_000_000
_DOWN = {MouseObservationKind.PRIMARY_DOWN: PointerButton.PRIMARY, MouseObservationKind.SECONDARY_DOWN: PointerButton.SECONDARY}
_UP = {MouseObservationKind.PRIMARY_UP: PointerButton.PRIMARY, MouseObservationKind.SECONDARY_UP: PointerButton.SECONDARY}


class WindowRecorder:
    def __init__(self, target: WindowTarget, verifier: WindowVerifier, backend: WindowObservationBackend,
                 stop: EmergencyStop, *, control_key: Key | None = Key.F10, record_control_key: bool = False,
                 ignore_injected: bool = True, min_move_interval_ms: int = 10,
                 clock_ns: Callable[[], int] = time.perf_counter_ns, max_events: int = MAX_EVENTS) -> None:
        if not self._accepts(target):
            raise TargetNotVerified("A verified WindowTarget is required to record.", reason_code="not_a_target")
        if control_key is not None and not isinstance(control_key, Key):
            raise ValueError("control_key must be a Key or None.")
        if not isinstance(min_move_interval_ms, int) or isinstance(min_move_interval_ms, bool) or min_move_interval_ms < 0:
            raise ValueError("min_move_interval_ms must be a non-negative integer.")
        self._target, self._verifier, self._backend, self._stop = target, verifier, backend, stop
        self._control_key, self._record_control, self._ignore_injected = control_key, record_control_key, ignore_injected
        self._min_move_ns = min_move_interval_ms * _NS_PER_MS
        self._session = RecordingSession(clock_ns=clock_ns, max_events=max_events)
        self._reset_capture()

    # ---- where things happen: one window (here) or the whole desktop (DesktopRecorder) -------------------------
    @staticmethod
    def _accepts(target: object) -> bool:
        return isinstance(target, WindowTarget)

    def _observed_hwnd(self) -> int:
        return self._target.hwnd

    def _recheck(self) -> None:
        """Raises TargetNotVerified when the target is no longer the one being recorded."""
        self._verifier.recheck(self._target)

    def _place(self, obs: MouseObservation) -> tuple[bool, int, int, bool] | None:
        """(over the selected window, x, y in its client coordinates, inside its client area), or None when the
        event cannot be placed."""
        if obs.client_origin is None or obs.client_size is None:
            return None
        x, y = obs.screen_x - obs.client_origin[0], obs.screen_y - obs.client_origin[1]
        return (obs.hit_hwnd == self._target.hwnd, x, y,
                0 <= x < obs.client_size[0] and 0 <= y < obs.client_size[1])

    def _size_of(self, obs: MouseObservation) -> tuple[int, int] | None:
        """The size of the space a placed event's point is in: the window's client area (None: none kept)."""
        return obs.client_size

    def _focused(self, obs: KeyObservation) -> bool:
        return obs.foreground_hwnd == self._target.hwnd

    def _reset_capture(self) -> None:
        self._held_buttons: dict[PointerButton, int] = {}       # button -> click_count of its recorded press
        self._held_keys: set[Key] = set()
        self._last_press: tuple[PointerButton, int, int, int, int] | None = None   # button, t_ns, x, y, count
        self._last_move: tuple[int, int, int] | None = None                         # t_ns, x, y
        self._double = DoubleClickSettings(500, 4, 4)

    # ---- Recorder contract ------------------------------------------------
    @property
    def state(self) -> RecorderState:
        self._poll_emergency()
        return self._session.state

    @property
    def target_identity(self) -> TargetIdentity:
        return self._target.identity

    @property
    def recording(self) -> Recording | None:
        return self._session.recording

    @property
    def event_count(self) -> int:
        """Events recorded so far in a running recording (read-only progress for a UI)."""
        return self._session.event_count

    @property
    def end_reason(self) -> str | None:
        return self._session.end_reason

    @property
    def end_detail(self) -> str:
        return self._session.end_detail

    def start(self) -> None:
        self._stop.check()
        require_capability(self._target, Capability.RECORD)
        self._recheck()                                       # raises TargetNotVerified: state unchanged
        self._session.start(self._target.identity)            # raises RecorderStateError if already started / not discarded
        self._reset_capture()
        try:
            self._double = self._backend.double_click_settings()
            self._backend.start(self._observed_hwnd(), self._on_mouse, self._on_key, self._on_idle)
        except Exception as exc:
            self._backend.stop()
            self._session.cancel("start_failed", type(exc).__name__)
            if isinstance(exc, PlatformError):
                raise
            raise PlatformError(f"Recording could not be started: {type(exc).__name__}.") from None

    def stop(self) -> Recording:
        if self._session.state is not RecorderState.RECORDING:
            raise RecorderStateError(f"Cannot stop: the recorder is {self._session.state.value}, not recording.")
        self._backend.stop()                                   # no more events after this point
        if self._stop.is_set():
            self._end_cancel("emergency_stop", "")
            raise EmergencyStopped("Emergency stop is active; the recording was cancelled.")
        return self._session.stop("stopped")

    def cancel(self) -> None:
        if self._session.state is not RecorderState.RECORDING:
            raise RecorderStateError(f"Cannot cancel: the recorder is {self._session.state.value}, not recording.")
        self._end_cancel("cancelled", "")

    def discard(self) -> None:
        self._session.discard()

    # ---- ending -----------------------------------------------------------
    def _end_cancel(self, reason: str, detail: str) -> None:
        self._backend.stop()
        if self._session.state is RecorderState.RECORDING:
            self._session.cancel(reason, detail)

    def _end_invalid(self, reason: str, detail: str) -> None:
        self._backend.stop()
        if self._session.state is RecorderState.RECORDING:
            self._session.invalidate(reason, detail)

    def _poll_emergency(self) -> None:
        if self._session.state is RecorderState.RECORDING and self._stop.is_set():
            self._end_cancel("emergency_stop", self._stop.reason or "")

    def _live(self) -> bool:
        """True if recording may continue: emergency stop clear and the target identity still verifies."""
        if self._session.state is not RecorderState.RECORDING:
            return False
        if self._stop.is_set():
            self._end_cancel("emergency_stop", self._stop.reason or "")
            return False
        try:
            self._recheck()
        except TargetNotVerified as exc:
            self._end_invalid(exc.reason_code, str(exc))
            return False
        except EmergencyStopped:
            self._end_cancel("emergency_stop", "")
            return False
        return True

    # ---- observation callbacks (dispatcher thread) -------------------------
    def _on_idle(self) -> None:
        self._guarded(self._live)

    def _on_mouse(self, obs: MouseObservation) -> None:
        self._guarded(lambda: self._mouse(obs))

    def _on_key(self, obs: KeyObservation) -> None:
        self._guarded(lambda: self._key(obs))

    def _guarded(self, fn) -> None:
        with self._session.lock:
            try:
                fn()
            except Exception as exc:                     # never let a capture bug corrupt or leak a recording
                if self._session.state is RecorderState.RECORDING:
                    self._end_cancel("internal_error", type(exc).__name__)

    def _mouse(self, obs: MouseObservation) -> None:
        if self._session.state is not RecorderState.RECORDING or (obs.injected and self._ignore_injected) \
                or obs.own:
            return
        dragging = bool(self._held_buttons)
        place = self._place(obs)
        if place is None:
            return
        over, x, y, inside = place
        if not (over or dragging):
            return
        kind = obs.kind
        if kind is MouseObservationKind.MOVE:
            if not ((over and inside) or dragging):
                return
        elif kind in _DOWN:
            if not (over and inside) or _DOWN[kind] in self._held_buttons:
                return
        elif kind in _UP:
            if _UP[kind] not in self._held_buttons:
                return
        else:
            return
        if not self._live():
            return
        size = self._size_of(obs)
        if size is not None:
            self._session.note_size(size)                # what a macro that adapts to the size scales from
        if kind is MouseObservationKind.MOVE:
            if self._last_move is not None:
                last_t, lx, ly = self._last_move
                if (lx, ly) == (x, y) or obs.t_ns - last_t < self._min_move_ns:
                    return
            self._session.record(RecordedEventKind.POINTER_MOVE, x=x, y=y, at_ns=obs.t_ns)
            self._last_move = (obs.t_ns, x, y)
        elif kind in _DOWN:
            button = _DOWN[kind]
            count = 1
            if self._last_press is not None:
                pb, pt, px, py, pc = self._last_press
                if (pb is button and pc == 1 and 0 <= obs.t_ns - pt <= self._double.time_ms * _NS_PER_MS
                        and abs(x - px) <= self._double.dx // 2 and abs(y - py) <= self._double.dy // 2):
                    count = 2
            self._session.record(RecordedEventKind.BUTTON_DOWN, x=x, y=y, button=button, click_count=count,
                                 at_ns=obs.t_ns)
            self._held_buttons[button] = count
            self._last_press = (button, obs.t_ns, x, y, count)
        else:
            button = _UP[kind]
            self._session.record(RecordedEventKind.BUTTON_UP, x=x, y=y, button=button,
                                 click_count=self._held_buttons.pop(button), at_ns=obs.t_ns)

    def _key(self, obs: KeyObservation) -> None:
        if self._session.state is not RecorderState.RECORDING or (obs.injected and self._ignore_injected) \
                or obs.own:
            return
        key = keymap.key_from_virtual_key(obs.vk)
        is_control = key is not None and key is self._control_key
        if not is_control or self._record_control:
            self._record_key(obs, key)
        if is_control and not obs.is_down and self._session.state is RecorderState.RECORDING and self._live():
            self._backend.stop()
            self._session.stop("control_hotkey")

    def _record_key(self, obs: KeyObservation, key: Key | None) -> None:
        if self._session.state is not RecorderState.RECORDING:
            return
        focused = self._focused(obs)
        if not obs.is_down:
            if key in self._held_keys and self._live():
                self._session.record(RecordedEventKind.KEY_UP, key=key, at_ns=obs.t_ns)
                self._held_keys.discard(key)
            return
        if not focused:
            return
        if obs.secure_focus:
            self._session.note_skipped("secure_input")
        elif key is None:
            self._session.note_skipped("unsupported_key")
        elif key not in self._held_keys and self._live():                # a repeated down is auto-repeat
            self._session.record(RecordedEventKind.KEY_DOWN, key=key, at_ns=obs.t_ns)
            self._held_keys.add(key)


class DesktopRecorder(WindowRecorder):
    """`WindowRecorder` for the whole desktop ("Full Windows"): what the person does anywhere on the screen except on
    this application's own windows (see the module docstring). `screen` reads the screen layout and which process
    owns a window -- the provider's input backend, as for `DesktopInputSession`."""

    def __init__(self, target: FullWindowsTarget, screen: WindowBackend, backend: WindowObservationBackend,
                 stop: EmergencyStop, *, own_pid: int | None = None, **options) -> None:
        super().__init__(target, None, backend, stop, **options)
        self._screen = screen
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._own_windows: set[int] = set()     # windows found to be ours: a click that closes one stays ours

    @staticmethod
    def _accepts(target: object) -> bool:
        return isinstance(target, FullWindowsTarget)

    def _observed_hwnd(self) -> int:
        return DESKTOP_HWND

    def _recheck(self) -> None:
        if screen_layout(self._screen) != self._target.layout_key:
            raise TargetNotVerified("The screen layout changed (monitors or resolution) while recording.",
                                    reason_code="layout_changed")

    def _ours(self, hwnd: int | None) -> bool:
        """This application's window or its console. Asked again every time it was not (a handle can be reused by
        a new window); once it was, it stays ours for the recording."""
        if hwnd is None:
            return False
        if hwnd not in self._own_windows and (hwnd in tuple(self._screen.console_windows())
                                              or self._screen.owner_pid(hwnd) == self._own_pid):
            self._own_windows.add(hwnd)
        return hwnd in self._own_windows

    def _place(self, obs: MouseObservation) -> tuple[bool, int, int, bool] | None:
        left, top, width, height = self._target.layout
        x, y = obs.screen_x - left, obs.screen_y - top
        over = obs.hit_hwnd is not None and not self._ours(obs.hit_hwnd)
        return over, x, y, 0 <= x < width and 0 <= y < height

    def _size_of(self, obs: MouseObservation) -> None:
        """None: its points are the screen layout's (its target id), never scaled -- another layout is refused --
        so "Adapt to the size" is not offered for it."""
        return None

    def _focused(self, obs: KeyObservation) -> bool:
        return obs.foreground_hwnd is not None and not self._ours(obs.foreground_hwnd)
