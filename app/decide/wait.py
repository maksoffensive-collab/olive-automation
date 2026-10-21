"""WAIT UNTIL: poll one condition on one target until it holds, with a bound that cannot be left out.

Three outcomes, kept apart because they mean different things to whoever is waiting:

    SATISFIED   the condition became true. `last` is the observation that made it true.
    TIMED_OUT   the bound expired with the condition still false. A RESULT, not an exception: "the dialog never
                appeared" is an ordinary answer, and `last` still says what WAS on the screen at the end.
    (raised)    the wait could not continue: `EmergencyStopped` (the platform's stop), `Cancelled` (this run's
                own cancel), or the observation layer's own errors when the screen could not be seen at all
                (`ObservationUnavailable`, `OcrUnavailable`, `InvalidQuery`). "Not found" during polling is
                neither -- it is the normal answer that makes the wait poll again.

Bounded by construction: `WaitUntil` refuses a missing, zero, negative or over-long timeout (the observation
layer's own `MAX_TIMEOUT_S`) and a non-positive interval, and the interval is never shorter than the observation
layer's own `MIN_INTERVAL_S`. The first look is immediate, so a condition that already holds costs one screenshot.

Sleeping is sliced (at most `SLICE_S`) and each slice checks both the emergency stop and the run's cancellation,
so either ends the wait within one slice instead of after the interval. Every poll takes a FRESH screenshot: a
cached frame can never become true.

Nothing here acts. A wait returns an answer; what to do about it is the caller's.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from app.core.cancel import EmergencyStop
from app.observe.context import DEFAULT_INTERVAL_S, MAX_TIMEOUT_S, MIN_INTERVAL_S, ObservationContext

from .condition import Condition, ConditionResult, check_cancel, evaluate
from .errors import InvalidCondition
from .position import Position, check_position

SLICE_S = 0.05


def _seconds(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class WaitUntil:
    """Wait for `condition`, for at most `timeout_s`, looking every `interval_s`. `at`: where its block is drawn
    (presentation only, see `position.py`)."""
    condition: Condition
    timeout_s: float
    interval_s: float = DEFAULT_INTERVAL_S
    at: Position = None

    def __post_init__(self) -> None:
        if not isinstance(self.condition, Condition):
            raise InvalidCondition("WAIT UNTIL needs a Condition.")
        if not _seconds(self.timeout_s) or not 0 < self.timeout_s <= MAX_TIMEOUT_S:
            raise InvalidCondition(f"timeout_s must be greater than 0 and at most {MAX_TIMEOUT_S:g} seconds; "
                                   "a wait without a bound is not allowed.")
        if not _seconds(self.interval_s) or not self.interval_s > 0:
            raise InvalidCondition("interval_s must be a positive number of seconds.")
        object.__setattr__(self, "at", check_position(self.at))

    @property
    def effective_interval_s(self) -> float:
        return max(float(self.interval_s), MIN_INTERVAL_S)


class WaitStatus(str, Enum):
    SATISFIED = "satisfied"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class WaitResult:
    status: WaitStatus
    last: ConditionResult          # the final answer: what made it true, or what was still there at the timeout
    attempts: int                  # screenshots taken
    waited_s: float

    @property
    def satisfied(self) -> bool:
        return self.status is WaitStatus.SATISFIED

    def diagnostics(self) -> dict:
        return {"status": self.status.value, "attempts": self.attempts, "waited_s": round(self.waited_s, 3),
                "last": self.last.diagnostics()}


def _sleep(seconds: float, stop: EmergencyStop | None, cancel: threading.Event | None) -> None:
    """Sleep in slices; wake at once for the stop (raising it) or for this run's cancel (raising that)."""
    end = time.monotonic() + seconds
    while True:
        if stop is not None:
            stop.check()
        check_cancel(cancel)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        chunk = min(remaining, SLICE_S)
        if stop is not None:
            stop.wait(chunk)
        elif cancel is not None:
            cancel.wait(chunk)
        else:
            time.sleep(chunk)


def wait_until(wait: WaitUntil, context: ObservationContext, *, stop: EmergencyStop | None = None,
               cancel: threading.Event | None = None) -> WaitResult:
    """Poll `wait.condition` on the context's target until it holds or `wait.timeout_s` passes."""
    if not isinstance(wait, WaitUntil):
        raise InvalidCondition("wait_until() needs a WaitUntil.")
    started = time.monotonic()
    attempts = 0
    while True:
        if stop is not None:
            stop.check()
        last = evaluate(wait.condition, context, cancel=cancel)    # max_age_s=0: always a fresh frame
        attempts += 1
        waited = time.monotonic() - started
        if last.satisfied:
            return WaitResult(WaitStatus.SATISFIED, last, attempts, waited)
        remaining = wait.timeout_s - waited
        if remaining <= 0:
            return WaitResult(WaitStatus.TIMED_OUT, last, attempts, waited)
        _sleep(min(wait.effective_interval_s, remaining), stop, cancel)
