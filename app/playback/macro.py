"""Macro model: an executable view of a `Recording`.

Decision (Macro vs Recording): a Recording is the immutable, faithful RECORD of what happened (events: presses,
releases, key downs and ups). A Macro WRAPS a Recording (no second event model, no copy) and adds only a stable
optional `name`. The executable form is derived from it deterministically by `compile_actions`, because providers
execute user-level operations (tap, key press, ...) rather than raw events, and a target that only supports `tap`
must still be able to run a recorded click. The macro is provider-neutral: target identity is provider id + target
id, coordinates are client-relative integers, keys are neutral `Key` values.

Compilation (all rules are exact and deterministic)
  * a press immediately followed by the release of the SAME button, held <= LONG_PRESS_MS and released within
    CLICK_SLOP_PX pixels (each axis), becomes ONE action at the press position: `tap` (primary) or `secondary_tap`;
  * four events press(1) release press(2) release of the primary button with every release/press in slop of the
    first press and each hold <= LONG_PRESS_MS become ONE `double_tap`;
  * a key down immediately followed by the up of the same key, held <= LONG_PRESS_MS, becomes `key_press`;
  * everything else maps one to one: `move`, `button_down`, `button_up`, `key_down`, `key_up`, `type_text`;
    secondary-button press/release that is not a click become `secondary_down` / `secondary_up`, which no
    provider offers, so preflight rejects them (never silently dropped).
  The delay of a compiled action is the delay of its FIRST event; the time between the merged events (hold time,
  the gap inside a double click) is not reproduced: the provider performs the operation in its own way.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cached_property

from app.providers.base import TargetIdentity
from app.providers.input import Key
from app.providers.recording import (PointerButton, RecordedEvent, RecordedEventKind as K, Recording,
                                     MAX_TEXT_LENGTH)
from app.providers.target import Capability

CLICK_SLOP_PX = 3
LONG_PRESS_MS = 500
MAX_NAME_LENGTH = 100


class ActionOp(str, Enum):
    TAP = "tap"
    SECONDARY_TAP = "secondary_tap"
    DOUBLE_TAP = "double_tap"
    MOVE = "move"
    BUTTON_DOWN = "button_down"
    BUTTON_UP = "button_up"
    SECONDARY_DOWN = "secondary_down"
    SECONDARY_UP = "secondary_up"
    KEY_PRESS = "key_press"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    TYPE_TEXT = "type_text"


# The capability each operation needs (None: no provider offers it) and the interface method that performs it.
REQUIRED_CAPABILITY: dict[ActionOp, Capability | None] = {
    ActionOp.TAP: Capability.TAP, ActionOp.SECONDARY_TAP: Capability.SECONDARY_TAP,
    ActionOp.DOUBLE_TAP: Capability.DOUBLE_TAP, ActionOp.MOVE: Capability.POINTER_MOVE,
    ActionOp.BUTTON_DOWN: Capability.POINTER_BUTTON, ActionOp.BUTTON_UP: Capability.POINTER_BUTTON,
    ActionOp.SECONDARY_DOWN: None, ActionOp.SECONDARY_UP: None,
    ActionOp.KEY_PRESS: Capability.KEY, ActionOp.KEY_DOWN: Capability.KEY, ActionOp.KEY_UP: Capability.KEY,
    ActionOp.TYPE_TEXT: Capability.TEXT}
METHOD: dict[ActionOp, str] = {op: op.value for op in ActionOp}
POINTER_OPS = frozenset({ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP, ActionOp.MOVE, ActionOp.BUTTON_DOWN,
                         ActionOp.BUTTON_UP, ActionOp.SECONDARY_DOWN, ActionOp.SECONDARY_UP})
KEY_OPS = frozenset({ActionOp.KEY_PRESS, ActionOp.KEY_DOWN, ActionOp.KEY_UP})


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@dataclass(frozen=True)
class MacroAction:
    """One user-level operation. `index` counts actions; `source` is the index of its first recorded event."""
    op: ActionOp
    delay_ms: int
    x: int | None = None
    y: int | None = None
    key: Key | None = None
    text: str | None = None
    source: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.op, ActionOp):
            raise ValueError("op must be an ActionOp.")
        if not _is_int(self.delay_ms) or self.delay_ms < 0:
            raise ValueError("delay_ms must be a non-negative integer.")
        pointer, keyed, texted = self.op in POINTER_OPS, self.op in KEY_OPS, self.op is ActionOp.TYPE_TEXT
        for name, present, wanted in (("x", self.x is not None, pointer), ("y", self.y is not None, pointer),
                                      ("key", self.key is not None, keyed), ("text", self.text is not None, texted)):
            if present != wanted:
                raise ValueError(f"{self.op.value} {'requires' if wanted else 'does not take'} '{name}'.")
        if pointer and not (_is_int(self.x) and _is_int(self.y)):
            raise ValueError("Coordinates must be integers.")
        if keyed and not isinstance(self.key, Key):
            raise ValueError("key must be a neutral Key.")
        if texted and (not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_TEXT_LENGTH):
            raise ValueError("text must be a string of 1..256 characters.")

    def describe(self) -> str:
        if self.x is not None:
            return f"{self.op.value} ({self.x}, {self.y})"
        if self.key is not None:
            return f"{self.op.value} {self.key.value}"
        if self.text is not None:
            return f"{self.op.value} ({len(self.text)} characters)"      # the text itself is never echoed
        return self.op.value


def _near(a: RecordedEvent, b: RecordedEvent) -> bool:
    return abs(a.x - b.x) <= CLICK_SLOP_PX and abs(a.y - b.y) <= CLICK_SLOP_PX


def _pair(down: RecordedEvent | None, up: RecordedEvent | None) -> bool:
    return (down is not None and up is not None and down.kind is K.BUTTON_DOWN and up.kind is K.BUTTON_UP
            and down.button is up.button and up.delay_ms <= LONG_PRESS_MS and _near(down, up))


def compile_actions(recording: Recording) -> tuple[MacroAction, ...]:
    ev, n, out, i = recording.events, len(recording.events), [], 0
    at = lambda k: ev[k] if k < n else None    # noqa: E731
    while i < n:
        e = ev[i]
        if e.kind is K.BUTTON_DOWN:
            if (e.button is PointerButton.PRIMARY and e.click_count == 1 and _pair(e, at(i + 1)) and at(i + 2)
                    and at(i + 2).kind is K.BUTTON_DOWN and at(i + 2).button is e.button and at(i + 2).click_count == 2
                    and at(i + 2).delay_ms <= LONG_PRESS_MS and _near(e, at(i + 2)) and _pair(at(i + 2), at(i + 3))):
                out.append(MacroAction(ActionOp.DOUBLE_TAP, e.delay_ms, e.x, e.y, source=i))
                i += 4
            elif _pair(e, at(i + 1)):
                op = ActionOp.TAP if e.button is PointerButton.PRIMARY else ActionOp.SECONDARY_TAP
                out.append(MacroAction(op, e.delay_ms, e.x, e.y, source=i))
                i += 2
            else:
                op = ActionOp.BUTTON_DOWN if e.button is PointerButton.PRIMARY else ActionOp.SECONDARY_DOWN
                out.append(MacroAction(op, e.delay_ms, e.x, e.y, source=i))
                i += 1
        elif e.kind is K.BUTTON_UP:
            op = ActionOp.BUTTON_UP if e.button is PointerButton.PRIMARY else ActionOp.SECONDARY_UP
            out.append(MacroAction(op, e.delay_ms, e.x, e.y, source=i))
            i += 1
        elif e.kind is K.POINTER_MOVE:
            out.append(MacroAction(ActionOp.MOVE, e.delay_ms, e.x, e.y, source=i))
            i += 1
        elif e.kind is K.KEY_DOWN:
            nxt = at(i + 1)
            if nxt is not None and nxt.kind is K.KEY_UP and nxt.key is e.key and nxt.delay_ms <= LONG_PRESS_MS:
                out.append(MacroAction(ActionOp.KEY_PRESS, e.delay_ms, key=e.key, source=i))
                i += 2
            else:
                out.append(MacroAction(ActionOp.KEY_DOWN, e.delay_ms, key=e.key, source=i))
                i += 1
        elif e.kind is K.KEY_UP:
            out.append(MacroAction(ActionOp.KEY_UP, e.delay_ms, key=e.key, source=i))
            i += 1
        else:
            out.append(MacroAction(ActionOp.TYPE_TEXT, e.delay_ms, text=e.text, source=i))
            i += 1
    return tuple(out)


@dataclass(frozen=True)
class Macro:
    recording: Recording
    name: str = ""
    adapt: bool = False         # "Adapt to the size": its clicks follow a change of the target's size (scaling.py)

    def __post_init__(self) -> None:
        if not isinstance(self.recording, Recording):
            raise ValueError("A Macro wraps a Recording.")
        if not isinstance(self.name, str) or len(self.name) > MAX_NAME_LENGTH or not self.name.isprintable():
            raise ValueError(f"name must be at most {MAX_NAME_LENGTH} printable characters.")
        if not isinstance(self.adapt, bool):
            raise ValueError("adapt must be True or False.")

    @property
    def target(self) -> TargetIdentity:
        return self.recording.target

    @cached_property
    def actions(self) -> tuple[MacroAction, ...]:
        return compile_actions(self.recording)

    @property
    def duration_ms(self) -> int:
        return sum(a.delay_ms for a in self.actions)
