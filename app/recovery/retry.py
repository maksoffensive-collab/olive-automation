"""Bounded retry with stop-aware back-off. Infinite retries are impossible by construction."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError, SafetyViolation

T = TypeVar("T")
MAX_ATTEMPTS_LIMIT = 10


class RetriesExhausted(PlatformError):
    def __init__(self, attempts: int, last: BaseException):
        super().__init__(f"Gave up after {attempts} attempt(s): {last}")
        self.attempts = attempts
        self.last = last


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    delay_s: float = 1.0
    backoff: float = 2.0
    max_delay_s: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise ValueError(f"max_attempts must be an integer 1..{MAX_ATTEMPTS_LIMIT}")
        if self.delay_s < 0 or self.max_delay_s < 0 or self.backoff < 1:
            raise ValueError("delay_s and max_delay_s must be >= 0 and backoff >= 1")

    def delay_before_retry(self, attempt: int) -> float:
        """Delay after failed attempt number `attempt` (1-based)."""
        return min(self.delay_s * (self.backoff ** (attempt - 1)), self.max_delay_s)


def run_with_retry(fn: Callable[[], T], policy: RetryPolicy, stop: EmergencyStop, *,
                   is_transient: Callable[[BaseException], bool] = lambda e: True,
                   on_attempt: Callable[[int, BaseException | None], None] | None = None) -> T:
    """Call `fn` up to policy.max_attempts times. Safety violations and emergency stops are never retried.
    Non-transient errors propagate unchanged; exhausting the attempts raises RetriesExhausted."""
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        stop.check()
        try:
            result = fn()
            if on_attempt:
                on_attempt(attempt, None)
            return result
        except (EmergencyStopped, SafetyViolation):
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            if on_attempt:
                on_attempt(attempt, exc)
            if not is_transient(exc):
                raise
            last = exc
            if attempt < policy.max_attempts and stop.wait(policy.delay_before_retry(attempt)):
                stop.check()
    assert last is not None
    raise RetriesExhausted(policy.max_attempts, last)
