"""Editing a version-2 macro: its steps as a list, with the same undo / redo / unsaved rules as a recorded one.

A v2 macro is `app.decide.format.ConditionalMacro`: a name and 1..MAX_STEPS steps, each a `WaitUntil`, a `Decision`
(IF / THEN / ELSE) or a `RunMacro`. Those are immutable domain objects whose constructors validate themselves, so
this module never holds a second copy of what is allowed. It does two things only:

  * turns what a person typed (text in fields) into arguments for those constructors -- the only rule here is "this
    field needs a whole number" -- and turns a refusal from any of them into an `EditError` the view can show;
  * keeps the document: every edit is a pure function returning a NEW `ConditionalMacro`, and `FlowSession.apply`
    accepts it only once `validate_macro` agrees it would be saved and read back as exactly that macro. That one
    call is where every rule of the file format lives (ranges, lengths, the polling floor, asset names), so a macro
    this editor holds can always be saved, and a saved one can always be opened again.

Templates are images (`Bitmap`) inside the domain objects; nothing here writes a file. The library saves a macro
through the v2 serializer, which stores each template as `<macro>.assets/template-<sha256>.png`.

Blocks are addressed by their PATH: `(2,)` is the third step, `(2, "then", 0)` the first block of its THEN branch,
`(2, "then", 0, "else", 1)` deeper still. An insertion point -- a "slot" -- is a path whose last number is where the
new block goes in that list. A branch holds the blocks a script holds (WAIT UNTIL, IF, a recorded macro) and, as its
first block only, a click on the match (since 2026-10-19; before, a branch was one action at most). Where a block is
drawn is part of it (`at`, see app/decide/position.py): moving a block is an edit like any other, undone the same way.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from app.decide import (ELSE, MAX_BLOCKS, MAX_NESTING, MAX_POSITION, MAX_START_DELAY_S, MAX_STEPS, THEN, Condition,
                        ConditionalMacro, Decision, DecisionError, RunMacro, TapMatch, WaitUntil, count_blocks, walk)
from app.decide.format import MacroFormatError, validate_macro
from app.gui.macro_edit import UNDO_DEPTH, EditError
from app.observe import (CONTAINS, EXACT, MAX_TEXT_LINES, ColorRegionQuery, InvalidQuery, PixelQuery, TemplateQuery,
                         TextLine, TextLinesQuery, TextQuery, scaled_part)
from app.providers.size import SizeChanged, fit
from app.playback.macro import ActionOp, Macro
from app.providers.recording import Recording
from app.vision.bitmap import Bitmap, VisionError
from app.vision.roi import Roi

WAIT, IF, RUN = "wait_until", "if", "run_macro"
STEP_KINDS = (WAIT, IF, RUN)
PIXEL, COLOR, TEMPLATE, TEXT = "pixel", "color", "template", "text"
CONDITION_KINDS = (PIXEL, COLOR, TEMPLATE, TEXT)
OFFERED_KINDS = (PIXEL, TEMPLATE, TEXT)         # what a new condition can be; a saved colour-area one still opens
NONE, TAP = "none", "tap_match"
BRANCH_KINDS = (NONE, TAP, RUN)
TAP_OPS = (ActionOp.TAP, ActionOp.DOUBLE_TAP, ActionOp.SECONDARY_TAP)
TEXT_MATCHES = (CONTAINS, EXACT)
Step = WaitUntil | Decision | RunMacro
_PROBE_RECORDING = Recording("probe", "probe", ())       # only to ask the format whether a name is acceptable


def _rejected(exc: BaseException) -> EditError:
    """A refusal from a constructor or from the file format's own reader, in its own words."""
    return EditError(f"The value was not accepted: {exc}", "err_flow_rejected", reason=str(exc))


def validate(macro: ConditionalMacro) -> ConditionalMacro:
    try:
        validate_macro(macro)
    except (MacroFormatError, InvalidQuery, DecisionError, VisionError, ValueError) as exc:
        raise _rejected(exc) from None
    return macro


def kind_of(step) -> str:
    """WAIT, IF, RUN -- or TAP for a click on the match, which only a branch holds."""
    if isinstance(step, TapMatch):
        return TAP
    return WAIT if isinstance(step, WaitUntil) else IF if isinstance(step, Decision) else RUN


