"""Deterministic JSON for a `Macro`: the Recording format wrapped, not a second format.

    {"schema": "automation-macro", "version": 1, "name": "...", "recording": <automation-recording object>}

The embedded recording is exactly `recording_to_dict` (see app/providers/recording_format.py), so the two formats
cannot drift apart and a recording can be turned into a macro file without conversion. Keys are sorted, output is
ASCII with a final newline; loading is strict (unknown or missing keys, wrong schema/version, any invalid
recording or name raise `MacroFormatError`; nothing is repaired). No live objects, handles or wall-clock times are
stored. Executable actions are NOT stored: they are recomputed from the recording, so a file can never disagree
with its own events.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.playback.macro import Macro
from app.providers.recording import Recording
from app.providers.recording_format import RecordingFormatError, recording_from_dict, recording_to_dict
from app.providers.strict_json import loads_strict

SCHEMA = "automation-macro"
VERSION = 1


class MacroFormatError(ValueError):
    """The text is not a valid macro of this schema version."""


def macro_from_recording(recording: Recording, name: str = "") -> Macro:
    return Macro(recording, name)


def macro_to_json(macro: Macro) -> str:
    data = {"schema": SCHEMA, "version": VERSION, "name": macro.name, "recording": recording_to_dict(macro.recording)}
    if macro.adapt:                            # only then: a macro that does not adapt is written as it always was
        data["adapt"] = True
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def macro_from_json(text: str) -> Macro:
    try:
        data = loads_strict(text)
    except (ValueError, TypeError) as exc:
        raise MacroFormatError(f"Not valid JSON: {exc}") from None
    if not isinstance(data, dict) or not {"schema", "version", "name", "recording"} <= set(data) \
            <= {"schema", "version", "name", "recording", "adapt"} or data.get("adapt", True) is not True:
        raise MacroFormatError("A macro needs exactly: schema, version, name, recording (and \"adapt\": true when it "
                               "adapts to the size).")
    if data["schema"] != SCHEMA or data["version"] != VERSION or isinstance(data["version"], bool):
        raise MacroFormatError(f"Unsupported schema/version: {data['schema']!r} {data['version']!r}.")
    try:
        return Macro(recording_from_dict(data["recording"]), data["name"], "adapt" in data)
    except (RecordingFormatError, ValueError, TypeError) as exc:
        raise MacroFormatError(str(exc)) from None


def save_macro(macro: Macro, path: str | Path) -> None:
    Path(path).write_text(macro_to_json(macro), encoding="utf-8", newline="\n")


def load_macro(path: str | Path) -> Macro:
    return macro_from_json(Path(path).read_text(encoding="utf-8"))
