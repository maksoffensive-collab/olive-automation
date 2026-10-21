"""Windows window target and its verification.

A `WindowTarget` is the Windows implementation of the generic `Target` contract. It can only be created by
`WindowVerifier.verify()`. Generic code sees only the neutral view (provider id, opaque target id, display name,
capabilities, verification provenance); the window handle, process id and process start time stay in here.

What VERIFIED means (three independent checks, all repeated by `recheck()` before every capture):
  1. window_valid       the handle is a live, visible, top-level window (an invalid, destroyed or child-window
                        handle fails);
  2. process_identity   the window belongs to ONE specific process instance: the process id AND the process
                        creation time (and executable name) are known and equal to the ones recorded when the
                        target was verified. A recycled process id, or a handle recycled by another program,
                        does not match. If the process identity cannot be read (for example access denied for a
                        higher-privileged process) verification is REFUSED, never assumed;
  3. window_class       the window class equals the recorded one, so a handle reused by a different kind of
                        window in the same process is caught.
Optionally the caller can additionally require an executable name (`expected_process_name`).

What it does NOT prove: that the window is the one the user "meant" (that is the selecting caller's job), nor
that its title is unchanged. Titles change and are informational only; they are not part of the identity.

Target ids are opaque and stable: a hash of handle, process id, process start and class, so the same window
gets the same id for as long as it lives and a recycled handle never inherits an old id.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.providers.base import TargetIdentity
from app.providers.target import Capability, Target, TargetVerification, VerificationState
from app.windows.backend import WindowBackend, WindowInfo
from app.windows.ids import CAPABILITIES, DESKTOP_CAPABILITIES, PROVIDER_ID

_MINT = object()
_MAX_HANDLE = 0xFFFFFFFF  # window handles are 32-bit significant on every Windows version
METHODS = ("window_valid", "process_identity", "window_class")


@dataclass(frozen=True)
class WindowTarget(Target):
    """Proof that a handle is a specific window of a specific process instance. Not forgeable by accident."""
    target_id_value: str
    hwnd: int
    pid: int
    process_start: int
    process_name: str
    class_name: str
    title: str                  # informational snapshot only
    verified_at: float
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TargetNotVerified("A WindowTarget can only be created by WindowVerifier.verify().",
                                    reason_code="forged")

    @property
    def identity(self) -> TargetIdentity:
        label = f"{self.process_name}: {self.title}" if self.title else self.process_name
        return TargetIdentity(PROVIDER_ID, self.target_id_value, label)

    @property
    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    @property
    def verification(self) -> TargetVerification:
        return TargetVerification(VerificationState.VERIFIED, METHODS, self.verified_at)


FULL_DESKTOP_ID = "win-full-desktop"


Rect = tuple[int, int, int, int]       # left, top, width, height


def desktop_id(layout: Rect, monitors: tuple[Rect, ...] = ()) -> str:
    """The identity of the whole desktop IN ONE SCREEN LAYOUT -- (left, top, width, height) of the virtual screen
    and, when there are several, of each monitor in it (two monitors can be rearranged inside the same bounding box).
    Screen coordinates mean the same thing only within one layout, so a recording, a picture and a run made in one
    layout carry it, and another layout is another target. With one monitor the id is the virtual screen's alone."""
    left, top, width, height = layout
    ident = f"{FULL_DESKTOP_ID}@{left},{top},{width}x{height}"
    shown = tuple(sorted(tuple(m) for m in monitors))
    if shown and shown != (tuple(layout),):
        ident += "".join(f";{ml},{mt},{mw}x{mh}" for ml, mt, mw, mh in shown)
    return ident


def screen_layout(screen) -> tuple[Rect, tuple[Rect, ...]]:
    """The screen layout as it is NOW -- the virtual screen and each monitor (sorted) -- read from `screen` (a
    `WindowBackend`); compared with `FullWindowsTarget.layout_key`."""
    return tuple(screen.virtual_screen()), tuple(sorted(tuple(m) for m in screen.monitors()))


def is_desktop_id(target_id: str) -> bool:
    """Whether `target_id` names the whole desktop (in any layout): its coordinates are screen coordinates."""
    return target_id == FULL_DESKTOP_ID or target_id.startswith(FULL_DESKTOP_ID + "@")