def condition_kind(condition: Condition) -> str:
    query = condition.query
    return {PixelQuery: PIXEL, ColorRegionQuery: COLOR, TemplateQuery: TEMPLATE, TextQuery: TEXT,
            TextLinesQuery: TEXT}[type(query)]


@dataclass(frozen=True)
class EmptyScript:
    """A new script before its first step: a name and no steps (its Start block may already have a delay and a
    place). It exists only in the editor -- the file format holds 1..MAX_STEPS steps, so an empty script is never
    written; the first inserted step turns it into an ordinary `ConditionalMacro`, and only that can be saved."""
    name: str
    steps: tuple = ()
    start_delay_s: float = 0
    start_at: tuple | None = None
    end_at: tuple | None = None
    size: tuple | None = None
    adapt: bool = True


def new_script(name: str) -> EmptyScript:
    """The starting point of a new script, its name checked by the format's own rule (via a one-step probe)."""
    name = name.strip()
    if not name:
        raise EditError("A script needs a name.", "err_script_name")
    try:
        ConditionalMacro(name, (RunMacro(Macro(_PROBE_RECORDING)),))
    except (MacroFormatError, ValueError, DecisionError) as exc:
        raise _rejected(exc) from None
    return EmptyScript(name)


# ---- paths: where a block is ---------------------------------------------------------------------------------
START, END = ("start",), ("end",)          # the Start and End blocks: places, not steps
COPY_SHIFT = 30                            # a copy is drawn this far right and down of its original


def _path(where) -> tuple:
    """A block's path (or a slot); a plain number is a top-level step, as before paths existed."""
    if isinstance(where, bool) or not isinstance(where, (int, tuple, list)):
        raise EditError("That block is no longer in the script.", "err_step_gone")
    return (where,) if isinstance(where, int) else tuple(where)


def _blocks_of(document, list_path: tuple) -> tuple:
    """The blocks of the list at `list_path`: () is the script's steps, (2, "then") step 2's THEN branch."""
    if not list_path:
        return tuple(document.steps)
    owner = item_at(document, list_path[:-1])
    if not isinstance(owner, Decision) or list_path[-1] not in (THEN, ELSE):
        raise EditError("That block is no longer in the script.", "err_step_gone")
    return owner.branch(list_path[-1])


def item_at(document, path) -> object:
    """The block at `path`; EditError if there is none (the script changed meanwhile)."""
    path = _path(path)
    blocks = tuple(document.steps)
    item = None
    for at in range(0, len(path), 2):
        index = path[at]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(blocks):
            raise EditError("That block is no longer in the script.", "err_step_gone")
        item = blocks[index]
        if at + 1 < len(path):
            if not isinstance(item, Decision) or path[at + 1] not in (THEN, ELSE):
                raise EditError("That block is no longer in the script.", "err_step_gone")
            blocks = item.branch(path[at + 1])
    if item is None:
        raise EditError("That block is no longer in the script.", "err_step_gone")
    return item


def _document(document, steps: tuple, **changes):
    """The same script with other steps (and other Start/End settings): a `ConditionalMacro` when it has steps."""
    values = dict(name=document.name, start_delay_s=document.start_delay_s, start_at=document.start_at,
                  end_at=document.end_at, size=document.size, adapt=document.adapt)
    values.update(changes)
    if not steps:
        raise EditError("A macro keeps at least one step.", "err_flow_last_step")
    try:
        return ConditionalMacro(steps=tuple(steps), **values)
    except (MacroFormatError, ValueError) as exc:
        raise _rejected(exc) from None


def _with_blocks(document, list_path: tuple, blocks: list):
    """The script with the list at `list_path` replaced by `blocks` -- every IF on the way rebuilt around it (each
    constructor checks itself again: a click on the match that is no longer first is refused, say)."""
    blocks = tuple(blocks)
    try:
        while list_path:
            owner_path, branch = list_path[:-2] + (list_path[-2],), list_path[-1]
            owner = item_at(document, owner_path)
            owner = replace(owner, then=blocks) if branch == THEN else replace(owner, otherwise=blocks)
            container = list(_blocks_of(document, owner_path[:-1]))
            container[owner_path[-1]] = owner
            blocks, list_path = tuple(container), owner_path[:-1]
    except (DecisionError, ValueError, MacroFormatError) as exc:
        raise _rejected(exc) from None
    if len(blocks) > MAX_STEPS:
        raise EditError(f"A macro has at most {MAX_STEPS} steps.", "err_flow_too_many", count=MAX_STEPS)
    return _document(document, blocks)


