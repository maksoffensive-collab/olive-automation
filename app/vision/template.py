"""Template matching: where does a small picture appear in a frame, and how alike is it there? Local and deterministic.

The measure (`confidence`), the same for every way of searching:

    confidence = shape x colour, rounded to 9 decimal places
    shape      = the normalised correlation of the two pictures (each channel less its average; OpenCV's
                 TM_CCOEFF_NORMED), 0 when negative or when the place is one flat colour
    colour     = 1 - the largest difference between the two pictures' average of a channel / 255

1.0 is the same picture. The shape is what tells pictures apart -- "PLAY" from "PLAN", a button from the background
around it -- and it hardly changes when a picture is drawn a little blurred or at another size; the colour keeps a
grey (disabled) button or a red one from passing for a green one of the same shape. A template of one flat colour
has no shape and is refused: look for a colour instead.

Places are taken best first (then by y, then by x) and never overlap, so equal inputs always give equal outputs.

Speed: with OpenCV and NumPy installed (optional, docs/DEPENDENCIES.md) `fast.py` finds the places worth measuring
in one pass (well under a second for a whole screen) and every confidence is then measured exactly, as below; without
them every place is measured here with the standard library (the same answer, far slower: minutes for a whole
screen). A search in any size needs them. `match_template_exact` always uses the standard-library search.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from operator import mul
from typing import Callable

from .bitmap import Bitmap, VisionError, fast_module
from .frame import Frame
from .roi import Roi, resolve_roi

MIN_SPREAD = 2.0          # a template whose values vary less than this (standard deviation) is one flat colour
ANY_SIZE = (0.5, 2.0)     # a search in any size: from half the template's size to twice it


@dataclass(frozen=True)
class TemplateMatch:
    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class TemplateSearch:
    """A search's answer: the places found (best first), the place that came closest whether or not it was found
    (None when it is not known: the standard-library search measures only the places that can be found), and the
    size, as a factor of the template's own, the picture was looked for at."""
    matches: tuple[TemplateMatch, ...]
    best: TemplateMatch | None
    scale: float = 1.0


def fast_matcher():
    """The fast search (`fast.py`) when OpenCV and NumPy can be loaded, else None (looked up once)."""
    return fast_module()


NEEDS_FAST = ("Looking for a picture in any size needs the optional fast picture search (requirements-vision.txt), "
              "which is not installed.")
TOO_PLAIN = ("The picture is one flat colour, so it has no shape to be found by: use a more distinctive picture, or "
             "look for the colour instead.")


def confidence(n: int, cross: int, sums: tuple[int, int, int], squares: int, tsums: tuple[int, int, int],
               tsquares: int) -> float:
    """The confidence (module docstring) of a place from whole-number sums over its `n` pixels: the products of its
    values with the template's (`cross`), each channel's values (`sums`) and their squares (`squares`); `tsums` and
    `tsquares` are the template's own. Computed in whole numbers to the last step, so any two ways of adding them
    up give exactly the same confidence."""
    spread = n * squares - sum(s * s for s in sums)              # n**2 x the variances (whole numbers, exact)
    tspread = n * tsquares - sum(t * t for t in tsums)
    if spread <= 0 or tspread <= 0:
        return 0.0                                               # a flat place (or template) has no shape
    together = n * cross - sum(s * t for s, t in zip(sums, tsums))
    shape = min(1.0, max(0.0, together / math.sqrt(spread * tspread)))
    colour = 1.0 - max(abs(s - t) for s, t in zip(sums, tsums)) / (255.0 * n)
    return round(shape * colour, 9)


def _template_sums(template: Bitmap) -> tuple[tuple[int, int, int], int]:
    rgb = template.rgb
    return (sum(rgb[0::3]), sum(rgb[1::3]), sum(rgb[2::3])), sum(map(mul, rgb, rgb))


def check_template(template: Bitmap) -> None:
    """Raise `VisionError` now -- before any search, before a run does anything -- if `template` is one flat colour
    (it has no shape to be found by)."""
    if not isinstance(template, Bitmap):
        raise VisionError("template must be a Bitmap (load one with load_png).")
    _check_spread(template)


