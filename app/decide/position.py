"""Where a block of a script is drawn: inert presentation data, never consulted by a run.

A step may carry `at` -- the top-left corner of its block in the editor's scene units (whole numbers, at zoom 1.0),
or None to be placed automatically. It is part of the step's value only so that it travels with the step through
every edit (insert, delete, move, copy) without any bookkeeping, and is stored in the file next to the step. What
a script DOES is decided by its steps and their order alone; a block placed anywhere on the canvas runs exactly
where the arrows say.
"""
from __future__ import annotations

from .errors import InvalidDecision

MAX_POSITION = 100_000          # a scene this large is already far beyond any screen

Position = tuple[int, int] | None


def check_position(at: object, what: str = "at") -> Position:
    """None, or two whole numbers from 0 to MAX_POSITION (as a tuple)."""
    if at is None:
        return None
    if not isinstance(at, (tuple, list)) or len(at) != 2 \
            or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_POSITION for v in at):
        raise InvalidDecision(f"{what} must be two whole numbers from 0 to {MAX_POSITION}, or nothing.")
    return (at[0], at[1])