def _with_steps(document: ConditionalMacro, steps: list[Step]) -> ConditionalMacro:
    if len(steps) > MAX_STEPS:
        raise EditError(f"A macro has at most {MAX_STEPS} steps.", "err_flow_too_many", count=MAX_STEPS)
    return _document(document, tuple(steps))


# ---- pure edits: each returns a NEW macro or raises EditError and changes nothing ----------------------------
def rename(document: ConditionalMacro, name: str) -> ConditionalMacro:
    try:
        return replace(document, name=name.strip())
    except (MacroFormatError, ValueError) as exc:
        raise _rejected(exc) from None


def insert_item(document, slot, item):
    """`item` put at `slot` -- its list, and its position in that list."""
    slot = _path(slot)
    if isinstance(document, EmptyScript) and slot[:-1] == ():
        if slot != (0,):
            raise EditError("That position is no longer in the macro.", "err_bad_position")
        return _document(document, (item,))
    blocks = list(_blocks_of(document, slot[:-1]))
    index = slot[-1]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(blocks):
        raise EditError("That position is no longer in the macro.", "err_bad_position")
    blocks.insert(index, item)
    return _with_blocks(document, slot[:-1], blocks)


def replace_item(document, path, item):
    path = _path(path)
    item_at(document, path)
    blocks = list(_blocks_of(document, path[:-1]))
    blocks[path[-1]] = item
    return _with_blocks(document, path[:-1], blocks)


def delete_item(document, path):
    path = _path(path)
    item_at(document, path)
    blocks = list(_blocks_of(document, path[:-1]))
    del blocks[path[-1]]
    return _with_blocks(document, path[:-1], blocks)


def duplicate_item(document, path):
    """The copy goes right after the original, in the same list; drawn a little beside it when the original has a
    place of its own. Blocks are immutable values: nothing can edit one "through" the other."""
    path = _path(path)
    item = item_at(document, path)
    copy = _shifted(item, COPY_SHIFT, COPY_SHIFT)
    blocks = list(_blocks_of(document, path[:-1]))
    blocks.insert(path[-1] + 1, copy)
    return _with_blocks(document, path[:-1], blocks)


def move_item(document, path, delta: int):
    """One place earlier or later in its own list (where it is DRAWN does not change: that is `place`)."""
    path = _path(path)
    item_at(document, path)
    if delta not in (-1, 1):
        raise EditError("A step moves one position at a time.", "err_step_one")
    blocks = list(_blocks_of(document, path[:-1]))
    index, other = path[-1], path[-1] + delta
    if not 0 <= other < len(blocks):
        raise EditError("The step is already at the end of the macro.", "err_step_end")
    blocks[index], blocks[other] = blocks[other], blocks[index]
    return _with_blocks(document, path[:-1], blocks)


def _shifted(item, dx: int, dy: int):
    """The block drawn `dx`, `dy` further (kept on the scene) -- an IF with every block its branches hold that has
    a place of its own, so a copy never lies exactly over the original's blocks. Unplaced blocks stay unplaced."""
    if isinstance(item, Decision):
        item = replace(item, then=tuple(_shifted(b, dx, dy) for b in item.then),
                       otherwise=tuple(_shifted(b, dx, dy) for b in item.otherwise))
    if item.at is None:
        return item
    return replace(item, at=on_scene((item.at[0] + dx, item.at[1] + dy)))


def place(document, positions: dict):
    """Blocks drawn where `positions` says: {path: (x, y) or None} -- the Start and End blocks too (`START`,
    `END`). Nothing about what the script does changes; one edit, however many blocks it moves."""
    changes = {}
    for key in (START, END):
        if key in positions:
            changes["start_at" if key == START else "end_at"] = on_scene(positions[key])
    deepest_first = sorted((p for p in positions if p not in (START, END)), key=len, reverse=True)
    for path in deepest_first:                        # children first: an IF is rebuilt around its moved blocks
        item = item_at(document, path)
        moved = replace(item, at=on_scene(positions[path]))
        blocks = list(_blocks_of(document, path[:-1]))
        blocks[path[-1]] = moved
        document = _with_blocks(document, path[:-1], blocks)
    if changes:
        document = _restart(document, **changes)
    return document