def _check_spread(template: Bitmap) -> tuple[tuple[int, int, int], int]:
    tsums, tsquares = _template_sums(template)
    n = template.width * template.height
    variance = (n * tsquares - sum(t * t for t in tsums)) / (3 * n * n)
    if variance < MIN_SPREAD ** 2:
        raise VisionError(TOO_PLAIN, code="flat_picture")
    return tsums, tsquares


def match_template(frame: Frame, template: Bitmap, *, min_confidence: float = 0.95, roi: Roi | None = None,
                   max_results: int = 1) -> list[TemplateMatch]:
    """Locations where `template` matches the frame with confidence >= min_confidence (non-overlapping)."""
    return list(search_template(frame, template, min_confidence=min_confidence, roi=roi,
                                max_results=max_results).matches)


def match_template_exact(frame: Frame, template: Bitmap, *, min_confidence: float = 0.95, roi: Roi | None = None,
                         max_results: int = 1) -> list[TemplateMatch]:
    """`match_template`, always with the standard-library search (the reference the fast one is held to)."""
    return list(_search(frame, template, min_confidence, roi, max_results, None, None).matches)


def search_template(frame: Frame, template: Bitmap, *, min_confidence: float = 0.95, roi: Roi | None = None,
                    max_results: int = 1, any_size: bool = False, min_side: int = 1,
                    check: Callable[[], None] | None = None) -> TemplateSearch:
    """The search, with what came closest. `any_size`: at the size, from ANY_SIZE, where the picture is found best
    -- never smaller than `min_side` pixels a side (or than it is) -- which needs the fast search. `check` runs
    between the parts of a search and raises to end it (the emergency stop, a cancel)."""
    fast = fast_matcher()
    if not any_size:
        return _search(frame, template, min_confidence, roi, max_results, fast, check)
    if fast is None:
        raise VisionError(NEEDS_FAST, code="needs_fast")
    _check_args(frame, template, min_confidence, max_results)
    _check_spread(template)
    r = resolve_roi(roi, frame.width, frame.height)
    size = fast.best_size(frame.bitmap.rgb, frame.width, frame.height, r, template.rgb, template.width,
                          template.height, low=ANY_SIZE[0], high=ANY_SIZE[1], min_side=min_side,
                          check=check or _no_check)
    if size is None:
        raise VisionError(f"The {template.width}x{template.height} template fits the {r.width}x{r.height} search "
                          f"region at no size from {ANY_SIZE[0]:.0%} to {ANY_SIZE[1]:.0%}.", code="picture_fits_no_size",
                          picture=f"{template.width}×{template.height}", area=f"{r.width}×{r.height}",
                          low=round(ANY_SIZE[0] * 100), high=round(ANY_SIZE[1] * 100))
    factor, w, h, y, x = size
    picture = template if (w, h) == (template.width, template.height) else \
        Bitmap(w, h, fast.resize(template.rgb, template.width, template.height, w, h))
    if max_results > 1:                  # every place, at the size it was found best at
        found = _search(frame, picture, min_confidence, roi, max_results, fast, check, accepted=True)
        return TemplateSearch(found.matches, found.best, factor)
    tsums, tsquares = _template_sums(picture)
    ((c, s, q),) = fast.sums(frame.bitmap.rgb, frame.width, frame.height, picture.rgb, w, h, [(y, x)])
    best = TemplateMatch(x, y, w, h, confidence(w * h, c, s, q, tsums, tsquares))
    return TemplateSearch((best,) if best.confidence >= min_confidence else (), best, factor)


def _no_check() -> None:
    return None


