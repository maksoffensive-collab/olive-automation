"""Editable block view of a macro, and the exact way back to a `Recording`.

A macro IS a recording (see app/playback/macro.py): its executable `MacroAction`s are DERIVED from the recorded
events by `compile_actions` and are deliberately never stored, "so a file can never disagree with its own events".
An editor therefore cannot simply edit actions -- to change a macro it must produce a new Recording whose compiled
actions are exactly the ones the user asked for. This module is that two-way bridge. It knows nothing about Tk,
about providers, or about how anything is displayed: the view turns `Block`s into translated text.

What a person sees (`Block`)
  PAUSE    "wait 1.5 seconds" -- not an action at all. A pause is the `delay_ms` of the action that FOLLOWS it,
                                which is why a pause can never stand before the first action (a recording's first
                                event must have delay 0) and why deleting a pause means setting that delay to 0.
  ACTION   one `MacroAction`: click, double click, move, key press, typed text, or one half of a held pair.

Fidelity. The document keeps each action's OWN recorded events (`Step`) and edits them in place, instead of
synthesising fresh ones: compilation ignores the time inside a click, so rebuilding a click from scratch would
silently drop the recorded hold and change the file of a macro that was merely opened and saved. Editing a
position rewrites the coordinates of that action's real events; editing a pause rewrites the delay of its first
event. Opening a macro and saving it unchanged therefore produces a byte-identical file.

Round trip. Every edit rebuilds the recording, re-compiles it, and requires the compiler to agree with the
document's own grouping of events into actions; anything else is refused (`EditNotRepresentable`) and the document
does not change. That check is what makes editing safe: it is exactly how an edited held pair that would now
merge into a single click gets caught instead of quietly becoming a different macro.

Structural edits (insert / delete / reorder) are offered only for SELF-CONTAINED actions. A recorded drag or a
held key arrives as a down ... up pair whose halves belong together; those blocks stay visible and their
parameters stay editable, but they are never inserted, deleted or reordered.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.playback.macro import ActionOp, Macro, MacroAction, compile_actions
from app.providers.input import Key
from app.providers.recording import (MAX_TEXT_LENGTH, PointerButton, RecordedEvent, RecordedEventKind as K,
                                     Recording)

MAX_PAUSE_MS = 10 * 60 * 1000          # 10 minutes; a longer pause is almost certainly a mistake
MAX_COORD = 100_000
UNDO_DEPTH = 100

# Actions that stand on their own: safe to insert, delete and reorder.
SELF_CONTAINED = frozenset({ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP, ActionOp.MOVE,
                            ActionOp.KEY_PRESS, ActionOp.TYPE_TEXT})
# What "add action" may offer: self-contained AND runnable by some provider (REQUIRED_CAPABILITY is not None).
INSERTABLE: tuple[ActionOp, ...] = (ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP, ActionOp.MOVE,
                                    ActionOp.KEY_PRESS, ActionOp.TYPE_TEXT)


class EditError(ValueError):
    """The edit was refused and the document did not change. The message is meant for the user, so it carries
    the translation key of that same sentence; the English text is the fallback and the log line."""

    def __init__(self, message: str, key: str = "", **params) -> None:
        super().__init__(message)
        self.key, self.params = key, params


class EditNotRepresentable(EditError):
    """The result cannot be expressed as a recording that compiles back to exactly these actions."""


class BlockKind(str, Enum):
    PAUSE = "pause"
    ACTION = "action"


@dataclass(frozen=True)
class Block:
    """One row of the editor. `action_index` is the action this row is, or (for a pause) the one it precedes."""
    kind: BlockKind
    action_index: int
    delay_ms: int = 0                      # PAUSE only
    action: MacroAction | None = None      # ACTION only
    number: int = 0                        # 1-based display number of an ACTION row
    structural: bool = True                # False: half of a held pair -- no insert / delete / reorder


@dataclass(frozen=True)
class Step:
    """One action together with the recorded events it is compiled from."""
    events: tuple[RecordedEvent, ...]
    action: MacroAction


@dataclass(frozen=True)
class Document:
    """The macro being edited. `steps` always satisfies: compiling the concatenated events gives exactly
    `[step.action for step in steps]`, with the same grouping -- `_with_groups` refuses anything else."""
    provider_id: str
    target_id: str
    name: str
    steps: tuple[Step, ...]
    end_reason: str = "stopped"
    skipped: tuple[tuple[str, int], ...] = ()
    size: tuple[int, int] | None = None        # what it was recorded at (app/providers/size.py)
    adapt: bool = False                        # "Adapt to the size"

    @property
    def actions(self) -> tuple[MacroAction, ...]:
        return tuple(step.action for step in self.steps)


def _shape(action: MacroAction) -> tuple:
    """Everything about an action except `source`, which is a position in the events and is recomputed."""
    return (action.op, action.delay_ms, action.x, action.y, action.key, action.text)


def _synthetic_events(action: MacroAction) -> tuple[RecordedEvent, ...]:
    """Events that compile back to exactly this action, for an action the user ADDED (nothing was recorded for
    it). The action's delay goes on the first event; the events that merge into it follow immediately."""
    d, x, y = action.delay_ms, action.x, action.y
    primary, secondary = PointerButton.PRIMARY, PointerButton.SECONDARY
    if action.op is ActionOp.TAP:
        return (RecordedEvent(K.BUTTON_DOWN, d, x, y, primary, 1), RecordedEvent(K.BUTTON_UP, 0, x, y, primary, 1))
    if action.op is ActionOp.SECONDARY_TAP:
        return (RecordedEvent(K.BUTTON_DOWN, d, x, y, secondary, 1),
                RecordedEvent(K.BUTTON_UP, 0, x, y, secondary, 1))
    if action.op is ActionOp.DOUBLE_TAP:
        return (RecordedEvent(K.BUTTON_DOWN, d, x, y, primary, 1), RecordedEvent(K.BUTTON_UP, 0, x, y, primary, 1),
                RecordedEvent(K.BUTTON_DOWN, 0, x, y, primary, 2), RecordedEvent(K.BUTTON_UP, 0, x, y, primary, 2))
    if action.op is ActionOp.MOVE:
        return (RecordedEvent(K.POINTER_MOVE, d, x, y),)
    if action.op is ActionOp.KEY_PRESS:
        return (RecordedEvent(K.KEY_DOWN, d, key=action.key), RecordedEvent(K.KEY_UP, 0, key=action.key))
    return (RecordedEvent(K.TEXT, d, text=action.text),)


