"""Frame: a decoded screenshot that remembers which provider and verified target produced it.

Target-agnostic: a Frame is built only from a provider `Screenshot` (which only a `ScreenshotProvider` can
create), so vision functions never see a raw device handle, address or title and never talk to a target.
Native pixel dimensions are preserved; nothing is resized.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import SafetyViolation
from app.providers.base import FrameSource, Screenshot

from .bitmap import Bitmap, VisionError, decode_png

_FRAME_MINT = object()


@dataclass(frozen=True)
class Frame:
    source: FrameSource
    bitmap: Bitmap
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _FRAME_MINT:
            raise SafetyViolation("A Frame can only be created with Frame.from_screenshot().")

    @property
    def width(self) -> int:
        return self.bitmap.width

    @property
    def height(self) -> int:
        return self.bitmap.height

    @property
    def target_id(self) -> str:
        return self.source.target_id

    @classmethod
    def from_screenshot(cls, shot: Screenshot) -> "Frame":
        if not isinstance(shot, Screenshot):
            raise VisionError("A Frame can only be built from a Screenshot issued by a ScreenshotProvider.")
        bitmap = decode_png(shot.png)
        if (bitmap.width, bitmap.height) != (shot.width, shot.height):
            raise VisionError("The decoded screenshot size differs from the size declared by the provider.")
        return cls(shot.source, bitmap, _FRAME_MINT)
