"""LDPlayer console wrapper (the ONLY module that runs ldconsole.exe).

Phase 3 scope is READ-ONLY discovery: only `list2` and `runninglist` are allowed. Commands
that start, stop, clone, reset or modify an instance are deliberately not implemented and
`run_readonly` refuses anything outside READ_ONLY_COMMANDS.

Facts: the 10-field shape of a `list2` line was observed on 2026-09-21 [VERIFIED for
stopped instances]. The meaning of each numeric column is from public LDPlayer
documentation [ASSUMPTION] and is [NEEDS TESTING] against a running instance.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.errors import PlatformError, SafetyViolation
from app.core.process import CommandResult, Runner
from app.logging.structured import Logger

READ_ONLY_COMMANDS = frozenset({"list2", "runninglist"})
_NUMERIC_TAIL = 8  # top_hwnd, bind_hwnd, android_started, pid, vbox_pid, width, height, dpi


class LDConsoleError(PlatformError):
    """ldconsole could not be run or returned something unexpected."""


@dataclass(frozen=True)
class LDInstance:
    index: int
    name: str
    top_hwnd: int          # [ASSUMPTION] top-level window handle, 0 when no window
    bind_hwnd: int         # [ASSUMPTION] bound render window handle
    android_started: int   # [ASSUMPTION] 1 when Android has booted
    pid: int               # [ASSUMPTION] LDPlayer process id, -1 when not running
    vbox_pid: int          # [ASSUMPTION] VirtualBox process id, -1 when not running
    width: int
    height: int
    dpi: int
    raw: str


def parse_list2(text: str) -> list[LDInstance]:
    """Parse `ldconsole list2` output. Any malformed line raises LDConsoleError (inspect, don't guess)."""
    out: list[LDInstance] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        fields = line.split(",")
        if len(fields) < 2 + _NUMERIC_TAIL:
            raise LDConsoleError(f"Unexpected ldconsole list2 line {line_no}: {line!r}")
        try:
            index = int(fields[0])
            numbers = [int(x) for x in fields[-_NUMERIC_TAIL:]]
        except ValueError:
            raise LDConsoleError(f"Unexpected ldconsole list2 line {line_no}: {line!r}") from None
        name = ",".join(fields[1:-_NUMERIC_TAIL])
        out.append(LDInstance(index, name, *numbers, raw=line))
    indexes = [i.index for i in out]
    if len(set(indexes)) != len(indexes):
        raise LDConsoleError("ldconsole list2 returned duplicate instance indexes; refusing to continue.")
    return out


def parse_runninglist(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def decode_console_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("mbcs")  # Windows ANSI code page
        except (LookupError, UnicodeDecodeError):
            return data.decode("cp1252", errors="replace")


class LDConsole:
    def __init__(self, ldconsole_path: Path, runner: Runner, timeout_s: float, logger: Logger | None = None) -> None:
        self._path = Path(ldconsole_path)
        self._runner = runner
        self._timeout = timeout_s
        self._log = logger

    def run_readonly(self, *args: str) -> CommandResult:
        if not args or args[0] not in READ_ONLY_COMMANDS:
            raise SafetyViolation(
                f"ldconsole command {args[:1]!r} is not allowed. Only read-only commands "
                f"{sorted(READ_ONLY_COMMANDS)} are permitted in this build.")
        if not self._path.is_file():
            raise LDConsoleError(f"ldconsole.exe was not found at '{self._path}'. Check the LDPlayer folder in Settings.")
        result = self._runner.run([str(self._path), *args], timeout=self._timeout)
        if self._log:
            self._log.log("INFO" if result.ok else "ERROR", f"LDCONSOLE_{args[0].upper()}",
                          "OK" if result.ok else "FAILED", duration_ms=result.duration_ms,
                          error=None if result.ok else (result.error or ("timeout" if result.timed_out else f"exit {result.returncode}")))
        if result.timed_out:
            raise LDConsoleError(f"ldconsole '{args[0]}' did not answer within {self._timeout:g} s.")
        if result.cancelled:
            raise LDConsoleError(f"ldconsole '{args[0]}' was cancelled.")
        if not result.ok:
            raise LDConsoleError(f"ldconsole '{args[0]}' failed: {result.error or result.stderr_text.strip() or result.returncode}")
        return result

    def list_instances(self) -> list[LDInstance]:
        return parse_list2(decode_console_output(self.run_readonly("list2").stdout))

    def running_names(self) -> list[str]:
        return parse_runninglist(decode_console_output(self.run_readonly("runninglist").stdout))
