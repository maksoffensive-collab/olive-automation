"""Deterministic colour detection on a Frame."""
from __future__ import annotations

from dataclasses import dataclass

from .bitmap import VisionError
from .frame import Frame
from .roi import Roi, resolve_roi


@dataclass(frozen=True)
class ColorMatch:
    found: bool                      # count >= min_pixels
    count: int                       # every matching pixel in the ROI
    bounds: Roi | None               # bounding box of all matches (None if no match)
    points: tuple[tuple[int, int], ...]  # first `max_points` matches in scan order (row by row), image coordinates


def find_color(frame: Frame, color: tuple[int, int, int], *, tolerance: int = 0, roi: Roi | None = None,
               min_pixels: int = 1, max_points: int = 1000) -> ColorMatch:
    """Find pixels whose R, G and B each differ from `color` by at most `tolerance` (0..255)."""
    if not isinstance(frame, Frame):
        raise VisionError("find_color needs a Frame from the verified screenshot pipeline.")
    if (len(color) != 3 or any(isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 255 for c in color)):
        raise VisionError("color must be three integers between 0 and 255.")
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
        raise VisionError("tolerance must be an integer between 0 and 255.")
    if min_pixels < 1 or max_points < 0:
        raise VisionError("min_pixels must be >= 1 and max_points >= 0.")
    r = resolve_roi(roi, frame.width, frame.height)
    tr, tg, tb = color
    data, w = frame.bitmap.rgb, frame.width
    count, points = 0, []
    minx = miny = 10**9
    maxx = maxy = -1
    for y in range(r.y, r.bottom):
        base = (y * w + r.x) * 3
        row = data[base:base + r.width * 3]
        for i in range(0, len(row), 3):
            if abs(row[i] - tr) <= tolerance and abs(row[i + 1] - tg) <= tolerance and abs(row[i + 2] - tb) <= tolerance:
                x = r.x + i // 3
                count += 1
                if len(points) < max_points:
                    points.append((x, y))
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    bounds = Roi(minx, miny, maxx - minx + 1, maxy - miny + 1) if count else None
    return ColorMatch(count >= min_pixels, count, bounds, tuple(points))
