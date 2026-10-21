"""Observation: "what is on the screen?" -- and nothing else.

    target provider -> Screenshot -> Frame -> Observation (pixel | colour | template | text) -> ObservationResult

This package sits above `app.vision` (the deterministic pixel algorithms) and `app.ocr` (the text contract), and
below whatever decides what to do. It answers questions about an image; it never decides and never acts. There
is no input session, engine or tap anywhere in it, so an observation cannot become a click by accident: a caller
that wants to act on a result must take it to the existing playback/input path deliberately.

It is provider-neutral in the same way `app.vision` is. It knows `ScreenshotProvider`, `Frame` and `FrameSource`
and nothing about how any target is reached -- no device technology, no operating system API, no serial, no
window handle, no engine name. A `TextQuery("Collect")` names the text, never the OCR implementation that reads
it; which implementation answers is chosen at the composition root, exactly as the screenshot provider is.

    errors.py     the taxonomy. "Not found" is a RESULT, never an exception.
    query.py      what to look for, as inert reusable data.
    observer.py   applying a query to a frame. Pure, deterministic, no capture and no waiting.
    result.py     one answer shape for every kind: found / satisfied / confidence / bounds / center / detail.
    context.py    one target's screen: capture, reuse, bounded and cancellable waiting.
"""
from .context import DEFAULT_INTERVAL_S, MAX_TIMEOUT_S, ObservationContext
from .errors import (InvalidQuery, ObservationCancelled, ObservationError, ObservationTimeout,
                     ObservationUnavailable, OcrLanguageUnavailable, OcrUnavailable)
# `OcrProvider` is part of this package's own API: it is the type of the text reader an `ObservationContext` is
# given, and `OcrResult` is what `read_text` answers. Re-exported so a caller that only hands a reader through or
# shows what it read (the GUI) can name those types without importing the OCR package itself -- it still depends
# on the OCR CONTRACT through this API (never on an engine), and says so.
from app.ocr.provider import OcrProvider, OcrResult
from .observer import applies_text_confidence, check_text_reader, observe, read_text, reader_languages
from .query import (CONTAINS, EXACT, MAX_TEXT_LINES, ColorRegionQuery, PixelQuery, Query, TemplateQuery, TextLine,
                    TextLinesQuery, TextQuery)
from .result import COLOR_REGION, KINDS, PIXEL, TEMPLATE, TEXT, TEXT_LINES, Match, ObservationResult
from .scale import PARTS, scaled_part, scaled_query

__all__ = [
    "scaled_query", "scaled_part", "PARTS",
    "ObservationContext", "observe",
    "PixelQuery", "ColorRegionQuery", "TemplateQuery", "TextQuery", "TextLine", "TextLinesQuery", "MAX_TEXT_LINES",
    "Query", "EXACT", "CONTAINS",
    "ObservationResult", "Match", "PIXEL", "COLOR_REGION", "TEMPLATE", "TEXT", "TEXT_LINES", "KINDS",
    "ObservationError", "InvalidQuery", "ObservationUnavailable", "OcrUnavailable", "OcrLanguageUnavailable",
    "ObservationTimeout", "ObservationCancelled", "OcrProvider", "applies_text_confidence", "check_text_reader",
    "OcrResult", "read_text", "reader_languages",
    "MAX_TIMEOUT_S", "DEFAULT_INTERVAL_S",
]
