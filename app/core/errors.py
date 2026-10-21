"""Error types shared across the platform. Messages are written for the end user."""
from __future__ import annotations


class PlatformError(Exception):
    """Base class for all expected platform errors."""


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
