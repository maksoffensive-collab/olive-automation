"""One structured answer shape for every kind of observation.

A pixel check, a colour-ratio check, a template search and a text search all answer the same question -- "is it
there, how sure are we, and where?" -- so they all answer in the same type. A caller can therefore treat them
uniformly (a future `WAIT UNTIL` or `IF` needs no per-kind branch), while the parts that genuinely differ live
in `detail` rather than in a different class per kind.

Never a bare bool: a colour check that says `False` still knows the ratio it measured, and a template search
that says `True` still knows where and how confidently. Throwing that away is what forces callers to re-observe.

`matches` is ordered by the observer and the order is deterministic for identical input (see `observer.py`);
`best` is simply the first. An empty `matches` with `found=False` is the normal "not there" answer -- not an
error, and not something to be confused with a failure to look (see `errors.py`).

Both timestamps come from `time.time()` so they can be shown to a person and compared with a run's history.
`captured_at` is when the IMAGE was taken and `observed_at` is when it was examined: with one screenshot reused
for several queries they differ, and the gap is exactly the staleness a caller may care about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from app.providers.base import FrameSource
from app.vision.coords import PixelPoint
from app.vision.roi import Roi

PIXEL = "pixel"
COLOR_REGION = "color_region"
TEMPLATE = "template"
TEXT = "text"
TEXT_LINES = "text_lines"
KINDS = (PIXEL, COLOR_REGION, TEMPLATE, TEXT, TEXT_LINES)

_INERT = (str, int, float, bool, type(None), tuple, list, dict)


def _freeze(detail: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Detail is data a caller reads, never a live object it could act through: the same rule `app.ai` applies to
    proposed actions, for the same reason -- an observation result may be logged, compared or shown, and must
    carry nothing that could be invoked."""
    if not detail:
        return MappingProxyType({})
    for key, value in detail.items():
        if not isinstance(key, str):
            raise ValueError("Observation detail keys must be strings.")
        if callable(value) or not isinstance(value, _INERT):
            raise ValueError(f"Observation detail {key!r} must be plain data, never a callable or live object.")
    return MappingProxyType(dict(detail))


@dataclass(frozen=True)
class Match:
    """One place the thing was seen, in the frame's own native pixel coordinates -- which, by the screenshot
    contract in `app.providers.base`, is the same space the provider's input methods accept. So `center` is
    directly usable as a tap point WITHOUT conversion; turning it into an actual tap remains the action layer's
    job and never happens here."""
    bounds: Roi
    confidence: float
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.bounds, Roi):
            raise ValueError("Match bounds must be a Roi.")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) \
                or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Match confidence must be a number between 0.0 and 1.0.")
        object.__setattr__(self, "detail", _freeze(self.detail))

    @property
    def center(self) -> PixelPoint:
        return PixelPoint(self.bounds.x + self.bounds.width // 2, self.bounds.y + self.bounds.height // 2)


@dataclass(frozen=True)
class ObservationResult:
    """What one query saw in one frame."""
    kind: str
    found: bool
    source: FrameSource
    captured_at: float                                   # when the image was taken
    observed_at: float                                   # when this query examined it
    matches: tuple[Match, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)
    expected: bool = True
    """What the query asked for: True for "it should be there", False for "it should NOT be there".

    Kept apart from `found` so a negative query never has to lie about what it saw. `found` always answers "is
    it on the screen"; `satisfied` answers "is that what we wanted". A wait waits for `satisfied`, and a
    "wait until the loading spinner is gone" still reports, in `found` and `matches`, the spinner it can
    still see."""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}.")
        if not isinstance(self.expected, bool):
            raise ValueError("expected must be True or False.")
        if not isinstance(self.source, FrameSource):
            raise ValueError("An observation result must carry the FrameSource it came from.")
        if not isinstance(self.matches, tuple) or not all(isinstance(m, Match) for m in self.matches):
            raise ValueError("matches must be a tuple of Match.")
        # one exception: text lines found because a line that must be ABSENT is absent -- there is nothing to point
        # at, and `center` is None, so nothing can be clicked "on" it
        if self.found and not self.matches and self.kind != TEXT_LINES:
            raise ValueError("A result cannot be found=True with no match to point at.")
        object.__setattr__(self, "detail", _freeze(self.detail))

    @property
    def satisfied(self) -> bool:
        """Did the frame match what the query asked for? This is what a wait waits for."""
        return self.found == self.expected

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None

    @property
    def confidence(self) -> float:
        """The best match's confidence, or 0.0 when nothing matched."""
        return self.matches[0].confidence if self.matches else 0.0

    @property
    def bounds(self) -> Roi | None:
        return self.matches[0].bounds if self.matches else None

    @property
    def center(self) -> PixelPoint | None:
        return self.matches[0].center if self.matches else None

    @property
    def target_id(self) -> str:
        return self.source.target_id

    @property
    def age_s(self) -> float:
        """How stale the image already was when this query ran."""
        return max(0.0, self.observed_at - self.captured_at)
