"""Applying a query to a frame. Pure: it reads an image and returns a description of it.

Nothing here captures, waits, retries or acts. `observe(frame, query)` is a function of its arguments and the
OCR provider it is given, so the same frame and query always produce the same result -- which is what makes
observation testable with synthetic images and what keeps "what is on the screen?" from quietly becoming "and
therefore tap it".

The algorithms themselves are NOT reimplemented here: colour and template work is delegated to `app.vision`,
which already does it deterministically, and text to whatever `OcrProvider` the composition root supplied. This
module's own job is the part that was missing -- turning three different shapes of answer into one
`ObservationResult`, and turning a vision-level complaint about a bad query into `InvalidQuery` while letting a
genuine inability to see propagate as itself.

Ordering is fixed here rather than left to the engine: matches are sorted by descending confidence, then y, then
x. So `best` means the same thing for every kind, and a provider that returns text blocks in a different order
on two runs cannot change which match a caller acts on.
"""
from __future__ import annotations

import inspect
import threading
import time
from typing import Callable

from app.core.errors import EmergencyStopped
from app.ocr.provider import (LanguageNotSupported, OcrProvider, OcrResult, ReadCancelled, is_language_tag,
                              reports_confidence)
from app.vision.bitmap import VisionError
from app.vision.color import find_color
from app.vision.frame import Frame
from app.vision.roi import Roi, resolve_roi
from app.vision.template import fast_matcher, match_template

from .errors import InvalidQuery, ObservationCancelled, OcrLanguageUnavailable, OcrUnavailable
from .query import EXACT, ColorRegionQuery, PixelQuery, Query, TemplateQuery, TextLinesQuery, TextQuery
from .result import COLOR_REGION, PIXEL, TEMPLATE, TEXT, TEXT_LINES, Match, ObservationResult
from .scale import Resize, too_small


def _resolve(query_region: Roi | None, frame: Frame) -> Roi:
    """A region is only checkable once there is an image to check it against."""
    try:
        return resolve_roi(query_region, frame.width, frame.height)
    except VisionError as exc:
        raise InvalidQuery(str(exc)) from exc


def _result(kind: str, frame: Frame, query, *, found: bool, matches, captured_at: float, detail) -> ObservationResult:
    ordered = tuple(sorted(matches, key=lambda m: (-m.confidence, m.bounds.y, m.bounds.x)))
    return ObservationResult(kind=kind, found=found, source=frame.source, captured_at=captured_at,
                             observed_at=time.time(), matches=ordered, detail=detail,
                             expected=getattr(query, "expected", True))


def _observe_pixel(frame: Frame, query: PixelQuery, captured_at: float) -> ObservationResult:
    x, y = query.point
    if not (0 <= x < frame.width and 0 <= y < frame.height):
        raise InvalidQuery(f"Point ({x}, {y}) is outside the {frame.width}x{frame.height} frame.")
    actual = frame.bitmap.pixel(x, y)
    distance = max(abs(a - b) for a, b in zip(actual, query.color))   # per-channel, like the tolerance
    found = distance <= query.tolerance
    matches = (Match(Roi(x, y, 1, 1), 1.0 - distance / 255.0, {"actual": actual, "distance": distance}),) \
        if found else ()
    return _result(PIXEL, frame, query, found=found, matches=matches, captured_at=captured_at,
                   detail={"actual": actual, "distance": distance, "tolerance": query.tolerance})


def _observe_color_region(frame: Frame, query: ColorRegionQuery, captured_at: float) -> ObservationResult:
    region = _resolve(query.region, frame)
    try:
        # max_points=0: the ratio and the bounding box are what this query is about; collecting every point
        # would cost time and memory for information nobody asked for.
        found_color = find_color(frame, query.color, tolerance=query.tolerance, roi=region,
                                 min_pixels=1, max_points=0)
    except VisionError as exc:
        raise InvalidQuery(str(exc)) from exc
    total = region.width * region.height
    ratio = found_color.count / total if total else 0.0
    found = ratio >= query.min_ratio
    matches = (Match(found_color.bounds or region, min(1.0, ratio / query.min_ratio),
                     {"ratio": ratio, "count": found_color.count}),) if found else ()
    return _result(COLOR_REGION, frame, query, found=found, matches=matches, captured_at=captured_at,
                   detail={"ratio": ratio, "count": found_color.count, "total": total,
                           "min_ratio": query.min_ratio})


