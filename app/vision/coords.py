"""Target-agnostic coordinate spaces (different TYPES so they cannot be mixed by accident).

  PixelPoint      pixel in a screenshot, origin top-left, native resolution of that screenshot.
  NormalizedPoint position as a fraction (0..1) of the image, resolution independent.

Pixel <-> normalized uses pixel centres ((x + 0.5) / width), so a pixel survives a round trip exactly.
Coordinate spaces that belong to one target technology (raw units of a macro format, a native window
coordinate system, ...) live with that technology, not here. Nothing here sends input.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.errors import PlatformError


class CoordinateError(PlatformError):
    pass


@dataclass(frozen=True)
class PixelPoint:
    x: int
    y: int

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (self.x, self.y)):
            raise CoordinateError("Pixel coordinates must be integers.")


@dataclass(frozen=True)
class NormalizedPoint:
    u: float
    v: float

    def __post_init__(self) -> None:
        for c in (self.u, self.v):
            if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or not 0.0 <= c <= 1.0:
                raise CoordinateError("Normalized coordinates must be finite numbers between 0 and 1.")


def _size(size: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
        raise CoordinateError("Image size must be two positive integers.")
    return w, h


def pixel_to_normalized(p: PixelPoint, size: tuple[int, int]) -> NormalizedPoint:
    w, h = _size(size)
    if not (0 <= p.x < w and 0 <= p.y < h):
        raise CoordinateError(f"Pixel ({p.x}, {p.y}) is outside the {w}x{h} image.")
    return NormalizedPoint((p.x + 0.5) / w, (p.y + 0.5) / h)


def normalized_to_pixel(n: NormalizedPoint, size: tuple[int, int]) -> PixelPoint:
    w, h = _size(size)
    return PixelPoint(min(int(n.u * w), w - 1), min(int(n.v * h), h - 1))


def rescale_pixel(p: PixelPoint, from_size: tuple[int, int], to_size: tuple[int, int]) -> PixelPoint:
    """Map a pixel of a `from_size` screenshot to the same relative position in a `to_size` screen."""
    return normalized_to_pixel(pixel_to_normalized(p, from_size), to_size)
