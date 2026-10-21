"""What can go wrong while observing, kept apart from what can be observed.

The distinction this module exists to enforce: **"not found" is not an error.** A query that ran correctly and
saw nothing returns an `ObservationResult` with `found=False` -- it answered the question. An error here always
means the question could not be asked at all: the image never arrived, the query was malformed, or the run was
told to stop.

    InvalidQuery            the query itself is wrong (region outside the image, template larger than the
                            search area, a colour that is not a colour). It would be wrong for any frame, so it
                            is raised before anything is captured where possible. A caller's bug.
    ObservationUnavailable  the image could not be obtained: the provider refused, the target no longer
                            verifies, the device is gone, the screenshot failed its own integrity checks. An
                            infrastructure failure, never a statement about what is on screen.
    OcrUnavailable          an OCR engine was asked for and could not answer: none configured, the engine is
                            missing from this machine, or it failed. Deliberately separate from
                            `ObservationUnavailable` so "I could not see" and "I could not read" are different
                            answers, and separate from an empty `OcrResult`, which means "no text here".
      OcrLanguageUnavailable  ... specifically: the text was asked for in a language the engine cannot read.
    ObservationTimeout      a bounded wait ended without the condition becoming true. Carries the last result
                            observed, so a caller can say what it did see instead of only that it gave up.
    ObservationCancelled    the caller's OWN cancel ended an observation that was under way (an OCR read).

The emergency stop is NOT modelled here: it raises the platform's existing `EmergencyStopped` straight through, so
observation stops exactly like every other activity and for exactly one reason. When both the stop and a caller's
cancel are set, the stop wins (`ObservationContext.observe` re-checks it).
"""
from __future__ import annotations

from app.core.errors import PlatformError


class ObservationError(PlatformError):
    """Base for every failure to ASK the question. Never raised to mean "nothing was found"."""


class InvalidQuery(ObservationError):
    """The query could not be applied to this frame: bad region, bad template, bad colour."""


class ObservationUnavailable(ObservationError):
    """No image to observe: capture failed, the provider refused, or the target no longer verifies."""


class OcrUnavailable(ObservationError):
    """No OCR engine could answer. Not the same as an engine that answered "no text"."""


class OcrLanguageUnavailable(OcrUnavailable):
    """The text was asked for in a language the OCR engine cannot read (no such language, not installed, or a
    provider that cannot be told a language). Refused rather than read in another language."""


class ObservationCancelled(ObservationError):
    """The caller's own cancel ended an observation that was under way (an OCR read, say). Never the emergency
    stop -- that still arrives as the platform's own `EmergencyStopped` -- and never a failure to see."""


class ObservationTimeout(ObservationError):
    """A bounded wait ended with the condition still false."""

    def __init__(self, message: str, *, waited_s: float = 0.0, attempts: int = 0, last=None) -> None:
        super().__init__(message)
        self.waited_s = waited_s
        self.attempts = attempts
        self.last = last               # the final ObservationResult, so the caller can report what it did see
