"""Windows screenshot provider: captures ONE verified window as a generic `Screenshot`.

Method: `PrintWindow` with the full-content flag into an off-screen bitmap, so the window does not need to be
in front and nothing is moved, focused or clicked. The result is the window's CLIENT area only (frame, title bar
and borders are cropped away), so screenshot pixel (x, y) IS client coordinate (x, y): the very same space that
`WindowInputSession` accepts, with the same origin (top-left), axes (x right, y down) and unit (one pixel). A
resize changes the image size and the input bounds together; nothing is scaled or clamped. Pixels are physical
pixels; when the target's own scale is ambiguous (DPI-unaware on a scaled monitor) the capture is refused. Every
capture:
  1. checks the emergency stop and re-verifies the target (`WindowVerifier.recheck`);
  2. refuses windows that cannot be captured reliably: minimized, hidden by the desktop manager (cloaked),
     empty, absurdly large, or of ambiguous DPI scale (each raises `WindowCaptureError` with the reason);
  3. copies the client-area pixels, checks their size against the client area and refuses an entirely black image;
  4. re-verifies the target and requires the same window AND client size, so the image cannot belong to another
     window or to another layout;
  5. encodes a PNG and stamps it with this provider's identity (`_issue_screenshot`).

Known limitation, stated rather than hidden: some windows (hardware-accelerated or protected content, some
games and video players) yield a fully black image from `PrintWindow` while reporting success. That cannot be
told apart from a genuinely black window, so an entirely black capture is refused as unreliable.
"""
from __future__ import annotations

from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.providers.base import Screenshot, ScreenshotProvider, TargetIdentity
from app.providers.target import Capability, Target, require_capability
from app.windows.backend import WindowBackend, WindowCaptureError, WindowInfo
from app.windows.png import encode_rgb_png_from_bgra
from app.windows.target import FullWindowsTarget, WindowTarget, WindowVerifier, screen_layout

MAX_SIDE = 8192


def _has_any_colour(bgra: bytes) -> bool:
    """True if any pixel has a non-zero colour channel (alpha is ignored: PrintWindow often leaves it 0)."""
    return bool(bgra[0::4].strip(b"\x00") or bgra[1::4].strip(b"\x00") or bgra[2::4].strip(b"\x00"))


class WindowScreenshotProvider(ScreenshotProvider):
    def __init__(self, target: Target, verifier: WindowVerifier, stop: EmergencyStop) -> None:
        require_capability(target, Capability.SCREENSHOT)  # verified + explicitly declared
        if not isinstance(target, WindowTarget):
            raise TargetNotVerified("The Windows screenshot provider can only capture a WindowTarget.",
                                    reason_code="not_verified_target")
        self._target = target
        self._verifier = verifier
        self._stop = stop
        self._identity: TargetIdentity = target.identity

    @property
    def identity(self) -> TargetIdentity:
        return self._identity

    @staticmethod
    def _require_capturable(info: WindowInfo) -> None:
        if info.minimized:
            raise WindowCaptureError("The window is minimized; its contents cannot be captured.")
        if info.cloaked:
            raise WindowCaptureError("The window is hidden by the desktop manager "
                                     "(for example on another virtual desktop).")
        if info.client_width <= 0 or info.client_height <= 0:
            raise WindowCaptureError("The window has no visible area.")
        if info.client_width > MAX_SIDE or info.client_height > MAX_SIDE:
            raise WindowCaptureError("The window is larger than the supported capture size.")
        if info.dpi_ambiguous:
            raise WindowCaptureError("The window's DPI scale is ambiguous (a DPI-unaware program on a scaled "
                                     "monitor), so pixel coordinates cannot be related to the screen; refused.")

    def capture(self) -> Screenshot:
        before = self._verifier.recheck(self._target)            # stop check + identity proof
        self._require_capturable(before)
        raw = self._verifier.backend.capture(self._target.hwnd, before.client_width, before.client_height)
        if ((raw.width, raw.height) != (before.client_width, before.client_height)
                or len(raw.bgra) != raw.width * raw.height * 4):
            raise WindowCaptureError("The captured image does not match the client area size; discarded.")
        if not _has_any_colour(raw.bgra):
            raise WindowCaptureError("The capture is entirely black; this window cannot be captured reliably.")
        after = self._verifier.recheck(self._target)             # the window is still the same one
        if ((after.width, after.height, after.client_width, after.client_height, after.client_left, after.client_top)
                != (before.width, before.height, before.client_width, before.client_height, before.client_left,
                    before.client_top)):
            raise WindowCaptureError("The window changed size or layout during capture; discarded.")
        png = encode_rgb_png_from_bgra(raw.bgra, raw.width, raw.height)
        return self._issue_screenshot(png, raw.width, raw.height, detail=f"window of {self._target.process_name}")


class DesktopScreenshotProvider(ScreenshotProvider):
    """Captures the WHOLE desktop -- the virtual screen, every monitor -- for the "Full Windows" target.

    One picture, read only: nothing is moved, focused or clicked. The image's pixel (0, 0) is the top-left corner of
    the virtual screen, and everything downstream (text boxes, conditions, a click on what was found) measures in the
    image's own pixels -- the same pixels as `DesktopInputSession`'s coordinates, in the same screen layout. Like a window capture, every capture checks the emergency stop first and refuses an image that is
    entirely black (a locked or protected desktop cannot be told apart from a black one)."""

    def __init__(self, target: Target, backend: WindowBackend, stop: EmergencyStop) -> None:
        require_capability(target, Capability.SCREENSHOT)
        if not isinstance(target, FullWindowsTarget):
            raise TargetNotVerified("The desktop screenshot provider can only capture the Full Windows target.",
                                    reason_code="not_verified_target")
        self._target, self._backend, self._stop = target, backend, stop
        self._identity: TargetIdentity = target.identity

    @property
    def identity(self) -> TargetIdentity:
        return self._identity

    def capture(self) -> Screenshot:
        self._stop.check()
        shot = self._backend.capture_desktop(max_side=MAX_SIDE)    # larger is refused before it is allocated
        raw = shot.bitmap
        if not (0 < raw.width <= MAX_SIDE and 0 < raw.height <= MAX_SIDE) \
                or len(raw.bgra) != raw.width * raw.height * 4:
            raise WindowCaptureError("The desktop capture has an unsupported size; discarded.")
        if (shot.left, shot.top, raw.width, raw.height) != tuple(self._target.layout) \
                or screen_layout(self._backend)[1] != self._target.screens:
            # its pixels would be stamped with a layout they do not have: a click computed from them would land
            # elsewhere
            raise WindowCaptureError("The screen layout changed (monitors or resolution) since Full Windows was "
                                     "chosen; choose it again.")
        if not _has_any_colour(raw.bgra):
            raise WindowCaptureError("The desktop capture is entirely black (a locked or protected desktop); "
                                     "it cannot be used.")
        self._stop.check()
        png = encode_rgb_png_from_bgra(raw.bgra, raw.width, raw.height)
        return self._issue_screenshot(png, raw.width, raw.height,
                                      detail=f"desktop {raw.width}x{raw.height} at ({shot.left}, {shot.top})")

