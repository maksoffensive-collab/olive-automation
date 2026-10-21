"""Descriptive analysis of a parsed Macro.

Reports what the file CONTAINS. It does not interpret coordinates, touch states or
timing semantics; those are unresolved (docs/MACRO_FORMAT.md, hypotheses H1/H2).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import schema
from .model import Macro, Operation, RecordInfo


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class TimelineStats:
    label: str
    operations: int = 0
    first_timing: Any = None
    last_timing: Any = None
    span: Any = None
    min_gap: Any = None
    max_gap: Any = None
    decreasing_steps: int = 0
    negative_timings: int = 0
    non_numeric_timings: int = 0
    record_info_circle_duration: Any = None
    record_info_resolution: tuple[Any, Any] = (None, None)


@dataclass
class MacroReport:
    path: str | None
    size_bytes: int | None
    sha256: str | None
    fmt: dict[str, Any]
    top_level_keys: list[str]
    unknown_top_level_keys: list[str]
    record_info_keys_missing: list[str]
    record_info_keys_unknown: list[str]
    record_info: dict[str, Any] | None
    merged_entries: int
    unknown_merged_entry_keys: list[str]
    timelines: list[TimelineStats]
    total_operations: int
    operation_ids: Counter
    unknown_operation_ids: list[str]
    unknown_operation_keys: dict[str, list[str]]
    total_points: int
    ops_with_points: int
    ops_with_empty_points: int
    max_points_per_op: int
    point_states: Counter
    point_ids: Counter
    unknown_point_keys: list[str]
    x_range: tuple[Any, Any]
    y_range: tuple[Any, Any]
    points_outside_recorded_resolution: int
    points_checked_against_resolution: int
    issues: list[str] = field(default_factory=list)


def _timeline(label: str, ops: list[Operation], info: RecordInfo | None) -> TimelineStats:
    t = TimelineStats(label=label, operations=len(ops))
    timings = []
    for op in ops:
        if _num(op.timing):
            timings.append(op.timing)
            if op.timing < 0:
                t.negative_timings += 1
        else:
            t.non_numeric_timings += 1
    if timings:
        t.first_timing, t.last_timing = timings[0], timings[-1]
        t.span = timings[-1] - timings[0]
        gaps = [b - a for a, b in zip(timings, timings[1:])]
        t.decreasing_steps = sum(1 for g in gaps if g < 0)
        if gaps:
            t.min_gap, t.max_gap = min(gaps), max(gaps)
    if info is not None:
        t.record_info_circle_duration = info.get("circleDuration")
        t.record_info_resolution = info.resolution
    return t


def analyze(macro: Macro) -> MacroReport:
    timelines: list[TimelineStats] = []
    all_ops: list[tuple[Operation, RecordInfo | None]] = []
    if macro.operations is not None:
        timelines.append(_timeline("main", macro.operations, macro.record_info))
        all_ops += [(o, macro.record_info) for o in macro.operations]
    unknown_entry_keys: set[str] = set()
    for i, entry in enumerate(macro.merged_records or []):
        ops = entry.operations or []
        timelines.append(_timeline(f"merged[{i}]", ops, entry.record_info))
        all_ops += [(o, entry.record_info) for o in ops]
        unknown_entry_keys |= set(entry.extras)

    ids: Counter = Counter()
    unknown_op_keys: dict[str, set[str]] = {}
    unknown_ids: set[str] = set()
    states: Counter = Counter()
    point_ids: Counter = Counter()
    unknown_point_keys: set[str] = set()
    xs: list[Any] = []
    ys: list[Any] = []
    total_points = ops_with_points = ops_empty = max_pts = outside = checked = 0

    for op, info in all_ops:
        oid = op.operation_id if isinstance(op.operation_id, str) else repr(op.operation_id)
        ids[oid] += 1
        known_params = schema.KNOWN_OPERATION_PARAMS.get(oid)
        if known_params is None:
            unknown_ids.add(oid)
        else:
            extra = set(op.params) - known_params
            if extra:
                unknown_op_keys.setdefault(oid, set()).update(extra)
        if op.points is None:
            continue
        if op.points:
            ops_with_points += 1
        else:
            ops_empty += 1
        max_pts = max(max_pts, len(op.points))
        res_w, res_h = info.resolution if info is not None else (None, None)
        res_ok = _num(res_w) and _num(res_h) and res_w > 0 and res_h > 0
        for p in op.points:
            total_points += 1
            states[schema.hashable_key(p.state)] += 1
            point_ids[schema.hashable_key(p.id)] += 1
            unknown_point_keys |= set(p.extras)
            if _num(p.x):
                xs.append(p.x)
            if _num(p.y):
                ys.append(p.y)
            if res_ok and _num(p.x) and _num(p.y):
                checked += 1
                if not (0 <= p.x <= res_w and 0 <= p.y <= res_h):
                    outside += 1

    info_keys = set(macro.record_info.values) if macro.record_info is not None else set()
    return MacroReport(
        path=macro.source_path,
        size_bytes=macro.source_size,
        sha256=macro.source_sha256,
        fmt={
            "encoding": macro.fmt.encoding, "bom": macro.fmt.bom,
            "newline": "CRLF" if macro.fmt.newline == "\r\n" else "LF",
            "indent": macro.fmt.indent, "trailing_newline": macro.fmt.trailing_newline,
            "ensure_ascii": macro.fmt.ensure_ascii,
        },
        top_level_keys=list(macro.key_order),
        unknown_top_level_keys=sorted(set(macro.extras)),
        record_info_keys_missing=sorted(schema.KNOWN_RECORD_INFO_KEYS - info_keys) if macro.record_info else [],
        record_info_keys_unknown=sorted(info_keys - schema.KNOWN_RECORD_INFO_KEYS),
        record_info=dict(macro.record_info.values) if macro.record_info is not None else None,
        merged_entries=len(macro.merged_records or []),
        unknown_merged_entry_keys=sorted(unknown_entry_keys),
        timelines=timelines,
        total_operations=len(all_ops),
        operation_ids=ids,
        unknown_operation_ids=sorted(unknown_ids),
        unknown_operation_keys={k: sorted(v) for k, v in unknown_op_keys.items()},
        total_points=total_points,
        ops_with_points=ops_with_points,
        ops_with_empty_points=ops_empty,
        max_points_per_op=max_pts,
        point_states=states,
        point_ids=point_ids,
        unknown_point_keys=sorted(unknown_point_keys),
        x_range=(min(xs), max(xs)) if xs else (None, None),
        y_range=(min(ys), max(ys)) if ys else (None, None),
        points_outside_recorded_resolution=outside,
        points_checked_against_resolution=checked,
        issues=[f"{i.severity.upper()} {i.path}: {i.message}" for i in macro.issues],
    )


def format_report(r: MacroReport) -> str:
    """Human-readable report. Facts only; no interpretation of coordinates or states."""
    lines = [
        f"File: {r.path}",
        f"Size: {r.size_bytes} bytes   SHA-256: {r.sha256}",
        "Format: " + ", ".join(f"{k}={v}" for k, v in r.fmt.items()),
        f"Top-level keys (source order): {r.top_level_keys}",
        f"Unknown top-level keys: {r.unknown_top_level_keys or 'none'}",
    ]
    if r.record_info is not None:
        ri = r.record_info
        lines.append(
            "recordInfo: resolution={}x{} loopType={} loopTimes={} circleDuration={} createTime={}".format(
                ri.get("resolutionWidth"), ri.get("resolutionHeight"), ri.get("loopType"),
                ri.get("loopTimes"), ri.get("circleDuration"), ri.get("createTime")))
        lines.append(f"  missing known keys: {r.record_info_keys_missing or 'none'}; "
                     f"unknown keys: {r.record_info_keys_unknown or 'none'}")
    else:
        lines.append("recordInfo: absent")
    if r.merged_entries:
        lines.append(f"mergedRecords: {r.merged_entries} entries; unknown entry keys: "
                     f"{r.unknown_merged_entry_keys or 'none'}")
    lines.append(f"Operations: {r.total_operations}   ids: {dict(r.operation_ids)}")
    lines.append(f"  unknown operation ids: {r.unknown_operation_ids or 'none'}; "
                 f"unknown operation keys: {r.unknown_operation_keys or 'none'}")
    main = r.timelines[0] if r.timelines else None
    if main is not None and len(r.timelines) == 1:
        lines.append(
            f"Timing: first={main.first_timing} last={main.last_timing} span={main.span} "
            f"min_gap={main.min_gap} max_gap={main.max_gap} decreasing_steps={main.decreasing_steps} "
            f"negative={main.negative_timings} non_numeric={main.non_numeric_timings} "
            f"(recordInfo.circleDuration={main.record_info_circle_duration})")
    elif r.timelines:
        bad = [t.label for t in r.timelines if t.decreasing_steps or t.negative_timings or t.non_numeric_timings]
        spans = [t.span for t in r.timelines if t.span is not None]
        lines.append(f"Timing: {len(r.timelines)} timelines; spans min={min(spans, default=None)} "
                     f"max={max(spans, default=None)}; timelines with timing anomalies: {bad or 'none'}")
    lines.append(
        f"Points: total={r.total_points} ops_with_points={r.ops_with_points} "
        f"ops_with_empty_points={r.ops_with_empty_points} max_points_per_op={r.max_points_per_op}")
    lines.append(f"  states={dict(r.point_states)} pointer_ids={dict(r.point_ids)} "
                 f"unknown point keys: {r.unknown_point_keys or 'none'}")
    lines.append(f"  x range={r.x_range} y range={r.y_range}; outside recorded resolution: "
                 f"{r.points_outside_recorded_resolution} of {r.points_checked_against_resolution} checked "
                 f"(resolution 0x0 or missing is skipped)")
    lines.append("Parse issues: " + ("; ".join(r.issues) if r.issues else "none"))
    return "\n".join(lines)
