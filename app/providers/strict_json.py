"""Strict JSON reading for the provider-neutral file formats (recordings, macros)."""
from __future__ import annotations

import json


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")     # last-wins would silently pick one of two values
        out[key] = value
    return out


def loads_strict(text: str) -> object:
    """`json.loads` that refuses an object with a repeated key (raises ValueError)."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)
