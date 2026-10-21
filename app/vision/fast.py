"""The exact template matcher's answer, found fast with OpenCV and NumPy -- optional (docs/DEPENDENCIES.md): this is
the ONE module that imports them, and `template.py` falls back to its own exact search when they are not installed.

The answer is exactly the exact matcher's, never an approximation. OpenCV only narrows down WHERE to look: its
sum of squared differences (SSD, `TM_SQDIFF`) is computed for every position in one fast pass, and since no
difference exceeds 255, SAD >= SSD / 255 -- so a position whose SSD is above 255 * limit cannot reach the
threshold and is passed over. Every position left is then measured with the exact SAD, in integers, as
`template.py` measures it; positions, confidences and the refusal of an undistinctive template are the same.
"""
from __future__ import annotations

import cv2
import numpy as np

CHUNK = 1024                    # positions measured exactly at a time, at most ...
MEMORY = 64 << 20               # ... and about this many bytes for them at most: a large picture, fewer at a time
BYTES_PER_VALUE = 7             # its copy (1), as int16 (2), the difference (2) and its absolute value (2)


def chunk_for(tw: int, th: int) -> int:
    """How many positions are measured at a time for a `tw` x `th` template: CHUNK, or fewer when they would not
    fit in MEMORY -- one at the least."""
    return max(1, min(CHUNK, MEMORY // (tw * th * 3 * BYTES_PER_VALUE)))


def margin(limit: float, tw: int, th: int) -> float:
    """How far above 255 * limit an SSD may be and still be measured. OpenCV's float32 SSD is the sum of the
    squares of both pictures less twice their products, each up to 65025 a value, so its rounding grows with the
    template (7252 seen for a bright 500 x 500 one): the margin grows with it, generously -- it can only add
    positions to measure, never lose one."""
    return 255.0 * limit * (1 + 1e-4) + 1e-5 * 2 * 65025 * tw * th * 3 + 1024.0


def candidates(rgb: bytes, width: int, height: int, region, template_rgb: bytes, tw: int, th: int,
               limit: float, max_candidates: int) -> list[tuple[int, int, int]] | None:
    """(sad, y, x) of every position in `region` whose exact SAD is at most `limit`; None when there are more than
    `max_candidates` of them (the caller refuses the template as not distinctive)."""
    frame = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    area = np.ascontiguousarray(frame[region.y:region.bottom, region.x:region.right])
    pattern = np.frombuffer(template_rgb, dtype=np.uint8).reshape(th, tw, 3)
    ssd = cv2.matchTemplate(area, pattern, cv2.TM_SQDIFF)           # float32, one value per position
    ys, xs = np.nonzero(ssd <= margin(limit, tw, th))
    patches = np.lib.stride_tricks.sliding_window_view(area, (th, tw, 3))[:, :, 0]
    pattern16 = pattern.astype(np.int16)
    found: list[tuple[int, int, int]] = []
    chunk = chunk_for(tw, th)
    for start in range(0, len(ys), chunk):
        y, x = ys[start:start + chunk], xs[start:start + chunk]
        sad = np.abs(patches[y, x].astype(np.int16) - pattern16).sum(axis=(1, 2, 3), dtype=np.int64)
        keep = sad <= limit
        found.extend(zip(sad[keep].tolist(), (y[keep] + region.y).tolist(), (x[keep] + region.x).tolist()))
        if len(found) > max_candidates:
            return None
    return found