ANY_SIZE = tuple(round(0.5 + 0.05 * i, 2) for i in range(31))     # 50% .. 200%, in steps of 5%
NEEDS_FAST = ("Looking for a picture in any size needs the optional fast picture search (requirements-vision.txt), "
              "which is not installed.")


def check_any_size(query: TemplateQuery) -> None:
    if query.any_size and fast_matcher() is None:
        raise InvalidQuery(NEEDS_FAST)


def _observe_template(frame: Frame, query: TemplateQuery, captured_at: float, check: Callable[[], None],
                      resize: Resize | None) -> ObservationResult:
    check_any_size(query)
    scale = 1.0
    try:
        if query.any_size:
            found_matches, scale = _any_size(frame, query, check, resize)
        else:
            found_matches = match_template(frame, query.template, min_confidence=query.min_confidence,
                                           roi=query.region, max_results=query.max_results)
    except VisionError as exc:
        # Every refusal match_template makes is about the QUERY -- a template that does not fit the search
        # area, a region outside the image, a template too plain to locate -- not about the image being
        # unavailable, so they are all the caller's to fix.
        raise InvalidQuery(str(exc)) from exc
    matches = tuple(Match(Roi(m.x, m.y, m.width, m.height), m.confidence) for m in found_matches)
    return _result(TEMPLATE, frame, query, found=bool(matches), matches=matches, captured_at=captured_at,
                   detail={"min_confidence": query.min_confidence, "count": len(matches), "scale": scale})


def _any_size(frame: Frame, query: TemplateQuery, check: Callable[[], None],
              resize: Resize | None) -> tuple[list, float]:
    """The size, of ANY_SIZE, at which the picture is found best (the highest confidence; nearer 100% first on a
    tie). A size it cannot be searched at (too large for the area, too small to tell apart, too plain there) is
    passed over. When nothing was found, the last refusal is the answer, if a size was refused after the last
    search that could run (the safer answer: "not found" is said only of sizes that were all really searched).
    `check` runs before each size (the stop and the caller's cancel are heard between sizes); `resize` makes the
    picture at a size (the caller's cache)."""
    area = query.region if query.region is not None else Roi(0, 0, frame.width, frame.height)
    t = query.template
    best: list = []
    best_scale, refusal, tried = 1.0, None, set()
    for factor in sorted(ANY_SIZE, key=lambda f: (abs(f - 1.0), f)):
        w, h = round(t.width * factor), round(t.height * factor)
        if too_small(t, w, h) or w > area.width or h > area.height or (w, h) in tried:
            continue                                  # (a small picture: two factors can give the same size)
        tried.add((w, h))
        factor = round(w / t.width, 2)                # the size it is really searched at
        check()
        picture = resize(t, w, h) if resize is not None else t.resized(w, h)
        try:
            found = match_template(frame, picture, min_confidence=query.min_confidence, roi=query.region,
                                   max_results=query.max_results)
        except VisionError as exc:
            refusal = exc
            continue
        refusal = None if found or best else refusal
        if found and (not best or found[0].confidence > best[0].confidence):
            best, best_scale = found, factor
    if not best and refusal is not None:
        raise refusal
    return best, best_scale


def _text_hits(blocks, query: TextQuery, *, filter_confidence: bool = True) -> list:
    wanted = query.text.casefold() if query.ignore_case else query.text
    out = []
    for block in blocks:
        if filter_confidence and block.confidence < query.min_confidence:
            continue
        text = block.text.casefold() if query.ignore_case else block.text
        if not query.text:
            hit = True                                   # "" asks only whether there is any text at all
        elif query.match == EXACT:
            hit = text.strip() == wanted.strip()
        else:
            hit = wanted in text
        if hit:
            out.append(Match(block.bounds, block.confidence, {"text": block.text}))
    return out


def _accepts(ocr: OcrProvider, keyword: str) -> bool:
    """Whether `read_text` takes `keyword` (one of the contract's optional extensions)."""
    return _takes(ocr.read_text, keyword)


def _takes(method: Callable[..., object], keyword: str) -> bool:
    """Whether `method` takes `keyword` by name."""
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == keyword and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
               or p.kind is p.VAR_KEYWORD for p in parameters)


