"""WHAT to look for, as data. Nothing here looks at anything or does anything.

A query is an inert, comparable, reusable value: it can be built once and applied to many frames and many
targets, stored in a profile, logged, or compared for equality. Keeping it separate from the code that
evaluates it (`observer.py`) is what lets a future `WAIT UNTIL` / `IF` / `FIND` step hold a query without
holding a target, a screenshot or a provider -- and it is why a query can never perform an action: it has no
method that does anything.

Validation splits in two on purpose. Everything checkable without an image is checked HERE, at construction, so
a malformed query fails where it is written rather than at some later frame. Anything that depends on the image
(does the region fit? is the template smaller than the search area?) can only be checked when a frame exists,
and is raised as `InvalidQuery` by the observer.

`region=None` always means "the whole frame", never "somewhere sensible".
"""
from __future__ import annotations

from dataclasses import dataclass

from app.vision.bitmap import Bitmap
from app.vision.roi import Roi

from .errors import InvalidQuery

MAX_TOLERANCE = 255
EXACT = "exact"
CONTAINS = "contains"
TEXT_MATCHES = (EXACT, CONTAINS)


def _check_color(color) -> tuple[int, int, int]:
    if not isinstance(color, (tuple, list)) or len(color) != 3:
        raise InvalidQuery("A colour must be three values: (red, green, blue).")
    for c in color:
        if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 255:
            raise InvalidQuery("Each colour channel must be a whole number between 0 and 255.")
    return tuple(color)                                              # type: ignore[return-value]


def _check_tolerance(tolerance: int) -> int:
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= MAX_TOLERANCE:
        raise InvalidQuery(f"tolerance must be a whole number between 0 and {MAX_TOLERANCE}.")
    return tolerance


def _check_region(region) -> Roi | None:
    if region is not None and not isinstance(region, Roi):
        raise InvalidQuery("region must be a Roi, or None for the whole frame.")
    return region


def _check_ratio(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < value <= 1.0:
        raise InvalidQuery(f"{name} must be greater than 0.0 and at most 1.0.")
    return float(value)


@dataclass(frozen=True)
class PixelQuery:
    """Is the pixel at this point this colour (within tolerance)?

    `expected` inverts the question without inverting the answer: with `expected=False` the query asks that the
    pixel is NOT that colour, and `found` is then True when the colour is absent. The measurement in `detail`
    is the same either way, so "not there" never loses the information about what WAS there."""
    point: tuple[int, int]
    color: tuple[int, int, int]
    tolerance: int = 0
    expected: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.point, (tuple, list)) or len(self.point) != 2 \
                or any(isinstance(v, bool) or not isinstance(v, int) for v in self.point):
            raise InvalidQuery("point must be two whole numbers: (x, y).")
        if any(v < 0 for v in self.point):
            raise InvalidQuery("point coordinates must not be negative.")
        object.__setattr__(self, "point", tuple(self.point))
        object.__setattr__(self, "color", _check_color(self.color))
        object.__setattr__(self, "tolerance", _check_tolerance(self.tolerance))
        if not isinstance(self.expected, bool):
            raise InvalidQuery("expected must be True or False.")


@dataclass(frozen=True)
class ColorRegionQuery:
    """Is at least `min_ratio` of this region that colour?

    A ratio rather than a pixel count on purpose: a count only means something once you know the region's size,
    so it cannot be reused across differently sized regions or targets of different resolutions, while "at
    least 30% of this area is red" travels."""
    color: tuple[int, int, int]
    tolerance: int = 0
    region: Roi | None = None
    min_ratio: float = 0.5
    expected: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "color", _check_color(self.color))
        object.__setattr__(self, "tolerance", _check_tolerance(self.tolerance))
        object.__setattr__(self, "region", _check_region(self.region))
        object.__setattr__(self, "min_ratio", _check_ratio(self.min_ratio, "min_ratio"))
        if not isinstance(self.expected, bool):
            raise InvalidQuery("expected must be True or False.")


