"""The size a recording or a script was made at, and whether another size can be adapted to.

A size is (width, height) in the pixels of the coordinate space a recording's points or a script's areas are in: a
target's own drawing area, a device's screen, the whole screen. Adapting to another size (the person's decision of
2026-10-20) is a scale. For what ACTS at a point nobody looked at -- a recorded click, a click at a distance from
what was found -- it is only done when the proportions are the same (`fit`): within PROPORTION_TOLERANCE the two
directions are scaled each by its own factor; beyond it the proportions changed (16:9 became 21:9, a target was made
only wider), what was at a point may now be somewhere else entirely, and it is refused rather than guessed. What
only LOOKS -- a condition's point, area and picture -- follows any size, each direction by its own factor
(`factors`; decided on the person's report of 2026-10-25 that an area picked on the window must stay on the same part
of it when the window is resized): a look that misses clicks nothing, and a click ON what it found follows it.
"""
from __future__ import annotations

from app.providers.base import MAX_SIZE, check_size  # noqa: F401 (the one place to ask)

PROPORTION_TOLERANCE = 0.02     # 2%: rounding of window sizes, a border or two


class SizeChanged(ValueError):
    """The size changed in a way that cannot be adapted to: other proportions."""


def factors(made_on: tuple[int, int], now: tuple[int, int]) -> tuple[float, float] | None:
    """The factors (x, y) from `made_on` to `now`, in any proportions; None when the size is the same."""
    (w0, h0), (w1, h1) = check_size(made_on), check_size(now)
    if (w0, h0) == (w1, h1):
        return None
    return w1 / w0, h1 / h0


def fit(made_on: tuple[int, int], now: tuple[int, int]) -> tuple[float, float] | None:
    """The factors (x, y) from `made_on` to `now`; None when the size is the same; `SizeChanged` when the proportions
    are not the same within PROPORTION_TOLERANCE."""
    (w0, h0), (w1, h1) = check_size(made_on), check_size(now)
    if (w0, h0) == (w1, h1):
        return None
    sx, sy = w1 / w0, h1 / h0
    if abs(sx / sy - 1) > PROPORTION_TOLERANCE:
        raise SizeChanged(f"Made at {w0}x{h0}, now {w1}x{h1}: other proportions, so what was at a point may now be "
                          "somewhere else entirely; nothing is guessed.")
    return sx, sy
