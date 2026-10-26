"""A Full Windows recording played click by click in the window each click was in (the person's list of 2026-10-25,
item 12, and their answers of 2026-10-26).

A recording on the whole desktop keeps, for each press and release, the window under it
(`app.providers.recording.Place`: its program, class, title, client size, whether it was open when the recording
started, and a number for that recorded window) and the point inside that window's client area
(app/windows/recorder.py). Playing it, each such click is aimed anew just before it is sent
(`playback.PlacedPlaybackEngine`):

  * a window is found again by its program, class AND title -- the one visible top-level window that matches, never
    one of this application's; when none or several match, it is not there (nothing is guessed: the programs that
    host many apps, every folder window of the file manager, every dialog of a program share their program and
    class -- the independent review of 2026-10-26). Once found, the later clicks of the same recorded window follow
    that very window (its title may change as the run goes on), re-read before each; two recorded windows are never
    the same window; and a window that opened during the recording must be one that opened during the run;
  * a window open when the recording started must be there, at a size of the same proportions, before the first
    click (a preflight issue: nothing is sent, and the window is named); one that opened during the recording -- a
    dialog an earlier click opened -- is waited for, at most PLACE_WAIT_S, when its click comes, and so is a window
    that does not take clicks yet (disabled, behind its own dialog); the run stops before that click when it does
    not come (earlier clicks were sent by then). The emergency stop and a cancel end every wait at once; after a
    wait the click keeps its recorded pause, and the clicks after it are timed from it;
  * the click goes to the same point of the window's client area where the window is now; a window resized in the
    same proportions (within `app.providers.size.PROPORTION_TOLERANCE`) gets the point scaled -- a point outside
    the client area (the title bar, a border) keeps its distance from the nearest edge instead; other proportions
    are refused before that click; a window whose scale cannot be established (`dpi_ambiguous`) is refused;
  * before a press the window is brought to the front (without taking the focus: the click itself activates it, as
    the person's own would) and given up to RAISE_SETTLE_S to show at the point; the press is refused when another
    window is still on top there (`DesktopInputSession.expect_window`). A drag's moves follow its press; its
    release goes into the window it was recorded over (a drop into another window) -- which must be there and be
    the one at the drop point before the drag is pressed (the person's decision of 2026-10-26) -- else follows the
    press; a release is never refused (it presses nothing). A move with no button held follows the last press;
  * a press whose window could not be identified when it was recorded refuses the run (never its old screen
    point); a click added by hand in the editor goes to the screen point picked (the person's decision);
  * a dry run sends nothing, raises nothing and waits for nothing: it checks the windows open at the start, and
    aims a click where its window is already there.

Every gate of `DesktopInputSession` still applies to the aimed point: the screen layout, a monitor under the point,
never on this application's own windows, the person's takeover.
"""
from __future__ import annotations

from typing import Iterable

from app.playback.preflight import Issue
from app.providers.recording import Place, clean_place_text
from app.providers.size import SizeChanged, fit
from app.windows.backend import WindowBackend, WindowInfo
from app.windows.input import InputRefused

PLACE_WAIT_S = 10.0      # a window an earlier click opened, or one that does not take clicks yet: the longest wait
PLACE_POLL_S = 0.1
RAISE_SETTLE_S = 1.0     # a window just brought up from behind or from the taskbar: the longest wait for it to be up


class PlaceRefused(InputRefused):
    """A click of a Full Windows recording cannot be aimed at its window; nothing was sent for it. `reason_code`:
    place_missing (not there, or alike windows not told apart), place_size (other proportions), place_disabled
    (it does not take clicks), place_dpi (its scale cannot be established)."""

    def __init__(self, message: str, *, code: str, params: tuple[tuple[str, object], ...]) -> None:
        super().__init__(message)
        self.reason_code, self.params = code, params


def same_window(place: Place, window: WindowInfo) -> bool:
    """`window` is of the place's program and class (titles aside)."""
    return (clean_place_text(window.process_name).lower() == place.program.lower()
            and clean_place_text(window.class_name) == place.kind)


def find_window(place: Place, windows: Iterable[WindowInfo], *, ours, excluded=frozenset(),
                only=None) -> WindowInfo | None:
    """The ONE window `place` names among `windows` (or those of them in `only`): of its program and class and with
    its title (compared as the recorder kept it); never one of this application's (`ours(hwnd)`) nor one in
    `excluded`. None otherwise."""
    found = [w for w in windows if w.top_level and w.visible and not w.cloaked and w.hwnd not in excluded
             and (only is None or w.hwnd in only)
             and same_window(place, w) and clean_place_text(w.title) == place.title and not ours(w.hwnd)]
    return found[0] if len(found) == 1 else None


def _along(value: int, made: int, now: int, factor: float) -> int:
    if value < 0:
        return value                             # left of / above the client area: the same distance from its edge
    if value >= made:
        return now + (value - made)              # right of / below it: likewise
    return min(now - 1, max(0, round(value * factor)))


