"""Error types shared across the platform. Messages are written for the end user."""
from __future__ import annotations

from typing import Self


class PlatformError(Exception):
    """Base class for all expected platform errors.

    Its message is English. `said` may hold the same message in other languages, by language code ("ru"), written
    where the error is raised -- by a provider whose words the screens may not hold themselves -- and shown to the
    person in their language (`said_in`)."""

    said: dict[str, str] = {}
    remedy: object | None = None      # what its provider can do about it on the person's yes (app.providers.remedy)

    def saying(self, **languages: str) -> Self:
        """The same message in other languages (`ru="..."`); returns the error itself, to be raised."""
        self.said = {**self.said, **{code: text for code, text in languages.items() if text}}
        return self


def said_in(exc: BaseException, language: str) -> str:
    """`exc`'s message in `language` when it carries one, else its own message."""
    return (getattr(exc, "said", None) or {}).get(language) or str(exc)


def said_params(exc: BaseException) -> dict[str, str]:
    """`exc`'s message in its other languages as the params of a run's detail line: {"message_ru": "..."}."""
    return {f"message_{code}": text for code, text in (getattr(exc, "said", None) or {}).items()}


class ConfigError(PlatformError):
    """Configuration is invalid."""


class EmergencyStopped(PlatformError):
    """The global emergency stop is active; no new work may start."""


class CommandTimeout(PlatformError):
    """An external command exceeded its timeout."""


class SafetyViolation(PlatformError):
    """An operation was refused because it would break a safety rule."""


class NoTargetSelected(SafetyViolation):
    """An operation needs at least one explicitly selected target. Empty never means all."""


class CapabilityNotAvailable(SafetyViolation):
    """A target was asked to do something it does not explicitly declare it can do."""


class TargetNotVerified(SafetyViolation):
    """The identity of an instance could not be proven, so no action may be sent to it."""

    def __init__(self, message: str, *, reason_code: str = "unverified"):
        super().__init__(message)
        self.reason_code = reason_code
