"""Strict structural comparison of two JSON-like values.

Stricter than `==`: key ORDER matters, and types must match exactly (so `1`, `1.0` and
`True` are all different). Used by the round-trip tests and reusable for later
validation. Returns human-readable differences; an empty list means identical.
"""
from __future__ import annotations

from typing import Any


def structural_diff(a: Any, b: Any, path: str = "$", limit: int = 20) -> list[str]:
    diffs: list[str] = []
    _walk(a, b, path, diffs, limit)
    return diffs


def _walk(a: Any, b: Any, path: str, diffs: list[str], limit: int) -> None:
    if len(diffs) >= limit:
        return
    if type(a) is not type(b):
        diffs.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
        return
    if isinstance(a, dict):
        ka, kb = list(a.keys()), list(b.keys())
        if ka != kb:
            missing = [k for k in ka if k not in b]
            added = [k for k in kb if k not in a]
            if missing or added:
                diffs.append(f"{path}: keys differ (missing={missing}, added={added})")
            else:
                diffs.append(f"{path}: key order differs ({ka} != {kb})")
            return
        for key in ka:
            _walk(a[key], b[key], f"{path}.{key}", diffs, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{path}: length {len(a)} != {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            _walk(x, y, f"{path}[{i}]", diffs, limit)
    elif a != b:
        diffs.append(f"{path}: {a!r} != {b!r}")
