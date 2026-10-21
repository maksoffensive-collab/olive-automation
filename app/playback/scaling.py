"""A recorded macro adapted to the size its target has now (the person's decision of 2026-10-20): every point scaled
by `app.providers.size.fit`; one that was inside the recorded size is kept inside the new one (rounding only), one
that was outside (a drag that went past the window's edge) stays outside, for the input session to refuse exactly
as it would refuse it unscaled -- nothing is moved to an edge. Only for a macro whose `adapt` is chosen and whose recording
knows the size it was made at; the provider's playback adapter decides when (it alone can read the size now)."""
from __future__ import annotations

from dataclasses import replace

from app.playback.macro import Macro
from app.providers.size import check_size, fit


def scaled_macro(macro: Macro, size: tuple[int, int]) -> Macro:
    """`macro` with every recorded point moved to where it is at `size`; `SizeChanged` for other proportions. The
    same macro when its size is unknown or already `size`."""
    size = check_size(size)
    made_on = macro.recording.size
    factors = None if made_on is None else fit(made_on, size)
    if factors is None:
        return macro
    sx, sy = factors
    width, height = size
    made_w, made_h = made_on

    def along(value: int, factor: float, made: int, now: int) -> int:
        moved = round(value * factor)
        if value < 0:
            return min(moved, -1)                # made smaller, -1 would round to 0: inside
        if value >= made:
            return moved                         # at least `now`: the factor is now / made
        return min(now - 1, max(0, moved))

    def moved(event):
        if event.x is None:
            return event
        return replace(event, x=along(event.x, sx, made_w, width), y=along(event.y, sy, made_h, height))
    events = tuple(moved(e) for e in macro.recording.events)
    return replace(macro, recording=replace(macro.recording, events=events, size=size))
