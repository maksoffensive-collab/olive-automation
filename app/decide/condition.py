"""Condition: "is this true on the screen right now?" -- answered, never acted on.

A `Condition` is one observation query plus a label. It adds no second query language: the query kinds of
`app.observe` (pixel, colour region, template, text, text lines) already describe what to look for, and their own
`expected` flag already says whether the thing should be there or should NOT be there. What this layer adds is the
answer shape a decision needs: `ConditionResult` is `satisfied` true/false PLUS the observation it was read from, so a
"no" still says what was seen, how confidently and where.

    Condition.pixel((10, 20), (255, 0, 0))              the pixel is red
    Condition.template(button, region=Roi(...))          the button is visible
    Condition.text("Collect", expected=False)            the word is NOT on the screen

`evaluate()` is the only function here that looks at anything, and it looks through the caller's
`ObservationContext`, so it sees exactly one target's screen. It never waits (see `wait.py`), never decides (see
`decision.py`) and never acts: there is no input, no engine and no tap in this module.

Errors are the observation layer's own and are not wrapped: `InvalidQuery` (the question does not fit this
frame), `ObservationUnavailable` (no image), `OcrUnavailable` (no text engine), `EmergencyStopped` (stop). A
false condition is never an exception.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

from app.observe.context import ObservationContext
from app.observe.query import (CONTAINS, ColorRegionQuery, PixelQuery, Query, TemplateQuery, TextLine,
                               TextLinesQuery, TextQuery)
from app.observe.result import ObservationResult
from app.vision.bitmap import Bitmap
from app.vision.coords import PixelPoint
from app.vision.roi import Roi

from .errors import Cancelled, InvalidCondition

MAX_LABEL_LENGTH = 100
QUERY_TYPES = (PixelQuery, ColorRegionQuery, TemplateQuery, TextQuery, TextLinesQuery)


def check_cancel(cancel: threading.Event | None) -> None:
    """Raise `Cancelled` if this run's own cancellation event is set."""
    if cancel is not None and cancel.is_set():
        raise Cancelled("The run was cancelled.")


@dataclass(frozen=True)
class Condition:
    """One question about one screen. Inert data: comparable, reusable across targets, never callable."""
    query: Query
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.query, QUERY_TYPES):
            raise InvalidCondition("A condition needs an observation query (pixel, colour region, template or "
                                   f"text), not {type(self.query).__name__}.")
        if not isinstance(self.label, str) or len(self.label) > MAX_LABEL_LENGTH or not self.label.isprintable():
            raise InvalidCondition(f"label must be at most {MAX_LABEL_LENGTH} printable characters.")

    # ---- the four kinds, as thin constructors over the existing queries ------------------------------------
    @classmethod
    def pixel(cls, point: tuple[int, int], color: tuple[int, int, int], *, tolerance: int = 0,
              expected: bool = True, label: str = "") -> "Condition":
        return cls(PixelQuery(point, color, tolerance, expected), label)

    @classmethod
    def color(cls, color: tuple[int, int, int], *, region: Roi | None = None, tolerance: int = 0,
              min_ratio: float = 0.5, expected: bool = True, label: str = "") -> "Condition":
        return cls(ColorRegionQuery(color, tolerance, region, min_ratio, expected), label)

    @classmethod
    def template(cls, template: Bitmap, *, region: Roi | None = None, min_confidence: float = 0.95,
                 expected: bool = True, label: str = "") -> "Condition":
        return cls(TemplateQuery(template, min_confidence, region, 1, expected), label)

    @classmethod
    def text(cls, text: str, *, match: str = CONTAINS, ignore_case: bool = True, region: Roi | None = None,
             language: str = "", min_confidence: float = 0.0, expected: bool = True,
             label: str = "") -> "Condition":
        return cls(TextQuery(text, match, ignore_case, region, language, min_confidence, 1, expected), label)

    @classmethod
    def text_lines(cls, lines, *, ignore_case: bool = True, region: Roi | None = None, language: str = "",
                   min_confidence: float = 0.0, expected: bool = True, label: str = "") -> "Condition":
        """Several texts, each `(text, present)` or a `TextLine`: true when at least one line holds."""
        made = tuple(line if isinstance(line, TextLine) else TextLine(*line) for line in lines)
        return cls(TextLinesQuery(made, ignore_case, region, language, min_confidence, expected), label)

    # ---- reading it ----------------------------------------------------------------------------------------
    @property
    def expected(self) -> bool:
        """True: "it should be there". False: "it should NOT be there". Owned by the query, never duplicated."""
        return self.query.expected

    @property
    def kind(self) -> str:
        return type(self.query).__name__

    @property
    def points_at_match(self) -> bool:
        """Whether every "found" answer comes with a place to click. Not for text lines with a line that must be
        absent: that line alone can make it found, with nothing seen."""
        return getattr(self.query, "points_at_match", True)

    def negated(self) -> "Condition":
        """The same question with the opposite expectation."""
        return Condition(replace(self.query, expected=not self.query.expected), self.label)

    def describe(self) -> str:
        what = self.label or self.kind
        return what if self.expected else f"not {what}"


@dataclass(frozen=True)
class ConditionResult:
    """What one condition answered on one frame: true/false, and the observation that answer came from."""
    condition: Condition
    observation: ObservationResult

    def __post_init__(self) -> None:
        if not isinstance(self.condition, Condition) or not isinstance(self.observation, ObservationResult):
            raise InvalidCondition("A condition result pairs a Condition with the ObservationResult it read.")
        if self.observation.expected != self.condition.expected:
            raise InvalidCondition("This observation was made for a different expectation than the condition's.")

    @property
    def satisfied(self) -> bool:
        return self.observation.satisfied

    @property
    def found(self) -> bool:
        return self.observation.found

    @property
    def confidence(self) -> float:
        return self.observation.confidence

    @property
    def center(self) -> PixelPoint | None:
        return self.observation.center

    @property
    def target_id(self) -> str:
        return self.observation.target_id

    def diagnostics(self) -> dict[str, Any]:
        """Plain data for a log line or a report: no live object, nothing that could be invoked."""
        center = self.center
        # A reader without confidence (some OCR engines) reports a placeholder; it is not shown as a number.
        measured = self.observation.detail.get("confidence_measured", True) is not False
        return {"condition": self.condition.describe(), "kind": self.observation.kind,
                "satisfied": self.satisfied, "found": self.found, "expected": self.condition.expected,
                "confidence": self.confidence if measured else None,       # never rounded up to a threshold
                "center": None if center is None else (center.x, center.y),
                "target": self.target_id, "detail": dict(self.observation.detail)}


def evaluate(condition: Condition, context: ObservationContext, *, max_age_s: float = 0.0,
             cancel: threading.Event | None = None, picked_on: tuple[int, int] | None = None) -> ConditionResult:
    """Answer one condition on the context's target. A fresh screenshot unless `max_age_s` allows reuse."""
    if not isinstance(condition, Condition):
        raise InvalidCondition("evaluate() needs a Condition.")
    if not isinstance(context, ObservationContext):
        raise InvalidCondition("evaluate() needs the ObservationContext of the target being asked about.")
    check_cancel(cancel)
    # the run's own cancel goes along, so it can end an OCR read under way instead of waiting for it to finish
    return ConditionResult(condition, context.observe(condition.query, max_age_s=max_age_s, cancel=cancel,
                                                      picked_on=picked_on))