def on_scene(value) -> tuple | None:
    """A place as a script keeps it: two whole numbers, each kept on the scene (0..MAX_POSITION) -- a block dragged
    or copied past an edge stops at it, and a place is never one the file would refuse. None: placed automatically."""
    if value is None:
        return None
    try:
        x, y = (int(round(v)) for v in value)
    except (TypeError, ValueError, OverflowError):
        raise EditError("A place is two numbers.", "err_flow_whole", field="field_x") from None
    return (min(MAX_POSITION, max(0, x)), min(MAX_POSITION, max(0, y)))


def _restart(document, **changes):
    """Other Start/End settings for a script -- or for a new one that has no step yet."""
    if isinstance(document, EmptyScript):
        try:
            return replace(document, **changes)
        except (ValueError, TypeError) as exc:
            raise _rejected(exc) from None
    return _document(document, tuple(document.steps), **changes)


def arrange(document):
    """Every block placed automatically again ("Arrange automatically"): no block keeps a place of its own."""
    positions = {path: None for path, item in walk(document.steps) if item.at is not None}
    if document.start_at is not None:
        positions[START] = None
    if document.end_at is not None:
        positions[END] = None
    return place(document, positions) if positions else document


def placed(document) -> bool:
    """Whether the script is laid out by hand: some block has a place of its own."""
    return document.start_at is not None or document.end_at is not None \
        or any(item.at is not None for _path, item in walk(document.steps))


def can_insert(document, slot, kind: str) -> bool:
    """Whether a block of `kind` may go at `slot` at all -- asked before offering it in a menu. A click on the
    match only opens the branch whose condition has a match to click (and only once); an IF only so deep."""
    slot = tuple(slot)
    list_path = slot[:-1]
    try:
        blocks = _blocks_of(document, list_path)
    except EditError:
        return False
    if not isinstance(slot[-1], int) or not 0 <= slot[-1] <= len(blocks) or count_blocks(document.steps) >= MAX_BLOCKS:
        return False
    if not list_path and len(blocks) >= MAX_STEPS:
        return False
    if list_path and slot[-1] == 0 and blocks and isinstance(blocks[0], TapMatch):
        return False                                  # the click stays first: it clicks right after the look
    if kind == TAP:
        if not list_path or slot[-1] != 0:
            return False
        owner = item_at(document, list_path[:-1])
        return list_path[-1] == owner.found_side() and owner.condition.points_at_match
    if kind == IF:
        return len(list_path) // 2 + 1 <= MAX_NESTING
    return kind in (WAIT, RUN)


def insert_placed(document, slot, item, room: dict | None = None):
    """`item` put at `slot`, after the blocks in `room` ({path: (x, y)}, paths as they are BEFORE the insert) moved
    out of its way: one edit, undone in one step."""
    if room:
        document = place(document, room)
    return insert_item(document, slot, item)


def set_start(document, seconds, adapt: bool):
    """The Start block's settings in one edit: the delay and "Adapt to the size"."""
    return set_adapt(set_start_delay(document, seconds), adapt)


def set_adapt(document, adapt: bool):
    """"Adapt to the size" on or off: whether the script's points and areas follow a change of the target's size."""
    return _restart(document, adapt=bool(adapt))


def fit_condition(document, condition, picked: dict):
    """`condition` in the script's own size, and the size the script has after it. `picked` says which parts of it
    (`PARTS`: the point, the area, the picture) were just picked, each on a picture of what size; only those are
    scaled into the script's size (the same proportions) -- the others already are in it; parts the condition no
    longer has (picked before its kind was changed) count for nothing. The first ones picked give the script its
    size; other proportions are refused (the script's areas would not all be in one space), and so is a picture that
    would be too small. A script that runs as is asks everything as it is stored, so a part picked on another size
    is refused there: scaled it would be asked elsewhere, unscaled it would not be in the script's size."""
    query = condition.query
    picked = {part: tuple(size) for part, size in (picked or {}).items() if size is not None and hasattr(query, part)}
    if not picked:
        return condition, None
    own = tuple(document.size) if document.size is not None else next(iter(picked.values()))
    for part, size in picked.items():
        if size == own:
            continue
        if not document.adapt:
            raise EditError(f"This script runs as is, at {own[0]}x{own[1]}; this was picked at {size[0]}x{size[1]}.",
                            "err_flow_as_is_other_size", made=f"{own[0]}x{own[1]}", now=f"{size[0]}x{size[1]}")
        try:
            query = scaled_part(query, part, *fit(size, own), own[0], own[1])
        except SizeChanged as exc:
            raise EditError(f"Picked on a screen of other proportions than the script's other areas: {exc}",
                            "err_flow_other_proportions", made=f"{own[0]}x{own[1]}",
                            now=f"{size[0]}x{size[1]}") from None
        except InvalidQuery as exc:
            raise EditError(str(exc), "err_flow_picture_too_small", made=f"{own[0]}x{own[1]}") from None
    return replace(condition, query=query), own


