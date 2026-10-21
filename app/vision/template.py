"""Deterministic template matching (sum of absolute differences), local and exact.

confidence = 1 - SAD / (765 * template pixels), where SAD sums |dR|+|dG|+|dB| over every template
pixel. 1.0 is a pixel-perfect match. The search is exact: a cheap lower bound (SAD over a fixed
sample of template pixels) only rejects positions that provably cannot reach `min_confidence`.
Results are ordered by confidence, then y, then x, so equal inputs always give equal outputs.
This is not scale or rotation invariant by design.

Speed: when OpenCV and NumPy are installed (optional, docs/DEPENDENCIES.md), `fast.py` finds the same candidates
much faster -- the same answer, exactly (it measures every position it keeps with the same SAD); without them the
search below runs, as it always has. `match_template_exact` always uses the search below.
"""
from __future__ import annotations

from dataclasses import dataclass

from .bitmap import Bitmap, VisionError
from .frame import Frame
from .roi import Roi, resolve_roi

MAX_CANDIDATES = 5000


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


def _samples(t: Bitmap, target: int = 200) -> list[tuple[int, int, int, int, int]]:
    """A deterministic grid of template pixels: (x, y, r, g, b)."""
    sx = max(1, t.width // 15 or 1)
    sy = max(1, t.height // 15 or 1)
    out = []
    for y in range(0, t.height, sy):
        for x in range(0, t.width, sx):
            r, g, b = t.pixel(x, y)
            out.append((x, y, r, g, b))
    return out[:max(target, 1)] if len(out) > target else out


_FAST: list = []                # [the fast module, or None], filled on first use


def fast_matcher():
    """`fast.candidates` when OpenCV and NumPy can be loaded, else None (looked up once)."""
    if not _FAST:
        try:
            from . import fast
        except ImportError:
            _FAST.append(None)
        else:
            _FAST.append(fast.candidates)
    return _FAST[0]


def match_template(frame: Frame, template: Bitmap, *, min_confidence: float = 0.95, roi: Roi | None = None,
                   max_results: int = 1) -> list[TemplateMatch]:
    """Locations where `template` matches the frame with confidence >= min_confidence (non-overlapping)."""
    return _match(frame, template, min_confidence, roi, max_results, fast_matcher())


def match_template_exact(frame: Frame, template: Bitmap, *, min_confidence: float = 0.95, roi: Roi | None = None,
                         max_results: int = 1) -> list[TemplateMatch]:
    """`match_template`, always with the standard-library search (the reference the fast one is held to)."""
    return _match(frame, template, min_confidence, roi, max_results, None)


def _match(frame, template, min_confidence, roi, max_results, fast) -> list[TemplateMatch]:
    if not isinstance(frame, Frame):
        raise VisionError("match_template needs a Frame from the verified screenshot pipeline.")
    if not isinstance(template, Bitmap):
        raise VisionError("template must be a Bitmap (load one with load_png).")
    if isinstance(min_confidence, bool) or not 0 < min_confidence <= 1:
        raise VisionError("min_confidence must be greater than 0 and at most 1.")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise VisionError("max_results must be a positive integer.")
    r = resolve_roi(roi, frame.width, frame.height)
    tw, th = template.width, template.height
    if tw > r.width or th > r.height:
        raise VisionError(f"The {tw}x{th} template does not fit in the {r.width}x{r.height} search region.")

    total = tw * th * 765
    limit = (1.0 - min_confidence) * total  # a position is a candidate only if SAD <= limit
    if fast is not None:
        found = fast(frame.bitmap.rgb, frame.width, frame.height, r, template.rgb, tw, th, limit, MAX_CANDIDATES)
        if found is None:
            raise VisionError(TOO_MANY)
        candidates = [(1.0 - sad / total, y, x) for sad, y, x in found]
    else:
        candidates = _exact(frame, template, r, limit, total)
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    picked: list[TemplateMatch] = []
    for conf, y, x in candidates:
        if all(abs(x - m.x) >= tw or abs(y - m.y) >= th for m in picked):
            picked.append(TemplateMatch(x, y, tw, th, conf))
            if len(picked) == max_results:
                break
    return picked


TOO_MANY = ("Too many candidate positions: the template is not distinctive enough (for example a flat colour). Use "
            "a more specific template or a smaller ROI.")


def _exact(frame: Frame, template: Bitmap, r: Roi, limit: float, total: int) -> list[tuple[float, int, int]]:
    tw, th = template.width, template.height
    samples = _samples(template)
    data, w = frame.bitmap.rgb, frame.width
    trows = [template.rgb[y * tw * 3:(y + 1) * tw * 3] for y in range(th)]
    candidates: list[tuple[float, int, int]] = []

    for y in range(r.y, r.bottom - th + 1):
        for x in range(r.x, r.right - tw + 1):
            partial = 0
            for sx, sy, sr, sg, sb in samples:  # lower bound of the full SAD
                i = ((y + sy) * w + x + sx) * 3
                partial += abs(data[i] - sr) + abs(data[i + 1] - sg) + abs(data[i + 2] - sb)
                if partial > limit:
                    break
            if partial > limit:
                continue
            sad = 0
            for ty in range(th):
                i = ((y + ty) * w + x) * 3
                for a, b in zip(data[i:i + tw * 3], trows[ty]):
                    sad += abs(a - b)
                if sad > limit:
                    break
            if sad > limit:
                continue
            candidates.append((1.0 - sad / total, y, x))
            if len(candidates) > MAX_CANDIDATES:
                raise VisionError(TOO_MANY)
    return candidates
