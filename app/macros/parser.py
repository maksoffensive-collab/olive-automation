"""Read-only .record parser.

`parse_file` opens the source in binary READ mode only. Nothing in this module can
write, rename or delete a file. Anything structurally unexpected raises
MacroFormatError (stop and inspect) or is recorded as a ParseIssue; unknown keys are
preserved.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .model import (
    FileFormat, Macro, MacroFormatError, MergedEntry, Operation, ParseIssue, Point, RecordInfo,
)

_OP_STRUCT_KEYS = ("operationId", "timing", "points")
_POINT_KEYS = ("id", "x", "y", "state")


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MacroFormatError(f"duplicate JSON key {key!r}; cannot be represented losslessly")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise MacroFormatError(f"non-standard JSON constant {name!r} is not supported")


def _finite_float(text: str) -> float:
    """A number that overflows to infinity (`1e400`) is as unusable as the literal `Infinity`: refuse it."""
    value = float(text)
    if not math.isfinite(value):
        raise MacroFormatError(f"number {text!r} is not finite; non-finite numbers are not supported")
    return value


def _detect_format(text: str, bom: bool, issues: list[ParseIssue]) -> FileFormat:
    has_crlf = "\r\n" in text
    bare_lf = "\n" in text.replace("\r\n", "")
    if has_crlf and bare_lf:
        issues.append(ParseIssue("warning", "$", "mixed CRLF and LF line endings; CRLF assumed on write"))
    newline = "\r\n" if has_crlf else "\n" if "\n" in text else "\r\n"

    indent: int | str | None = None
    for line in text.replace("\r\n", "\n").split("\n")[1:]:
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) != len(line):
            lead = line[: len(line) - len(stripped)]
            indent = "\t" if lead[0] == "\t" else len(lead)
            break
    if indent is None and "\n" not in text:
        issues.append(ParseIssue("info", "$", "single-line (compact) JSON; separators guessed on write"))
    return FileFormat(
        encoding="utf-8",
        bom=bom,
        newline=newline,
        trailing_newline=text.endswith("\n"),
        indent=indent,
        ensure_ascii=("\\u" in text and all(ord(c) < 128 for c in text)),
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _build_point(raw: Any, path: str) -> Point:
    if not isinstance(raw, dict):
        raise MacroFormatError(f"{path}: point is not an object")
    return Point(
        id=raw.get("id"), x=raw.get("x"), y=raw.get("y"), state=raw.get("state"),
        extras={k: v for k, v in raw.items() if k not in _POINT_KEYS},
        key_order=tuple(raw.keys()),
    )


def _build_operation(raw: Any, path: str, issues: list[ParseIssue]) -> Operation:
    if not isinstance(raw, dict):
        raise MacroFormatError(f"{path}: operation is not an object")
    if "operationId" not in raw:
        issues.append(ParseIssue("error", path, "operation has no operationId"))
    elif not isinstance(raw["operationId"], str):
        issues.append(ParseIssue("warning", path, "operationId is not a string"))
    if "timing" not in raw:
        issues.append(ParseIssue("error", path, "operation has no timing"))
    elif not _is_number(raw["timing"]):
        issues.append(ParseIssue("warning", path, f"timing is not a number: {raw['timing']!r}"))

    points: list[Point] | None = None
    if "points" in raw:
        raw_points = raw["points"]
        if not isinstance(raw_points, list):
            raise MacroFormatError(f"{path}.points: expected a list")
        points = [_build_point(p, f"{path}.points[{i}]") for i, p in enumerate(raw_points)]
    return Operation(
        operation_id=raw.get("operationId"),
        timing=raw.get("timing"),
        points=points,
        params={k: v for k, v in raw.items() if k not in _OP_STRUCT_KEYS},
        key_order=tuple(raw.keys()),
    )


def _build_operations(raw: Any, path: str, issues: list[ParseIssue]) -> list[Operation]:
    if not isinstance(raw, list):
        raise MacroFormatError(f"{path}: expected a list of operations")
    return [_build_operation(o, f"{path}[{i}]", issues) for i, o in enumerate(raw)]


def _build_record_info(raw: Any, path: str) -> RecordInfo:
    if not isinstance(raw, dict):
        raise MacroFormatError(f"{path}: recordInfo is not an object")
    return RecordInfo(values=dict(raw))


def _build_merged_entry(raw: Any, path: str, issues: list[ParseIssue]) -> MergedEntry:
    if not isinstance(raw, dict):
        raise MacroFormatError(f"{path}: merged record entry is not an object")
    return MergedEntry(
        operations=_build_operations(raw["operations"], f"{path}.operations", issues) if "operations" in raw else None,
        record_info=_build_record_info(raw["recordInfo"], f"{path}.recordInfo") if "recordInfo" in raw else None,
        extras={k: v for k, v in raw.items() if k not in ("operations", "recordInfo")},
        key_order=tuple(raw.keys()),
    )


def parse_bytes(data: bytes, source_path: str | None = None) -> Macro:
    """Parse the raw bytes of a .record file into a Macro. Pure function, no I/O."""
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data[3:].decode("utf-8") if bom else data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MacroFormatError(f"file is not valid UTF-8: {exc}") from exc
    try:
        root = json.loads(text, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant,
                          parse_float=_finite_float)
    except MacroFormatError:
        raise
    except json.JSONDecodeError as exc:
        raise MacroFormatError(f"file is not valid JSON: {exc}") from exc
    except ValueError as exc:                    # e.g. an integer with more digits than Python will convert
        raise MacroFormatError(f"file contains a number that cannot be read: {exc}") from exc
    except RecursionError as exc:
        raise MacroFormatError("file is nested too deeply") from exc
    if not isinstance(root, dict):
        raise MacroFormatError("top level is not a JSON object")

    if "mergedRecords" in root and not isinstance(root["mergedRecords"], list):
        raise MacroFormatError("$.mergedRecords: expected a list")

    issues: list[ParseIssue] = []
    fmt = _detect_format(text, bom, issues)
    macro = Macro(
        operations=_build_operations(root["operations"], "$.operations", issues) if "operations" in root else None,
        record_info=_build_record_info(root["recordInfo"], "$.recordInfo") if "recordInfo" in root else None,
        merged_records=(
            [_build_merged_entry(e, f"$.mergedRecords[{i}]", issues) for i, e in enumerate(root["mergedRecords"])]
            if "mergedRecords" in root else None
        ),
        extras={k: v for k, v in root.items() if k not in ("operations", "recordInfo", "mergedRecords")},
        key_order=tuple(root.keys()),
        fmt=fmt,
        source_path=source_path,
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_size=len(data),
        issues=issues,
    )
    return macro


def parse_file(path: str | Path) -> Macro:
    """Parse a .record file. Opens the file in binary READ mode only."""
    p = Path(path)
    with open(p, "rb") as handle:
        data = handle.read()
    return parse_bytes(data, source_path=str(p))