def fit_to_script(document, step, picked: dict):
    """`step` with its condition in the script's own size (`fit_condition`), and the size the script has after it."""
    if not picked or not hasattr(step, "condition"):
        return step, None
    condition, size = fit_condition(document, step.condition, picked)
    return replace(step, condition=condition), size


def probe_condition(document, condition, picked: dict):
    """(condition, picked_on) for testing `condition` now exactly as a run of the script `document` would ask it:
    in the script's size, scaled to the screen's when the script adapts."""
    if not isinstance(document, ConditionalMacro):
        return condition, None
    condition, size = fit_condition(document, condition, picked)
    size = tuple(document.size) if document.size is not None else size
    return condition, (size if document.adapt else None)


def sized(edit):
    """`edit`, and the script given `size` (where its areas were just picked) if it has none yet: one edit."""
    def apply(document, *args, size=None, **kwargs):
        new = edit(document, *args, **kwargs)
        if size is None or new.size is not None:
            return new
        return _restart(new, size=tuple(size))
    return apply


def set_start_delay(document, seconds):
    """How long the run waits before its first step (0 = not at all), at most MAX_START_DELAY_S."""
    value = parse_float(seconds, "field_start_delay")
    if not 0 <= value <= MAX_START_DELAY_S:
        raise EditError(f"The start delay is 0 to {MAX_START_DELAY_S:g} seconds.", "err_flow_start_delay",
                        hours=round(MAX_START_DELAY_S / 3600))
    value = int(value) if value == int(value) else value
    return _restart(document, start_delay_s=value)


# the names the editor has always used: a number is a top-level step, a tuple a block anywhere
def insert_step(document, at, step):
    return insert_item(document, at, step)


def replace_step(document, index, step):
    return replace_item(document, index, step)


def delete_step(document, index):
    return delete_item(document, index)


def duplicate_step(document, index):
    return duplicate_item(document, index)


def move_step(document, index, delta: int):
    return move_item(document, index, delta)


class FlowSession:
    """One open v2 editor. Same interface as `macro_edit.EditSession`, so the controller and the editor screen
    treat both the same way: `apply`, `undo`, `redo`, `dirty`, `macro()`, `mark_saved()`. It may also start from an
    `EmptyScript` (a new script): then `macro()` refuses until a step has been added, so nothing empty is saved."""

    def __init__(self, macro: ConditionalMacro | EmptyScript) -> None:
        if not isinstance(macro, (ConditionalMacro, EmptyScript)):
            raise EditError("This is not a version-2 macro.", "err_flow_not_v2")
        self.document = macro
        self._saved = macro
        self._undo: list[ConditionalMacro] = []
        self._redo: list[ConditionalMacro] = []

    @property
    def dirty(self) -> bool:
        return self.document != self._saved

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def apply(self, edit, *args, **kwargs) -> None:
        new = edit(self.document, *args, **kwargs)                # raises EditError: nothing below runs
        if isinstance(new, ConditionalMacro):
            new = validate(new)
        elif not isinstance(new, EmptyScript):                    # a new script's own settings, before any step
            raise EditError("This is not a version-2 macro.", "err_flow_not_v2")
        if new == self.document:
            return
        self._undo.append(self.document)
        del self._undo[:-UNDO_DEPTH]
        self._redo.clear()
        self.document = new

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.document)
        self.document = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.document)
        self.document = self._redo.pop()
        return True

    @property
    def empty(self) -> bool:
        return not self.document.steps

    def macro(self) -> ConditionalMacro:
        if not isinstance(self.document, ConditionalMacro):
            raise EditError("Add at least one step before saving the script.", "err_script_empty")
        return self.document


    def mark_saved(self) -> None:
        self._saved = self.document

    def rename(self, name: str) -> None:
        """Another name for the whole session -- its undo and redo states too, so undoing an edit never brings back
        a name that was given up (one a file has taken, say). Raises EditError, changing nothing, for a name the
        format refuses. Still unsaved afterwards: only a save makes it a file."""
        renamed = rename(self.document, name) if isinstance(self.document, ConditionalMacro) else new_script(name)

        def again(document):
            return replace(document, name=renamed.name)
        self._undo = [again(document) for document in self._undo]
        self._redo = [again(document) for document in self._redo]
        self.document = renamed


