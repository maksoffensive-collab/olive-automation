"""Macro files, versions 2 and 3: the same `automation-macro` schema, able to hold conditions and decisions.

A macro file is UNTRUSTED DATA. It describes allowed operations and nothing else: every object in it is one
of a closed set of types, read into the platform's EXISTING objects -- `Macro`/`Recording` for recorded input,
the `app.observe` queries for what to look for, `Condition`, `WaitUntil`, `Decision`, `RunMacro`, `TapMatch` --
whose own constructors validate it. There is no second implementation of any of them here: this module only
maps JSON fields to constructor arguments and back. Nothing is evaluated, imported or executed; an unknown type,
an unknown or missing key, a wrong type or an out-of-range value is `MacroFormatError`, never a fallback.

Version 1 is unchanged and still read and written by `app.playback.format` -- this module hands a v1 file to
that code untouched, so a v1 macro loads, runs and saves exactly as before and stays byte-identical. Reading
NEVER rewrites a file. Turning a v1 macro into v2 is explicit (`migrate_v1`) and only reaches the disk if the
caller then saves it.

Version 3 (2026-10-19) is version 2 with branches that are LISTS of blocks (an IF inside a branch included), a
start delay, and where each block is drawn. A script is written as version 2 whenever it uses none of that -- so
every file version 2 can hold is saved exactly as before, byte for byte -- and as version 3 otherwise:

    {"schema": "automation-macro", "version": 3, "name": "...",
     "start": {"delay_s": 0, "at": [x, y]|null}, "end": {"at": [x, y]|null},
     "steps": [<step>, ...]}            1 .. MAX_STEPS steps; MAX_BLOCKS blocks in all, branches included
    step       {"type": "run_macro", "name": "...", "recording": <automation-recording v1>, "at": ...}
               {"type": "wait_until", "condition": <condition>, "timeout_s": 20, "interval_s": 0.5, "at": ...}
               {"type": "if", "condition": <condition>, "then": [<block>, ...], "else": [<block>, ...], "at": ...}
    block      <step> | {"type": "tap_match", "op": "tap", "offset": [dx, dy], "at": ...}   (a tap: first only)
    at         [x, y] -- where the editor draws the block (0 .. 100000), or null for "placed automatically"

Version 2
---------
    {"schema": "automation-macro", "version": 2, "name": "...",
     "steps": [<step>, ...]}                                       1 .. MAX_STEPS steps, run in order

    step       {"type": "run_macro", "name": "...", "recording": <automation-recording v1>}
               {"type": "wait_until", "condition": <condition>, "timeout_s": 20, "interval_s": 0.5}
               {"type": "if", "condition": <condition>, "then": <branch>|null, "else": <branch>|null}
    branch     {"type": "run_macro", ...as above} | {"type": "tap_match", "op": "tap", "offset": [dx, dy]}
    condition  {"type": "pixel",    "point": [x, y], "color": [r, g, b], "tolerance": 0, ...}
               {"type": "color",    "color": [r, g, b], "tolerance": 0, "region": <region>|null,
                                    "min_ratio": 0.5, ...}
               {"type": "template", "asset": "template-<16 hex>.png", "region": <region>|null,
                                    "min_confidence": 0.95, "max_results": 1, ...}
               {"type": "text_lines", "lines": [{"text": "...", "present": true}, ...], "ignore_case": true,
                                    "region": <region>|null, "language": "", "min_confidence": 0.0, ...}
                                    1 .. MAX_TEXT_LINES lines; true when at least one line holds
               {"type": "text",     "text": "...", "match": "contains"|"exact", "ignore_case": true,
                                    "region": <region>|null, "language": "", "min_confidence": 0.0,
                                    "max_results": 1, ...}
               every condition also has "expected": true|false and "label": "..."
    region     {"x": 0, "y": 0, "width": 10, "height": 10}

Every key is required, so what a file says is always what runs -- there are no defaults to guess from.

What is deliberately NOT in a file
----------------------------------
* No target selection. `tap_match` has no coordinates of its own and no target: it clicks where the condition
  saw its match on the target the run is bound to. A recording inside `run_macro` keeps the provider/target it
  was recorded for exactly as v1 does -- as something preflight CHECKS, never as something that picks a target.
* No `allow_different_target`. Binding a recorded macro to another target stays the caller's run-time choice
  (`run_steps(..., allow_different_target=...)`), so a file cannot grant itself a different target.
* No image data. A template is a PNG file beside the macro (see below), referred to by name only.

Template assets
---------------
    <folder>/<stem>.macro.json
    <folder>/<stem>.assets/template-<16 hex>.png

The name is derived from the image's own pixels (first 16 hex digits of the SHA-256 of its canonical PNG
encoding), so it is deterministic, the same template is stored once, and it can only be a plain file name --
anything else (separators, "..", a drive, an absolute path, another extension) does not match the one allowed
pattern and is refused before the file system is touched. On load the resolved path must still lie inside the
asset folder (a link pointing elsewhere is refused), the file must be a PNG within `MAX_ASSET_BYTES` and
`MAX_TEMPLATE_SIDE`, and its pixels must hash to its name, so a replaced or corrupted template is an error, not a
different click. Each asset is read once per load; the decoded image is an immutable `Bitmap`, safe to share
between targets. There is no module-level cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import Path

from app.observe.errors import InvalidQuery
from app.observe.query import (MAX_TEXT_LINES, ColorRegionQuery, PixelQuery, TemplateQuery, TextLine,
                               TextLinesQuery, TextQuery)
import app.playback.format as v1
from app.playback.format import MacroFormatError
from app.playback.macro import MAX_NAME_LENGTH, ActionOp, Macro
from app.providers.recording_format import RecordingFormatError, recording_from_dict, recording_to_dict
from app.providers.strict_json import loads_strict
from app.vision.bitmap import Bitmap, VisionError, decode_png, encode_png
from app.vision.roi import Roi

from .condition import Condition
from .decision import ELSE, MAX_NESTING, THEN, Decision, RunMacro, TapMatch, count_blocks, walk
from .errors import DecisionError
from app.providers.size import check_size

from .position import MAX_POSITION, Position, check_position
from .run import MAX_BLOCKS, MAX_STEPS, Step, check_start_delay
from .wait import WaitUntil

SCHEMA = v1.SCHEMA
VERSION = 2
VERSION_3 = 3
SCRIPT_VERSIONS = (VERSION, VERSION_3)     # what a script file may say; 1 is a recorded macro
SUFFIX = ".macro.json"
ASSET_DIR_SUFFIX = ".assets"
ASSET_NAME = re.compile(r"template-[0-9a-f]{16}\.png\Z")
MAX_ASSET_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_SIDE = 2048
MAX_COORD = 100_000
MAX_TEXT = 256
MIN_INTERVAL_S = 0.05
LANGUAGE = re.compile(r"[A-Za-z0-9-]{0,35}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

__all__ = ["ConditionalMacro", "MacroFormatError", "MacroSummary", "SCHEMA", "VERSION", "migrate_v1",
           "conditional_to_json", "conditional_from_json", "load_macro_file", "save_macro_file", "asset_dir_for",
           "asset_name", "summarize_macro_text", "referenced_assets", "validate_macro"]


@dataclass(frozen=True)
class ConditionalMacro:
    """A script: a name and a list of steps (WaitUntil, Decision, RunMacro) run in order, an IF's branches holding
    blocks of their own; a delay before the first step; where the Start and End blocks are drawn; the size of the
    picture its points and areas were picked on (`size`, None: not known) and whether it adapts to another
    (`adapt`, "Adapt to the size", the default for scripts -- the person's decision of 2026-10-20)."""
    name: str
    steps: tuple[Step, ...]
    start_delay_s: float = 0
    start_at: Position = None
    end_at: Position = None
    size: tuple[int, int] | None = None
    adapt: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or len(self.name) > MAX_NAME_LENGTH or not self.name.isprintable():
            raise MacroFormatError(f"name must be at most {MAX_NAME_LENGTH} printable characters.")   # v1's rule
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= MAX_STEPS:
            raise MacroFormatError(f"A macro has 1 to {MAX_STEPS} steps.")
        for step in self.steps:
            if not isinstance(step, (WaitUntil, Decision, RunMacro)):
                raise MacroFormatError(f"Not a step: {type(step).__name__}.")
        if count_blocks(self.steps) > MAX_BLOCKS:
            raise MacroFormatError(f"A macro has at most {MAX_BLOCKS} blocks, the ones inside branches included.")
        try:
            check_start_delay(self.start_delay_s)
            object.__setattr__(self, "start_at", check_position(self.start_at, "start_at"))
            object.__setattr__(self, "end_at", check_position(self.end_at, "end_at"))
        except DecisionError as exc:
            raise MacroFormatError(str(exc)) from None
        try:
            object.__setattr__(self, "size", check_size(self.size))
        except ValueError as exc:
            raise MacroFormatError(str(exc)) from None
        if not isinstance(self.adapt, bool):
            raise MacroFormatError("adapt must be True or False.")


def migrate_v1(macro: Macro) -> ConditionalMacro:
    """A v1 macro as v2: one step that runs it. Explicit only; nothing calls this while reading a file."""
    if not isinstance(macro, Macro):
        raise MacroFormatError("migrate_v1 needs a version-1 Macro.")
    return ConditionalMacro(macro.name, (RunMacro(macro),))


# ---- assets ---------------------------------------------------------------------------------------------
def asset_name(bitmap: Bitmap) -> str:
    return f"template-{hashlib.sha256(encode_png(bitmap)).hexdigest()[:16]}.png"


def asset_dir_for(path: str | Path) -> Path:
    """`x/daily.macro.json` -> `x/daily.assets`: the one folder that belongs to that macro file."""
    p = Path(path)
    if not p.name.endswith(SUFFIX) or len(p.name) == len(SUFFIX):
        raise MacroFormatError(f"A macro file name ends with {SUFFIX}.")
    return p.with_name(p.name[:-len(SUFFIX)] + ASSET_DIR_SUFFIX)


def _check_asset_name(name: object) -> str:
    if not isinstance(name, str) or not ASSET_NAME.match(name):
        raise MacroFormatError(f"Template asset name {name!r} is not allowed: it must be a plain file name "
                               "like template-0123456789abcdef.png (no folders, drives or other types).")
    return name


class _AssetReader:
    """Reads the templates of ONE load, each at most once. Lives only as long as that load."""

    def __init__(self, directory: Path | None) -> None:
        self._dir = directory
        self._seen: dict[str, Bitmap] = {}

    def read(self, name: object) -> Bitmap:
        name = _check_asset_name(name)
        if name in self._seen:
            return self._seen[name]
        if self._dir is None:
            raise MacroFormatError(f"Template asset {name} cannot be read: this macro has no asset folder.")
        folder = self._dir.resolve()
        path = (self._dir / name).resolve()
        if path.parent != folder:
            raise MacroFormatError(f"Template asset {name} does not lie inside the macro's asset folder.")
        if not path.is_file():
            raise MacroFormatError(f"Template asset {name} is missing from {self._dir.name}.")
        try:
            size = path.stat().st_size
            if size > MAX_ASSET_BYTES:
                raise MacroFormatError(f"Template asset {name} is larger than {MAX_ASSET_BYTES} bytes.")
            data = path.read_bytes()
        except OSError as exc:
            raise MacroFormatError(f"Template asset {name} could not be read: {exc.strerror or exc}.") from None
        if not data.startswith(_PNG_SIGNATURE) or len(data) < 24 or data[12:16] != b"IHDR":
            raise MacroFormatError(f"Template asset {name} is not a PNG image.")
        width, height = struct.unpack(">II", data[16:24])
        if not (0 < width <= MAX_TEMPLATE_SIDE and 0 < height <= MAX_TEMPLATE_SIDE):
            raise MacroFormatError(f"Template asset {name} is {width}x{height}; a template is at most "
                                   f"{MAX_TEMPLATE_SIDE}x{MAX_TEMPLATE_SIDE}.")
        try:
            bitmap = decode_png(data)
        except VisionError as exc:
            raise MacroFormatError(f"Template asset {name} is not a valid image: {exc}") from None
        if asset_name(bitmap) != name:
            raise MacroFormatError(f"Template asset {name} does not contain the image it was saved with "
                                   "(its pixels do not match its name).")
        self._seen[name] = bitmap
        return bitmap


class _AssetNames(_AssetReader):
    """For a summary only: every asset NAME is checked exactly as a load checks it, but no file is opened. The
    placeholder it hands the query constructors never leaves `summarize_macro_text`."""

    _PLACEHOLDER = Bitmap(1, 1, b"\x00\x00\x00")

    def __init__(self) -> None:
        super().__init__(None)
        self.names: dict[str, None] = {}              # insertion-ordered set of the names referred to

    def read(self, name: object) -> Bitmap:
        self.names[_check_asset_name(name)] = None
        return self._PLACEHOLDER


# ---- reading --------------------------------------------------------------------------------------------
def _obj(value: object, keys, what: str) -> dict:
    if not isinstance(value, dict):
        raise MacroFormatError(f"{what} must be an object.")
    unknown, missing = set(value) - keys, keys - set(value)
    if unknown or missing:
        raise MacroFormatError(f"{what}: unknown {sorted(unknown)} / missing {sorted(missing)} keys.")
    return value


def _int(value: object, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise MacroFormatError(f"{what} must be a whole number from {low} to {high}.")
    return value


def _num(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MacroFormatError(f"{what} must be a finite number.")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise MacroFormatError(f"{what} must be true or false.")
    return value


def _str(value: object, what: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        raise MacroFormatError(f"{what} must be at most {limit} printable characters.")
    return value


def _pair(value: object, what: str, low: int, high: int) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise MacroFormatError(f"{what} must be two whole numbers.")
    return (_int(value[0], what, low, high), _int(value[1], what, low, high))


def _color(value: object, what: str) -> tuple[int, int, int]:
    if not isinstance(value, list) or len(value) != 3:
        raise MacroFormatError(f"{what} must be three whole numbers (red, green, blue).")
    return tuple(_int(c, what, 0, 255) for c in value)                       # type: ignore[return-value]


def _region(value: object, what: str) -> Roi | None:
    if value is None:
        return None
    d = _obj(value, {"x", "y", "width", "height"}, what)
    x, y = _int(d["x"], f"{what}.x", 0, MAX_COORD), _int(d["y"], f"{what}.y", 0, MAX_COORD)
    w, h = _int(d["width"], f"{what}.width", 1, MAX_COORD), _int(d["height"], f"{what}.height", 1, MAX_COORD)
    return Roi(x, y, w, h)


_CONDITION_KEYS = MappingProxyType({
    "pixel": frozenset({"point", "color", "tolerance"}),
    "color": frozenset({"color", "tolerance", "region", "min_ratio"}),
    "template": frozenset({"asset", "region", "min_confidence", "max_results"}),
    "text": frozenset({"text", "match", "ignore_case", "region", "language", "min_confidence", "max_results"}),
    "text_lines": frozenset({"lines", "ignore_case", "region", "language", "min_confidence"}),
})
_COMMON = frozenset({"type", "expected", "label"})


def _condition(value: object, what: str, assets: _AssetReader) -> Condition:
    if not isinstance(value, dict) or value.get("type") not in _CONDITION_KEYS:
        raise MacroFormatError(f"{what}: unknown condition type "
                               f"{value.get('type') if isinstance(value, dict) else value!r}; "
                               f"allowed: {sorted(_CONDITION_KEYS)}.")
    kind = value["type"]
    any_size = kind == "template" and "any_size" in value          # written only when it is on
    if any_size:
        if value["any_size"] is not True:
            raise MacroFormatError(f"{what}.any_size is written only as true.")
        value = {k: v for k, v in value.items() if k != "any_size"}
    d = _obj(value, _CONDITION_KEYS[kind] | _COMMON, f"{what} ({kind})")
    expected, label = _bool(d["expected"], f"{what}.expected"), _str(d["label"], f"{what}.label", 100)
    try:
        if kind == "pixel":
            query = PixelQuery(_pair(d["point"], f"{what}.point", 0, MAX_COORD), _color(d["color"], f"{what}.color"),
                               _int(d["tolerance"], f"{what}.tolerance", 0, 255), expected)
        elif kind == "color":
            query = ColorRegionQuery(_color(d["color"], f"{what}.color"),
                                     _int(d["tolerance"], f"{what}.tolerance", 0, 255),
                                     _region(d["region"], f"{what}.region"), _num(d["min_ratio"], f"{what}.min_ratio"),
                                     expected)
        elif kind == "template":
            query = TemplateQuery(assets.read(d["asset"]), _num(d["min_confidence"], f"{what}.min_confidence"),
                                  _region(d["region"], f"{what}.region"),
                                  _int(d["max_results"], f"{what}.max_results", 1, 100), expected, any_size)
        else:
            if not isinstance(d["language"], str) or not LANGUAGE.match(d["language"]):
                raise MacroFormatError(f"{what}.language must be a language tag such as 'en' or 'ru', or ''.")
            if kind == "text_lines":
                return Condition(TextLinesQuery(_lines(d["lines"], f"{what}.lines"),
                                                _bool(d["ignore_case"], f"{what}.ignore_case"),
                                                _region(d["region"], f"{what}.region"), d["language"],
                                                _num(d["min_confidence"], f"{what}.min_confidence"), expected), label)
            query = TextQuery(_str(d["text"], f"{what}.text", MAX_TEXT), d["match"],
                              _bool(d["ignore_case"], f"{what}.ignore_case"), _region(d["region"], f"{what}.region"),
                              d["language"], _num(d["min_confidence"], f"{what}.min_confidence"),
                              _int(d["max_results"], f"{what}.max_results", 1, 100), expected)
        return Condition(query, label)
    except (InvalidQuery, DecisionError, VisionError) as exc:
        raise MacroFormatError(f"{what}: {exc}") from None


def _lines(value: object, what: str) -> tuple[TextLine, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TEXT_LINES:
        raise MacroFormatError(f"{what} must be a list of 1 to {MAX_TEXT_LINES} lines.")
    out = []
    for i, item in enumerate(value):
        d = _obj(item, {"text", "present"}, f"{what}[{i}]")
        out.append(TextLine(_str(d["text"], f"{what}[{i}].text", MAX_TEXT), _bool(d["present"], f"{what}[{i}].present")))
    return tuple(out)


def _run_macro(value: object, what: str, *, placed: bool = False) -> RunMacro:
    d = _obj(value, {"type", "name", "recording"} | ({"at"} if placed else set()), f"{what} (run_macro)")
    at = _position(d["at"], f"{what}.at") if placed else None
    try:
        return RunMacro(Macro(recording_from_dict(d["recording"]), _str(d["name"], f"{what}.name", 100)), at=at)
    except (RecordingFormatError, ValueError, TypeError, DecisionError) as exc:
        raise MacroFormatError(f"{what}: {exc}") from None


def _position(value: object, what: str) -> Position:
    if value is None:
        return None
    return _pair(value, what, 0, MAX_POSITION)


def _tap_match(value: object, what: str, *, placed: bool = False) -> TapMatch:
    d = _obj(value, {"type", "op", "offset"} | ({"at"} if placed else set()), f"{what} (tap_match)")
    if d["op"] not in {op.value for op in (ActionOp.TAP, ActionOp.DOUBLE_TAP, ActionOp.SECONDARY_TAP)}:
        raise MacroFormatError(f"{what}.op must be tap, double_tap or secondary_tap.")
    at = _position(d["at"], f"{what}.at") if placed else None
    try:
        return TapMatch(ActionOp(d["op"]), _pair(d["offset"], f"{what}.offset", -10_000, 10_000), at)
    except DecisionError as exc:
        raise MacroFormatError(f"{what}: {exc}") from None


def _branch(value: object, what: str):
    if value is None:
        return None
    kind = value.get("type") if isinstance(value, dict) else None
    if kind == "run_macro":
        return _run_macro(value, what)
    if kind == "tap_match":
        return _tap_match(value, what)
    raise MacroFormatError(f"{what}: unknown branch type {kind!r}; allowed: run_macro, tap_match, or null.")


def _step(value: object, what: str, assets: _AssetReader) -> Step:
    kind = value.get("type") if isinstance(value, dict) else None
    if kind == "run_macro":
        return _run_macro(value, what)
    if kind == "wait_until":
        d = _obj(value, {"type", "condition", "timeout_s", "interval_s"}, f"{what} (wait_until)")
        timeout, interval = _num(d["timeout_s"], f"{what}.timeout_s"), _num(d["interval_s"], f"{what}.interval_s")
        if not interval >= MIN_INTERVAL_S:
            raise MacroFormatError(f"{what}.interval_s must be at least {MIN_INTERVAL_S} seconds.")
        condition = _condition(d["condition"], f"{what}.condition", assets)
        try:
            return WaitUntil(condition, timeout, interval)
        except DecisionError as exc:
            raise MacroFormatError(f"{what}: {exc}") from None
    if kind == "if":
        d = _obj(value, {"type", "condition", "then", "else"}, f"{what} (if)")
        condition = _condition(d["condition"], f"{what}.condition", assets)
        try:
            return Decision(condition, _branch(d["then"], f"{what}.then"), _branch(d["else"], f"{what}.else"))
        except DecisionError as exc:
            raise MacroFormatError(f"{what}: {exc}") from None
    raise MacroFormatError(f"{what}: unknown step type {kind!r}; allowed: run_macro, wait_until, if.")


def _step3(value: object, what: str, assets: _AssetReader, *, in_branch: bool = False, depth: int = 0):
    """One block of a version-3 file: a step, or (inside a branch) a click on the match. `depth` is how many IFs
    hold it: an IF deeper than MAX_NESTING is refused right there -- before its branches are read -- so a file
    nested far too deep never takes the reader (and the stack) any further than the rule allows."""
    kind = value.get("type") if isinstance(value, dict) else None
    if kind == "run_macro":
        return _run_macro(value, what, placed=True)
    if kind == "tap_match" and in_branch:
        return _tap_match(value, what, placed=True)
    if kind == "wait_until":
        d = _obj(value, {"type", "condition", "timeout_s", "interval_s", "at"}, f"{what} (wait_until)")
        timeout, interval = _num(d["timeout_s"], f"{what}.timeout_s"), _num(d["interval_s"], f"{what}.interval_s")
        if not interval >= MIN_INTERVAL_S:
            raise MacroFormatError(f"{what}.interval_s must be at least {MIN_INTERVAL_S} seconds.")
        condition = _condition(d["condition"], f"{what}.condition", assets)
        try:
            return WaitUntil(condition, timeout, interval, _position(d["at"], f"{what}.at"))
        except DecisionError as exc:
            raise MacroFormatError(f"{what}: {exc}") from None
    if kind == "if":
        if depth >= MAX_NESTING:
            raise MacroFormatError(f"{what}: IF blocks go at most {MAX_NESTING} levels deep (an IF inside an IF ...).")
        d = _obj(value, {"type", "condition", "then", "else", "at"}, f"{what} (if)")
        condition = _condition(d["condition"], f"{what}.condition", assets)
        branches = []
        for key, name in (("then", THEN), ("else", ELSE)):
            blocks = d[key]
            if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
                raise MacroFormatError(f"{what}.{key} must be a list of at most {MAX_BLOCKS} blocks.")
            branches.append(tuple(_step3(b, f"{what}.{name}[{i}]", assets, in_branch=True, depth=depth + 1)
                                  for i, b in enumerate(blocks)))
        try:
            return Decision(condition, branches[0], branches[1], _position(d["at"], f"{what}.at"))
        except DecisionError as exc:
            raise MacroFormatError(f"{what}: {exc}") from None
    allowed = "run_macro, wait_until, if" + (", tap_match" if in_branch else "")
    raise MacroFormatError(f"{what}: unknown block type {kind!r}; allowed: {allowed}.")


def _parse(text: str) -> object:
    if not isinstance(text, str):
        raise MacroFormatError("A macro file is text.")
    try:
        return loads_strict(text)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MacroFormatError(f"Not valid JSON: {exc}") from None


def _conditional(top: object, assets: _AssetReader) -> ConditionalMacro:
    version = top.get("version") if isinstance(top, dict) else None
    if isinstance(top, dict) and type(version) is int and version == VERSION_3:
        return _conditional3(top, assets)
    top = _obj(top, {"schema", "version", "name", "steps"}, "macro")
    if top["schema"] != SCHEMA or type(top["version"]) is not int or top["version"] != VERSION:
        raise MacroFormatError(f"Unsupported schema/version: {top['schema']!r} {top['version']!r}.")
    if not isinstance(top["steps"], list) or not 1 <= len(top["steps"]) <= MAX_STEPS:
        raise MacroFormatError(f"steps must be a list of 1 to {MAX_STEPS} steps.")
    steps = tuple(_step(s, f"step {i}", assets) for i, s in enumerate(top["steps"]))
    return ConditionalMacro(_str(top["name"], "name", 100), steps)


def _conditional3(top: dict, assets: _AssetReader) -> ConditionalMacro:
    has_screen, screen = "screen" in top, top.get("screen")   # only a script with a size, or that does not adapt
    top = _obj({k: v for k, v in top.items() if k != "screen"}, {"schema", "version", "name", "start", "end",
                                                                  "steps"}, "macro")
    size, adapt = None, True
    if has_screen:
        screen = _obj(screen, {"size", "adapt"}, "screen")
        if not isinstance(screen["adapt"], bool):
            raise MacroFormatError("screen.adapt must be true or false.")
        size, adapt = screen["size"], screen["adapt"]
        if size is None and adapt:
            raise MacroFormatError("screen is written only with a size, or with adapt false.")
    if top["schema"] != SCHEMA:
        raise MacroFormatError(f"Unsupported schema: {top['schema']!r}.")
    if not isinstance(top["steps"], list) or not 1 <= len(top["steps"]) <= MAX_STEPS:
        raise MacroFormatError(f"steps must be a list of 1 to {MAX_STEPS} steps.")
    start = _obj(top["start"], {"delay_s", "at"}, "start")
    end = _obj(top["end"], {"at"}, "end")
    steps = tuple(_step3(s, f"step {i}", assets) for i, s in enumerate(top["steps"]))
    return ConditionalMacro(_str(top["name"], "name", 100), steps, _num(start["delay_s"], "start.delay_s"),
                            _position(start["at"], "start.at"), _position(end["at"], "end.at"),
                            tuple(size) if isinstance(size, list) else size, adapt)


def conditional_from_json(text: str, asset_dir: str | Path | None = None) -> ConditionalMacro:
    """Read a version-2 macro. `asset_dir` is the macro's own asset folder (see `asset_dir_for`)."""
    return _conditional(_parse(text), _AssetReader(Path(asset_dir) if asset_dir is not None else None))


def _is_v1(top: object) -> bool:
    version = top.get("version") if isinstance(top, dict) else None
    return isinstance(top, dict) and top.get("schema") == SCHEMA and not isinstance(version, bool) and version == 1


def load_macro_file(path: str | Path) -> Macro | ConditionalMacro:
    """Read a macro file of either version. Never writes anything."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MacroFormatError(f"The macro could not be read: {exc}") from None
    top = _parse(text)
    if _is_v1(top):
        return v1.macro_from_json(text)                      # the v1 code path, byte for byte as before
    from .graph_format import is_version_4, script_from_json      # version 4: blocks joined by arrows
    if is_version_4(top):
        return script_from_json(text, asset_dir_for(p))
    return conditional_from_json(text, asset_dir_for(p))


@dataclass(frozen=True)
class MacroSummary:
    """What a list of macros shows about one file. `size` is the macro in its own unit: actions for v1, steps for
    v2. `provider_id`/`target_id`/`events` exist only for v1, which is recorded for one target; `assets` only for
    v2, as the template names its conditions refer to."""
    version: int
    name: str
    size: int
    provider_id: str = ""
    target_id: str = ""
    events: int = 0
    assets: tuple[str, ...] = ()


def summarize_macro_text(text: str) -> MacroSummary:
    """Validate a macro file's text exactly as a load would -- every type, key, range and asset NAME -- without
    opening a single template asset. That is what lets a library list many macros cheaply: the image files are
    read (and hash-checked) only when one macro is actually opened or run, by `load_macro_file`."""
    top = _parse(text)
    if _is_v1(top):
        macro = v1.macro_from_json(text)
        return MacroSummary(1, macro.name, len(macro.actions), macro.target.provider_id, macro.target.target_id,
                            len(macro.recording))
    from .graph_format import VERSION as VERSION_4, is_version_4, summarize_script
    if is_version_4(top):
        name, blocks, pictures = summarize_script(top)
        return MacroSummary(VERSION_4, name, blocks, assets=pictures)
    names = _AssetNames()
    macro = _conditional(top, names)
    return MacroSummary(top["version"], macro.name, len(macro.steps), assets=tuple(names.names))


# ---- writing --------------------------------------------------------------------------------------------
def _region_out(region: Roi | None):
    return None if region is None else {"x": region.x, "y": region.y, "width": region.width,
                                        "height": region.height}


def _condition_out(c: Condition, assets: dict[str, bytes]) -> dict:
    q = c.query
    out: dict = {"expected": q.expected, "label": c.label}
    if isinstance(q, PixelQuery):
        out.update(type="pixel", point=list(q.point), color=list(q.color), tolerance=q.tolerance)
    elif isinstance(q, ColorRegionQuery):
        out.update(type="color", color=list(q.color), tolerance=q.tolerance, region=_region_out(q.region),
                   min_ratio=q.min_ratio)
    elif isinstance(q, TemplateQuery):
        name = asset_name(q.template)
        assets[name] = encode_png(q.template)
        out.update(type="template", asset=name, region=_region_out(q.region), min_confidence=q.min_confidence,
                   max_results=q.max_results)
        if q.any_size:
            out["any_size"] = True
    elif isinstance(q, TextLinesQuery):
        out.update(type="text_lines", lines=[{"text": line.text, "present": line.present} for line in q.lines],
                   ignore_case=q.ignore_case, region=_region_out(q.region), language=q.language,
                   min_confidence=q.min_confidence)
    else:
        out.update(type="text", text=q.text, match=q.match, ignore_case=q.ignore_case, region=_region_out(q.region),
                   language=q.language, min_confidence=q.min_confidence, max_results=q.max_results)
    return out


def _run_macro_out(r: RunMacro) -> dict:
    if r.allow_different_target:
        raise MacroFormatError("allow_different_target is a run-time choice and is never stored in a file.")
    return {"type": "run_macro", "name": r.macro.name, "recording": recording_to_dict(r.macro.recording)}


def _branch_out(items: tuple) -> dict | None:
    if not items:
        return None
    (b,) = items                                   # version 2: a branch is one block at most
    if isinstance(b, RunMacro):
        return _run_macro_out(b)
    return {"type": "tap_match", "op": b.op.value, "offset": list(b.offset)}


def needs_version_3(macro: ConditionalMacro) -> bool:
    """Whether a script uses anything version 2 cannot hold: a start delay, a block's place on the canvas, a branch
    of more than one block, or a branch holding a WAIT UNTIL or an IF."""
    if macro.start_delay_s or macro.start_at is not None or macro.end_at is not None:
        return True
    if macro.size is not None or not macro.adapt:
        return True
    for _path, item in walk(macro.steps):
        if item.at is not None:
            return True
        if isinstance(item, Decision):
            for items in (item.then, item.otherwise):
                if len(items) > 1 or any(not isinstance(b, (RunMacro, TapMatch)) for b in items):
                    return True
    return False


def _wait_out(step: WaitUntil, assets: dict[str, bytes]) -> dict:
    if step.interval_s < MIN_INTERVAL_S:
        raise MacroFormatError(f"interval_s must be at least {MIN_INTERVAL_S} seconds: a stored wait looks "
                               "at most that often.")
    return {"type": "wait_until", "condition": _condition_out(step.condition, assets),
            "timeout_s": step.timeout_s, "interval_s": step.interval_s}


def _block3_out(item, assets: dict[str, bytes]) -> dict:
    at = None if item.at is None else list(item.at)
    if isinstance(item, RunMacro):
        out = _run_macro_out(item)
    elif isinstance(item, TapMatch):
        out = {"type": "tap_match", "op": item.op.value, "offset": list(item.offset)}
    elif isinstance(item, WaitUntil):
        out = _wait_out(item, assets)
    else:
        out = {"type": "if", "condition": _condition_out(item.condition, assets),
               "then": [_block3_out(b, assets) for b in item.then],
               "else": [_block3_out(b, assets) for b in item.otherwise]}
    out["at"] = at
    return out


def conditional_to_json(macro: ConditionalMacro) -> tuple[str, dict[str, bytes]]:
    """The file text and the template assets it refers to ({name: PNG bytes}): version 2 whenever that can hold the
    script, version 3 otherwise (see the module docstring)."""
    if not isinstance(macro, ConditionalMacro):
        raise MacroFormatError("conditional_to_json needs a ConditionalMacro.")
    assets: dict[str, bytes] = {}
    steps = []
    if needs_version_3(macro):
        steps = [_block3_out(step, assets) for step in macro.steps]
        data = {"schema": SCHEMA, "version": VERSION_3, "name": macro.name,
                "start": {"delay_s": macro.start_delay_s,
                          "at": None if macro.start_at is None else list(macro.start_at)},
                "end": {"at": None if macro.end_at is None else list(macro.end_at)}, "steps": steps}
        if macro.size is not None or not macro.adapt:
            data["screen"] = {"size": None if macro.size is None else list(macro.size), "adapt": macro.adapt}
        try:
            text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        except ValueError as exc:
            raise MacroFormatError(str(exc)) from None
        return text, assets
    for step in macro.steps:
        if isinstance(step, RunMacro):
            steps.append(_run_macro_out(step))
        elif isinstance(step, WaitUntil):
            steps.append(_wait_out(step, assets))
        else:
            steps.append({"type": "if", "condition": _condition_out(step.condition, assets),
                          "then": _branch_out(step.then), "else": _branch_out(step.otherwise)})
    data = {"schema": SCHEMA, "version": VERSION, "name": macro.name, "steps": steps}
    try:
        text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    except ValueError as exc:
        raise MacroFormatError(str(exc)) from None
    return text, assets


def referenced_assets(macro) -> frozenset[str]:
    """The template file names a saved copy of `macro` (a script of version 2, 3 or 4) refers to (what its asset
    folder must hold)."""
    return frozenset(script_text(macro)[1])


def script_text(macro) -> tuple[str, dict[str, bytes]]:
    """The file text and pictures of a script of any version: `conditional_to_json`, or version 4's own."""
    from .graph_format import Script, script_to_json
    if isinstance(macro, Script):
        return script_to_json(macro)
    return conditional_to_json(macro)


class _MemoryAssets(_AssetReader):
    """The templates of a save that has not happened yet, held in memory: the same name, size and hash checks
    as a load from disk, applied to the exact bytes a save would write."""

    def __init__(self, assets: dict[str, bytes]) -> None:
        super().__init__(None)
        self._assets = assets

    def read(self, name: object) -> Bitmap:
        name = _check_asset_name(name)
        png = self._assets.get(name)
        if png is None:
            raise MacroFormatError(f"Template asset {name} is not part of this macro.")
        try:
            bitmap = decode_png(png)
        except VisionError as exc:
            raise MacroFormatError(f"Template asset {name} is not a valid image: {exc}") from None
        if not (bitmap.width <= MAX_TEMPLATE_SIDE and bitmap.height <= MAX_TEMPLATE_SIDE):
            raise MacroFormatError(f"Template asset {name} is {bitmap.width}x{bitmap.height}; a template is at "
                                   f"most {MAX_TEMPLATE_SIDE}x{MAX_TEMPLATE_SIDE}.")
        if asset_name(bitmap) != name:
            raise MacroFormatError(f"Template asset {name} does not contain the image it was saved with.")
        return bitmap


def validate_macro(macro: ConditionalMacro) -> None:
    """Refuse anything a save would write that a load would not read back as the same macro.

    The writer only maps objects to JSON; the reader is where every rule of the file lives (key sets, ranges,
    text lengths, language tags, the polling floor, asset names and sizes). Running the reader over what the writer
    produced -- templates included, from memory -- applies all of them in one place, with the reader's own words,
    so an editor never needs a second copy of those rules and can never save a file it could not open again."""
    from .graph_format import Script, validate_script
    if isinstance(macro, Script):
        validate_script(macro)
        return
    text, assets = conditional_to_json(macro)
    again = _conditional(_parse(text), _MemoryAssets(assets))
    if again != macro:
        raise MacroFormatError("This macro would not read back exactly as it would be saved.")


def _write_new(path: Path, data: bytes, overwrite: bool) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="save-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)                               # never replaces an existing file, even in a race
            except FileExistsError:
                raise MacroFormatError(f"{path.name} already exists.") from None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def save_macro_file(macro: Macro | ConditionalMacro, path: str | Path, *, overwrite: bool = False) -> Path:
    """Write a macro. A v1 `Macro` is written as v1 (never upgraded silently); a `ConditionalMacro` as v2 or v3,
    its templates first, into the macro's own asset folder. An existing asset with the same name must hold the
    same image (names are content hashes), so saving never changes an asset another save relies on."""
    p = Path(path)
    asset_dir = asset_dir_for(p)
    if isinstance(macro, Macro):
        _write_new(p, v1.macro_to_json(macro).encode("utf-8"), overwrite)
        return p
    from .graph_format import Script, save_script_file
    if isinstance(macro, Script):
        return save_script_file(macro, p, overwrite=overwrite)
    text, assets = conditional_to_json(macro)
    if p.exists() and not overwrite:
        raise MacroFormatError(f"{p.name} already exists.")
    if assets:
        asset_dir.mkdir(parents=True, exist_ok=True)
        for name, png in sorted(assets.items()):
            target = asset_dir / name
            if target.exists():
                if target.read_bytes() != png:
                    raise MacroFormatError(f"Template asset {name} already exists with different content.")
                continue
            _write_new(target, png, False)
    _write_new(p, text.encode("utf-8"), overwrite)
    return p