def screen_point(place: Place, rect: tuple[int, int, int, int], x: int, y: int) -> tuple[int, int]:
    """The screen point of (x, y) of the place's client area, that client area being `rect` (left, top, width,
    height) now. `SizeChanged` when its proportions are not the place's."""
    left, top, width, height = rect
    factor = fit((place.width, place.height), (width, height))
    if factor is None:
        return left + x, top + y
    sx, sy = factor
    return left + _along(x, place.width, width, sx), top + _along(y, place.height, height, sy)


def _named(place: Place, **extra) -> tuple[tuple[str, object], ...]:
    return (("program", place.program), ("title", place.title)) + tuple(sorted(extra.items()))


class PlaceFinder:
    """Finds a recording's windows on the desktop now. `backend` reads windows; the only thing it changes is the
    Z order, in `bring_to_front` (never anything sent)."""

    def __init__(self, backend: WindowBackend, own_pid: int) -> None:
        self._backend, self._own_pid = backend, own_pid

    def _ours(self, hwnd: int) -> bool:
        return hwnd in tuple(self._backend.console_windows()) or self._backend.owner_pid(hwnd) == self._own_pid

    def windows(self) -> list[WindowInfo]:
        return list(self._backend.enumerate())

    def find(self, place: Place, *, excluded=frozenset(), only=None, windows=None) -> WindowInfo | None:
        return find_window(place, self.windows() if windows is None else windows, ours=self._ours,
                           excluded=excluded, only=only)

    def exists(self, hwnd: int) -> bool:
        """`hwnd` is still a window (perhaps hidden for a moment)."""
        return self._backend.query(hwnd) is not None

    def recheck(self, hwnd: int, place: Place) -> WindowInfo | None:
        """The window found earlier for `place`'s recorded window, read again: None when it is gone, hidden, or now
        another program's or class's (a reused handle), or this application's."""
        info = self._backend.query(hwnd)
        if info is None or not info.visible or info.cloaked or not same_window(place, info) or self._ours(hwnd):
            return None
        return info

    def window_at(self, x: int, y: int) -> int | None:
        return self._backend.window_at(x, y)

    def missing(self, place: Place) -> PlaceRefused:
        return PlaceRefused(f"The window “{place.title}” of {place.program} that the click was recorded in is not "
                            "there, or several alike cannot be told apart; nothing sent.", code="place_missing",
                            params=_named(place))

    def drop_covered(self, place: Place) -> PlaceRefused:
        return PlaceRefused(f"Another window covers the window “{place.title}” of {place.program} where the drag "
                            "drops; nothing pressed.", code="place_drop_covered", params=_named(place))

    def disabled(self, place: Place) -> PlaceRefused:
        return PlaceRefused(f"The window “{place.title}” of {place.program} does not take clicks (another of its "
                            "windows may be open over it); nothing sent.", code="place_disabled",
                            params=_named(place))

    def unscaled(self, place: Place) -> PlaceRefused:
        return PlaceRefused(f"The scale of the window “{place.title}” of {place.program} cannot be established, so "
                            "where a click would land cannot be told; nothing sent.", code="place_dpi",
                            params=_named(place))

    def aim(self, place: Place, window: WindowInfo, x: int, y: int) -> tuple[int, int]:
        """The screen point of the click at (x, y) of `place`, in `window` as it is now. Raises PlaceRefused."""
        if window.dpi_ambiguous:
            raise self.unscaled(place)
        rect = self._backend.client_screen_rect(window.hwnd)
        if rect is None or rect[2] <= 0 or rect[3] <= 0:
            raise self.missing(place)
        try:
            return screen_point(place, rect, x, y)
        except (SizeChanged, ValueError):
            raise PlaceRefused(f"The window “{place.title}” of {place.program} was {place.width}x{place.height} when "
                               f"the click was recorded and is {rect[2]}x{rect[3]} now: other proportions, so the "
                               "click would land on something else; nothing sent.", code="place_size",
                               params=_named(place, was=f"{place.width}x{place.height}", now=f"{rect[2]}x{rect[3]}")
                               ) from None

    def bring_to_front(self, place: Place, hwnd: int) -> bool:
        """Raise the window found for `place` -- read again first: only that very window, never this application's
        (PlaceRefused otherwise). True when it was minimised or covered (it needs a moment to come up)."""
        if self.recheck(hwnd, place) is None:
            raise self.missing(place)
        return bool(self._backend.raise_window(hwnd, activate=False))

    def issue(self, place: Place, windows, excluded) -> tuple[Issue | None, int | None]:
        """Before the first click: the problem with a window open when the recording started (None when there is
        none), and the window found for it (None when none)."""
        window = self.find(place, excluded=excluded, windows=windows)
        if window is None:
            refusal = self.missing(place)
        elif window.minimized:
            return None, window.hwnd             # brought back when its click comes; its size is read then
        else:
            try:
                self.aim(place, window, 0, 0)
                return None, window.hwnd
            except PlaceRefused as exc:
                refusal = exc
        return Issue(refusal.reason_code, str(refusal), params=refusal.params, deferrable=True), \
            (window.hwnd if window is not None else None)