def _check_args(frame, template, min_confidence, max_results) -> None:
    if not isinstance(frame, Frame):
        raise VisionError("match_template needs a Frame from the verified screenshot pipeline.")
    if not isinstance(template, Bitmap):
        raise VisionError("template must be a Bitmap (load one with load_png).")
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)) \
            or not 0 < min_confidence <= 1:
        raise VisionError("min_confidence must be greater than 0 and at most 1.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise VisionError("max_results must be a positive integer.")


def _search(frame, template, min_confidence, roi, max_results, fast, check, *, accepted=False) -> TemplateSearch:
    """`accepted`: the template is the one asked for, resized -- it was checked at its own size (made smaller, a fine
    pattern can look flat: that is no reason to refuse it)."""
    _check_args(frame, template, min_confidence, max_results)
    r = resolve_roi(roi, frame.width, frame.height)
    tw, th = template.width, template.height
    if tw > r.width or th > r.height:
        raise VisionError(f"The {tw}x{th} template does not fit in the {r.width}x{r.height} search region.",
                          code="picture_larger_than_area", picture=f"{tw}×{th}", area=f"{r.width}×{r.height}")
    tsums, tsquares = _template_sums(template) if accepted else _check_spread(template)
    n = tw * th
    best = None
    if fast is not None:
        places = fast.shortlist(frame.bitmap.rgb, frame.width, frame.height, r, template.rgb, tw, th, min_confidence,
                                check)
        measured = fast.sums(frame.bitmap.rgb, frame.width, frame.height, template.rgb, tw, th, places, check)
        scored = [(confidence(n, c, s, q, tsums, tsquares), y, x) for (c, s, q), (y, x) in zip(measured, places)]
        top = min(scored, key=lambda c: (-c[0], c[1], c[2]))
        best = TemplateMatch(top[2], top[1], tw, th, top[0])
        candidates = [c for c in scored if c[0] >= min_confidence]
    else:
        candidates = _exact(frame, template, r, min_confidence, tsums, tsquares, check)
    return TemplateSearch(tuple(_pick(candidates, tw, th, max_results)), best)


def _pick(candidates, tw: int, th: int, max_results: int) -> list[TemplateMatch]:
    """The best places first (then by y, then by x), none overlapping one already taken."""
    picked: list[TemplateMatch] = []
    for conf, y, x in sorted(set(candidates), key=lambda c: (-c[0], c[1], c[2])):
        if all(abs(x - m.x) >= tw or abs(y - m.y) >= th for m in picked):
            picked.append(TemplateMatch(x, y, tw, th, conf))
            if len(picked) == max_results:
                break
    return picked


def _integral(values: bytes, width: int, area: Roi, offset: int, squared: bool, check) -> list[list[int]]:
    """Summed-area table of `area` of a picture `width` wide -- of one channel (`offset` 0..2) or, `squared`, of the
    squares of all three: (area.height + 1) rows of (area.width + 1) whole numbers. `check` runs before each row."""
    table = [[0] * (area.width + 1)]
    for y in range(area.y, area.bottom):
        if check is not None:
            check()
        row = values[(y * width + area.x) * 3:(y * width + area.right) * 3]
        above, line, total = table[-1], [0], 0
        for x in range(area.width):
            if squared:
                i = x * 3
                total += row[i] * row[i] + row[i + 1] * row[i + 1] + row[i + 2] * row[i + 2]
            else:
                total += row[x * 3 + offset]
            line.append(above[x + 1] + total)
        table.append(line)
    return table


def _box(table: list[list[int]], x: int, y: int, w: int, h: int) -> int:
    return table[y + h][x + w] - table[y][x + w] - table[y + h][x] + table[y][x]


def _exact(frame: Frame, template: Bitmap, r: Roi, min_confidence: float, tsums, tsquares,
           check) -> list[tuple[float, int, int]]:
    """Every place in `r` measured with the standard library. A place whose average colour alone keeps it below
    the threshold (the colour factor is an upper bound of the confidence) is passed over unmeasured."""
    tw, th = template.width, template.height
    data, w = frame.bitmap.rgb, frame.width
    tables = [_integral(data, w, r, c, False, check) for c in range(3)]      # over the area only
    squares = _integral(data, w, r, 0, True, check)
    trows = [template.rgb[y * tw * 3:(y + 1) * tw * 3] for y in range(th)]
    n = tw * th
    reach = (1.0 - min_confidence) * 255.0 * n      # the largest colour difference (as a sum) that can still pass
    candidates: list[tuple[float, int, int]] = []
    for y in range(r.y, r.bottom - th + 1):
        if check is not None:
            check()
        for x in range(r.x, r.right - tw + 1):
            sums = tuple(_box(t, x - r.x, y - r.y, tw, th) for t in tables)
            if max(abs(s - t) for s, t in zip(sums, tsums)) > reach + 1e-6 * n:
                continue
            cross = 0
            for ty in range(th):
                i = ((y + ty) * w + x) * 3
                cross += sum(map(mul, data[i:i + tw * 3], trows[ty]))
            conf = confidence(n, cross, sums, _box(squares, x - r.x, y - r.y, tw, th), tsums, tsquares)
            if conf >= min_confidence:
                candidates.append((conf, y, x))
    return candidates