def check_text_reader(ocr: OcrProvider | None, query: TextQuery | TextLinesQuery) -> OcrProvider:
    """Whether this text question can be asked of this reader at all -- decided without reading anything, so a run
    can refuse it before its first step instead of failing half-way, after input was already sent. Returns the
    reader; raises `OcrUnavailable` (no reader) or `OcrLanguageUnavailable` (a language it cannot be asked for)."""
    if ocr is None:
        raise OcrUnavailable("No OCR provider is configured, so text cannot be observed. "
                             "Configure one at the composition root, or use a colour or template query.")
    if query.language:
        if not is_language_tag(query.language):
            raise OcrLanguageUnavailable(f"{query.language!r} is not a language tag such as 'en', 'ru' or 'sv-SE'.")
        # A language the provider cannot even be told about would be silently ignored: refuse instead, so a
        # query for Russian text never quietly reads in another language and answers "not there".
        if not _accepts(ocr, "language"):
            raise OcrLanguageUnavailable(f"The OCR provider cannot read in a chosen language ({query.language!r}); "
                                         "leave the language empty or configure a provider that supports it.")
    return ocr


def applies_text_confidence(ocr: object) -> bool:
    """Whether a text query's `min_confidence` can be applied with this reader: there is one, and it reports real
    confidence. For the GUI, which must not offer a setting that would have no effect."""
    return ocr is not None and reports_confidence(ocr)


def _read(frame: Frame, query: TextQuery | TextLinesQuery, ocr: OcrProvider | None,
          cancel: threading.Event | None = None) -> OcrResult:
    """One read of `query`'s region (or the whole frame), in its language, through the reader -- with every check
    and every error mapping a text observation has. Shared by text queries and by `read_text`."""
    ocr = check_text_reader(ocr, query)
    if query.region is not None:
        _resolve(query.region, frame)                    # fail on a bad region before running an engine
    if cancel is not None and cancel.is_set():
        raise ObservationCancelled("The text reading was cancelled before it started.")
    options: dict = {}
    if query.language:
        options["language"] = query.language
    if cancel is not None and _accepts(ocr, "cancel"):
        options["cancel"] = cancel                       # so a run's own cancel can end a read under way
    try:
        read: OcrResult = ocr.read_text(frame, roi=query.region, **options)
    except EmergencyStopped:
        raise                                            # a stop while reading is a stop, never "no engine"
    except ReadCancelled as exc:
        raise ObservationCancelled(f"The text reading was cancelled: {exc}") from exc
    except LanguageNotSupported as exc:
        raise OcrLanguageUnavailable(f"The OCR provider cannot read {query.language or 'its default language'!r} "
                                     f"text: {exc}") from exc
    except Exception as exc:
        raise OcrUnavailable(f"The OCR provider could not read this frame: {exc}") from exc
    if not isinstance(read, OcrResult):
        raise OcrUnavailable("The OCR provider returned something that is not an OcrResult.")
    return read


def read_text(frame: Frame, *, ocr: OcrProvider | None, region: Roi | None = None, language: str = "",
              cancel: threading.Event | None = None) -> OcrResult:
    """All the text in `frame` (or in `region`), as the reader returned it: for a person who wants to SEE what is
    written on a screen, not to test one condition. Same checks and errors as a text query; nothing acts."""
    return _read(frame, TextQuery("", region=region, language=language), ocr, cancel)


def reader_languages(ocr: object, *, cancel: threading.Event | None = None) -> tuple[str, ...] | None:
    """The languages this reader can read here, or None when it cannot say (an optional part of the contract).
    Errors are the observation layer's own: the stop passes through, a cancel is `ObservationCancelled`, any other
    failure `OcrUnavailable`."""
    if ocr is None:
        raise OcrUnavailable("No OCR provider is configured, so no language can be read.")
    lister = getattr(ocr, "languages", None)
    if not callable(lister):
        return None
    if cancel is not None and cancel.is_set():
        raise ObservationCancelled("Listing the languages was cancelled before it started.")
    try:
        tags = lister(cancel=cancel) if _takes(lister, "cancel") else lister()
    except EmergencyStopped:
        raise
    except ReadCancelled as exc:
        raise ObservationCancelled(f"Listing the languages was cancelled: {exc}") from exc
    except Exception as exc:
        raise OcrUnavailable(f"The OCR provider could not list its languages: {exc}") from exc
    if not isinstance(tags, tuple) or not all(is_language_tag(t) for t in tags):
        raise OcrUnavailable("The OCR provider listed something that is not a set of language tags.")
    return tags