@dataclass(frozen=True)
class TemplateQuery:
    """Where does this small image appear inside the frame?

    `max_results` > 1 asks for every non-overlapping place it appears, in a deterministic order. The template
    is a decoded `Bitmap` (load one with `app.vision.bitmap.load_png`) rather than a path, so a query never
    touches the file system while an observation is running."""
    template: Bitmap
    min_confidence: float = 0.95
    region: Roi | None = None
    max_results: int = 1
    expected: bool = True
    any_size: bool = False      # also at 50% .. 200% of its size (needs the optional fast search: OpenCV)

    def __post_init__(self) -> None:
        if not isinstance(self.template, Bitmap):
            raise InvalidQuery("template must be a Bitmap; load one with app.vision.bitmap.load_png().")
        if not isinstance(self.any_size, bool):
            raise InvalidQuery("any_size must be True or False.")
        object.__setattr__(self, "min_confidence", _check_ratio(self.min_confidence, "min_confidence"))
        object.__setattr__(self, "region", _check_region(self.region))
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int) or self.max_results < 1:
            raise InvalidQuery("max_results must be a whole number of at least 1.")
        if not isinstance(self.expected, bool):
            raise InvalidQuery("expected must be True or False.")


@dataclass(frozen=True)
class TextQuery:
    """Is this text on the screen, and where?

    The automation names the text it wants -- `TextQuery("Collect")` -- and never the engine that reads it:
    which OCR implementation answers is decided at the composition root, exactly as the screenshot provider is.

    `language` is a hint passed to the engine (a BCP-47 tag such as "en" or "ru"); an engine that cannot honour
    it says so rather than silently reading in the wrong language. `text=""` asks only "is there any text
    here?", which is a real question and not an error."""
    text: str
    match: str = CONTAINS
    ignore_case: bool = True
    region: Roi | None = None
    language: str = ""
    min_confidence: float = 0.0
    max_results: int = 1
    expected: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidQuery("text must be a string.")
        if self.match not in TEXT_MATCHES:
            raise InvalidQuery(f"match must be one of {TEXT_MATCHES}.")
        for flag in (self.ignore_case, self.expected):
            if not isinstance(flag, bool):
                raise InvalidQuery("ignore_case and expected must be True or False.")
        object.__setattr__(self, "region", _check_region(self.region))
        if not isinstance(self.language, str):
            raise InvalidQuery("language must be a BCP-47 string such as 'en' or 'ru', or '' for the default.")
        if isinstance(self.min_confidence, bool) or not isinstance(self.min_confidence, (int, float)) \
                or not 0.0 <= self.min_confidence <= 1.0:
            raise InvalidQuery("min_confidence must be between 0.0 and 1.0.")
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int) or self.max_results < 1:
            raise InvalidQuery("max_results must be a whole number of at least 1.")


MAX_TEXT_LINES = 20


@dataclass(frozen=True)
class TextLine:
    """One text of a `TextLinesQuery`, and whether it should be there (`present=True`) or NOT be there."""
    text: str
    present: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise InvalidQuery("Each line needs a text to look for.")
        if not isinstance(self.present, bool):
            raise InvalidQuery("present must be True or False.")


@dataclass(frozen=True)
class TextLinesQuery:
    """Several texts in one region, each one either there or NOT there: the region is read ONCE, and the query is
    found when at least one of its lines holds.

    Each text is looked for anywhere in the region -- inside any piece of text read there -- ignoring case unless
    `ignore_case` is False, with runs of spaces counted as one. Where it points: the first line, in order, that holds
    because its text IS there (the piece of text read there that contains it). When only a line that must be
    absent holds, there is nothing to point at, so `points_at_match` says whether a "found" answer always comes with
    a place (every line must be present)."""
    lines: tuple[TextLine, ...]
    ignore_case: bool = True
    region: Roi | None = None
    language: str = ""
    min_confidence: float = 0.0
    expected: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.lines, tuple) or not 1 <= len(self.lines) <= MAX_TEXT_LINES \
                or not all(isinstance(line, TextLine) for line in self.lines):
            raise InvalidQuery(f"A text-lines query has 1 to {MAX_TEXT_LINES} lines (TextLine values).")
        for flag in (self.ignore_case, self.expected):
            if not isinstance(flag, bool):
                raise InvalidQuery("ignore_case and expected must be True or False.")
        object.__setattr__(self, "region", _check_region(self.region))
        if not isinstance(self.language, str):
            raise InvalidQuery("language must be a BCP-47 string such as 'en' or 'ru', or '' for the default.")
        if isinstance(self.min_confidence, bool) or not isinstance(self.min_confidence, (int, float)) \
                or not 0.0 <= self.min_confidence <= 1.0:
            raise InvalidQuery("min_confidence must be between 0.0 and 1.0.")

    @property
    def points_at_match(self) -> bool:
        """Whether every "found" answer has a place to point at: only when no line must be absent."""
        return all(line.present for line in self.lines)


Query = PixelQuery | ColorRegionQuery | TemplateQuery | TextQuery | TextLinesQuery
