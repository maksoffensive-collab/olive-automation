"""Script files, version 4: blocks joined by arrows (`app.decide.graph`), the same `automation-macro` schema.

    {"schema": "automation-macro", "version": 4, "name": "...",
     "nodes": [<node>, ...],                              1 .. MAX_NODES; exactly one "start"
     "edges": [{"from": "<id>", "out": "<output>", "to": "<id>"}, ...]}

    node   {"id": "n1", "type": "start",  "at": [x, y], "delay_s": 0}
           {"id": .., "type": "wait",   "at": .., "seconds": 1}
           {"id": .., "type": "check",  "at": .., "mode": "any"|"all", "mixed": false,
                                        "parts": [{"condition": <condition>, "size": [w, h]|null}, ...]}
           {"id": .., "type": "repeat", "at": .., "mode": "manual"|"until"|"for"|"times", "until": "HH:MM"|"",
                                        "seconds": 0, "times": 0}
           {"id": .., "type": "click",  "at": .., "how": "tap"|"double"|"right"|"hold"|"drag",
                                        "where": "point"|"match", "point": [x, y]|null, "to": [x, y]|null,
                                        "size": [w, h]|null, "hold_s": 1}
           {"id": .., "type": "macro",  "at": .., "macro": "<the macro's key in the library>"}
           {"id": .., "type": "stop",   "at": .., "success": true, "message": ""}

    <condition> is exactly a version-2/3 condition (app/decide/format.py), its picture a PNG in the script's own
    asset folder, named by its content.

Like every macro file this is UNTRUSTED DATA, read into the graph's own validated objects: every key is required,
nothing unknown is accepted, nothing is evaluated. A macro block names a recorded macro of the library by its key;
it never holds input of its own, and which target a run acts on is never in the file.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import MappingProxyType

from .format import (SCHEMA, MacroFormatError, _AssetNames, _AssetReader, _condition, _condition_out, _MemoryAssets,
                     _obj, _parse, _str, _write_new, asset_dir_for)
from .graph import (CHECK, CLICK, MACRO, MAX_NODES, REPEAT, START, STOP, WAIT, CheckNode, ClickNode, Edge, GraphError,
                    MacroNode, Node, Part, RepeatNode, Script, StartNode, StopNode, WaitNode)

__all__ = ["Script"]                                  # re-exported for the readers in format.py

VERSION = 4
_NODE_KEYS = MappingProxyType({
    START: frozenset({"delay_s"}), WAIT: frozenset({"seconds"}), CHECK: frozenset({"mode", "mixed", "parts"}),
    REPEAT: frozenset({"mode", "until", "seconds", "times"}),
    CLICK: frozenset({"how", "where", "point", "to", "size", "hold_s"}),
    MACRO: frozenset({"macro"}), STOP: frozenset({"success", "message"}),
})


def is_version_4(top: object) -> bool:
    version = top.get("version") if isinstance(top, dict) else None
    return isinstance(top, dict) and top.get("schema") == SCHEMA and type(version) is int and version == VERSION


def _pair(value: object, what: str) -> tuple[int, int]:
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
        raise MacroFormatError(f"{what} must be [x, y], two whole numbers.")
    return value[0], value[1]


def _maybe_pair(value: object, what: str) -> tuple[int, int] | None:
    return None if value is None else _pair(value, what)


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MacroFormatError(f"{what} must be a finite number.")
    return value


def _node(value: object, what: str, assets: _AssetReader) -> Node:
    kind = value.get("type") if isinstance(value, dict) else None
    if kind not in _NODE_KEYS:
        raise MacroFormatError(f"{what}: unknown block type {kind!r}.")
    d = _obj(value, {"id", "type", "at"} | _NODE_KEYS[kind], f"{what} ({kind})")
    common = {"id": d["id"], "at": _pair(d["at"], f"{what}.at")}
    if kind == START:
        return StartNode(**common, delay_s=_number(d["delay_s"], f"{what}.delay_s"))
    if kind == WAIT:
        return WaitNode(**common, seconds=_number(d["seconds"], f"{what}.seconds"))
    if kind == CHECK:
        if not isinstance(d["parts"], list):
            raise MacroFormatError(f"{what}.parts must be a list.")
        parts = []
        for i, raw in enumerate(d["parts"]):
            p = _obj(raw, {"condition", "size"}, f"{what}.parts[{i}]")
            parts.append(Part(_condition(p["condition"], f"{what}.parts[{i}].condition", assets),
                              _maybe_pair(p["size"], f"{what}.parts[{i}].size")))
        return CheckNode(**common, parts=tuple(parts), mode=d["mode"], mixed=d["mixed"])
    if kind == REPEAT:
        return RepeatNode(**common, mode=d["mode"], until=d["until"], seconds=_number(d["seconds"], f"{what}.seconds"),
                          times=d["times"])
    if kind == CLICK:
        return ClickNode(**common, how=d["how"], where=d["where"], point=_maybe_pair(d["point"], f"{what}.point"),
                         to=_maybe_pair(d["to"], f"{what}.to"), size=_maybe_pair(d["size"], f"{what}.size"),
                         hold_s=_number(d["hold_s"], f"{what}.hold_s"))
    if kind == MACRO:
        return MacroNode(**common, macro=d["macro"])
    return StopNode(**common, success=d["success"], message=d["message"])


def _script(top: object, assets: _AssetReader) -> Script:
    top = _obj(top, {"schema", "version", "name", "nodes", "edges"}, "script")
    if not is_version_4(top):
        raise MacroFormatError(f"Unsupported schema/version: {top['schema']!r} {top['version']!r}.")
    if not isinstance(top["nodes"], list) or not 1 <= len(top["nodes"]) <= MAX_NODES:
        raise MacroFormatError(f"nodes must be a list of 1 to {MAX_NODES} blocks.")
    if not isinstance(top["edges"], list):
        raise MacroFormatError("edges must be a list.")
    try:
        nodes = tuple(_node(n, f"block {i}", assets) for i, n in enumerate(top["nodes"]))
        edges = []
        for i, raw in enumerate(top["edges"]):
            e = _obj(raw, {"from", "out", "to"}, f"arrow {i}")
            if not all(isinstance(e[k], str) for k in ("from", "out", "to")):
                raise MacroFormatError(f"arrow {i}: from, out and to are text.")
            edges.append(Edge(e["from"], e["out"], e["to"]))
        return Script(_str(top["name"], "name", 120), nodes, tuple(edges))
    except GraphError as exc:
        raise MacroFormatError(str(exc)) from None
    except (TypeError, ValueError) as exc:
        raise MacroFormatError(f"Not a valid script: {exc}") from None


def script_from_json(text: str, asset_dir: str | Path | None = None) -> Script:
    return _script(_parse(text), _AssetReader(Path(asset_dir) if asset_dir is not None else None))


def _node_out(node: Node, assets: dict[str, bytes]) -> dict:
    out: dict = {"id": node.id, "type": node.kind, "at": list(node.at)}
    if isinstance(node, StartNode):
        out["delay_s"] = node.delay_s
    elif isinstance(node, WaitNode):
        out["seconds"] = node.seconds
    elif isinstance(node, CheckNode):
        out.update(mode=node.mode, mixed=node.mixed,
                   parts=[{"condition": _condition_out(p.condition, assets),
                           "size": None if p.size is None else list(p.size)} for p in node.parts])
    elif isinstance(node, RepeatNode):
        out.update(mode=node.mode, until=node.until, seconds=node.seconds, times=node.times)
    elif isinstance(node, ClickNode):
        out.update(how=node.how, where=node.where, point=None if node.point is None else list(node.point),
                   to=None if node.to is None else list(node.to), size=None if node.size is None else list(node.size),
                   hold_s=node.hold_s)
    elif isinstance(node, MacroNode):
        out["macro"] = node.macro
    elif isinstance(node, StopNode):
        out.update(success=node.success, message=node.message)
    return out


def script_to_json(script: Script) -> tuple[str, dict[str, bytes]]:
    """The file text and the pictures it refers to ({name: PNG bytes})."""
    if not isinstance(script, Script):
        raise MacroFormatError("script_to_json needs a version-4 Script.")
    assets: dict[str, bytes] = {}
    data = {"schema": SCHEMA, "version": VERSION, "name": script.name,
            "nodes": [_node_out(n, assets) for n in script.nodes],
            "edges": [{"from": e.source, "out": e.output, "to": e.target} for e in script.edges]}
    try:
        text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    except ValueError as exc:
        raise MacroFormatError(str(exc)) from None
    return text, assets


def validate_script(script: Script) -> None:
    """Refuse anything a save would write that a load would not read back as the same script (the reader holds
    every rule of the file, as for versions 2 and 3)."""
    text, assets = script_to_json(script)
    if _script(_parse(text), _MemoryAssets(assets)) != script:
        raise MacroFormatError("This script would not read back exactly as it would be saved.")


def summarize_script(top: dict) -> tuple[str, int, tuple[str, ...]]:
    """(name, blocks, picture names) of a version-4 file's parsed JSON, no picture opened."""
    names = _AssetNames()
    script = _script(top, names)
    return script.name, len(script.nodes), tuple(names.names)


def save_script_file(script: Script, path: str | Path, *, overwrite: bool = False) -> Path:
    p = Path(path)
    text, assets = script_to_json(script)
    if p.exists() and not overwrite:
        raise MacroFormatError(f"{p.name} already exists.")
    if assets:
        folder = asset_dir_for(p)
        folder.mkdir(parents=True, exist_ok=True)
        for name, png in sorted(assets.items()):
            target = folder / name
            if target.exists():
                if target.read_bytes() != png:
                    raise MacroFormatError(f"Template asset {name} already exists with different content.")
                continue
            _write_new(target, png, False)
    _write_new(p, text.encode("utf-8"), overwrite)
    return p


def referenced_pictures(script: Script) -> frozenset[str]:
    return frozenset(script_to_json(script)[1])


__all__ = ["VERSION", "is_version_4", "script_from_json", "script_to_json", "validate_script", "save_script_file",
           "summarize_script", "referenced_pictures"]
