"""The single place where external programs are started.

Rules enforced here:
- arguments are a list (never a shell string); no shell is used;
- a positive timeout is REQUIRED for every call;
- the child is killed on timeout, on cancellation, or when the emergency stop triggers;
- nothing is raised for a failing command: the caller gets a CommandResult describing
  exactly what happened (exit code, output, duration, timed_out, cancelled, error).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

from .cancel import EmergencyStop
from .errors import EmergencyStopped

_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled and self.error is None

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class Runner(Protocol):
    """Anything that can run a command. Real code uses CommandRunner; tests use a fake."""

    def run(self, args: Sequence[str], *, timeout: float, cwd: str | None = None,
            cancel: threading.Event | None = None) -> CommandResult: ...


class CommandRunner:
    def __init__(self, stop: EmergencyStop) -> None:
        self._stop = stop

    def run(self, args: Sequence[str], *, timeout: float, cwd: str | None = None,
            cancel: threading.Event | None = None) -> CommandResult:
        if isinstance(args, (str, bytes)):
            raise TypeError("args must be a list of strings, not a shell string")
        argv = tuple(str(a) for a in args)
        if not argv:
            raise ValueError("empty command")
        if timeout is None or timeout <= 0:
            raise ValueError("a positive timeout is required for every external command")
        self._stop.check()  # raises EmergencyStopped: never start new work while stopped

        started = time.monotonic()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            return CommandResult(argv, None, error=f"could not start: {exc}",
                                 duration_ms=int((time.monotonic() - started) * 1000))

        timed_out = cancelled = False
        deadline = started + timeout
        out = err = b""
        while True:
            try:
                out, err = proc.communicate(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                    cancelled = True
                elif time.monotonic() >= deadline:
                    timed_out = True
                if cancelled or timed_out:
                    proc.kill()
                    out, err = proc.communicate()
                    break
        return CommandResult(
            argv, proc.returncode, out, err,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out, cancelled=cancelled,
        )


__all__ = ["CommandResult", "CommandRunner", "Runner", "EmergencyStopped"]
