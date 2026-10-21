"""Provider-neutral OCR contract: "where is this text, what text is visible?" -- nothing else.

An `OcrProvider` reads text out of a `Frame` (the same provenance-carrying, provider-independent image type
`app.vision` already uses) and returns where each piece of text is. It never decides what the automation should DO
about that text (that is the separate AI contract in `app.ai`) and it never executes any action itself, so a
broken or malicious OCR provider cannot do anything beyond returning wrong text.

Local engine or cloud API: a caller holding an `OcrProvider` cannot tell which, and nothing here assumes either.
Provider SELECTION (which implementation to construct, local vs. cloud, with what credentials) is deliberately
outside this module; it is a composition-root concern, the same way `WindowsServices.create_default()` is the one
place that chooses a concrete Windows backend.

Optional extensions, added after the first providers were written and therefore never required:

    language            keyword of `read_text`: read in this BCP-47 language, or raise `LanguageNotSupported`.
    cancel              keyword of `read_text`: a `threading.Event`; when it is set the read ends promptly by
                        raising `ReadCancelled`. (The emergency stop is separate and arrives as `EmergencyStopped`.)
    reports_confidence  attribute; False when the provider has no confidence to report (see `OcrResult`).
    languages           method `languages(*, cancel=None) -> tuple[str, ...]`: the languages it can read here.

The observation layer passes `language` / `cancel` only to a provider whose `read_text` accepts them, and refuses
a language request outright for one that does not -- so an older provider keeps working unchanged and is never
told something it would silently ignore.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.errors import PlatformError
from app.vision.frame import Frame
from app.vision.roi import Roi

# The shape of a BCP-47 language tag ("en", "ru", "sv-SE", "zh-Hans-CN"): a 2-8 letter language, then subtags of 1-8
# letters or digits. Never starting with "-", so a tag can never be mistaken for a command-line option.
LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*\Z")


def is_language_tag(value: object) -> bool:
    return isinstance(value, str) and LANGUAGE_TAG.match(value) is not None


class LanguageNotSupported(PlatformError):
    """The provider cannot read in the requested language (not a language it knows, or not installed). Raised
    instead of reading in another language, which would turn "the text is not there" into a wrong answer."""


class ReadCancelled(PlatformError):
    """The caller's own cancel event ended the read before it finished. Not an engine failure, not a "no text"
    answer, and not the emergency stop."""


@dataclass(frozen=True)
class OcrTextBlock:
    """One recognized piece of text and where it is, in the frame's own native pixel coordinates."""
    text: str
    bounds: Roi
    confidence: float          # 0.0..1.0; 1.0 is also the placeholder of a provider that cannot estimate one --
                               # `OcrResult.confidence_measured` says which it is

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("text must be a string.")
        if not isinstance(self.bounds, Roi):
            raise ValueError("bounds must be a Roi.")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) \
                or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be a number between 0.0 and 1.0.")


@dataclass(frozen=True)
class OcrResult:
    """Every text block a provider found, in the order it returned them. Empty means "no text found", never an
    error; a provider that cannot read the image at all raises instead of returning an empty result.

    `confidence_measured` is False when the engine reports no confidence at all: every block then carries the
    placeholder 1.0, which is NOT a measurement and must never be shown, compared or filtered on as one.

    `language` is the language the text was actually read in (a BCP-47 tag), or "" when the provider cannot say --
    so asking for no particular language never hides which one was used."""
    blocks: tuple[OcrTextBlock, ...] = ()
    confidence_measured: bool = True
    language: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.confidence_measured, bool):
            raise ValueError("confidence_measured must be True or False.")
        if self.language != "" and not is_language_tag(self.language):
            raise ValueError("language must be a language tag such as 'en' or 'sv-SE', or ''.")

    @property
    def text(self) -> str:
        """Every recognized block's text, in order, one per line."""
        return "\n".join(b.text for b in self.blocks)


@runtime_checkable
class OcrProvider(Protocol):
    """Anything that can read text out of a Frame. Deliberately tiny: one operation, no state, no side effect
    beyond returning data. `roi` restricts the search to part of the frame; omitted, a provider searches all of
    it.

    `language` is the BCP-47 tag a caller wants the text read in ("en", "ru", "sv-SE"); "" means no preference.
    A provider that accepts it must honour it or raise `LanguageNotSupported` -- never read in another language and
    answer as if it had not. `cancel`, when given and set, ends the read with `ReadCancelled`. Both are optional
    for providers written before they existed (see the module docstring)."""

    def read_text(self, frame: Frame, *, roi: Roi | None = None, language: str = "",
                  cancel: threading.Event | None = None) -> OcrResult: ...


def reports_confidence(provider: object) -> bool:
    """Whether `provider` has real confidence values to report; one that does not say is assumed to."""
    return getattr(provider, "reports_confidence", True) is not False
