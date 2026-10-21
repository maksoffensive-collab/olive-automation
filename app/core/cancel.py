"""Global emergency stop.

One EmergencyStop object is shared by every component that starts external work. When
triggered it (a) makes `check()` raise so no new action starts and (b) is polled by the
command runner, which kills any running child process. `reset()` is a deliberate,
separate step so a stop can never silently clear itself.
"""
from __future__ import annotations

import threading

from .errors import EmergencyStopped


class EmergencyStop:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def trigger(self, reason: str = "emergency stop") -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
            self._event.set()

    def reset(self) -> None:
        with self._lock:
            self._reason = None
            self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def check(self) -> None:
        """Raise EmergencyStopped if the stop is active. Call before starting any action."""
        if self._event.is_set():
            raise EmergencyStopped(f"Emergency stop is active ({self._reason}). Reset it before continuing.")

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout` seconds; return True immediately if the stop is triggered."""
        return self._event.wait(timeout)
