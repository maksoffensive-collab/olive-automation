"""Deterministic screen-change detection between two Frames of the same instance."""
from __future__ import annotations

from dataclasses import dataclass

from .bitmap import VisionError
from .frame import Frame
from .roi import Roi, resolve_roi


@dataclass(frozen=True)
class ChangeResult:
    changed: bool               # ratio >= min_changed_ratio
    changed_pixels: int
    total_pixels: int
    ratio: float
    bounds: Roi | None          # bounding box of changed pixels (image coordinates)


def compare_frames(before: Frame, after: Frame, *, roi: Roi | None = None, pixel_threshold: int = 16,
                   min_changed_ratio: float = 0.01) -> ChangeResult:
    """A pixel counts as changed if any of R, G, B differs by MORE than `pixel_threshold`.
    Frames must be the same size and come from the same provider and target."""
    if not isinstance(before, Frame) or not isinstance(after, Frame):
        raise VisionError("compare_frames needs two Frames from the verified screenshot pipeline.")
    if not before.source.same_target(after.source):
        raise VisionError("Refusing to compare screenshots of different targets.")
    if (before.width, before.height) != (after.width, after.height):
        raise VisionError("Screenshots have different dimensions and cannot be compared.")
    if isinstance(pixel_threshold, bool) or not isinstance(pixel_threshold, int) or not 0 <= pixel_threshold <= 255:
        raise VisionError("pixel_threshold must be an integer between 0 and 255.")
    if isinstance(min_changed_ratio, bool) or not 0 < min_changed_ratio <= 1:
        raise VisionError("min_changed_ratio must be greater than 0 and at most 1.")
    r = resolve_roi(roi, before.width, before.height)
    a, b, w = before.bitmap.rgb, after.bitmap.rgb, before.width
    changed = 0
    minx = miny = 10**9
    maxx = maxy = -1
    for y in range(r.y, r.bottom):
        base = (y * w + r.x) * 3
        ra, rb = a[base:base + r.width * 3], b[base:base + r.width * 3]
        if ra == rb:
            continue
        for i in range(0, len(ra), 3):
            if (abs(ra[i] - rb[i]) > pixel_threshold or abs(ra[i + 1] - rb[i + 1]) > pixel_threshold
                    or abs(ra[i + 2] - rb[i + 2]) > pixel_threshold):
                x = r.x + i // 3
                changed += 1
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    total = r.width * r.height
    ratio = changed / total
    bounds = Roi(minx, miny, maxx - minx + 1, maxy - miny + 1) if changed else None
    return ChangeResult(changed > 0 and ratio >= min_changed_ratio, changed, total, ratio, bounds)
