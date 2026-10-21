"""Macro playback engine (Phase 7). It never talks to a device by itself.

Safety architecture:
  * `play()` accepts only a `VerifiedTarget`; a bare index or serial is refused.
  * Actions go to an `InputSink`. Only `DryRunSink` exists: no real ADB input sink is
    implemented, because the live identity mapping and the coordinate space (hypothesis H2 in
    docs/MACRO_FORMAT.md) are [NEEDS TESTING].
  * Coordinates are NEVER passed through unless the caller supplies an explicit
    `CoordinateTransform`; the default is to refuse.
  * Macros with validator ERRORs and `mergedRecords` files are refused.
  * The emergency stop is checked before every step and interrupts waiting.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.core.cancel import EmergencyStop
from app.core.errors import PlatformError, TargetNotVerified
from app.instances.mapping import VerifiedTarget

from .model import Macro
from .validator import ERROR, validate


class PlaybackRefused(PlatformError):
    pass


@dataclass(frozen=True)
class CoordinateTransform:
    """pixel = coordinate * scale. Must be supplied explicitly; nothing is assumed (H2 unverified)."""
    scale_x: float
    scale_y: float

    def __post_init__(self) -> None:
        if not (self.scale_x > 0 and self.scale_y > 0):
            raise ValueError("scales must be positive")

    def apply(self, x: float, y: float) -> tuple[int, int]:
        return round(x * self.scale_x), round(y * self.scale_y)


@dataclass(frozen=True)
class Step:
    at_ms: float
    operation_id: Any
    points: tuple[tuple[Any, int, int, Any], ...]  # (pointer id, x, y, state), already transformed
    params: dict[str, Any]


class InputSink(Protocol):
    def send(self, target: VerifiedTarget, step: Step) -> None: ...


class DryRunSink:
    """Records what would be sent. Sends nothing anywhere."""
    def __init__(self) -> None:
        self.steps: list[tuple[str, Step]] = []

    def send(self, target: VerifiedTarget, step: Step) -> None:
        self.steps.append((target.adb_serial, step))


def build_steps(macro: Macro, transform: CoordinateTransform | None) -> list[Step]:
    if macro.merged_records is not None:
        raise PlaybackRefused("Playback of 'mergedRecords' files is not supported (join semantics unverified).")
    if macro.operations is None:
        raise PlaybackRefused("The macro has no operations.")
    errors = [f for f in validate(macro) if f.severity == ERROR]
    if errors:
        raise PlaybackRefused("The macro is invalid: " + "; ".join(str(e) for e in errors[:5]))
    steps = []
    for op in macro.operations:
        pts = []
        for p in op.points or []:
            if transform is None:
                raise PlaybackRefused("Touch coordinates need an explicit CoordinateTransform; the coordinate "
                                      "space of .record files is unverified (H2).")
            x, y = transform.apply(p.x, p.y)
            pts.append((p.id, x, y, p.state))
        steps.append(Step(op.timing, op.operation_id, tuple(pts), dict(op.params)))
    return steps


@dataclass
class PlaybackResult:
    sent: int
    completed: bool
    stopped: bool


def play(target: VerifiedTarget, macro: Macro, sink: InputSink, stop: EmergencyStop, *,
         transform: CoordinateTransform | None = None, speed: float = 1.0,
         clock: Callable[[], float] = time.monotonic) -> PlaybackResult:
    if not isinstance(target, VerifiedTarget):
        raise TargetNotVerified("Playback requires a VerifiedTarget.", reason_code="not_verified_target")
    if not speed > 0:
        raise PlaybackRefused("speed must be positive")
    stop.check()
    steps = build_steps(macro, transform)
    start = clock()
    sent = 0
    for step in steps:
        due = start + step.at_ms / 1000.0 / speed
        while True:
            remaining = due - clock()
            if remaining <= 0:
                break
            if stop.wait(min(remaining, 0.05)):
                return PlaybackResult(sent, False, True)
        if stop.is_set():
            return PlaybackResult(sent, False, True)
        sink.send(target, step)
        sent += 1
    return PlaybackResult(sent, True, False)
