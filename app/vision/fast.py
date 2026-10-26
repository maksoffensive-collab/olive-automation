"""The picture search made fast with OpenCV and NumPy -- optional (docs/DEPENDENCIES.md): this is the ONE module that
imports them. Without them `template.py` measures every place itself (the same measure, far slower), PNG pictures
are decoded by the standard library, and a search in any size is refused.

What a match IS is defined once, in `template.py` (`confidence`): the likeness of the shapes (normalised correlation,
as OpenCV's TM_CCOEFF_NORMED defines it) times the likeness of the average colours. Here OpenCV computes an
approximation of that confidence at every place in one pass (`score_map`, float32 -- both pictures less the
template's average first, so the sums stay small near a match and keep their precision), and every place it puts
within MARGIN of the threshold is then measured exactly (`sums`: whole numbers, the same ones the standard-library
search adds up). So the answer is the standard-library search's -- the same places, confidences and order -- as long
as the map is within MARGIN of the exact value near the threshold (tests.test_vision_fast holds it to that on noise,
smooth, bright and low-contrast pictures, ramps, and templates one pixel wide). Only when more places come that close
than can be measured in a fraction of a second (LIMIT_PLACES, MEASURE_VALUES) are they first thinned to the map's
local maxima. A template of one flat colour has no shape: 0 at every place, as the exact measure says.

A search in any size (`best_size`) looks at every size from `low` to `high` on a smaller copy of the area first
(coarse steps of STEP, the shape on grey -- in colour when the grey template is flat -- times the colour), then
measures the most promising places again at full size and in colour, in steps of 1% and then of a pixel around their
size: the size and the place where the picture is found best. `check` is called between the parts of a search (the
strips of a large area, the sizes, the looks); it raises to end it.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

MARGIN = 0.02                   # the float32 map vs the exact measure: a place this close is measured exactly
MEASURE_VALUES = 200_000_000    # template values measured exactly per search, at most ...
LIMIT_PLACES = 20_000           # ... and places (each costs a few microseconds of Python): a fraction of a second ...
CLOSEST = 16                    # ... and always the best places, also below the threshold: what came closest
MEMORY = 64 << 20               # bytes for the places measured at a time, at most
STEP = 1.06                     # the coarse steps between sizes in a search in any size
FINE = 1.01                     # the fine steps around a promising size
COARSE_PIXELS = 150_000         # a search in any size first looks at a copy of the area of about this many pixels ...
COARSE_SIDE = 8                 # ... in which the picture keeps at least this many pixels a side, when it has them
COARSE_PEAKS = 8                # the best places kept per size there ...
PROMISING = 8                   # ... and of all of them, places measured again at full size: at most this many,
NEARLY = 0.15                   # each at most this much below the best place there
FLAT = 2.0                      # a grey template varying less than this (standard deviation) is searched in colour
STRIP_PIXELS = 600_000          # a large area's map is made in strips of about this many pixels ...
WORKERS = min(8, os.cpu_count() or 1)   # ... this many at a time (OpenCV makes one strip on about one core)


def _view(rgb: bytes, width: int, height: int) -> np.ndarray:
    return np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)


def shape_map(area: np.ndarray, pattern: np.ndarray) -> np.ndarray:
    """The normalised correlation (float32, 0 when negative) at every place of `area`, one value per top-left
    corner. Both pictures are first less the template's average (per channel): the correlation does not change, and
    near a match the values OpenCV adds up in float32 are small, so they keep their precision."""
    channels = 1 if pattern.ndim == 2 else pattern.shape[2]
    values = pattern.reshape(-1, channels)
    if (values == values[0]).all():          # one flat colour: no shape anywhere (OpenCV would say 1.0 everywhere)
        return np.zeros((area.shape[0] - pattern.shape[0] + 1, area.shape[1] - pattern.shape[1] + 1), np.float32)
    wanted = values.mean(axis=0).astype(np.float32)
    if pattern.ndim == 2:
        wanted = wanted[0]
    shape = cv2.matchTemplate(area.astype(np.float32) - wanted, pattern.astype(np.float32) - wanted,
                              cv2.TM_CCOEFF_NORMED)
    np.nan_to_num(shape, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    np.clip(shape, 0.0, 1.0, out=shape)
    return shape


def _colour(area: np.ndarray, pattern: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """1 - the largest difference of a channel's average / 255, at every place (float32)."""
    th, tw = pattern.shape[:2]
    means = cv2.boxFilter(area, cv2.CV_32F, (tw, th), anchor=(0, 0), normalize=True)
    wanted = pattern.reshape(-1, 3).mean(axis=0, dtype=np.float64)
    r, g, b = cv2.split(cv2.absdiff(means, (float(wanted[0]), float(wanted[1]), float(wanted[2]), 0.0)))
    largest = cv2.max(cv2.max(r, g), b)[:rows, :cols]          # (numpy's max over 3 channels is ten times slower)
    return 1.0 - largest / np.float32(255.0)


