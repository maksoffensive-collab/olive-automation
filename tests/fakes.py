"""Test doubles. No real process, ADB or LDPlayer is touched by tests that use these."""
from __future__ import annotations

from typing import Callable, Sequence

from app.core.process import CommandResult


def result(stdout: str = "", stderr: str = "", rc: int = 0, *, timed_out: bool = False,
           cancelled: bool = False, args: tuple[str, ...] = ("fake",)) -> CommandResult:
    return CommandResult(args, None if timed_out else rc, stdout.encode("utf-8"), stderr.encode("utf-8"),
                         duration_ms=1, timed_out=timed_out, cancelled=cancelled)


class FakeRunner:
    """Records every call and answers with `handler(args)`."""

    def __init__(self, handler: Callable[[tuple[str, ...]], CommandResult]) -> None:
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, args: Sequence[str], *, timeout: float, cwd: str | None = None, cancel=None) -> CommandResult:
        argv = tuple(args)
        self.calls.append((argv, timeout))
        return self.handler(argv)