# ---- what a person typed -> the constructors' arguments ---------------------------------------------------
def parse_int(text: object, field_key: str) -> int:
    try:
        value = str(text).strip()
        if not value.lstrip("+-").isdigit():
            raise ValueError
        return int(value)
    except ValueError:
        raise EditError(f"{field_key} must be a whole number.", "err_flow_whole", field=field_key) from None


def parse_float(text: object, field_key: str) -> float:
    try:
        value = float(str(text).strip().replace(",", "."))
    except ValueError:
        raise EditError(f"{field_key} must be a number.", "err_flow_number", field=field_key) from None
    if not math.isfinite(value):
        raise EditError(f"{field_key} must be a number.", "err_flow_number", field=field_key)
    return value


def parse_color(text: object) -> tuple[int, int, int]:
    """"255, 0, 0" or "#ff0000". Channel ranges are the query's to check."""
    raw = str(text).strip()
    if raw.startswith("#") and len(raw) == 7:
        try:
            return int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16)
        except ValueError:
            pass
    parts = [p for p in raw.replace(";", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 3:
        raise EditError("A colour is three numbers (red, green, blue) or #rrggbb.", "err_flow_color")
    return tuple(parse_int(p, "field_color") for p in parts)                    # type: ignore[return-value]


def format_color(color: tuple[int, int, int]) -> str:
    return ", ".join(str(c) for c in color)


@dataclass
class ConditionForm:
    """The condition as the dialog holds it: text fields, plus the two things that are not text -- the region
    and the template image, which come from the screen pickers, never from a typed path -- and, for text, its lines:
    (text, whether it must be there)."""
    kind: str = TEXT
    expected: bool = True
    label: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    region: Roi | None = None
    template: Bitmap | None = None
    lines: list[tuple[str, bool]] = field(default_factory=list)
    kept: TextQuery | None = None             # a text saved before text lines: kept as it is until edited
    ignore_case: bool = True                  # text lines: never offered (new ones ignore case); a file's kept


def condition_form(condition: Condition | None) -> ConditionForm:
    """An existing condition, back into fields (so editing starts from what is there)."""
    if condition is None:
        return ConditionForm(fields={"language": "", "min_confidence": "0"}, lines=[("", True)])
    q = condition.query
    form = ConditionForm(condition_kind(condition), q.expected, condition.label)
    if isinstance(q, PixelQuery):
        form.fields = {"x": str(q.point[0]), "y": str(q.point[1]), "color": format_color(q.color),
                       "tolerance": str(q.tolerance)}
    elif isinstance(q, ColorRegionQuery):
        form.fields = {"color": format_color(q.color), "tolerance": str(q.tolerance),
                       "min_ratio": repr(q.min_ratio)}
        form.region = q.region
    elif isinstance(q, TemplateQuery):
        form.fields = {"min_confidence": repr(q.min_confidence), "max_results": str(q.max_results),
                       "any_size": "1" if q.any_size else "0"}
        form.region, form.template = q.region, q.template
    elif isinstance(q, TextLinesQuery):
        form.fields = {"language": q.language, "min_confidence": repr(q.min_confidence)}
        form.lines = [(line.text, line.present) for line in q.lines]
        form.region, form.ignore_case = q.region, q.ignore_case      # a file's own rules are never turned around
    else:
        # a text saved before text lines existed opens as one line with the condition's own expectation (so "IF it
        # is not there ... ELSE click it" stays one); it is saved as text lines -- looked for anywhere in the
        # region, ignoring case -- only once it is edited
        form.fields = {"language": q.language, "min_confidence": repr(q.min_confidence)}
        form.lines = [(q.text, True)]
        form.region, form.kept = q.region, q
    return form


def _unchanged(form: ConditionForm) -> bool:
    """Whether a text saved before text lines existed was opened and left exactly as it was: then it stays that
    query, its own settings (an exact match, say) untouched -- opening and closing the editor changes nothing."""
    q = form.kept
    return q is not None and form.lines == [(q.text, True)] and form.expected == q.expected \
        and form.region == q.region \
        and form.fields.get("language", "").strip() == q.language \
        and form.fields.get("min_confidence", "") == repr(q.min_confidence)


def text_lines(form: ConditionForm) -> tuple[TextLine, ...]:
    """The lines a person wrote: blank ones left out, at least one, at most MAX_TEXT_LINES."""
    lines = [(text.strip(), present) for text, present in form.lines if text.strip()]
    if not lines:
        raise EditError("Write at least one text to look for.", "err_flow_no_text")
    if len(lines) > MAX_TEXT_LINES:
        raise EditError(f"At most {MAX_TEXT_LINES} lines.", "err_flow_too_many_lines", count=MAX_TEXT_LINES)
    return tuple(TextLine(text, present) for text, present in lines)


def build_condition(form: ConditionForm) -> Condition:
    """Only the fields of the chosen kind are read; the query's own constructor checks every value."""
    f = form.fields
    try:
        if form.kind == PIXEL:
            query = PixelQuery((parse_int(f.get("x", ""), "field_x"), parse_int(f.get("y", ""), "field_y")),
                               parse_color(f.get("color", "")), parse_int(f.get("tolerance", "0"), "field_tolerance"),
                               form.expected)
        elif form.kind == COLOR:
            query = ColorRegionQuery(parse_color(f.get("color", "")),
                                     parse_int(f.get("tolerance", "0"), "field_tolerance"), form.region,
                                     parse_float(f.get("min_ratio", "0.5"), "field_min_ratio"), form.expected)
        elif form.kind == TEMPLATE:
            if form.template is None:
                raise EditError("Capture a template from the screen first.", "err_flow_no_template")
            query = TemplateQuery(form.template, parse_float(f.get("min_confidence", "0.95"), "field_min_confidence"),
                                  form.region, parse_int(f.get("max_results", "1"), "field_max_results"),
                                  form.expected, f.get("any_size", "0") == "1")
        elif form.kind == TEXT and _unchanged(form):
            query = form.kept
        elif form.kind == TEXT:
            query = TextLinesQuery(text_lines(form), form.ignore_case, form.region, f.get("language", "").strip(),
                                   parse_float(f.get("min_confidence", "0"), "field_min_confidence"), form.expected)
        else:
            raise EditError("Unknown kind of condition.", "err_flow_kind")
        return Condition(query, form.label.strip())
    except EditError:
        raise
    except (InvalidQuery, DecisionError, VisionError, ValueError) as exc:
        raise _rejected(exc) from None


def build_wait(condition: Condition, timeout: object, interval: object) -> WaitUntil:
    try:
        step = WaitUntil(condition, parse_float(timeout, "field_timeout"), parse_float(interval, "field_interval"))
    except EditError:
        raise
    except (DecisionError, ValueError) as exc:
        raise _rejected(exc) from None
    return check_step(step)


def build_branch(kind: str, *, op: ActionOp = ActionOp.TAP, dx: object = "0", dy: object = "0",
                 macro: Macro | None = None) -> TapMatch | RunMacro | None:
    try:
        if kind == NONE:
            return None
        if kind == TAP:
            return TapMatch(op, (parse_int(dx, "field_offset_x"), parse_int(dy, "field_offset_y")))
        if kind == RUN:
            if macro is None:
                raise EditError("Choose the macro this branch runs.", "err_flow_no_macro")
            # the script's own "Adapt to the size" decides for the macros inside it (`adapting`); a file keeps none
            return RunMacro(replace(macro, adapt=False))
    except EditError:
        raise
    except (DecisionError, ValueError) as exc:
        raise _rejected(exc) from None
    raise EditError("Unknown kind of branch.", "err_flow_kind")


def build_decision(condition: Condition, then, otherwise) -> Decision:
    found = then if condition.expected else otherwise                    # the branch that may click the match
    if isinstance(found, (tuple, list)):
        found = found[0] if found else None
    if isinstance(found, TapMatch) and not condition.points_at_match:
        raise EditError("A click on the found text needs every line to be one that must be there.",
                        "err_flow_tap_absent_line")
    try:
        step = Decision(condition, then, otherwise)
    except (DecisionError, ValueError) as exc:
        raise _rejected(exc) from None
    return check_step(step)


def build_run(macro: Macro | None) -> RunMacro:
    step = build_branch(RUN, macro=macro)
    return check_step(step)


def check_step(step: Step) -> Step:
    """One step on its own, through the same save-and-read-back check a whole macro gets."""
    validate(ConditionalMacro("check", (step,)))
    return step
