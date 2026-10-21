"""A small local GUI preferences file (theme + language choice), in the same spirit as `MacroLibrary`: one plain
JSON file, injected by the composition root -- this module never decides where it lives.

Preferences are strictly best-effort: they change nothing about whether the application can run. `load()` returns
whatever it can read as a flat string-to-string mapping and `{}` on any problem (missing file, first run, unreadable
JSON); `save()` swallows `OSError` (read-only data directory, disk full, ...). A user's chosen theme silently not
surviving a restart on a broken machine is an acceptable degradation; a startup crash because of it is not.
"""
from __future__ import annotations

import json
from pathlib import Path


class PreferencesStore:
    def __init__(self, path: str | Path | None) -> None:
        """`path=None` makes every load/save a no-op (in-memory only, nothing ever touches disk) -- the default
        for tests and for any composition root that has not decided where preferences should live yet."""
        self.path = Path(path) if path is not None else None

    def load(self) -> dict[str, str]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def save(self, values: dict[str, str]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass
