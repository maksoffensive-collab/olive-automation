"""Serialize a Macro back to .record bytes, in memory only.

This module never touches the filesystem. Writing a macro to disk is a separate,
deliberately absent step until a safe "write new file, never overwrite" helper exists.

CONTRACT: a canonical SEMANTIC round trip. parse -> serialize -> parse preserves every supported value, its
type (int / float / bool / null / string), unknown keys and the list order. Byte-exact reproduction of the
source is NOT guaranteed: number spelling (`1.50` -> `1.5`, `1E3` -> `1000.0`), whitespace, indentation, empty-list
layout, escapes, trailing newlines and mixed line endings are normalized. The text format is the source's own
detected one (newline, indent, trailing newline, BOM), which happens to reproduce LDPlayer's own files exactly, but
that is an observation, not a promise. Key order is semantically irrelevant; it is kept deterministically from the
source (canonical schema order for keys added in code). The output is a fixed point (serializing what was parsed
from it gives the same bytes). Non-finite numbers are refused (`MacroFormatError`), never written as
`Infinity`/`NaN`.
"""
from __future__ import annotations

import json
from typing import Any

from .model import Macro, MacroFormatError, MergedEntry, Operation, Point, RecordInfo


def _ordered(key_order: tuple[str, ...], known: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in key_order:
        if key in known:
            out[key] = known[key]
        elif key in extras:
            out[key] = extras[key]
    for key, value in known.items():
        out.setdefault(key, value)
    for key, value in extras.items():
        out.setdefault(key, value)
    return out


def _present(known: dict[str, Any], key: str, value: Any, key_order: tuple[str, ...]) -> None:
    if value is not None or key in key_order:
        known[key] = value


def _point(p: Point) -> dict[str, Any]:
    known: dict[str, Any] = {}
    for key, value in (("id", p.id), ("x", p.x), ("y", p.y), ("state", p.state)):
        _present(known, key, value, p.key_order)
    return _ordered(p.key_order, known, p.extras)


def _operation(op: Operation) -> dict[str, Any]:
    known: dict[str, Any] = {}
    _present(known, "operationId", op.operation_id, op.key_order)
    _present(known, "timing", op.timing, op.key_order)
    if op.points is not None:
        known["points"] = [_point(p) for p in op.points]
    return _ordered(op.key_order, known, op.params)


def _record_info(info: RecordInfo) -> dict[str, Any]:
    return dict(info.values)


def _merged_entry(entry: MergedEntry) -> dict[str, Any]:
    known: dict[str, Any] = {}
    if entry.operations is not None:
        known["operations"] = [_operation(o) for o in entry.operations]
    if entry.record_info is not None:
        known["recordInfo"] = _record_info(entry.record_info)
    return _ordered(entry.key_order, known, entry.extras)


def to_jsonable(macro: Macro) -> dict[str, Any]:
    """The macro as plain dict/list data, in source key order."""
    known: dict[str, Any] = {}
    if macro.operations is not None:
        known["operations"] = [_operation(o) for o in macro.operations]
    if macro.record_info is not None:
        known["recordInfo"] = _record_info(macro.record_info)
    if macro.merged_records is not None:
        known["mergedRecords"] = [_merged_entry(e) for e in macro.merged_records]
    return _ordered(macro.key_order, known, macro.extras)


def _render(macro: Macro, ensure_ascii: bool) -> str:
    fmt = macro.fmt
    try:
        if fmt.indent is None:
            text = json.dumps(to_jsonable(macro), ensure_ascii=ensure_ascii, separators=(",", ":"), allow_nan=False)
        else:
            text = json.dumps(to_jsonable(macro), indent=fmt.indent, ensure_ascii=ensure_ascii, allow_nan=False)
    except ValueError as exc:                    # NaN / Infinity (allow_nan=False) or another unrepresentable value
        raise MacroFormatError(f"the macro cannot be written as JSON: {exc}") from exc
    text = text.replace("\n", fmt.newline)
    return text + fmt.newline if fmt.trailing_newline else text


def serialize(macro: Macro) -> bytes:
    fmt = macro.fmt
    try:
        data = _render(macro, fmt.ensure_ascii).encode(fmt.encoding)
    except UnicodeEncodeError:                   # e.g. a lone-surrogate escape next to literal text: escape it
        data = _render(macro, True).encode(fmt.encoding)
    return b"\xef\xbb\xbf" + data if fmt.bom else data
