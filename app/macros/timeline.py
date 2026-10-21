"""Timing and merge engine. Every function returns a NEW Macro; inputs are never mutated.

Timing is never changed silently: offsets, gaps and speed factors are explicit arguments,
and results that would contain a negative or out-of-order timing are refused.

Scope: plain macros (top-level `operations`). Files that use `mergedRecords` (LDPlayer's own
merge format, each entry with its own timeline) are refused, because how LDPlayer joins
those entries is [NEEDS TESTING]. `recordInfo` is copied from the first macro and
`circleDuration` is NOT recomputed (its meaning is unresolved). Whether LDPlayer itself plays
a merged result correctly is [NEEDS TESTING]: nothing here plays anything.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.core.errors import PlatformError

from .model import Macro, Operation


class MergeError(PlatformError):
    pass


def _plain(macro: Macro, name: str) -> list[Operation]:
    if macro.merged_records is not None:
        raise MergeError(f"{name} uses LDPlayer's 'mergedRecords' format; merging those files is not supported yet.")
    if macro.operations is None:
        raise MergeError(f"{name} has no operations.")
    for i, op in enumerate(macro.operations):
        if isinstance(op.timing, bool) or not isinstance(op.timing, (int, float)):
            raise MergeError(f"{name}: operation {i} has no numeric timing.")
    return macro.operations


def span_ms(macro: Macro) -> float:
    """Timing of the last operation (0 for an empty macro)."""
    ops = _plain(macro, "macro")
    return ops[-1].timing if ops else 0


def _copy_ops(ops: list[Operation], offset: float = 0) -> list[Operation]:
    out = copy.deepcopy(ops)
    if offset:
        for op in out:
            op.timing = op.timing + offset
    return out


def _with_ops(template: Macro, ops: list[Operation]) -> Macro:
    result = copy.deepcopy(template)
    result.operations = ops
    result.source_path = None
    result.source_sha256 = None
    result.source_size = None
    result.issues = []
    return result


def _check(ops: list[Operation], what: str) -> None:
    prev = None
    for i, op in enumerate(ops):
        if op.timing < 0:
            raise MergeError(f"{what} would give operation {i} a negative timing ({op.timing}).")
        if prev is not None and op.timing < prev:
            raise MergeError(f"{what} would put operation {i} out of order ({op.timing} < {prev}).")
        prev = op.timing


def _same_resolution(a: Macro, b: Macro) -> None:
    ra = a.record_info.resolution if a.record_info else (None, None)
    rb = b.record_info.resolution if b.record_info else (None, None)
    if ra != rb:
        raise MergeError(f"Recorded resolutions differ ({ra[0]}x{ra[1]} vs {rb[0]}x{rb[1]}); refusing to merge.")


def offset(macro: Macro, ms: float) -> Macro:
    """Shift every operation by `ms` (may be negative as long as no timing goes below 0)."""
    ops = _copy_ops(_plain(macro, "macro"), ms)
    _check(ops, f"Shifting by {ms} ms")
    return _with_ops(macro, ops)


def append(a: Macro, b: Macro, gap_ms: float = 0) -> Macro:
    """B starts `gap_ms` after A's last operation: A 0..5000 + B 0..1000 -> B occupies 5000..6000."""
    if gap_ms < 0:
        raise MergeError("gap_ms must not be negative.")
    ops_a, ops_b = _plain(a, "first macro"), _plain(b, "second macro")
    _same_resolution(a, b)
    start = (ops_a[-1].timing if ops_a else 0) + gap_ms
    # B is shifted as one block, so its own relative timing (including any lead-in before its
    # first operation) is preserved exactly.
    merged = _copy_ops(ops_a) + _copy_ops(ops_b, start)
    _check(merged, "Appending")
    return _with_ops(a, merged)


def insert_at(a: Macro, b: Macro, at_ms: float) -> Macro:
    """Interleave B into A so that B's time 0 falls at `at_ms` of A. Equal timings keep A first."""
    if at_ms < 0:
        raise MergeError("at_ms must not be negative.")
    ops_a, ops_b = _plain(a, "first macro"), _plain(b, "second macro")
    _same_resolution(a, b)
    merged = _copy_ops(ops_a) + _copy_ops(ops_b, at_ms)
    merged.sort(key=lambda op: op.timing)  # stable: A's operations stay before B's at equal timing
    _check(merged, "Inserting")
    return _with_ops(a, merged)


def repeat(a: Macro, times: int, gap_ms: float = 0) -> Macro:
    if times < 1:
        raise MergeError("times must be at least 1.")
    if gap_ms < 0:
        raise MergeError("gap_ms must not be negative.")
    ops = _plain(a, "macro")
    result = _with_ops(a, _copy_ops(ops))
    for _ in range(times - 1):
        result = append(result, _with_ops(a, _copy_ops(ops)), gap_ms)
    return result


def scale_speed(macro: Macro, factor: float) -> Macro:
    """Explicit speed change: factor 2 plays twice as fast (timings halved, rounded to whole ms)."""
    if not factor > 0:
        raise MergeError("factor must be greater than 0.")
    ops = _copy_ops(_plain(macro, "macro"))
    for op in ops:
        op.timing = round(op.timing / factor)
    _check(ops, f"Scaling by {factor}")
    return _with_ops(macro, ops)


@dataclass(frozen=True)
class MergePreview:
    operations: int
    first_ms: float | None
    last_ms: float | None
    span_ms: float
    detail: str


def preview(macro: Macro, label: str = "result") -> MergePreview:
    ops = _plain(macro, label)
    first, last = (ops[0].timing, ops[-1].timing) if ops else (None, None)
    return MergePreview(len(ops), first, last, last - first if ops else 0,
                        f"{label}: {len(ops)} operations, {first}..{last} ms")
