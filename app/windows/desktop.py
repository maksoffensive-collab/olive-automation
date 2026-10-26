"""The whole desktop ("Full Windows") as a place to act: real input at a point of the screen, wherever it lands.

Allowed on the person's explicit decision of 2026-10-19, after being told what it means -- a click goes to whatever
window is under the point -- and with these safeguards, each enforced here:

  * only on a `FullWindowsTarget` the person chose, in ONE screen layout -- the virtual screen and each monitor in
    it: before every event the layout is read again and a changed one (a monitor added, removed or moved, a
    resolution changed) refuses it, a point in a gap between monitors is refused, and a macro recorded, or a
    picture taken, in another layout never reaches this session (`playback.desktop_refusals`);
  * never on this application's own windows nor its console: a point on one of them, or a key while one of them
    holds the keyboard focus, is refused -- a macro must not press Olive's own Run, Reset or Stop; so is input
    whose destination cannot be told (no window has the focus, or a window's owner cannot be read). A key the
    session itself holds may always be let go: a release presses nothing;
  * the person can always take over: while a LIVE run acts, `DesktopRunGuard` watches the input, and Esc, a click,
    or the mouse moved more than a few pixels by hand trigger the emergency stop at once. Events this application
    sends carry its own mark and are ignored; everything else counts as the person's -- also over remote control,
    where Windows flags the person's input as injected. The person's movement is added up across the run's own
    moves (a recorded macro moves the pointer every few milliseconds). If the watch cannot start, the run does not
    either; if it stops seeing the run's own marked events, the run is stopped;
  * everything `WindowInputSession` does that is not about a window applies unchanged: the emergency stop before
    and after the gates, the budget, whole-number coordinates, the held-state rules, phased delivery with a full
    re-check between phases and a report of how far a partial delivery got, and a dry run that checks everything
    and sends nothing.

Coordinates: whole pixels of the virtual screen, (0, 0) its top-left corner -- exactly the pixels of the Full
Windows picture (`provider.DesktopScreenshotProvider`), so a click on something found in that picture lands on it.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.playback.engine import PlaybackRefused
from app.playback.preflight import Issue
from app.providers.input import Key
from app.providers.target import Capability, Target, require_capability
from app.windows.backend import (DESKTOP_HWND, KeyEvent, MouseEvent, MouseObservationKind, WindowBackend,
                                 WindowInputBackend, WindowObservationBackend)
from app.windows.input import IntendedInput, InputRefused, WindowInputSession, _validate_text
from app.windows.target import FullWindowsTarget, screen_layout

VK_ESCAPE = 0x1B


class PlaceCovered(InputRefused):
    """A press of a Full Windows recording would land on another window than the one it was recorded in."""
    reason_code = "place_covered"

TAKEOVER_DISTANCE = 24     # pixels the person's own mouse may drift (a bump of the desk) before it counts as taking over
DRIFT_FORGET_S = 1.0       # the person's small movements are forgotten after this long without any
WATCH_GRACE_S = 1.0        # an event the run sent must be seen by the watch within this long


class DesktopInputSession(WindowInputSession):
    """`WindowInputSession`'s operations and sequencing for the whole desktop: the same public surface, with the
    gates about a WINDOW replaced by gates about the SCREEN (see the module docstring). `backend` is the provider's
    input backend, which also reads the screen (`WindowBackend.virtual_screen`, `owner_pid`), as
    `Win32InputBackend` does."""

    def __init__(self, target: Target, backend: WindowInputBackend, stop: EmergencyStop, *, max_actions: int,
                 dry_run: bool = False, own_pid: int | None = None, watch: "DesktopRunGuard | None" = None) -> None:
        if not isinstance(backend, WindowBackend):
            raise TypeError("backend must also read the screen (a WindowBackend)")
        super().__init__(target, None, backend, stop, max_actions=max_actions, dry_run=dry_run)
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._watch = watch                      # the run's watch: told of every send, so it notices going blind
        self._call = threading.local()           # what the current call on this thread is (a key release or not)

    def expect_window(self, hwnd: int) -> None:
        """The NEXT pointer operation's presses must land in window `hwnd` (a Full Windows recording played click by
        click in the window each click was in, app/windows/places.py): checked at the point right before each of
        its phases -- a phase begins with a press: a click's, or a double click's second -- and refused when
        another window is on top there. A release within a phase is not checked again: it presses nothing (the
        person's decision of 2026-10-26)."""
        self._call.expected = hwnd

    @staticmethod
    def _check_target(target: Target) -> None:
        if not isinstance(target, FullWindowsTarget):
            raise TargetNotVerified("The desktop input session can only act on Full Windows.",
                                    reason_code="not_verified_target")

    def type_text(self, text: str) -> None:
        require_capability(self._target, Capability.TEXT)
        text = _validate_text(text)
        phases = [[(lambda c=ch: self._backend.desktop_char(c))] for ch in text]
        self._execute(IntendedInput(self.provider_id, self.target_id, "type_text", text_length=len(text)), None,
                      phases, None)

    def _pointer(self, capability: Capability, action: str, x: int, y: int, events: list[list[MouseEvent]], *,
                 click: bool = False, after: Callable[[], None] | None = None) -> None:
        require_capability(self._target, capability)
        self._require_point(x, y)
        if click and self._left_held:
            raise InputRefused("The primary button is held; release it before clicking; nothing sent.")
        left, top = self._target.layout[0], self._target.layout[1]
        phases = [[self._noting(e, lambda e=e: self._backend.desktop_mouse(e, left + x, top + y)) for e in phase]
                  for phase in events]
        self._call.check_window, self._call.expected = getattr(self._call, "expected", None), None   # its first gate
        try:
            self._execute(IntendedInput(self.provider_id, self.target_id, action, x, y), (x, y), phases, after)
        finally:
            self._call.check_window = None

    def _keyboard(self, capability: Capability, action: str, key: Key, events: list[list[tuple[KeyEvent, Key]]], *,
                  after: Callable[[], None] | None = None) -> None:
        require_capability(self._target, capability)
        phases = [[self._noting_key(e, k, lambda e=e, k=k: self._backend.desktop_key(e, k)) for e, k in phase]
                  for phase in events]
        self._call.releasing = action == "key_up"
        try:
            self._execute(IntendedInput(self.provider_id, self.target_id, action, key=key.value), None, phases, after)
        finally:
            self._call.releasing = False

    def _refuse_unless_foreign(self, hwnd: int, refusal: str) -> None:
        """Only a window that is known NOT to be this application's -- nor its console -- may receive input."""
        if hwnd in tuple(self._backend.console_windows()):
            raise InputRefused(f"{refusal} (its console); nothing sent.")
        owner = self._backend.owner_pid(hwnd)
        if owner is None:
            raise InputRefused("Which program owns that window cannot be told (it may just have closed); nothing "
                               "sent.")
        if owner == self._own_pid:
            raise InputRefused(f"{refusal}; nothing sent.")

    def _gate(self, point: tuple[int, int] | None) -> None:
        """Every check that must hold immediately before input is sent to the screen."""
        self._stop.check()
        if self._watch is not None:
            self._watch.check_seeing()           # a watch gone blind triggers the stop ...
            self._stop.check()                   # ... and the stop is what is raised
        if screen_layout(self._backend) != self._target.layout_key:
            raise TargetNotVerified("The screen layout changed (monitors or resolution) since Full Windows was "
                                    "chosen; nothing sent.", reason_code="layout_changed")
        left, top, width, height = self._target.layout
        if point is not None:
            x, y = point
            if not (0 <= x < width and 0 <= y < height):
                raise InputRefused(f"Point ({x}, {y}) is outside the {width}x{height} screen; nothing sent.")
            if not self._target.on_a_monitor(left + x, top + y):
                raise InputRefused(f"({x}, {y}) is on no monitor (in the gap between them); nothing sent.")
            hit = self._backend.window_at(left + x, top + y)
            if hit is None:
                raise InputRefused(f"No window is at ({x}, {y}); nothing sent.")
            self._refuse_unless_foreign(hit, f"({x}, {y}) is on Olive Automation's own window")
            expected = getattr(self._call, "check_window", None)
            if expected is not None:
                if hit != expected:
                    raise PlaceCovered("Another window is on top of the click's window at that point; nothing "
                                       "sent.")
        elif not getattr(self._call, "releasing", False):
            # a key release presses nothing (and Olive reacts to none), so it may go wherever the focus is: refusing
            # it would only leave the key held for the whole desktop
            focused = self._backend.foreground_window()
            if focused is None:
                raise InputRefused("No window has the keyboard focus right now (it is changing hands), so where the "
                                   "key would go cannot be told; nothing sent.")
            self._refuse_unless_foreign(focused, "Olive Automation itself has the keyboard focus, so the key would "
                                                 "go to it")
        self._stop.check()
        if self._watch is not None and not self._dry_run:
            self._watch.expect_own()             # sent right after this: the watch must see it


class DesktopRunGuard:
    """While a LIVE run acts on the whole desktop, the person can take over at once: Esc, a click, or the mouse
    moved more than TAKEOVER_DISTANCE pixels by hand trigger the emergency stop. It only watches (the same read-only
    hooks a recording uses) and acts on nothing but those three; input this application sends is marked and never
    counts. `start()` raises if the watch cannot be set up -- a run on the whole desktop never goes unwatched.
    Usable as a context manager around a whole run (a script, with its waits) and started by the playback engine
    around each recorded macro."""

    def __init__(self, observer: WindowObservationBackend, stop: EmergencyStop, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._observer, self._stop, self._clock = observer, stop, clock
        self._active = False
        self._forget()

    def _forget(self) -> None:
        self._last: tuple[int, int] | None = None   # where the pointer last was, by anyone's event
        self._drift = (0, 0)                        # how far the PERSON's own movements took it, added up
        self._drift_at = float("-inf")              # when the person last moved it
        self._unseen_since: float | None = None     # when the run sent an event the watch has not seen yet

    def start(self) -> None:
        self._forget()
        try:
            self._observer.start(DESKTOP_HWND, self._mouse, self._key, lambda: None)
        except Exception as exc:
            raise PlaybackRefused((Issue("takeover_watch_unavailable",
                                         "Esc and the mouse cannot be watched, so nothing is sent to the whole "
                                         f"desktop: {exc}"),)) from None
        self._active = True

    def stop(self) -> None:
        if self._active:
            self._active = False
            self._observer.stop()

    def __enter__(self) -> "DesktopRunGuard":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---- is the watch still seeing? (every event the run sends carries this application's mark) ----------------
    def expect_own(self) -> None:
        """The run is about to send an event: the watch must see it within WATCH_GRACE_S."""
        if self._unseen_since is None:
            self._unseen_since = self._clock()

    def check_seeing(self) -> None:
        """Before the run sends anything more: an event it sent over WATCH_GRACE_S ago and the watch never saw
        means the hooks are gone (Windows removes a hook that answers too slowly, without a word, and sees nothing
        while a program of higher rights has the focus) -- so Esc and the mouse could no longer stop the run."""
        since = self._unseen_since
        if self._active and since is not None and self._clock() - since > WATCH_GRACE_S:
            self._stop.trigger("Full Windows: the watch stopped seeing the run's own input, so Esc and the mouse "
                               "might not stop it")

    def _mouse(self, obs) -> None:
        here = (obs.screen_x, obs.screen_y)
        if obs.own:
            self._unseen_since = None
            self._last = here                           # the run moved it: the person's movement counts from here
            return
        if obs.kind is not MouseObservationKind.MOVE:
            self._stop.trigger("Full Windows: a click by hand")
            return
        last, self._last = self._last, here
        if last is None:
            return
        now = self._clock()
        if now - self._drift_at > DRIFT_FORGET_S:
            self._drift = (0, 0)                        # a bump long ago is forgotten
        self._drift_at = now
        # added up across the run's own moves, which each put the pointer somewhere new: a recorded macro moves it
        # every few milliseconds, and the person's movement must not have to outrun that to count
        dx, dy = self._drift[0] + here[0] - last[0], self._drift[1] + here[1] - last[1]
        self._drift = (dx, dy)
        if dx * dx + dy * dy > TAKEOVER_DISTANCE * TAKEOVER_DISTANCE:
            self._stop.trigger("Full Windows: the mouse was moved by hand")

    def _key(self, obs) -> None:
        if obs.own:
            self._unseen_since = None
        elif obs.is_down and obs.vk == VK_ESCAPE:
            self._stop.trigger("Full Windows: Esc was pressed")
