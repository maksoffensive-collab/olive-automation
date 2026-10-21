"""Decision: given a condition's answer, take one of two branches that were written down in advance.

    IF condition THEN [blocks ...] ELSE [blocks ...]

A branch is a short list of blocks run in order -- the blocks a script is made of (WAIT UNTIL, IF, a recorded
macro) plus one only a branch can hold: a click where THIS decision's condition saw its match. So a decision can do
several things in a row ("click it, wait for the dialog, run the reward macro"), and a branch can decide again (an
IF inside an IF), at most `MAX_NESTING` levels deep. (Before 2026-10-19 a branch was at most one action; nested
branches are the person's request of that day.) There are still no loops, no jumps, no variables and no
expressions: every block is data and runs at most once, so a script stays finite. `decide()` is a pure function
from (decision, condition result) to the chosen branch: it captures nothing, waits for nothing and sends nothing.

The actions are expressed in the platform's EXISTING action representation -- a `Macro`:

    RunMacro(macro)       a macro that already exists (recorded, or built in the editor). Run as-is.
    TapMatch(op, offset)  one click where the condition saw its match: the observation's `center` (plus an
                          optional offset) becomes a one-action macro. By the screenshot contract the frame's pixel
                          space IS the input space, so no conversion happens and none is hidden here. Allowed only
                          as the FIRST block of a branch: the click comes right after the look, never after other
                          blocks may have changed the screen.

The macro a `TapMatch` produces is stamped with the provider and target the OBSERVATION came from. Acting on it
therefore goes through the same preflight as every other macro, and that preflight refuses it on any other
target: a match seen on one target can never become a click on another.

`decide()` never runs what it chooses. `run.py` hands each chosen macro to the provider's own playback factory,
which is the only way anything here reaches an input.

Validation happens where a decision is written: a `TapMatch` that is not first in its branch, on a branch whose
observation can never contain a match (with `expected=True` only THEN has a match to click; with `expected=False`
only ELSE does, because ELSE there means "the thing that should be gone is still there"), or where a text line
that must be absent could make the condition true with nothing seen; nesting deeper than `MAX_NESTING`. A decision
whose branches are both empty is allowed: it looks and does nothing, and the editor fills a branch block by block.
Every block may carry `at`, where the editor draws it (presentation only, see `position.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from app.playback.macro import ActionOp, Macro
from app.providers.recording import PointerButton, RecordedEvent, RecordedEventKind, Recording
from app.observe.result import ObservationResult

from .condition import Condition, ConditionResult
from .errors import InvalidDecision
from .position import Position, check_position
from .wait import WaitUntil

THEN = "then"
ELSE = "else"
CLICK_OPS = (ActionOp.TAP, ActionOp.DOUBLE_TAP, ActionOp.SECONDARY_TAP)
MAX_OFFSET = 10_000
MAX_NESTING = 4                 # an IF inside an IF inside an IF inside an IF, and no deeper


@dataclass(frozen=True)
class RunMacro:
    """Run an existing macro. `allow_different_target` is the same explicit opt-in playback already has."""
    macro: Macro
    allow_different_target: bool = False
    at: Position = None

    def __post_init__(self) -> None:
        if not isinstance(self.macro, Macro):
            raise InvalidDecision("RunMacro needs a Macro.")
        if not isinstance(self.allow_different_target, bool):
            raise InvalidDecision("allow_different_target must be True or False.")
        object.__setattr__(self, "at", check_position(self.at))


@dataclass(frozen=True)
class TapMatch:
    """Click at the matched place: `op` is tap, double tap or secondary tap; `offset` shifts the point."""
    op: ActionOp = ActionOp.TAP
    offset: tuple[int, int] = (0, 0)
    at: Position = None

    def __post_init__(self) -> None:
        if self.op not in CLICK_OPS:
            raise InvalidDecision(f"TapMatch clicks; op must be one of {[o.value for o in CLICK_OPS]}.")
        if not isinstance(self.offset, (tuple, list)) or len(self.offset) != 2 \
                or any(isinstance(v, bool) or not isinstance(v, int) or abs(v) > MAX_OFFSET for v in self.offset):
            raise InvalidDecision("offset must be two whole numbers (dx, dy).")
        object.__setattr__(self, "offset", tuple(self.offset))
        object.__setattr__(self, "at", check_position(self.at))


Branch = RunMacro | TapMatch          # what a branch's first block may click or run (kept for its callers)
ITEM_TYPES: tuple = ()                # set below, once Decision exists


def _items(value: object, name: str) -> tuple:
    """A branch as written -- nothing, one block, or a list of blocks -- as a tuple of blocks."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items = tuple(value)
    else:
        items = (value,)
    for i, item in enumerate(items):
        if not isinstance(item, ITEM_TYPES):
            raise InvalidDecision(f"Block {i + 1} of the {name} branch is not a block (WAIT UNTIL, IF, a macro or a "
                                  f"click on the match): {type(item).__name__}.")
        if isinstance(item, TapMatch) and i:
            raise InvalidDecision(f"A click on the match can only be the first block of the {name} branch: it clicks "
                                  "where the condition has just looked, before anything else changes the screen.")
    return items


def nesting(item: object) -> int:
    """How many IFs deep a block goes: 0 for anything but an IF, 1 for an IF whose branches hold no IF."""
    if not isinstance(item, Decision):
        return 0
    return 1 + max((nesting(i) for i in item.then + item.otherwise), default=0)


