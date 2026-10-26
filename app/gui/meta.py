"""Favourites and descriptions: the small facts about a macro that its FILE deliberately does not carry.

The macro format is strict and holds only what is needed to replay a macro (schema, version, name, recording).
A star or a sentence of description is a library concern, not a recording concern: extending the macro format for
them would rewrite every macro file for a GUI feature and add a way for a file to disagree with its own recording.
They live in one sidecar JSON file instead, keyed by the macro's path relative to the macro folder
(`MacroInfo.key`), which is also what the run history uses.

Best-effort, like the preferences file: metadata that cannot be read or written never blocks the application --
a lost star is cosmetic, a crash is not. `rename_key` and `forget` keep the file in step when a macro is renamed,
moved or deleted, so entries never pile up for files that no longer exist.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_DESCRIPTION = 500


class MacroMeta:
    def __init__(self, path: str | Path | None) -> None:
        """`path=None` keeps everything in memory and never touches disk (tests, and any caller that has not
        decided where metadata should live)."""
        self.path = Path(path) if path is not None else None
        self._unreadable = False                # there, but not read: kept as it is, never written over
        self._data: dict[str, dict] = self._read()

    # ---- storage ---------------------------------------------------------------------------------
    def _read(self) -> dict[str, dict]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self._unreadable = True             # locked, or cut short: what it holds is not thrown away
            return {}
        if not isinstance(raw, dict):
            self._unreadable = True
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}

    def _write(self) -> None:
        """The whole file, through a temporary file that then takes its place (a write cut short leaves the old
        file whole). Not at all when the file is there but could not be read: this session keeps its changes in
        memory only, rather than replace stars, descriptions and remembered targets it never saw."""
        if self.path is None or self._unreadable:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass

    def _entry(self, key: str) -> dict:
        return self._data.get(key) or {}

    def _set(self, key: str, field: str, value: object, default: object) -> None:
        entry = dict(self._entry(key))
        if value == default:
            entry.pop(field, None)
        else:
            entry[field] = value
        if entry:
            self._data[key] = entry
        else:
            self._data.pop(key, None)          # an entry that says nothing is not kept
        self._write()

    # ---- favourites -------------------------------------------------------------------------------
    def is_favorite(self, key: str) -> bool:
        return self._entry(key).get("favorite") is True

    def set_favorite(self, key: str, favorite: bool) -> None:
        self._set(key, "favorite", bool(favorite), False)

    def toggle_favorite(self, key: str) -> bool:
        new = not self.is_favorite(key)
        self.set_favorite(key, new)
        return new

    def favorites(self) -> frozenset[str]:
        return frozenset(k for k, v in self._data.items() if v.get("favorite") is True)

    # ---- description ------------------------------------------------------------------------------
    def description(self, key: str) -> str:
        value = self._entry(key).get("description")
        return value if isinstance(value, str) else ""

    def set_description(self, key: str, text: str) -> None:
        self._set(key, "description", str(text).strip()[:MAX_DESCRIPTION], "")

    # ---- the target it remembers (the person's list of 2026-10-25, item 12) -----------------------
    def target(self, key: str) -> dict | None:
        """What a macro or script remembers of the target it was made or last run on (`target_memory.Remembered`
        as plain data), or None."""
        value = self._entry(key).get("target")
        return value if isinstance(value, dict) else None

    def set_target(self, key: str, target: dict | None) -> None:
        self._set(key, "target", dict(target) if target else None, None)

    # ---- keeping in step with the files -----------------------------------------------------------
    def rename_key(self, old: str, new: str) -> None:
        if old == new:
            return
        entry = self._data.pop(old, None)
        if entry:
            self._data[new] = entry
            self._write()

    def forget(self, key: str) -> None:
        if self._data.pop(key, None) is not None:
            self._write()

    def keep_only(self, keys: frozenset[str]) -> None:
        """Drop entries whose macro no longer exists (called after a refresh of the library)."""
        stale = set(self._data) - set(keys)
        if stale:
            for key in stale:
                self._data.pop(key, None)
            self._write()
