"""LDPlayer .record coordinates. Kept out of the target-agnostic vision layer on purpose.

`RecordPoint` is a raw x/y from an LDPlayer .record file. Its meaning is UNRESOLVED (hypotheses H2/H3 in
docs/MACRO_FORMAT.md), so converting it to screenshot pixels is refused unless the caller supplies an
explicit, clearly labelled `AssumedRecordCanvas`. Nothing in the project supplies one; no .record playback
uses this yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.vision.coords import CoordinateError, PixelPoint, _size


class CoordinateSpaceUnresolved(CoordinateError):
    pass


@dataclass(frozen=True)
class RecordPoint:
    x: int
    y: int


@dataclass(frozen=True)
class AssumedRecordCanvas:
    """ASSUMPTION, NOT VERIFIED (H2): .record coordinates live on a fixed canvas (e.g. 19200 x 10800) that is
    scaled to the screen. The caller must opt in explicitly."""
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise CoordinateError("Canvas size must be positive.")


def record_to_pixel(point: RecordPoint, screen_size: tuple[int, int],
                    canvas: AssumedRecordCanvas | None = None) -> PixelPoint:
    """Refuses unless an explicit AssumedRecordCanvas is given (the .record coordinate space is unresolved)."""
    if not isinstance(point, RecordPoint):
        raise CoordinateError("record_to_pixel needs a RecordPoint.")
    if canvas is None:
        raise CoordinateSpaceUnresolved(
            "The coordinate space of .record files is not verified (docs/MACRO_FORMAT.md, H2/H3); "
            "pass an explicit AssumedRecordCanvas to convert, knowing it is an assumption.")
    w, h = _size(screen_size)
    if not (0 <= point.x <= canvas.width and 0 <= point.y <= canvas.height):
        raise CoordinateError("The record point lies outside the assumed canvas.")
    return PixelPoint(min(point.x * w // canvas.width, w - 1), min(point.y * h // canvas.height, h - 1))
