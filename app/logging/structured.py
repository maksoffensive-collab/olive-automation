"""Structured logging.

Every event has: timestamp, level, component, instance, target, operation, result,
duration_ms, error, recovery_action (+ free-form `details`). `target` is an opaque, provider-specific
address label (for example a device address or window handle); nothing here interprets it. Events go to sinks (memory
ring buffer for the GUI, rotating JSON-lines file, console). Secrets and device
identifiers are redacted before any sink sees an event.

This module does not use the standard-library `logging` package; it is independent of it.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol, TextIO

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential|imei|imsi|"
    r"android[_-]?id|mac[_-]?address|sim[_-]?serial|phone(imei|imsi|androidid|simserial))", re.I)
_LONG_DIGITS = re.compile(r"(?<!\d)\d{15,}(?!\d)")
REDACTED = "[REDACTED]"


def redact(value: Any, key: str | None = None) -> Any:
    """Return a copy of `value` with secrets and device identifiers masked."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _LONG_DIGITS.sub(REDACTED, value)
    return value


@dataclass
class LogEvent:
    timestamp: str
    level: str
    component: str
    operation: str
    instance: str | None = None
    target: str | None = None
    result: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    recovery_action: str | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def level_no(self) -> int:
        return LEVELS[self.level]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)


class Sink(Protocol):
    def emit(self, event: LogEvent) -> None: ...


class MemorySink:
    """Bounded in-memory buffer; the GUI's log view reads from it."""

    def __init__(self, max_events: int = 5000) -> None:
        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def emit(self, event: LogEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(self, *, min_level: str = "DEBUG", component: str | None = None, instance: str | None = None,
              operation: str | None = None, since: str | None = None, limit: int | None = None) -> list[LogEvent]:
        floor = LEVELS[min_level]
        with self._lock:
            events = list(self._events)
        out = [e for e in events
               if e.level_no >= floor
               and (component is None or e.component == component)
               and (instance is None or e.instance == instance)
               and (operation is None or e.operation == operation)
               and (since is None or e.timestamp >= since)]
        return out[-limit:] if limit else out


class JsonlFileSink:
    """Append-only JSON-lines file with size-based rotation (keeps `backups` old files)."""

    def __init__(self, path: Path, max_bytes: int = 5_000_000, backups: int = 3) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate(self) -> None:
        for i in range(self._backups, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{i - 1}") if i > 1 else self._path
            dst = self._path.with_name(f"{self._path.name}.{i}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)

    def emit(self, event: LogEvent) -> None:
        line = event.to_json() + "\n"
        with self._lock:
            if self._path.exists() and self._path.stat().st_size + len(line.encode("utf-8")) > self._max_bytes:
                self._rotate()
            with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)


class ConsoleSink:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()               # one write() at a time: several targets can log concurrently

    def emit(self, event: LogEvent) -> None:
        stream = self._stream or sys.stderr
        parts = [f"[{event.level}]", event.component]
        if event.instance:
            parts.append(f"instance={event.instance}")
        if event.target:
            parts.append(f"target={event.target}")
        parts.append(f"op={event.operation}")
        if event.result:
            parts.append(f"result={event.result}")
        if event.duration_ms is not None:
            parts.append(f"{event.duration_ms}ms")
        if event.error:
            parts.append(f"error={event.error}")
        if event.message:
            parts.append(event.message)
        with self._lock:
            stream.write(" ".join(parts) + "\n")


class LogHub:
    """Shared configuration for all loggers: minimum level and the list of sinks."""

    def __init__(self, sinks: list[Sink] | None = None, min_level: str = "INFO") -> None:
        self.sinks: list[Sink] = list(sinks or [])
        self.min_level = min_level

    def logger(self, component: str) -> "Logger":
        return Logger(self, component)


class Logger:
    def __init__(self, hub: LogHub, component: str, instance: str | None = None,
                 target: str | None = None) -> None:
        self._hub = hub
        self.component = component
        self.instance = instance
        self.target = target

    def bind(self, *, instance: str | None = None, target: str | None = None) -> "Logger":
        return Logger(self._hub, self.component, instance or self.instance, target or self.target)

    def log(self, level: str, operation: str, result: str | None = None, *, duration_ms: int | None = None,
            error: str | None = None, recovery_action: str | None = None, message: str | None = None,
            **details: Any) -> LogEvent | None:
        if LEVELS[level] < LEVELS[self._hub.min_level]:
            return None
        event = LogEvent(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            level=level, component=self.component, operation=operation,
            instance=self.instance, target=self.target, result=result,
            duration_ms=duration_ms, error=redact(error), recovery_action=recovery_action,
            message=redact(message), details=redact(details),
        )
        for sink in self._hub.sinks:
            try:
                sink.emit(event)
            except Exception:  # a broken sink must never break the automation
                pass
        return event

    def debug(self, operation: str, **kw: Any) -> None:
        self.log("DEBUG", operation, **kw)

    def info(self, operation: str, **kw: Any) -> None:
        self.log("INFO", operation, **kw)

    def warning(self, operation: str, **kw: Any) -> None:
        self.log("WARNING", operation, **kw)

    def error(self, operation: str, **kw: Any) -> None:
        self.log("ERROR", operation, **kw)

    def critical(self, operation: str, **kw: Any) -> None:
        self.log("CRITICAL", operation, **kw)

    @contextmanager
    def timed(self, operation: str, **details: Any) -> Iterator[dict[str, Any]]:
        """Log an INFO event with the duration; on an exception log ERROR and re-raise.

        The body may set `ctx["result"]` to override the default "OK".
        """
        ctx: dict[str, Any] = {}
        started = time.monotonic()
        try:
            yield ctx
        except BaseException as exc:
            self.log("ERROR", operation, "FAILED", duration_ms=int((time.monotonic() - started) * 1000),
                     error=f"{type(exc).__name__}: {exc}", **details)
            raise
        self.log("INFO", operation, ctx.get("result", "OK"),
                 duration_ms=int((time.monotonic() - started) * 1000), **details)