@dataclass(frozen=True)
class Decision:
    condition: Condition
    then: tuple = ()
    otherwise: tuple = ()
    at: Position = None

    def __post_init__(self) -> None:
        if not isinstance(self.condition, Condition):
            raise InvalidDecision("A decision needs a Condition.")
        object.__setattr__(self, "then", _items(self.then, THEN))
        object.__setattr__(self, "otherwise", _items(self.otherwise, ELSE))
        object.__setattr__(self, "at", check_position(self.at))
        first_then = self.then[0] if self.then else None
        first_else = self.otherwise[0] if self.otherwise else None
        if isinstance(first_then, TapMatch) and not self.condition.expected:
            raise InvalidDecision("THEN of a condition that expects the thing to be absent has no match to click.")
        if isinstance(first_else, TapMatch) and self.condition.expected:
            raise InvalidDecision("ELSE of a condition that expects the thing to be present has no match to click.")
        if isinstance(first_then if self.condition.expected else first_else, TapMatch) \
                and not self.condition.points_at_match:
            raise InvalidDecision("A click on the match needs every text line to be one that must BE there: a line "
                                  "that must be absent can make the condition true with nothing to click.")
        if nesting(self) > MAX_NESTING:
            raise InvalidDecision(f"IF blocks go at most {MAX_NESTING} levels deep (an IF inside an IF ...).")

    def branch(self, name: str) -> tuple:
        if name not in (THEN, ELSE):
            raise InvalidDecision(f"A decision's branches are {THEN!r} and {ELSE!r}.")
        return self.then if name == THEN else self.otherwise

    def found_side(self) -> str:
        """The branch whose observation has a match to click: THEN when the thing must be there, else ELSE."""
        return THEN if self.condition.expected else ELSE


ITEM_TYPES = (WaitUntil, Decision, RunMacro, TapMatch)
Item = WaitUntil | Decision | RunMacro | TapMatch
Path = tuple                          # (2,), (2, "then", 0), (2, "then", 0, "else", 1), ...


def walk(items, prefix: Path = ()) -> Iterator[tuple[Path, object]]:
    """Every block of a list of blocks and of the branches inside it, in run order (a block before what its
    branches hold, THEN before ELSE), each with its path."""
    for index, item in enumerate(items):
        path = prefix + (index,)
        yield path, item
        if isinstance(item, Decision):
            yield from walk(item.then, path + (THEN,))
            yield from walk(item.otherwise, path + (ELSE,))


def count_blocks(items) -> int:
    """How many blocks a list holds, the ones inside branches included."""
    return sum(1 for _ in walk(items))


@dataclass(frozen=True)
class Choice:
    """The branch a decision took: its blocks, the first of them (`action`) and the exact macro that first block
    stands for (None: the branch is empty, or begins with a WAIT UNTIL or an IF)."""
    branch: str                                   # THEN or ELSE
    action: object | None
    macro: Macro | None
    result: ConditionResult
    items: tuple = field(default=())

    @property
    def allow_different_target(self) -> bool:
        return isinstance(self.action, RunMacro) and self.action.allow_different_target


def tap_macro(observation: ObservationResult, tap: TapMatch, scale: tuple[float, float] = (1.0, 1.0)) -> Macro:
    """The one-action macro that clicks where `observation` found its best match, on the target it came from --
    beside it by the tap's offset times `scale` (the script's size to the screen's, when it adapts)."""
    center = observation.center
    if center is None:
        raise InvalidDecision("There is no match to click: the observation found nothing.")
    x, y = center.x + round(tap.offset[0] * scale[0]), center.y + round(tap.offset[1] * scale[1])
    button = PointerButton.SECONDARY if tap.op is ActionOp.SECONDARY_TAP else PointerButton.PRIMARY
    down = RecordedEvent(RecordedEventKind.BUTTON_DOWN, 0, x, y, button, 1)
    up = RecordedEvent(RecordedEventKind.BUTTON_UP, 0, x, y, button, 1)
    events = (down, up)
    if tap.op is ActionOp.DOUBLE_TAP:
        events += (RecordedEvent(RecordedEventKind.BUTTON_DOWN, 0, x, y, button, 2),
                   RecordedEvent(RecordedEventKind.BUTTON_UP, 0, x, y, button, 2))
    source = observation.source
    macro = Macro(Recording(source.provider_id, source.target_id, events))
    if [a.op for a in macro.actions] != [tap.op]:           # the compiler must agree, or nothing is sent
        raise InvalidDecision(f"Could not express {tap.op.value} as exactly one action.")
    return macro


def decide(decision: Decision, result: ConditionResult, *, scale: tuple[float, float] = (1.0, 1.0)) -> Choice:
    """Pick THEN when the condition is satisfied, ELSE otherwise. Pure: no capture, no wait, no input. `scale`
    multiplies a click's offset from the match (`tap_macro`)."""
    if not isinstance(decision, Decision) or not isinstance(result, ConditionResult):
        raise InvalidDecision("decide() needs a Decision and the ConditionResult of its condition.")
    if result.condition != decision.condition:
        raise InvalidDecision("This result answers a different condition than the decision's.")
    name, items = (THEN, decision.then) if result.satisfied else (ELSE, decision.otherwise)
    action = items[0] if items else None
    if isinstance(action, RunMacro):
        macro = action.macro
    elif isinstance(action, TapMatch):
        macro = tap_macro(result.observation, action, scale)
    else:
        macro = None
    return Choice(name, action, macro, result, items)
