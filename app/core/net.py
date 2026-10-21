"""Read-only network facts: which process owns which listening TCP port.

Provider verification code can use it as independent evidence (process-to-port ownership).
It runs `netstat -ano -p tcp` through the shared CommandRunner (timeout, cancellation).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import PlatformError
from .process import Runner

_LINE = re.compile(r"^\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.I)


class NetstatError(PlatformError):
    pass


def parse_netstat_listeners(text: str) -> dict[int, set[int]]:
    """{port: {owning pids}} for LISTENING TCP sockets."""
    out: dict[int, set[int]] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            out.setdefault(int(m.group(2)), set()).add(int(m.group(3)))
    return out


def listening_port_owners(runner: Runner, timeout_s: float = 15.0) -> dict[int, set[int]]:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "NETSTAT.EXE"
    result = runner.run([str(exe), "-ano", "-p", "tcp"], timeout=timeout_s)
    if not result.ok:
        raise NetstatError(f"Could not read the list of open ports: {result.error or result.stderr_text.strip() or 'timeout'}")
    return parse_netstat_listeners(result.stdout_text)