def _observe_text(frame: Frame, query: TextQuery, captured_at: float, ocr: OcrProvider | None,
                  cancel: threading.Event | None = None) -> ObservationResult:
    read = _read(frame, query, ocr, cancel)
    # An engine without confidence reports a placeholder 1.0 on every block. Filtering on it would pretend a
    # threshold was applied, so it is not filtered on, and the detail says so -- a saved condition that asks for
    # a minimum keeps working exactly as it always did with such an engine, and nothing claims the minimum held.
    measured = read.confidence_measured
    hits = _text_hits(read.blocks, query, filter_confidence=measured)
    matches = tuple(hits[:query.max_results]) if hits else ()
    return _result(TEXT, frame, query, found=bool(matches), matches=matches, captured_at=captured_at,
                   detail={"blocks_read": len(read.blocks), "query_text": query.text,
                           "match": query.match, "language": query.language,
                           "confidence_measured": measured,
                           "min_confidence_applied": measured and query.min_confidence > 0})


def _spaced(text: str, ignore_case: bool) -> str:
    """Runs of spaces counted as one, and the case folded unless asked otherwise: how lines are compared."""
    text = " ".join(text.split())
    return text.casefold() if ignore_case else text


def _observe_text_lines(frame: Frame, query: TextLinesQuery, captured_at: float, ocr: OcrProvider | None,
                        cancel: threading.Event | None = None) -> ObservationResult:
    """ONE reading of the region, then every line against everything read there. Found when at least one line
    holds (its text is there and should be, or is not there and should not be). The match is where the first line
    that holds BECAUSE its text is there was seen -- the whole piece of text the reader returned that contains it
    (a reader returns lines, not single words), so a click "on the match" goes to the middle of that piece, which in
    a longer line is beside the words themselves; when only an absent line holds there is no match (see
    `TextLinesQuery.points_at_match`)."""
    read = _read(frame, query, ocr, cancel)
    measured = read.confidence_measured                  # a placeholder confidence is never filtered on
    blocks = [(_spaced(b.text, query.ignore_case), b) for b in read.blocks
              if not measured or b.confidence >= query.min_confidence]
    lines, matches = [], ()
    for index, line in enumerate(query.lines):
        wanted = _spaced(line.text, query.ignore_case)
        hits = sorted((b for text, b in blocks if wanted in text),
                      key=lambda b: (-b.confidence, b.bounds.y, b.bounds.x))
        holds = bool(hits) == line.present
        lines.append({"text": line.text, "present": line.present, "seen": bool(hits), "holds": holds})
        if holds and line.present and not matches:
            matches = (Match(hits[0].bounds, hits[0].confidence, {"text": hits[0].text, "line": index}),)
    found = any(line["holds"] for line in lines)
    return _result(TEXT_LINES, frame, query, found=found, matches=matches, captured_at=captured_at,
                   detail={"blocks_read": len(read.blocks), "lines": tuple(lines), "language": query.language,
                           "confidence_measured": measured,
                           "min_confidence_applied": measured and query.min_confidence > 0})


def _no_check() -> None:
    return None


def observe(frame: Frame, query: Query, *, ocr: OcrProvider | None = None,
            captured_at: float | None = None, cancel: threading.Event | None = None,
            check: Callable[[], None] = _no_check, resize: Resize | None = None) -> ObservationResult:
    """Apply one query to one already-captured frame.

    `captured_at` is when that frame was taken; it defaults to now, which is right for a frame captured for
    this call and wrong (but harmless, and visible in `age_s`) for a reused one -- so `ObservationContext`,
    which is what reuses frames, always passes the real capture time. A look made of many (a picture in any size)
    runs `check` between them, which raises to end it; `resize` makes a picture at another size."""
    if not isinstance(frame, Frame):
        raise InvalidQuery("observe() needs a Frame from the verified screenshot pipeline.")
    taken = time.time() if captured_at is None else captured_at
    if isinstance(query, PixelQuery):
        return _observe_pixel(frame, query, taken)
    if isinstance(query, ColorRegionQuery):
        return _observe_color_region(frame, query, taken)
    if isinstance(query, TemplateQuery):
        return _observe_template(frame, query, taken, check, resize)
    if isinstance(query, TextQuery):
        return _observe_text(frame, query, taken, ocr, cancel)
    if isinstance(query, TextLinesQuery):
        return _observe_text_lines(frame, query, taken, ocr, cancel)
    raise InvalidQuery(f"Not an observation query: {type(query).__name__}.")
