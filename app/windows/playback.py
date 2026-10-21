"""Windows playback adapter: binds a neutral `Macro` to ONE verified Windows target and nothing else.

It adds no input mechanism. The `PlaybackEngine` (app/playback) drives the existing `WindowInputSession`, so every
operation keeps that session's gates (verified target, capability, emergency stop, identity re-check, window
eligibility, client-area bounds, held-state rules, budget) and its handle-addressed message delivery -- all but
letting go at the end of a live run, which presses nothing new (`WindowInputSession.release_held`). The session's
budget is exactly the number of macro actions, so a macro cannot send more operations than it has actions. There is
no foreground, global-coordinate or "matching window" fallback: the window is the one the caller verified.

The whole desktop ("Full Windows", on the person's explicit decision of 2026-10-19) is played on through
`DesktopInputSession` instead (app/windows/desktop.py), and only a macro recorded on the whole desktop IN THE SAME
SCREEN LAYOUT is accepted for it; a LIVE run there is watched by `DesktopRunGuard` from before its first event to its
end, and refused if it cannot be. Coordinates of the two kinds never mix: a macro recorded in a window is refused on
the desktop, and one recorded on the desktop is refused in a window -- before anything is sent, as preflight issues.
"""
from __future__ import annotations

import time
from dataclasses import replace

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.playback.preflight import Issue, PreflightReport
from app.playback.scaling import scaled_macro
from app.providers.size import SizeChanged
from app.windows.backend import WindowInputBackend, WindowObservationBackend
from app.windows.desktop import DesktopInputSession, DesktopRunGuard
from app.windows.input import MAX_SESSION_ACTIONS, WindowInputSession
from app.windows.target import FullWindowsTarget, WindowTarget, WindowVerifier, is_desktop_id

MAX_ACTIONS = MAX_SESSION_ACTIONS     # one limit, owned by the input session


class ScreenPlaybackEngine(PlaybackEngine):
    """`PlaybackEngine` with this provider's own refusals added to its preflight (`refusals`, never sent), and, for
    a live run on the whole desktop, the person's way to take over (`guard`): started before the first event, ended
    with the run; a run whose guard cannot start does not start either."""

    def __init__(self, *args, refusals: tuple[Issue, ...] = (), guard: DesktopRunGuard | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._refusals, self._guard = tuple(refusals), guard

    def preflight(self) -> PreflightReport:
        report = super().preflight()
        return replace(report, issues=report.issues + self._refusals) if self._refusals else report

    def _begin(self) -> None:
        if self._guard is not None and self.preflight().ok:
            self._guard.start()                  # raises PlaybackRefused when it cannot watch: nothing is sent
        try:
            super()._begin()
        except BaseException:
            if self._guard is not None:
                self._guard.stop()
            raise

    def start_in_background(self) -> None:
        try:
            super().start_in_background()
        except BaseException:
            if self._guard is not None and (self._thread is None or not self._thread.is_alive()):
                self._guard.stop()               # no thread to end the watch: end it here
            raise

    def _loop(self) -> None:
        try:
            super()._loop()
        finally:
            if self._guard is not None:
                self._guard.stop()


def window_refusals(macro: Macro) -> tuple[Issue, ...]:
    if is_desktop_id(macro.target.target_id):
        return (Issue("recorded_on_full_windows", "This macro was recorded on the whole screen (Full Windows): its "
                                                  "points are screen points, so it can only run on Full Windows."),)
    return ()


def desktop_refusals(macro: Macro, target: FullWindowsTarget) -> tuple[Issue, ...]:
    recorded = macro.target.target_id
    if not is_desktop_id(recorded):
        return (Issue("recorded_in_a_window", "This macro was recorded in one window: its points are that window's, "
                                              "not the screen's, so it cannot run on Full Windows."),)
    if recorded != target.target_id:
        return (Issue("screen_layout_changed", "This macro was made with another screen layout (monitors or "
                                               "resolution): its points would land elsewhere, so nothing is sent."),)
    return ()


def create_window_playback(macro: Macro, target: WindowTarget, verifier: WindowVerifier, backend: WindowInputBackend,
                           stop: EmergencyStop, *, dry_run: bool = False, allow_different_target: bool = False,
                           **engine_options) -> PlaybackEngine:
    # A macro above the limit is not an error here: the session advertises its `action_limit`, so the engine's
    # preflight refuses it (`too_many_actions`, typed, before any input, live and dry run alike). Never chunked.
    refusals = window_refusals(macro)
    if macro.adapt and macro.recording.size is not None:
        # "Adapt to the size": its points scaled to the window's client area as it is now; other proportions refused
        size = backend.client_size(target.hwnd)
        unreadable = Issue("client_area_unreadable", "The window's size cannot be read now (is it minimised?), so "
                                                     "this macro's clicks cannot be adapted to it; nothing is sent.")
        try:
            if size is None:
                refusals += (unreadable,)
            else:
                macro = scaled_macro(macro, size)
        except SizeChanged as exc:
            refusals += (Issue("size_proportions_changed", f"The window's proportions changed: {exc}"),)
        except ValueError:                       # no area at all (0 x 0): nothing to scale to
            refusals += (unreadable,)
    count = min(len(macro.actions), MAX_SESSION_ACTIONS)
    session = WindowInputSession(target, verifier, backend, stop, max_actions=max(count, 1), dry_run=dry_run)
    return ScreenPlaybackEngine(macro, target, session, stop, dry_run=dry_run,
                                allow_different_target=allow_different_target, refusals=refusals,
                                **engine_options)


def create_desktop_playback(macro: Macro, target: FullWindowsTarget, backend: WindowInputBackend, stop: EmergencyStop,
                            *, observer: WindowObservationBackend | None, dry_run: bool = False,
                            allow_different_target: bool = False, own_pid: int | None = None,
                            **engine_options) -> PlaybackEngine:
    count = min(len(macro.actions), MAX_SESSION_ACTIONS)
    refusals = desktop_refusals(macro, target)
    guard = None
    if not dry_run:
        if observer is None:
            refusals += (Issue("takeover_watch_unavailable", "Esc and the mouse cannot be watched, so nothing is "
                                                             "sent to the whole desktop."),)
        else:
            guard = DesktopRunGuard(observer, stop, clock=engine_options.get("clock", time.monotonic))
    session = DesktopInputSession(target, backend, stop, max_actions=max(count, 1), dry_run=dry_run, own_pid=own_pid,
                                  watch=guard)
    return ScreenPlaybackEngine(macro, target, session, stop, dry_run=dry_run,
                                allow_different_target=allow_different_target, refusals=refusals, guard=guard,
                                **engine_options)