def score_map(area: np.ndarray, pattern: np.ndarray) -> np.ndarray:
    """An approximation (float32) of the confidence at every place of `area`: the shape (`shape_map`) times the
    colour."""
    shape = shape_map(area, pattern)
    return shape * _colour(area, pattern, *shape.shape)


def _peaks(scores: np.ndarray, floor: float, limit: int, *, reach: tuple[int, int] = (1, 1)) -> tuple[np.ndarray, np.ndarray]:
    """(ys, xs) of the local maxima of `scores` at `floor` or above -- each the best within `reach` (x, y) places
    around it -- the `limit` best of them, best first."""
    rx, ry = reach
    top = cv2.dilate(scores, np.ones((2 * ry + 1, 2 * rx + 1), np.uint8)) if rx or ry else scores
    ys, xs = np.nonzero((scores >= floor) & (scores >= top))
    return _best(scores, ys, xs, limit)


def _best(scores: np.ndarray, ys: np.ndarray, xs: np.ndarray, limit: int) -> tuple[np.ndarray, np.ndarray]:
    if len(ys) > limit:
        keep = np.argpartition(-scores[ys, xs], limit - 1)[:limit]
        ys, xs = ys[keep], xs[keep]
    order = np.argsort(-scores[ys, xs], kind="stable")
    return ys[order], xs[order]


