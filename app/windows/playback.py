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

import os
import time
from dataclasses import replace

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackEngine
from app.playback.macro import ActionOp, Macro, MacroAction
from app.playback.preflight import Issue, PreflightReport
from app.playback.scaling import scaled_macro
from app.providers.size import SizeChanged
from app.windows.backend import WindowInputBackend, WindowObservationBackend
from app.windows.desktop import DesktopInputSession, DesktopRunGuard
from app.windows.input import MAX_SESSION_ACTIONS, WindowInputSession
from app.windows.places import PLACE_POLL_S, PLACE_WAIT_S, RAISE_SETTLE_S, PlaceFinder, PlaceRefused
from app.providers.recording import UNKNOWN_PLACE
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


_PRESSES = frozenset({ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP, ActionOp.BUTTON_DOWN,
                      ActionOp.SECONDARY_DOWN})
_DOWNS = frozenset({ActionOp.BUTTON_DOWN, ActionOp.SECONDARY_DOWN})
_UPS = frozenset({ActionOp.BUTTON_UP, ActionOp.SECONDARY_UP})


class PlacedPlaybackEngine(ScreenPlaybackEngine):
    """A Full Windows recording that knows the window of each click, played click by click in it
    (app/windows/places.py): the windows open when it was recorded are checked before the first click (preflight),
    and every click is aimed anew just before it is sent."""

    def __init__(self, *args, finder: PlaceFinder, layout: tuple[int, int, int, int],
                 wait_s: float = PLACE_WAIT_S, settle_s: float = RAISE_SETTLE_S, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._finder, self._layout, self._wait_s, self._settle_s = finder, layout, wait_s, settle_s
        self._bound: dict[int, int] = {}         # recorded window number -> the window found for it in this run
        self._at_run_start: set[int] = set()     # the windows there when the run began
        self._press: tuple[int, int, int, int] | None = None   # a held press: recorded x, y and aimed x, y
        self._press_window: int | None = None    # the recorded window it was in
        self._last: tuple[int, int, int, int] | None = None    # the last press, which a free move follows

    # ---- before the first click ----------------------------------------------------------------------------
    def _first_places(self):
        """Each recorded window's first place, in the order the recording reaches them."""
        recording, firsts = self._macro.recording, {}
        for event in recording.events:
            if event.place is not None and event.place[0] != UNKNOWN_PLACE:
                place = recording.places[event.place[0]]
                firsts.setdefault(place.window, place)
        return list(firsts.values())

    def preflight(self) -> PreflightReport:
        report = super().preflight()
        recording = self._macro.recording
        windows, claimed, issues = self._finder.windows(), set(), []
        for place in self._first_places():
            if place.at_start:
                issue, hwnd = self._finder.issue(place, windows, claimed)
                claimed.add(hwnd)
                if issue is not None:
                    issues.append(issue)
        for index, action in enumerate(self._macro.actions):
            place = recording.events[action.source].place
            if action.op in _PRESSES and place is not None and place[0] == UNKNOWN_PLACE:
                issues.append(Issue("place_unknown", f"Click {index + 1} was recorded where the window could not be "
                                                     "identified, so it would go to its old point of the screen, "
                                                     "whatever is there now; nothing is sent.",
                                    index=index, params=(("number", index + 1),)))
        return replace(report, issues=report.issues + tuple(issues)) if issues else report

    def _begin(self) -> None:
        at_start = {w.hwnd for w in self._finder.windows()}       # read first: a failure refuses before anything
        super()._begin()                                          # (a refused start changes nothing here)
        self._bound, self._press, self._press_window, self._last = {}, None, None, None
        self._at_run_start = at_start

    # ---- each action, just before it is sent ----------------------------------------------------------------
    def _aim(self, index: int, action: MacroAction) -> MacroAction | None:
        try:
            return self._aimed(index, action)
        except PlaceRefused:
            if self._abort_reason() is not None:
                return None                      # stopped or cancelled meanwhile: a stop, never a failure
            raise

    def _aimed(self, index: int, action: MacroAction) -> MacroAction | None:
        recording = self._macro.recording
        event = recording.events[action.source]
        if action.op in _PRESSES:
            started = self._clock()
            if event.place is None or event.place[0] == UNKNOWN_PLACE:
                aimed = action                   # added by hand: the screen point picked (unknown: refused before)
            else:
                aimed = self._aim_press(index, action, event.place)
            if aimed is None:
                return None
            if action.op in _DOWNS:
                self._press = (action.x, action.y, aimed.x, aimed.y)
                self._press_window = (None if event.place is None or event.place[0] == UNKNOWN_PLACE
                                      else recording.places[event.place[0]].window)
            self._last = (action.x, action.y, aimed.x, aimed.y)
            if self._clock() > started:
                self._resumed = True             # it waited: what follows is timed from this click
            return aimed
        if action.op is ActionOp.MOVE:
            if self._press is not None and "primary_button" in self._held:
                return self._offset(action, self._press)              # a drag follows its press
            return self._free_move(index, action)
        if action.op in _UPS:
            aimed = self._aim_release(action, event)
            self._press = self._press_window = None
            return aimed
        return action

    def _point(self, action: MacroAction, screen: tuple[int, int]) -> MacroAction:
        return replace(action, x=screen[0] - self._layout[0], y=screen[1] - self._layout[1])

    @staticmethod
    def _offset(action: MacroAction, anchor: tuple[int, int, int, int]) -> MacroAction:
        rx, ry, ax, ay = anchor
        return replace(action, x=ax + action.x - rx, y=ay + action.y - ry)

    def _free_move(self, index: int, action: MacroAction) -> MacroAction:
        """A move with no button held follows the last press -- or, before the first, the window of the next press
        where it is now (found, never waited for or raised); as recorded only when neither is known."""
        if self._last is not None:
            return self._offset(action, self._last)
        recording = self._macro.recording
        for later in self._macro.actions[index + 1:]:
            ref = recording.events[later.source].place
            if later.op not in _PRESSES or ref is None or ref[0] == UNKNOWN_PLACE:
                continue
            place = recording.places[ref[0]]
            window = self._known(place)
            if window is None:
                return action
            try:
                sx, sy = self._finder.aim(place, window, ref[1], ref[2])
            except PlaceRefused:
                return action
            return self._offset(action, (later.x, later.y, sx - self._layout[0], sy - self._layout[1]))
        return action

    def _aim_press(self, index: int, action: MacroAction, ref: tuple[int, int, int]) -> MacroAction | None:
        number, x, y = ref
        place = self._macro.recording.places[number]
        if self._dry:                            # a dry run checks where it can, and raises and waits for nothing
            window = self._known(place)
            if window is None or not window.enabled:
                return action
            return self._point(action, self._finder.aim(place, window, x, y))
        window, waited = self._window_for(place)
        if window is None:
            return None
        if waited:                               # it came only now: the click keeps its recorded pause after it
            if not self._pause(action.delay_ms / 1000.0):
                return None
            window = self._finder.recheck(window.hwnd, place)
            if window is None:
                raise self._finder.missing(place)
        drop = self._drop_of(action) if action.op in _DOWNS else None
        self._finder.bring_to_front(place, window.hwnd)
        point = self._settled(place, window, x, y)
        if point is None:
            return None
        if drop is not None:
            self._check_drop(*drop)
        self._input.expect_window(window.hwnd)
        return self._point(action, point)

    def _drop_of(self, action: MacroAction):
        """A drag's drop -- its release over another recorded window than its press: (that place, x, y); else None."""
        recording = self._macro.recording
        press = recording.events[action.source]
        if press.place is None or press.place[0] == UNKNOWN_PLACE:
            return None
        for event in recording.events[action.source + 1:]:
            if event.button is press.button and event.kind.value == "button_up":
                if event.place is None or event.place[0] == UNKNOWN_PLACE:
                    return None
                place = recording.places[event.place[0]]
                if place.window == recording.places[press.place[0]].window:
                    return None
                return place, event.place[1], event.place[2]
        return None

    def _check_drop(self, place, x: int, y: int) -> None:
        """Before a drag is pressed (the person's decision of 2026-10-26): the window it drops into is there and
        is the one at the drop point -- else nothing is pressed (once pressed, the button is let go wherever the
        pointer is)."""
        window = self._known(place)
        if window is None:
            raise self._finder.missing(place)
        point = self._finder.aim(place, window, x, y)
        if self._finder.window_at(*point) != window.hwnd:
            raise self._finder.drop_covered(place)

    def _aim_release(self, action: MacroAction, event) -> MacroAction:
        """A release over another recorded window than its press (a drop) goes into that window where it is now;
        anything else follows the press. Never waited for, never refused: a release presses nothing."""
        if event.place is not None and event.place[0] != UNKNOWN_PLACE:
            number, x, y = event.place
            place = self._macro.recording.places[number]
            if self._press_window is None or place.window != self._press_window:
                window = self._known(place)
                if window is not None:
                    try:
                        return self._point(action, self._finder.aim(place, window, x, y))
                    except PlaceRefused:
                        pass
        return self._offset(action, self._press) if self._press is not None else action

    def _known(self, place):
        """The window of `place`'s recorded window as it is now: the one found for it earlier in this run (read
        again), else the one that matches now (bound to it). A window open when the recording started is looked for
        among those open when the run began; one that opened during the recording among the others. None when there
        is none."""
        hwnd = self._bound.get(place.window)
        if hwnd is not None:
            return self._finder.recheck(hwnd, place)
        bound = set(self._bound.values())
        if place.at_start:
            window = self._finder.find(place, excluded=bound, only=self._at_run_start)
        else:
            window = self._finder.find(place, excluded=bound | self._at_run_start)
        if window is not None:
            self._bound[place.window] = window.hwnd
        return window

    def _window_for(self, place):
        """(the window to click in, whether it was waited for); (None, ...) when the run was stopped or cancelled
        meanwhile. A window open when the recording started, or one found earlier in this run that has closed, is
        not waited for; one that opened during the recording, one hidden for a moment, or one that does not take
        clicks yet, is -- at most `wait_s`. Raises PlaceRefused."""
        deadline, waited = self._clock() + self._wait_s, False
        while True:
            if self._abort_reason() is not None:
                return None, waited
            hwnd = self._bound.get(place.window)
            window = self._known(place)
            if window is not None and window.enabled:
                return window, waited
            if window is None and (place.at_start if hwnd is None else not self._finder.exists(hwnd)):
                raise self._finder.missing(place)        # it was there: gone now -- not waited for
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise self._finder.missing(place) if window is None else self._finder.disabled(place)
            waited = True
            self._sleep(min(remaining, PLACE_POLL_S))

    def _settled(self, place, window, x: int, y: int) -> tuple[int, int] | None:
        """The point once the window can be read and is the one at it -- it may have just been brought up from
        behind or from the taskbar, which Windows does a moment later: at most `settle_s`. After that the point as
        it is (the press is then refused if another window is still on top), or the last reason it could not be
        aimed. None when the run was stopped or cancelled meanwhile."""
        deadline, refusal = self._clock() + self._settle_s, None
        while True:
            try:
                point = self._finder.aim(place, window, x, y)
            except PlaceRefused as exc:
                point, refusal = None, exc
            if point is not None and self._finder.window_at(*point) == window.hwnd:
                return point
            if self._abort_reason() is not None:
                return None
            remaining = deadline - self._clock()
            if remaining <= 0:
                if point is None:
                    raise refusal
                return point
            self._sleep(min(remaining, PLACE_POLL_S))

    def _pause(self, seconds: float) -> bool:
        end = self._clock() + max(0.0, min(seconds, self._wait_s))
        while self._clock() < end:
            if self._abort_reason() is not None:
                return False
            self._sleep(min(end - self._clock(), self._tick))
        return self._abort_reason() is None


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
    if macro.recording.places:                   # the window of each click is known: each is played in it
        finder = PlaceFinder(backend, os.getpid() if own_pid is None else own_pid)
        return PlacedPlaybackEngine(macro, target, session, stop, dry_run=dry_run,
                                    allow_different_target=allow_different_target, refusals=refusals, guard=guard,
                                    finder=finder, layout=target.layout, **engine_options)
    return ScreenPlaybackEngine(macro, target, session, stop, dry_run=dry_run,
                                allow_different_target=allow_different_target, refusals=refusals, guard=guard,
                                **engine_options)
