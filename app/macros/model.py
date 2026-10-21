"""Internal representation of an LDPlayer .record macro.

Design goal: LOSSLESS. Every key, its order, its value and the file's text format
(newline style, indent, trailing newline, BOM) are kept so a parse -> serialize round
trip reproduces the source. Fields the application does not understand live in
`extras` / `params` and are never dropped.

Presence rule: a key is present in the output if it was present in the source
(recorded in `key_order`) or if code set a non-None value on a new object.
Nothing here interprets coordinates or touch states; see docs/MACRO_FORMAT.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import schema


class MacroFormatError(ValueError):
    """The file is not a structure this parser understands. INSPECT, do not guess."""


@dataclass
class ParseIssue:
    severity: str  # "error" | "warning" | "info"
    path: str
    message: str


@dataclass
class FileFormat:
    """Text-level format of the source file. Defaults match the observed LDPlayer files."""
    encoding: str = "utf-8"
    bom: bool = False
    newline: str = "\r\n"
    trailing_newline: bool = False
    indent: int | str | None = 4
    ensure_ascii: bool = False


@dataclass
class Point:
    id: Any = None
    x: Any = None
    y: Any = None
    state: Any = None
    extras: dict[str, Any] = field(default_factory=dict)
    key_order: tuple[str, ...] = schema.CANONICAL_POINT_ORDER


@dataclass
class Operation:
    operation_id: Any = None
    timing: Any = None
    points: list[Point] | None = None  # None = key absent
    params: dict[str, Any] = field(default_factory=dict)  # every other key (text, app, unknown)
    key_order: tuple[str, ...] = schema.CANONICAL_OPERATION_ORDER


@dataclass
class RecordInfo:
    values: dict[str, Any] = field(default_factory=dict)  # all keys, source order

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    @property
    def resolution(self) -> tuple[Any, Any]:
        return self.values.get("resolutionWidth"), self.values.get("resolutionHeight")


@dataclass
class MergedEntry:
    operations: list[Operation] | None = None
    record_info: RecordInfo | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    key_order: tuple[str, ...] = schema.CANONICAL_MERGED_ENTRY_ORDER


@dataclass
class Macro:
    operations: list[Operation] | None = None  # None = key absent (merged files)
    record_info: RecordInfo | None = None
    merged_records: list[MergedEntry] | None = None
    extras: dict[str, Any] = field(default_factory=dict)  # unknown top-level keys
    key_order: tuple[str, ...] = schema.CANONICAL_TOP_ORDER
    fmt: FileFormat = field(default_factory=FileFormat)
    source_path: str | None = None
    source_sha256: str | None = None
    source_size: int | None = None
    issues: list[ParseIssue] = field(default_factory=list)
