"""Provider-neutral AI contract: "what should the automation do, given this information?" -- nothing else.

An `AiProvider` is given a structured request (a prompt plus whatever structured context the application chooses
to hand it, for example an `OcrResult`'s text) and returns a structured response. It NEVER executes anything
itself and is never given the means to: `AiResponse.actions` is inert data (plain dicts/lists/scalars, checked
recursively to any depth), never a callable or another live object at any level, so an `AiProvider` cannot run a
command on its own however it is implemented. Turning that data into a
real automation step -- validating it, authorizing it, and only then calling into `app.playback`/a provider's
input session -- is a separate, application-controlled layer that does not exist yet; this module intentionally
stops at "here is what the AI proposed", so that boundary can be built deliberately later instead of being implied
by a provider that could already act.

Local model or cloud API: a caller holding an `AiProvider` cannot tell which, and the application does not depend
on any one vendor. Provider SELECTION (local vs. cloud, which vendor, credentials, routing between several) is
deliberately outside this module, the same way it is for `app.ocr`.

OCR and AI stay conceptually separate on purpose: OCR answers "what text is visible, where"; AI answers "what
should happen next". An `AiProvider` may be given OCR results as part of its context, but it is not an OCR
provider and does not read images itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

_INERT_SCALARS = (str, int, float, bool, type(None))
_MAX_INERT_DEPTH = 20   # generous for any real action description; far below Python's default recursion limit, so
                        # a pathologically (or maliciously) deep structure from an untrusted AI provider is REJECTED
                        # like any other non-inert value, never crashes this check with an uncaught RecursionError


def _is_inert(value: object, _depth: int = 0) -> bool:
    """True if `value` is safe, transport-shaped data: a string/number/bool/None, or a list/tuple/dict built only
    from further inert values, checked recursively up to `_MAX_INERT_DEPTH`. Nothing else passes -- in particular,
    anything callable, any other custom object, and anything nested deeper than the limit -- so an AI action can
    never smuggle out something an application could invoke, however deeply it is nested, and a provider cannot
    turn this safety check itself into a crash by returning absurdly deep data."""
    if _depth > _MAX_INERT_DEPTH:
        return False
    if isinstance(value, _INERT_SCALARS):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_inert(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_inert(v, _depth + 1) for k, v in value.items())
    return False


@dataclass(frozen=True)
class AiRequest:
    """What the AI is asked, plus whatever structured, inert context the application supplies (for example OCR
    text or a plain description of the current target). Never a live object, handle, credential or callable."""
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string.")
        if not isinstance(self.context, dict):
            raise ValueError("context must be a dict.")


@dataclass(frozen=True)
class AiResponse:
    """What the AI proposed. `text` is always present. `actions` is a provider-neutral, inert description of
    proposed steps (for example `{"op": "tap", "x": 10, "y": 20}`) -- plain data only, checked recursively so
    nothing callable or otherwise live can hide inside a nested dict or list, never something that can be called
    or that already holds a target/session/credential. Nothing may execute an entry from `actions` without first
    passing it through the application's own validation and authorization, which this module does not do."""
    text: str
    actions: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("text must be a string.")
        if not isinstance(self.actions, tuple) or not all(isinstance(a, dict) for a in self.actions):
            raise ValueError("actions must be a tuple of plain dicts.")
        for a in self.actions:
            if not _is_inert(a):
                raise ValueError("An action must be inert structured data only (no callables or other live "
                                 "objects, at any nesting depth); it must be safe to serialize.")


@runtime_checkable
class AiProvider(Protocol):
    """Anything that can answer an AiRequest. Cannot execute anything; it only returns data."""

    def ask(self, request: AiRequest) -> AiResponse: ...
