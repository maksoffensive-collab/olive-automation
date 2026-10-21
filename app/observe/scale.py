"""A query made on a picture of one size, asked of a frame of another (the person's decision of 2026-10-20): its
point, its area and its picture scaled by the factors `app.providers.size.fit` gives -- the same place, the same
thing, at the new size. Kept inside the frame. Nothing else about the query changes. A picture is never made smaller
than MIN_SIDE pixels a side (or than it was picked, when it was picked smaller): a few pixels match almost anywhere."""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable

from app.vision.bitmap import Bitmap
from app.vision.roi import Roi

from .errors import InvalidQuery
from .query import ColorRegionQuery, PixelQuery, Query, TemplateQuery, TextLinesQuery, TextQuery

MIN_SIDE = 4
PARTS = ("point", "region", "template")      # what of a query was picked on a picture, and so has that size
Resize = Callable[[Bitmap, int, int], Bitmap]


def _plain(picture: Bitmap, w: int, h: int) -> Bitmap:
    return picture.resized(w, h)


def too_small(picture: Bitmap, w: int, h: int) -> bool:
    """Whether `picture` at `w` x `h` would be smaller than it can be told apart at."""
    return w < min(MIN_SIDE, picture.width) or h < min(MIN_SIDE, picture.height)


def _picture(picture: Bitmap, sx: float, sy: float, resize: Resize) -> Bitmap:
    w, h = max(1, round(picture.width * sx)), max(1, round(picture.height * sy))
    if too_small(picture, w, h):
        raise InvalidQuery(f"The {picture.width}x{picture.height} picture would be only {w}x{h} pixels at this size: "
                           "too small to be told apart from anything else.")
    return resize(picture, w, h)


def _point(point: tuple[int, int], sx: float, sy: float, width: int, height: int) -> tuple[int, int]:
    x, y = point
    return min(width - 1, max(0, round(x * sx))), min(height - 1, max(0, round(y * sy)))


def _region(region: Roi | None, sx: float, sy: float, width: int, height: int) -> Roi | None:
    if region is None:
        return None
    x0, y0 = min(width - 1, math.floor(region.x * sx)), min(height - 1, math.floor(region.y * sy))
    x1, y1 = min(width, math.ceil(region.right * sx)), min(height, math.ceil(region.bottom * sy))
    return Roi(x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def scaled_query(query: Query, sx: float, sy: float, width: int, height: int, *, resize: Resize = _plain) -> Query:
    """`query` for a `width` x `height` frame, from the size it was made at times (sx, sy). `resize` makes the
    picture at its new size (a cache of the caller's own, say); `InvalidQuery` when it would be too small."""
    if isinstance(query, PixelQuery):
        return replace(query, point=_point(query.point, sx, sy, width, height))
    if isinstance(query, TemplateQuery):
        return replace(query, template=_picture(query.template, sx, sy, resize),
                       region=_region(query.region, sx, sy, width, height))
    if isinstance(query, (ColorRegionQuery, TextQuery, TextLinesQuery)):
        return replace(query, region=_region(query.region, sx, sy, width, height))
    return query


def scaled_part(query: Query, part: str, sx: float, sy: float, width: int, height: int) -> Query:
    """Only `part` of `query` (one of PARTS, which it must have) scaled; the rest as it is -- for an editor where
    the point, the area and the picture can each have been picked on a picture of its own size."""
    if part == "point":
        return replace(query, point=_point(query.point, sx, sy, width, height))
    if part == "region":
        return replace(query, region=_region(query.region, sx, sy, width, height))
    if part == "template":
        return replace(query, template=_picture(query.template, sx, sy, _plain))
    raise ValueError(f"Not a part of a query: {part!r}.")