def _in_strips(area: np.ndarray, pattern: np.ndarray, check) -> np.ndarray:
    """`score_map` of a large area, made in horizontal strips of about STRIP_PIXELS each, WORKERS at a time (the same
    values: each strip is the area's rows its places need; at least as many rows of places as the template is tall,
    so the rows two strips share are at most as many as their own). `check` is called before each strip: the stop
    is heard within a strip's time, and once it is, no strip starts."""
    th = pattern.shape[0]
    rows = area.shape[0] - th + 1                                  # one row of places per top-left row
    per_strip = max(th, STRIP_PIXELS // area.shape[1])
    tops = list(range(0, rows, per_strip))

    def strip(top: int) -> np.ndarray:
        if check is not None:
            check()
        bottom = min(rows, top + per_strip)
        return score_map(np.ascontiguousarray(area[top:bottom + th - 1]), pattern)
    if len(tops) == 1 or WORKERS == 1:
        return np.vstack([strip(top) for top in tops]) if len(tops) > 1 else strip(0)
    with ThreadPoolExecutor(min(len(tops), WORKERS)) as pool:
        futures = [pool.submit(strip, top) for top in tops]
        try:
            parts = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()                                    # the strips not started never start
            raise
    return np.vstack(parts)


def shortlist(rgb: bytes, width: int, height: int, region, template_rgb: bytes, tw: int, th: int,
              floor: float, check=None) -> list[tuple[int, int]]:
    """The places in `region` -- (y, x) in the frame -- to measure exactly for a threshold of `floor`: every place
    the map puts within MARGIN of it, and the CLOSEST best local maxima also when they are below it, so what came
    closest can be said when nothing is found. When more places are that close than LIMIT_PLACES and MEASURE_VALUES
    allow, only the local maxima among them are kept (the best within a place of each other -- never two places that do not overlap),
    and then the best of those. `check` runs between the strips."""
    area = _view(rgb, width, height)[region.y:region.bottom, region.x:region.right]
    scores = _in_strips(area, _view(template_rgb, tw, th), check)
    allowed = max(CLOSEST, min(LIMIT_PLACES, MEASURE_VALUES // (tw * th * 3)))
    reach = (min(1, tw - 1), min(1, th - 1))
    ys, xs = np.nonzero(scores >= floor - MARGIN)
    if len(ys) > allowed:
        ys, xs = _peaks(scores, floor - MARGIN, allowed, reach=reach)
    else:
        ys, xs = _best(scores, ys, xs, allowed)
    cy, cx = _peaks(scores, 0.0, CLOSEST, reach=reach)
    places = dict.fromkeys((int(y) + region.y, int(x) + region.x) for y, x in zip(ys, xs))
    places.update(dict.fromkeys((int(y) + region.y, int(x) + region.x) for y, x in zip(cy, cx)))
    return list(places)


def sums(rgb: bytes, width: int, height: int, template_rgb: bytes, tw: int, th: int,
         places: list[tuple[int, int]], check=None) -> list[tuple[int, tuple[int, int, int], int]]:
    """For each (y, x): the sum of the products of the frame's values there with the template's, the sums of each
    channel and the sum of the squares -- whole numbers, exact (float64 holds every one of them: each is far below
    2**53). Measured in pieces of at most MEMORY bytes; `check` runs before each piece."""
    if not places:
        return []
    frame = _view(rgb, width, height)
    patches = np.lib.stride_tricks.sliding_window_view(frame, (th, tw, 3))
    pattern = _view(template_rgb, tw, th).reshape(-1).astype(np.float64)
    n = tw * th * 3
    chunk = max(1, MEMORY // (n * 8))
    out: list[tuple[int, tuple[int, int, int], int]] = []
    for start in range(0, len(places), chunk):
        if check is not None:
            check()
        part = places[start:start + chunk]
        ys, xs = np.array([p[0] for p in part]), np.array([p[1] for p in part])
        values = patches[ys, xs, 0].reshape(len(part), n).astype(np.float64)
        cross = values @ pattern
        channels = values.reshape(len(part), tw * th, 3).sum(axis=1)
        squares = np.einsum("ij,ij->i", values, values)
        for c, s, q in zip(cross.tolist(), channels.tolist(), squares.tolist()):
            out.append((int(c), (int(s[0]), int(s[1]), int(s[2])), int(q)))
    return out


def resize(rgb: bytes, width: int, height: int, w: int, h: int) -> bytes:
    """The picture at `w` x `h`: averaged when made smaller, interpolated when made larger."""
    method = cv2.INTER_AREA if w * h < width * height else cv2.INTER_LINEAR
    return cv2.resize(_view(rgb, width, height), (w, h), interpolation=method).tobytes()


def decode_png(data: bytes, width: int, height: int) -> bytes | None:
    """The RGB values of a PNG `bitmap.py` has already checked (its chunks, size, depth and colour type) -- exactly
    as stored: never turned by an orientation the file may carry -- or None when OpenCV cannot read it as a
    `width` x `height` picture (the caller then decodes it itself)."""
    picture = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if picture is None or picture.shape != (height, width, 3) or picture.dtype != np.uint8:
        return None
    return cv2.cvtColor(picture, cv2.COLOR_BGR2RGB).tobytes()


def _sizes(low: float, high: float) -> list[float]:
    """The coarse sizes from `low` to `high`: 1.0 and every STEP up and down from it, and the two ends -- nearest
    to 1.0 first."""
    out = {1.0, low, high}
    for direction in (STEP, 1 / STEP):
        f = direction
        while low <= f <= high:
            out.add(f)
            f *= direction
    return sorted(out, key=lambda f: (abs(math.log(f)), f))


def best_size(rgb: bytes, width: int, height: int, region, template_rgb: bytes, tw: int, th: int, *,
              low: float, high: float, min_side: int, check) -> tuple[float, int, int, int, int] | None:
    """The size, from `low` to `high` times the template's own, at which it is found best in `region`, and where:
    (factor, w, h, y, x) with (y, x) in the frame, or None when it fits at no size there. A size is passed over when
    the picture would be smaller than `min_side` (or than it is, when it is smaller) or larger than the area."""
    area = np.ascontiguousarray(_view(rgb, width, height)[region.y:region.bottom, region.x:region.right])
    pattern = _view(template_rgb, tw, th)
    in_colour = float(cv2.cvtColor(pattern, cv2.COLOR_RGB2GRAY).std()) < FLAT   # its shape is only in its colours
    look_at = area if in_colour else cv2.cvtColor(area, cv2.COLOR_RGB2GRAY)
    shape_of = pattern if in_colour else cv2.cvtColor(pattern, cv2.COLOR_RGB2GRAY)
    min_w, min_h = min(min_side, tw), min(min_side, th)

    def size_at(f: float) -> tuple[int, int] | None:
        w, h = round(tw * f), round(th * f)
        if w < min_w or h < min_h or w > region.width or h > region.height:
            return None
        return w, h

    def made_smaller(picture: np.ndarray, w: int, h: int) -> np.ndarray:
        method = cv2.INTER_AREA if w * h < picture.shape[0] * picture.shape[1] else cv2.INTER_LINEAR
        return cv2.resize(picture, (w, h), interpolation=method)

    # coarse: every size on a smaller copy of the area -- the shape (grey, or colour) times the colour -- the best
    # places of each kept
    whole = max(1.0, math.sqrt(region.width * region.height / COARSE_PIXELS))
    copies: dict[float, tuple[np.ndarray, np.ndarray]] = {1.0: (look_at, area)}   # per reduction: shape, colour
    found: list[tuple[float, float, float, float, float]] = []   # (score, factor, centre x, centre y, reduction)
    for f in _sizes(low, high):
        size = size_at(f)
        if size is None:
            continue
        check()
        w, h = size
        reduction = max(1.0, min(whole, min(w, h) / COARSE_SIDE))
        reduction = 2 ** (math.floor(2 * math.log2(reduction)) / 2)          # shared steps of sqrt(2)
        if reduction not in copies:
            cw_area, ch_area = max(1, round(region.width / reduction)), max(1, round(region.height / reduction))
            copies[reduction] = (made_smaller(look_at, cw_area, ch_area), made_smaller(area, cw_area, ch_area))
        shape_copy, colour_copy = copies[reduction]
        cw, ch = max(1, round(w / reduction)), max(1, round(h / reduction))
        if cw > shape_copy.shape[1] or ch > shape_copy.shape[0]:
            continue
        shape = shape_map(shape_copy, made_smaller(shape_of, cw, ch))
        scores = shape * _colour(colour_copy, made_smaller(pattern, cw, ch), *shape.shape)
        ys, xs = _peaks(scores, 0.0, COARSE_PEAKS)
        found.extend((float(scores[y, x]), f, (x + cw / 2) * reduction, (y + ch / 2) * reduction, reduction)
                     for y, x in zip(ys, xs))
    if not found:
        return None

    # the most promising places (one per place: its best size)
    found.sort(key=lambda c: (-c[0], abs(math.log(c[1]))))
    kept: list[tuple[float, float, float, float, float]] = []
    for candidate in found:
        if candidate[0] < found[0][0] - NEARLY:
            break
        _, f, cx, cy, _ = candidate
        w, h = size_at(f)
        if all(abs(cx - k[2]) >= w / 2 or abs(cy - k[3]) >= h / 2 for k in kept):
            kept.append(candidate)
            if len(kept) == PROMISING:
                break

    def look(f: float, cx: float, cy: float, pad: int) -> tuple[float, float, int, int, int, int] | None:
        """(score, factor, w, h, y, x in the area) of the best place at size `f` near the centre (cx, cy)."""
        check()
        size = size_at(f)
        if size is None:
            return None
        w, h = size
        left, top = max(0, round(cx - w / 2) - pad), max(0, round(cy - h / 2) - pad)
        right, bottom = min(region.width, left + w + 2 * pad), min(region.height, top + h + 2 * pad)
        left, top = max(0, min(left, right - w)), max(0, min(top, bottom - h))
        if right - left < w or bottom - top < h:
            return None
        picture = np.frombuffer(resize(template_rgb, tw, th, w, h), np.uint8).reshape(h, w, 3)
        scores = score_map(area[top:bottom, left:right], picture)
        y, x = np.unravel_index(int(np.argmax(scores)), scores.shape)
        return float(scores[y, x]), round(w / tw, 2), w, h, int(y) + top, int(x) + left

    def better(a, b) -> bool:
        return b is None or (a[0], -abs(math.log(a[1]))) > (b[0], -abs(math.log(b[1])))

    # fine: from each promising place's size, 1% at a time towards the better side while the likeness grows (at
    # most a coarse step either way), in colour, at full size, in a small area that follows the best place
    best: tuple[float, float, int, int, int, int] | None = None       # (score, factor, w, h, y, x in the area)
    for _, f0, cx, cy, reduction in kept:
        pad = math.ceil(reduction) + 2
        here = look(f0, cx, cy, pad)
        if here is None:
            continue
        for direction in (FINE, 1 / FINE):
            f, last = f0, here
            while True:
                f *= direction
                if not (low <= f <= high) or abs(math.log(f / f0)) > math.log(STEP) + 1e-9:
                    break
                step = look(f, last[5] + last[2] / 2, last[4] + last[3] / 2, pad)
                if step is None or step[0] <= last[0]:
                    break
                last = step
            if better(last, here):
                here = last
                break                    # the other side is downhill from where this one started
        # then one pixel at a time on the longer side: 1% of a large picture is more than a pixel
        side = max(tw, th)
        for direction in (1, -1):
            last = here
            while True:
                f = ((last[2] if tw >= th else last[3]) + direction) / side
                if not (low <= f <= high) or abs(math.log(f / f0)) > math.log(STEP) + 1e-9:
                    break
                step = look(f, last[5] + last[2] / 2, last[4] + last[3] / 2, pad)
                if step is None or (step[2], step[3]) == (last[2], last[3]) or step[0] <= last[0]:
                    break
                last = step
            if better(last, here):
                here = last
                break
        if better(here, best):
            best = here
    if best is None:
        return None
    return best[1], best[2], best[3], best[4] + region.y, best[5] + region.x
