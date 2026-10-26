"""Deterministic JSON form of a `Recording` (recorder output only; NOT the future macro file format).

    {"schema": "automation-recording", "version": 1,
     "target": {"provider_id": "...", "target_id": "..."},
     "end_reason": "stopped", "skipped": {"secure_input": 2},
     "events": [{"delay_ms": 0, "kind": "pointer_move", "x": 10, "y": 20}, ...]}

Keys are sorted, output is ASCII with two-space indentation and a final newline, so equal recordings give equal
bytes. Only stable, neutral data is stored (no handles, addresses or wall-clock times; no display names or titles --
except, on the person's decision of 2026-10-25/26, in a recording on the whole screen: the program, kind and title
of each surface a press was in, the optional "places" key, so each click can be played in it again).
Loading is strict: unknown keys, wrong types, an unknown version or an inconsistent event raise
`RecordingFormatError`; nothing is repaired or guessed.
"""
from __future__ import annotations

import json

from app.providers.input import Key
from app.providers.strict_json import loads_strict
from app.providers.recording import Place, PointerButton, RecordedEvent, RecordedEventKind, Recording

SCHEMA = "automation-recording"
VERSION = 1


class RecordingFormatError(ValueError):
    """The text is not a valid recording of this schema version."""


_FIELDS = ("x", "y", "button", "click_count", "key", "text")


def _event_to_dict(e: RecordedEvent) -> dict:
    out: dict = {"delay_ms": e.delay_ms, "kind": e.kind.value}
    for name in _FIELDS:
        value = getattr(e, name)
        if value is not None:
            out[name] = value.value if isinstance(value, (Key, PointerButton)) else value
    if e.place is not None:
        out["place"] = list(e.place)
    return out


def recording_to_dict(rec: Recording) -> dict:
    data = {"schema": SCHEMA, "version": VERSION,
            "target": {"provider_id": rec.provider_id, "target_id": rec.target_id},
            "end_reason": rec.end_reason, "skipped": dict(rec.skipped),
            "events": [_event_to_dict(e) for e in rec.events]}
    if rec.size is not None:                   # only then: a recording without one is written as it always was
        data["size"] = list(rec.size)
    if rec.places:                             # likewise (a recording on the whole screen, see recording.Places)
        data["places"] = [{"program": p.program, "kind": p.kind, "title": p.title, "width": p.width,
                           "height": p.height, "at_start": p.at_start, "window": p.window} for p in rec.places]
    return data


def recording_to_json(rec: Recording) -> str:
    return json.dumps(recording_to_dict(rec), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _exact(obj: object, keys: set[str], what: str, optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(obj, dict):
        raise RecordingFormatError(f"{what} must be an object.")
    unknown = set(obj) - keys - optional
    missing = keys - set(obj)
    if unknown or missing:
        raise RecordingFormatError(f"{what}: unknown {sorted(unknown)} / missing {sorted(missing)} keys.")
    return obj


def recording_from_dict(data: object) -> Recording:
    top = _exact(data, {"schema", "version", "target", "end_reason", "skipped", "events"}, "recording",
                 frozenset({"size", "places"}))
    if top["schema"] != SCHEMA or top["version"] != VERSION or isinstance(top["version"], bool):
        raise RecordingFormatError(f"Unsupported schema/version: {top['schema']!r} {top['version']!r}.")
    if "size" in top and top["size"] is None:
        raise RecordingFormatError("size, when it is there, is [width, height].")
    places = _places(top["places"]) if "places" in top else ()
    target = _exact(top["target"], {"provider_id", "target_id"}, "target")
    if not isinstance(top["skipped"], dict) or not isinstance(top["events"], list):
        raise RecordingFormatError("skipped must be an object and events a list.")
    events = []
    for i, raw in enumerate(top["events"]):
        d = _exact(raw, {"delay_ms", "kind"}, f"event {i}", frozenset(_FIELDS + ("place",)))
        if "place" in d and not (isinstance(d["place"], list) and len(d["place"]) == 3):
            raise RecordingFormatError(f"event {i}: place, when it is there, is [index, x, y].")
        try:
            events.append(RecordedEvent(
                RecordedEventKind(d["kind"]), d["delay_ms"], d.get("x"), d.get("y"),
                PointerButton(d["button"]) if "button" in d else None, d.get("click_count"),
                Key(d["key"]) if "key" in d else None, d.get("text"),
                tuple(d["place"]) if "place" in d else None))
        except (ValueError, TypeError) as exc:
            raise RecordingFormatError(f"event {i}: {exc}") from None
    try:
        return Recording(target["provider_id"], target["target_id"], tuple(events), top["end_reason"],
                         tuple(sorted(top["skipped"].items())), top.get("size"), places)
    except (ValueError, TypeError) as exc:
        raise RecordingFormatError(str(exc)) from None


def _places(raw: object) -> tuple[Place, ...]:
    if not isinstance(raw, list) or not raw:
        raise RecordingFormatError("places, when they are there, are a non-empty list.")
    out = []
    for i, item in enumerate(raw):
        d = _exact(item, {"program", "kind", "title", "width", "height", "at_start", "window"}, f"place {i}")
        try:
            out.append(Place(d["program"], d["kind"], d["title"], d["width"], d["height"], d["at_start"],
                             d["window"]))
        except (ValueError, TypeError) as exc:
            raise RecordingFormatError(f"place {i}: {exc}") from None
    return tuple(out)


def recording_from_json(text: str) -> Recording:
    try:
        data = loads_strict(text)
    except (ValueError, TypeError) as exc:
        raise RecordingFormatError(f"Not valid JSON: {exc}") from None
    return recording_from_dict(data)
