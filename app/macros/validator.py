"""Macro validator: ERROR / WARNING / INFO findings. Pure, no I/O.

What is checked is structural and timing-related. Touch pairing relies on hypothesis H1
(state 1 = finger down/moving, state 0 = finger up; docs/MACRO_FORMAT.md), which is
[NEEDS TESTING]; findings that depend on it say so. Coordinates are NOT checked against a
screen size because the coordinate space is unresolved (hypothesis H2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import schema
from .model import Macro, Operation

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity} [{self.code}] {self.path}: {self.message}"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_timeline(ops: list[Operation], path: str, min_gap_ms: float, out: list[Finding]) -> None:
    prev: float | None = None
    open_pointers: dict[Any, int] = {}
    for i, op in enumerate(ops):
        here = f"{path}[{i}]"
        if not _num(op.timing):
            out.append(Finding(ERROR, "timing_invalid", here, f"timing is missing or not a number ({op.timing!r})"))
        else:
            if op.timing < 0:
                out.append(Finding(ERROR, "timing_negative", here, f"negative timing {op.timing}"))
            if prev is not None:
                if op.timing < prev:
                    out.append(Finding(ERROR, "timing_out_of_order", here, f"timing {op.timing} is before the previous {prev}"))
                elif op.timing - prev < min_gap_ms:
                    out.append(Finding(WARNING, "gap_very_short", here, f"only {op.timing - prev} ms after the previous operation"))
            prev = op.timing
        if not isinstance(op.operation_id, str) or not op.operation_id:
            out.append(Finding(ERROR, "operation_id_invalid", here, "operationId is missing or not text"))
        for j, p in enumerate(op.points or []):
            pp = f"{here}.points[{j}]"
            if p.state not in (0, 1) or isinstance(p.state, bool):
                out.append(Finding(WARNING, "state_unknown", pp, f"touch state {p.state!r} is not 0 or 1"))
            for axis, val in (("x", p.x), ("y", p.y)):
                if not _num(val):
                    out.append(Finding(ERROR, "coordinate_invalid", pp, f"{axis} is missing or not a number ({val!r})"))
                elif val < 0:
                    out.append(Finding(WARNING, "coordinate_negative", pp, f"{axis} is negative ({val})"))
            if schema.hashable_key(p.id) is not p.id:
                out.append(Finding(ERROR, "point_id_invalid", pp, f"pointer id must be a number or text ({p.id!r})"))
            elif p.state == 1:
                open_pointers[p.id] = i
            elif p.state == 0:
                if p.id not in open_pointers:
                    out.append(Finding(WARNING, "touch_up_without_down", pp,
                                       f"pointer {p.id} lifted without a preceding state 1 [depends on hypothesis H1]"))
                open_pointers.pop(p.id, None)
        if op.points is not None and len(op.points) > 1:
            out.append(Finding(INFO, "multitouch", here, f"{len(op.points)} points in one operation"))
    for pid, idx in open_pointers.items():
        out.append(Finding(ERROR, "missing_touch_up", f"{path}[{idx}]",
                           f"pointer {pid} is never lifted (no final state 0) [depends on hypothesis H1]"))


def validate(macro: Macro, *, min_gap_ms: float = 0) -> list[Finding]:
    """`min_gap_ms=0` (default) disables the short-gap warning: the 40 real recordings contain
    3,261 zero-millisecond gaps, so warning on them would only be noise. Raise it to opt in."""
    out: list[Finding] = [Finding(ERROR if i.severity == "error" else WARNING if i.severity == "warning" else INFO,
                                  "parse_issue", i.path, i.message) for i in macro.issues]
    if macro.operations is None and macro.merged_records is None:
        out.append(Finding(ERROR, "no_operations", "$", "the file has neither operations nor mergedRecords"))
    if macro.operations is not None:
        _validate_timeline(macro.operations, "$.operations", min_gap_ms, out)
    if macro.merged_records is not None:
        out.append(Finding(INFO, "merged_file", "$.mergedRecords", f"{len(macro.merged_records)} merged entries"))
        for i, entry in enumerate(macro.merged_records):
            if entry.operations is None:
                out.append(Finding(ERROR, "entry_no_operations", f"$.mergedRecords[{i}]", "entry has no operations"))
            else:
                _validate_timeline(entry.operations, f"$.mergedRecords[{i}].operations", min_gap_ms, out)
    if macro.record_info is None:
        out.append(Finding(WARNING, "no_record_info", "$", "recordInfo is missing"))
    else:
        w, h = macro.record_info.resolution
        if w in (0, None) or h in (0, None):
            out.append(Finding(INFO, "resolution_unset", "$.recordInfo", f"recorded resolution is {w}x{h}"))
    if macro.extras:
        out.append(Finding(INFO, "unknown_top_level", "$", f"unknown top-level keys preserved: {sorted(macro.extras)}"))
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)