def _grouped(recording: Recording) -> tuple[Step, ...]:
    """Compile a recording and cut its events at the action boundaries the compiler itself reports."""
    actions = compile_actions(recording)
    bounds = [a.source for a in actions] + [len(recording.events)]
    return tuple(Step(recording.events[bounds[i]:bounds[i + 1]], actions[i]) for i in range(len(actions)))


def recording_of(document: Document) -> Recording:
    events = tuple(event for step in document.steps for event in step.events)
    try:
        return Recording(document.provider_id, document.target_id, events, document.end_reason, document.skipped,
                         document.size)
    except ValueError as exc:
        raise EditNotRepresentable(str(exc)) from None


def macro_of(document: Document) -> Macro:
    return Macro(recording_of(document), document.name, document.adapt)


def document_of(macro: Macro, name: str | None = None) -> Document:
    recording = macro.recording
    return Document(recording.provider_id, recording.target_id, macro.name if name is None else name,
                    _grouped(recording), recording.end_reason, recording.skipped, recording.size, macro.adapt)


def blocks_of(document: Document) -> tuple[Block, ...]:
    """The rows to display: a pause row before every action that waits, then the action row itself."""
    out: list[Block] = []
    for i, action in enumerate(document.actions):
        if action.delay_ms > 0:
            out.append(Block(BlockKind.PAUSE, i, delay_ms=action.delay_ms))
        out.append(Block(BlockKind.ACTION, i, action=action, number=i + 1,
                         structural=action.op in SELF_CONTAINED))
    return tuple(out)


def duration_ms(document: Document) -> int:
    return sum(a.delay_ms for a in document.actions)


# ---- edits (pure: each returns a NEW document, or raises and changes nothing) ---------------------------------
def _with_groups(document: Document, groups: list[tuple[RecordedEvent, ...]]) -> Document:
    """Rebuild the document from event groups: normalise, re-compile, and require the compiler to agree with
    this grouping (otherwise the edit would silently change what the macro does)."""
    if groups and groups[0] and groups[0][0].delay_ms != 0:
        # a recording's first event has delay 0, so whatever pause an action carried is dropped when it becomes
        # the first one -- the editor shows that pause row disappear.
        groups[0] = (replace(groups[0][0], delay_ms=0),) + tuple(groups[0][1:])
    candidate = replace(document, steps=tuple(Step(g, MacroAction(ActionOp.MOVE, 0, 0, 0)) for g in groups))
    recording = recording_of(candidate)
    regrouped = _grouped(recording)
    if [len(g) for g in groups] != [len(step.events) for step in regrouped]:
        raise EditNotRepresentable("These actions cannot be stored exactly as recorded events, so the change was "
                                   "not applied.", "err_not_representable")
    return replace(document, steps=regrouped)


def _require_index(document: Document, index: int) -> MacroAction:
    if not 0 <= index < len(document.steps):
        raise EditError("That action is no longer in the macro.", "err_step_gone")
    return document.steps[index].action


def _groups(document: Document) -> list[tuple[RecordedEvent, ...]]:
    return [step.events for step in document.steps]


