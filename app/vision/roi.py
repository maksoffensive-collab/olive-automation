"""Rectangular region of interest in screenshot pixel coordinates (origin top-left)."""
from __future__ import annotations

from dataclasses import dataclass

from .bitmap import VisionError


@dataclass(frozen=True)
class Roi:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for v in (self.x, self.y, self.width, self.height):
            if isinstance(v, bool) or not isinstance(v, int):
                raise VisionError("ROI values must be integers.")
        if self.x < 0 or self.y < 0:
            raise VisionError("ROI origin must not be negative.")
        if self.width <= 0 or self.height <= 0:
            raise VisionError("ROI width and height must be positive.")

    @property
    def right(self) -> int:  # exclusive
        return self.x + self.width

    @property
    def bottom(self) -> int:  # exclusive
        return self.y + self.height

    def check_within(self, width: int, height: int) -> "Roi":
        """Return self if the ROI lies completely inside a width x height image, else raise."""
        if self.right > width or self.bottom > height:
            raise VisionError(f"ROI {self.width}x{self.height} at ({self.x}, {self.y}) is outside the "
                              f"{width}x{height} image.", code="area_outside_image",
                              area=f"{self.width}×{self.height}", x=self.x, y=self.y, image=f"{width}×{height}")
        return self

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    @classmethod
    def full(cls, width: int, height: int) -> "Roi":
        return cls(0, 0, width, height)


def resolve_roi(roi: Roi | None, width: int, height: int) -> Roi:
    """None means the whole image; a given ROI must fit inside it."""
    if roi is None:
        return Roi.full(width, height)
    if not isinstance(roi, Roi):
        raise VisionError("roi must be a Roi or None.")
    return roi.check_within(width, height)
