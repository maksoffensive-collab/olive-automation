"""Screen picker geometry: where a mark on the SHOWN screenshot is on the REAL one.

A screenshot is shown scaled down by a whole factor `n` (Tk's `subsample(n)` keeps every n-th pixel), so shown pixel
`d` is image pixel `d * n`. Everything a picker returns is converted back with that factor into the screenshot's
own pixel grid -- the frame's native resolution, which by the screenshot contract is also the target's input space.
Neither the size of the window on the desktop nor the screen's DPI enters the calculation: only the image's own
width and height, and the factor used to show it.

Pure arithmetic, no Tk, so it is tested directly.
"""
from __future__ import annotations

import math

from app.vision.roi import Roi


def display_factor(width: int, height: int, max_width: int, max_height: int) -> int:
    """The smallest whole factor that makes a `width` x `height` image fit into `max_width` x `max_height`."""
    if min(width, height, max_width, max_height) <= 0:
        raise ValueError("Sizes must be positive.")
    return max(1, math.ceil(width / max_width), math.ceil(height / max_height))


def shown_size(width: int, height: int, factor: int) -> tuple[int, int]:
    return math.ceil(width / factor), math.ceil(height / factor)


def to_image_point(x: float, y: float, factor: int, width: int, height: int) -> tuple[int, int]:
    """A click on the shown image -> the image pixel under it, clamped inside the image."""
    return (min(max(int(x), 0) * factor, width - 1), min(max(int(y), 0) * factor, height - 1))


def to_image_roi(x0: float, y0: float, x1: float, y1: float, factor: int, width: int,
                 height: int) -> Roi | None:
    """A rectangle dragged on the shown image (either corner first) -> the image region it covers, clamped
    inside the image. None when nothing is covered (a click, or a drag entirely outside)."""
    shown_w, shown_h = shown_size(width, height, factor)
    left, right = sorted((min(max(int(x0), 0), shown_w), min(max(int(x1), 0), shown_w)))
    top, bottom = sorted((min(max(int(y0), 0), shown_h), min(max(int(y1), 0), shown_h)))
    ix0, iy0 = min(left * factor, width), min(top * factor, height)
    ix1, iy1 = min(right * factor, width), min(bottom * factor, height)
    if ix1 - ix0 < 1 or iy1 - iy0 < 1:
        return None
    return Roi(ix0, iy0, ix1 - ix0, iy1 - iy0)


def to_shown_rect(roi: Roi, factor: int) -> tuple[int, int, int, int]:
    """An image region -> the rectangle to draw over the shown image (x0, y0, x1, y1)."""
    return (roi.x // factor, roi.y // factor, math.ceil(roi.right / factor), math.ceil(roi.bottom / factor))


def expand(roi: Roi, margin: int, width: int, height: int) -> Roi:
    """`roi` grown by `margin` on every side, kept inside the image: a search area around a template."""
    x0, y0 = max(roi.x - margin, 0), max(roi.y - margin, 0)
    x1, y1 = min(roi.right + margin, width), min(roi.bottom + margin, height)
    return Roi(x0, y0, x1 - x0, y1 - y0)


# ---- a preview that may also be enlarged (the Text screen) ---------------------------------------------------------
def fit_factors(width: int, height: int, max_width: int, max_height: int, *, max_zoom: int = 4) -> tuple[int, int]:
    """(down, up): show a `width` x `height` image at up/down of its size -- shrunk by the smallest whole factor that
    fits when it is larger than the room (exactly like the pickers), or enlarged by a whole factor (at most
    `max_zoom`) when it is much smaller. Whole factors keep every shown pixel an exact image pixel."""
    down = display_factor(width, height, max(max_width, 1), max(max_height, 1))
    if down > 1:
        return down, 1
    return 1, max(1, min(max_zoom, max_width // max(width, 1), max_height // max(height, 1)))


def shown_box(x: int, y: int, w: int, h: int, down: int, up: int) -> tuple[int, int, int, int]:
    """An image box (x, y, w, h) -> the rectangle (x0, y0, x1, y1) that covers it on the shown image."""
    return (x * up // down, y * up // down, math.ceil((x + w) * up / down), math.ceil((y + h) * up / down))


def to_image_point_scaled(x: float, y: float, down: int, up: int, width: int, height: int) -> tuple[int, int]:
    """A point on the shown image (already relative to its top-left corner) -> the image pixel under it, clamped."""
    return (min(max(int(x * down / up), 0), width - 1), min(max(int(y * down / up), 0), height - 1))
