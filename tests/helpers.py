"""Builders for SYNTHETIC .record data (no real user macros are copied into the repo).

The layout mimics what was observed in real LDPlayer files: 4-space indent, CRLF line
endings, no trailing newline, UTF-8 without BOM.
"""
from __future__ import annotations

import json
from typing import Any

RECORD_INFO_KEYS = [
    "loopType", "loopTimes", "circleDuration", "loopInterval", "loopDuration",
    "accelerateTimes", "accelerateTimesEx", "recordName", "createTime", "playOnBoot",
    "rebootTiming", "closeTiming", "resolutionWidth", "resolutionHeight",
    "randomTouch", "shortkey", "modifyKey",
]


def record_info(width: int = 960, height: int = 540, **overrides: Any) -> dict[str, Any]:
    base = {
        "loopType": 0, "loopTimes": 1, "circleDuration": 0, "loopInterval": 0,
        "loopDuration": 0, "accelerateTimes": 1, "accelerateTimesEx": 1, "recordName": "",
        "createTime": "2026-01-01 00:00:00", "playOnBoot": False, "rebootTiming": 0,
        "closeTiming": 0, "resolutionWidth": width, "resolutionHeight": height,
        "randomTouch": False, "shortkey": 0, "modifyKey": 0,
    }
    base.update(overrides)
    return base


def touch(timing: int, x: int, y: int, state: int, pid: int = 1) -> list[dict[str, Any]]:
    """A point-carrying PutMultiTouch op followed by the empty-points op seen in real files."""
    return [
        {"timing": timing, "operationId": "PutMultiTouch",
         "points": [{"id": pid, "x": x, "y": y, "state": state}]},
        {"timing": timing + 4, "operationId": "PutMultiTouch", "points": []},
    ]


def tap(timing: int, x: int, y: int) -> list[dict[str, Any]]:
    return touch(timing, x, y, 1) + touch(timing + 120, x, y, 0)


def to_bytes(obj: Any, *, newline: str = "\r\n", trailing_newline: bool = False,
             bom: bool = False, indent: int | str | None = 4, ensure_ascii: bool = False) -> bytes:
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii).replace("\n", newline)
    if trailing_newline:
        text += newline
    data = text.encode("utf-8")
    return b"\xef\xbb\xbf" + data if bom else data


def simple_macro() -> dict[str, Any]:
    ops = [{"timing": 5, "operationId": "ImeClipboard", "text": "DrawPen"}]
    ops += tap(1000, 480, 270)
    ops += [{"timing": 2000, "operationId": "AndroidBack"},
            {"timing": 2500, "operationId": "StopApp", "app": "com.example.app"}]
    return {"operations": ops, "recordInfo": record_info()}


def merged_macro() -> dict[str, Any]:
    entries = [
        {"operations": tap(100, 10, 20), "recordInfo": record_info()},
        {"operations": tap(50, 30, 40), "recordInfo": record_info()},
    ]
    return {"recordInfo": record_info(0, 0), "mergedRecords": entries}
