"""Provider-independent contracts for targets, screenshots and their provenance.

    target  ->  ScreenshotProvider  ->  Screenshot (provenance-stamped)  ->  Frame  ->  vision

This module is the neutral foundation of the platform. It knows nothing about any particular kind of
target, device technology, operating system, application or file format; concrete providers live in
their own packages and implement these contracts. It defines:

  TargetIdentity      who a target is: provider id + stable logical target id (+ optional display name).
  FrameSource         who produced an image: provider id + target id (+ informational detail).
  Screenshot          an encoded PNG with its native size and a FrameSource. It can only be created by a
                      `ScreenshotProvider` (via `_issue_screenshot`), so code that receives one knows that a
                      provider which had already verified its target produced it.
  ScreenshotProvider  abstract base for providers. A provider is bound to ONE target at construction time and
                      is responsible for proving that target's identity before every capture. Consumers never
                      pass raw device handles, addresses or titles: the contract has no such parameter.
  InputProvider       structural interface for targets that also accept input.

Coordinate contract: pixel (x, y) of a `Screenshot` (origin at the top-left pixel, x growing right, y growing
down, one unit per pixel, native resolution) is in the SAME coordinate space as the integer coordinates that the
input methods of the same provider accept (`InputProvider.tap(x, y)` and the pointer operations of
`app.providers.input`). A provider whose raw image contains more than its input space (for example decoration
around the content) must crop it: there is exactly one pixel space per target, so a point found in a screenshot
can be used for input without conversion, and nothing is converted silently. How resizing and display scaling
affect that space is stated by each provider; if the scale cannot be established the provider refuses instead of
guessing.

Rules: `target_id` is a stable logical id, never a provider-specific handle. `detail` is informational only and
must never be used to make decisions or to address a target.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.errors import SafetyViolation

_SHOT_MINT = object()


@dataclass(frozen=True)
class TargetIdentity:
    provider_id: str   # names the provider implementation (stable string)
    target_id: str     # stable logical id of the verified target
    display_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not isinstance(self.target_id, str) \
                or not self.provider_id or not self.target_id:
            raise ValueError("A TargetIdentity needs a non-empty provider id and target id.")

    def source(self, detail: str = "") -> "FrameSource":
        return FrameSource(self.provider_id, self.target_id, detail)


@dataclass(frozen=True)
class FrameSource:
    provider_id: str
    target_id: str
    detail: str = ""   # informational only (never used for decisions)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.target_id:
            raise ValueError("A FrameSource needs a provider id and a target id.")

    def same_target(self, other: "FrameSource") -> bool:
        return (self.provider_id, self.target_id) == (other.provider_id, other.target_id)


@dataclass(frozen=True)
class Screenshot:
    """A PNG image of a verified target's screen at native resolution. Created only by ScreenshotProvider."""
    png: bytes
    width: int
    height: int
    source: FrameSource
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _SHOT_MINT:
            raise SafetyViolation("A Screenshot can only be created by a ScreenshotProvider.")


class ScreenshotProvider(ABC):
    """A source of screenshots bound to ONE verified target."""

    @property
    @abstractmethod
    def identity(self) -> TargetIdentity: ...

    @abstractmethod
    def capture(self) -> Screenshot:
        """Capture the target's screen. Must verify the target first and refuse if it cannot."""

    @property
    def provider_id(self) -> str:
        return self.identity.provider_id

    @property
    def target_id(self) -> str:
        return self.identity.target_id

    def _issue_screenshot(self, png: bytes, width: int, height: int, detail: str = "") -> Screenshot:
        """For subclasses only: stamp a captured image with this provider's provenance."""
        return Screenshot(png, width, height, self.identity.source(detail), _SHOT_MINT)


@runtime_checkable
class InputProvider(Protocol):
    """Targets that accept input. Deliberately tiny; more primitives are added only when approved."""

    @property
    def target_id(self) -> str: ...

    def tap(self, x: int, y: int) -> None: ...


MAX_SIZE = 100_000               # the largest width or height a size may have (app/providers/size.py)


def check_size(size: object, what: str = "size") -> tuple[int, int] | None:
    """None, or two whole numbers from 1 to MAX_SIZE (as a tuple)."""
    if size is None:
        return None
    if not isinstance(size, (tuple, list)) or len(size) != 2 \
            or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_SIZE for v in size):
        raise ValueError(f"{what} must be two whole numbers from 1 to {MAX_SIZE}, or nothing.")
    return (size[0], size[1])