@dataclass(frozen=True)
class FullWindowsTarget(Target):
    """The whole Windows desktop as one automation context, distinct from any specific window: there is no handle
    to lose, no process to outlive and no class to change -- but there is a SCREEN LAYOUT, `layout` = (left, top,
    width, height) of the virtual screen when it was chosen, part of its identity (`desktop_id`): coordinates on the
    desktop mean the same thing only within one layout, so a changed one (a monitor added, a resolution changed)
    is another target, and re-checking fails (WindowsServices.recheck).

    It is looked at as one picture of the virtual screen, every monitor (`provider.DesktopScreenshotProvider`), and,
    on the person's explicit decision of 2026-10-19, recorded on (`recorder.DesktopRecorder`) and played on
    (`desktop.DesktopInputSession`): input at a point of the screen, wherever it lands -- every event gated there
    (same layout, never on this application's own windows, Esc or the person's own mouse stops the run)."""
    verified_at: float
    layout: Rect
    monitors: tuple[Rect, ...] = ()      # each monitor; () = one, the whole virtual screen

    @property
    def screens(self) -> tuple[Rect, ...]:
        """Each monitor's rectangle, sorted (at least one)."""
        return tuple(sorted(tuple(m) for m in self.monitors)) or (tuple(self.layout),)

    @property
    def layout_key(self) -> tuple[Rect, tuple[Rect, ...]]:
        """What `screen_layout` must read for this target to still be the same screen."""
        return tuple(self.layout), self.screens

    def on_a_monitor(self, screen_x: int, screen_y: int) -> bool:
        """Whether a screen point is on one of the monitors (not in a gap of the bounding box between them)."""
        return any(ml <= screen_x < ml + mw and mt <= screen_y < mt + mh for ml, mt, mw, mh in self.screens)

    @property
    def identity(self) -> TargetIdentity:
        return TargetIdentity(PROVIDER_ID, desktop_id(self.layout, self.screens), "Full Windows")

    @property
    def capabilities(self) -> frozenset[Capability]:
        return DESKTOP_CAPABILITIES

    @property
    def verification(self) -> TargetVerification:
        return TargetVerification(VerificationState.VERIFIED, ("always_available",), self.verified_at)


def _target_id(info: WindowInfo) -> str:
    key = f"{info.hwnd}:{info.pid}:{info.process_start}:{info.class_name}".encode("utf-8")
    return "win-" + hashlib.sha256(key).hexdigest()[:16]


def _valid_handle(hwnd: object) -> bool:
    return isinstance(hwnd, int) and not isinstance(hwnd, bool) and 0 < hwnd <= _MAX_HANDLE


class WindowVerifier:
    def __init__(self, backend: WindowBackend, stop: EmergencyStop) -> None:
        self.backend = backend
        self._stop = stop

    def verify(self, hwnd: int, *, expected_process_name: str | None = None,
               expected: WindowInfo | None = None) -> WindowTarget:
        """Prove `hwnd`. If `expected` (a snapshot the caller listed earlier and the user chose) is given, the window
        NOW behind the handle must have the same handle, process id, process start time, executable and class;
        otherwise the handle was reused and verification is refused (`identity_changed`). The title is ignored."""
        self._stop.check()
        info = self._current(hwnd)
        self._check_window(info)
        self._check_process_known(info)
        if expected is not None and (info.hwnd, info.pid, info.process_start, info.process_name, info.class_name) != (
                expected.hwnd, expected.pid, expected.process_start, expected.process_name, expected.class_name):
            raise TargetNotVerified("That window is no longer the one that was listed (its handle now belongs to "
                                    "another program or window). Refresh the list and select again.",
                                    reason_code="identity_changed")
        if expected_process_name is not None and info.process_name.lower() != expected_process_name.strip().lower():
            raise TargetNotVerified("The window belongs to a different program than expected.",
                                    reason_code="unexpected_process")
        return WindowTarget(_target_id(info), info.hwnd, info.pid, info.process_start, info.process_name,
                            info.class_name, info.title, time.monotonic(), _mint=_MINT)

    def recheck(self, target: WindowTarget) -> WindowInfo:
        """Re-run all three checks against the recorded proof. Returns the fresh snapshot or raises."""
        if not isinstance(target, WindowTarget):
            raise TargetNotVerified("A WindowTarget is required.", reason_code="not_verified_target")
        self._stop.check()
        info = self._current(target.hwnd)
        self._check_window(info)
        self._check_process_known(info)
        if (info.pid, info.process_start, info.process_name) != (target.pid, target.process_start, target.process_name):
            raise TargetNotVerified("The window now belongs to a different process; the target is void.",
                                    reason_code="process_changed")
        if info.class_name != target.class_name:
            raise TargetNotVerified("The window handle now refers to a different kind of window.",
                                    reason_code="class_changed")
        return info

    # ---- individual checks ---------------------------------------------
    def _current(self, hwnd: int) -> WindowInfo:
        if not _valid_handle(hwnd):
            raise TargetNotVerified("Not a valid window identifier.", reason_code="invalid_handle")
        info = self.backend.query(hwnd)
        if info is None:
            raise TargetNotVerified("The window does not exist (any more).", reason_code="window_gone")
        return info

    @staticmethod
    def _check_window(info: WindowInfo) -> None:
        if not info.top_level:
            raise TargetNotVerified("The handle is not a top-level window.", reason_code="not_top_level")
        if not info.visible:
            raise TargetNotVerified("The window is not visible.", reason_code="not_visible")

    @staticmethod
    def _check_process_known(info: WindowInfo) -> None:
        if info.pid <= 0 or info.process_start <= 0 or not info.process_name:
            raise TargetNotVerified("The window's process identity cannot be read, so it cannot be verified "
                                    "(for example a process running with higher privileges).",
                                    reason_code="process_unknown")