def set_pause(document: Document, index: int, delay_ms: int) -> Document:
    """Set (0 removes) the pause BEFORE action `index`."""
    _require_index(document, index)
    if index == 0:
        raise EditError("A pause cannot stand before the first action.", "err_pause_first")
    if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or not 0 <= delay_ms <= MAX_PAUSE_MS:
        raise EditError(f"A pause must be between 0 and {MAX_PAUSE_MS // 1000} seconds.", "err_pause_range",
                            count=MAX_PAUSE_MS // 1000)
    groups = _groups(document)
    first, rest = groups[index][0], groups[index][1:]
    groups[index] = (replace(first, delay_ms=delay_ms),) + rest
    return _with_groups(document, groups)


def set_point(document: Document, index: int, x: int, y: int) -> Document:
    """Move an action to another point, keeping everything else about its recorded events."""
    action = _require_index(document, index)
    if action.x is None:
        raise EditError("This action has no position.", "err_no_position")
    for value in (x, y):
        if not isinstance(value, int) or isinstance(value, bool) or abs(value) > MAX_COORD:
            raise EditError("Coordinates must be whole numbers.", "err_coords")
    groups = _groups(document)
    groups[index] = tuple(replace(e, x=x, y=y) if e.x is not None else e for e in groups[index])
    return _with_groups(document, groups)


def set_key(document: Document, index: int, key: Key) -> Document:
    action = _require_index(document, index)
    if action.key is None:
        raise EditError("This action has no key.", "err_no_key")
    if not isinstance(key, Key):
        raise EditError("Unknown key.", "err_unknown_key")
    groups = _groups(document)
    groups[index] = tuple(replace(e, key=key) if e.key is not None else e for e in groups[index])
    return _with_groups(document, groups)


def set_text(document: Document, index: int, text: str) -> Document:
    action = _require_index(document, index)
    if action.text is None:
        raise EditError("This action has no text.", "err_no_text")
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_LENGTH:
        raise EditError(f"The text must be 1 to {MAX_TEXT_LENGTH} characters.", "err_text_range",
                        count=MAX_TEXT_LENGTH)
    groups = _groups(document)
    groups[index] = tuple(replace(e, text=text) if e.text is not None else e for e in groups[index])
    return _with_groups(document, groups)


def insert(document: Document, at: int, op: ActionOp, *, x: int | None = None, y: int | None = None,
           key: Key | None = None, text: str | None = None, delay_ms: int = 0) -> Document:
    """Insert a new action so that it becomes action number `at` (0 = first, len = last)."""
    if op not in INSERTABLE:
        raise EditError("That kind of action cannot be added by hand.", "err_not_addable")
    if not 0 <= at <= len(document.steps):
        raise EditError("That position is no longer in the macro.", "err_bad_position")
    try:
        action = MacroAction(op, delay_ms, x, y, key, text)
    except ValueError as exc:
        raise EditError(str(exc)) from None
    groups = _groups(document)
    groups.insert(at, _synthetic_events(action))
    return _with_groups(document, groups)


def delete(document: Document, index: int) -> Document:
    action = _require_index(document, index)
    if action.op not in SELF_CONTAINED:
        raise EditError("This action is one half of a press-and-hold that was recorded together; it cannot be "
                        "removed on its own.", "err_held_delete")
    groups = _groups(document)
    del groups[index]
    return _with_groups(document, groups)


def move(document: Document, index: int, delta: int) -> Document:
    """Move an action one step up (-1) or down (+1), swapping it with its neighbour."""
    action = _require_index(document, index)
    if delta not in (-1, 1):
        raise EditError("An action moves one step at a time.", "err_step_one")
    other = index + delta
    if not 0 <= other < len(document.steps):
        raise EditError("The action is already at the end of the macro.", "err_step_end")
    if action.op not in SELF_CONTAINED or document.steps[other].action.op not in SELF_CONTAINED:
        raise EditError("A press-and-hold that was recorded together cannot be reordered.", "err_held_move")
    groups = _groups(document)
    groups[index], groups[other] = groups[other], groups[index]
    return _with_groups(document, groups)


def set_adapt(document: Document, adapt: bool) -> Document:
    """"Adapt to the size": whether its clicks follow a change of the target's size (only when the size it was
    recorded at is known)."""
    return replace(document, adapt=bool(adapt))


def rename(document: Document, name: str) -> Document:
    name = name.strip()
    try:
        Macro(Recording(document.provider_id, document.target_id, ()), name)      # validates the name only
    except ValueError as exc:
        raise EditError(str(exc)) from None
    return replace(document, name=name)


class EditSession:
    """One open editor: the current document, undo/redo, and whether anything is unsaved.

    Every mutation goes through `apply`, which runs one of the pure edit functions above. A refused edit raises
    and leaves the session exactly as it was, so a failed change can never be half-applied."""

    def __init__(self, macro: Macro, name: str | None = None) -> None:
        self.document = document_of(macro, name)
        self._saved = self.document
        self._undo: list[Document] = []
        self._redo: list[Document] = []

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
        """`edit` is one of this module's pure functions; the document is supplied here."""
        new = edit(self.document, *args, **kwargs)          # raises EditError: nothing below runs
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

    def macro(self) -> Macro:
        return macro_of(self.document)

    def mark_saved(self) -> None:
        self._saved = self.document

    def blocks(self) -> tuple[Block, ...]:
        return blocks_of(self.document)
