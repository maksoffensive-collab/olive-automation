"""A script as blocks joined by arrows (version 4, the person's design of 2026-10-25): what it is, and its rules.

The person's list of 2026-10-25 replaced the step list of versions 2 and 3 with a free drawing: blocks anywhere on an
endless grid, an arrow from any output of a block to any other block. The blocks (docs/DECISION.md, last section):

    start    where a run begins (and how long it waits first)                         out: next
    wait     wait so many seconds                                                     out: next
    check    "If ... Then": looks at the screen once -- a pixel's colour, a picture, texts (each in its own area) --
             or "Mixed": several of those, true when at least one holds or when all do  out: found, missing
    repeat   the small circle on an arrow that goes back up: again, until a setting is reached (until stopped by
             hand, until a time of day, for a length of time, a number of times)       out: again, done
    click    a click at a point picked on the screen, or where the last check found its match: a click, a double
             click, a right click, a hold, a drag from one picked point to another      out: next
    macro    run a recorded macro of the library                                       out: next
    stop     the end: finished, or stopped with an error and its own words             (no outputs)

An output with no arrow ends the run there, finished. A loop is only drawn through a `repeat` circle: every cycle of
the drawing passes through a repeat's "again" output (`validate`), so a loop always has a setting -- even if that
setting is "until stopped by hand" (the person's explicit decision: a loop need not be bounded; the emergency stop
ends it at once, always, and every operation inside it keeps its own timeout).

Pure data, frozen and validated on construction; nothing here looks, waits or acts (`graph_run.py` does).
Provider-neutral: a point, an area and a picture carry the size of the screen they were picked on (`size`), so a run
can follow a target of another size; which target is not in here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.observe import PixelQuery, TemplateQuery, TextLinesQuery

from .condition import Condition
from .errors import DecisionError

START, WAIT, CHECK, REPEAT, CLICK, MACRO, STOP = "start", "wait", "check", "repeat", "click", "macro", "stop"
KINDS = (START, WAIT, CHECK, REPEAT, CLICK, MACRO, STOP)
NEXT, FOUND, MISSING, AGAIN, DONE = "next", "found", "missing", "again", "done"

ANY, ALL = "any", "all"                                    # a check: at least one of its parts holds / all of them do
MANUAL, UNTIL, FOR, TIMES = "manual", "until", "for", "times"   # a repeat: until stopped / a time of day / ... / n
REPEAT_MODES = (MANUAL, UNTIL, FOR, TIMES)
TAP, DOUBLE, RIGHT, HOLD, DRAG = "tap", "double", "right", "hold", "drag"
CLICK_KINDS = (TAP, DOUBLE, RIGHT, HOLD, DRAG)
AT_POINT, AT_MATCH = "point", "match"

MAX_NODES = 300
MAX_PARTS = 10                  # the parts of one check (texts in their areas, or the parts of a mixed one)
MAX_WAIT_S = 24 * 3600.0        # a wait, a start delay, a hold, a repeat's length: at most a day
MAX_TIMES = 1_000_000
MAX_HOLD_S = 60.0
MAX_MESSAGE = 200
MAX_COORD = 100_000
MAX_KEY = 400                   # a macro's key in the library (its path there)
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")


class GraphError(DecisionError):
    """The drawing breaks one of its rules; the message says which."""


def _size(value, what: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (not isinstance(value, tuple) or len(value) != 2 or any(isinstance(v, bool) or not isinstance(v, int)
                                                              or not 0 < v <= MAX_COORD for v in value)):
        raise GraphError(f"{what} must be (width, height) in whole pixels, or None.")
    return value


def _point(value, what: str) -> tuple[int, int]:
    if (not isinstance(value, tuple) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_COORD for v in value)):
        raise GraphError(f"{what} must be (x, y) in whole pixels.")
    return value


def _seconds(value, what: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low <= value <= high:
        raise GraphError(f"{what} must be a number of seconds from {low:g} to {high:g}.")
    return float(value)


# ---- the parts of a check --------------------------------------------------------------------------------------
PIXEL, PICTURE, TEXT = "pixel", "picture", "text"


def part_kind(condition: Condition) -> str:
    """Which of the three things a check looks for this condition is: a pixel's colour, a picture, a text."""
    query = condition.query
    if isinstance(query, PixelQuery):
        return PIXEL
    if isinstance(query, TemplateQuery):
        return PICTURE
    if isinstance(query, TextLinesQuery):
        return TEXT
    raise GraphError("A check looks for a pixel's colour, a picture or a text.")


@dataclass(frozen=True)
class Part:
    """One thing a check looks for, and the size of the screen its point, area or picture was picked on (None: not
    picked on a screen -- typed -- and then asked as it is)."""
    condition: Condition
    size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.condition, Condition):
            raise GraphError("A part of a check is a condition.")
        part_kind(self.condition)
        if not self.condition.expected:
            raise GraphError("A part of a check asks whether something IS there; its \"missing\" arrow is the other "
                             "answer.")
        object.__setattr__(self, "size", _size(self.size, "size"))

    @property
    def kind(self) -> str:
        return part_kind(self.condition)


# ---- the blocks -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    """What every block has: a name of its own in the drawing (`id`) and where it is drawn (`at`, scene units)."""
    id: str
    at: tuple[int, int] = (0, 0)

    kind = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.match(self.id):
            raise GraphError("A block's id is 1 to 32 letters, digits, '-' or '_'.")
        if (not isinstance(self.at, tuple) or len(self.at) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or abs(v) > MAX_COORD * 10 for v in self.at)):
            raise GraphError("A block's place is (x, y) in whole units.")

    @property
    def outputs(self) -> tuple[str, ...]:
        return (NEXT,)


@dataclass(frozen=True)
class StartNode(Node):
    delay_s: float = 0.0
    kind = START

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "delay_s", _seconds(self.delay_s, "The start delay", 0.0, MAX_WAIT_S))


@dataclass(frozen=True)
class WaitNode(Node):
    seconds: float = 1.0
    kind = WAIT

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "seconds", _seconds(self.seconds, "A wait", 0.05, MAX_WAIT_S))


@dataclass(frozen=True)
class CheckNode(Node):
    """"If ... Then" (`mixed` False: parts of one kind -- one pixel, one picture, or texts each in its own area, true
    when any holds) or "Mixed" (`mixed` True: parts of any kinds, `mode` ANY or ALL)."""
    parts: tuple[Part, ...] = ()
    mode: str = ANY
    mixed: bool = False
    kind = CHECK

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.parts, tuple) or not all(isinstance(p, Part) for p in self.parts):
            raise GraphError("A check's parts are a tuple of Part.")
        if not 0 <= len(self.parts) <= MAX_PARTS:
            raise GraphError(f"A check has at most {MAX_PARTS} parts.")
        if self.mode not in (ANY, ALL) or not isinstance(self.mixed, bool):
            raise GraphError("A check holds when ANY of its parts does, or ALL of them.")
        if not self.mixed:
            kinds = {p.kind for p in self.parts}
            if len(kinds) > 1:
                raise GraphError("An \"If ... Then\" looks for one kind of thing; several kinds make a mixed block.")
            if kinds & {PIXEL, PICTURE} and len(self.parts) > 1:
                raise GraphError("An \"If ... Then\" looks for one pixel or one picture; several texts may each have "
                                 "their own area.")
            if self.mode != ANY:
                raise GraphError("An \"If ... Then\" holds when any of its texts does.")

    @property
    def outputs(self) -> tuple[str, ...]:
        return (FOUND, MISSING)


@dataclass(frozen=True)
class RepeatNode(Node):
    """The circle of a loop: "again" until its setting is reached, then "done" (none for MANUAL: until stopped by
    hand). `until` is a time of day "HH:MM" (the next one to come), `seconds` a length of time from the first pass,
    `times` how many passes."""
    mode: str = MANUAL
    until: str = ""
    seconds: float = 0.0
    times: int = 0
    kind = REPEAT

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.mode not in REPEAT_MODES:
            raise GraphError("A repeat goes on until stopped by hand, until a time, for a time, or a number of times.")
        # every field is checked whatever the mode: a file says only what can be read back as it was
        if not isinstance(self.until, str) or (self.until and not _CLOCK.match(self.until)):
            raise GraphError("A repeat's time of day is HH:MM.")
        if self.mode == UNTIL and not self.until:
            raise GraphError("A repeat until a time needs the time as HH:MM.")
        low = 0.05 if self.mode == FOR else 0.0
        object.__setattr__(self, "seconds", _seconds(self.seconds, "A repeat's length", low, MAX_WAIT_S))
        least = 1 if self.mode == TIMES else 0
        if isinstance(self.times, bool) or not isinstance(self.times, int) or not least <= self.times <= MAX_TIMES:
            raise GraphError(f"A repeat a number of times needs 1 to {MAX_TIMES}.")

    @property
    def outputs(self) -> tuple[str, ...]:
        return (AGAIN,) if self.mode == MANUAL else (AGAIN, DONE)


@dataclass(frozen=True)
class ClickNode(Node):
    """A click at `point` (picked on a screen of `size`) or where the last check found its match (AT_MATCH). A drag
    goes from `point` to `to`, both picked; a hold presses for `hold_s`."""
    how: str = TAP
    where: str = AT_POINT
    point: tuple[int, int] | None = None
    to: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    hold_s: float = 1.0
    kind = CLICK

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.how not in CLICK_KINDS or self.where not in (AT_POINT, AT_MATCH):
            raise GraphError("A click is a click, a double click, a right click, a hold or a drag, at a point or "
                             "where something was found.")
        if self.point is not None:
            _point(self.point, "The click's point")
        if self.to is not None:
            _point(self.to, "Where the drag ends")
        if self.how == DRAG and self.where != AT_POINT:
            raise GraphError("A drag goes from one picked point to another.")
        object.__setattr__(self, "size", _size(self.size, "size"))
        object.__setattr__(self, "hold_s", _seconds(self.hold_s, "A hold", 0.05, MAX_HOLD_S))

    @property
    def ready(self) -> bool:
        """Whether it knows where to click (a new block does not yet)."""
        if self.where == AT_MATCH:
            return True
        return self.point is not None and (self.how != DRAG or self.to is not None)


@dataclass(frozen=True)
class MacroNode(Node):
    """Run a recorded macro of the library, named by its key there ("" while none is chosen)."""
    macro: str = ""
    kind = MACRO

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.macro, str) or len(self.macro) > MAX_KEY or not self.macro.isprintable():
            raise GraphError("A macro block names a macro of the library.")


@dataclass(frozen=True)
class StopNode(Node):
    """The end of a run: finished (`success`), or stopped with an error that says `message`."""
    success: bool = True
    message: str = ""
    kind = STOP

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.success, bool):
            raise GraphError("A stop finishes, or stops with an error.")
        if not isinstance(self.message, str) or len(self.message) > MAX_MESSAGE or not self.message.isprintable():
            raise GraphError(f"A stop's words are at most {MAX_MESSAGE} printable characters.")

    @property
    def outputs(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class Edge:
    """An arrow: from an output of one block to another block."""
    source: str
    output: str
    target: str


@dataclass(frozen=True)
class Script:
    """A version-4 script: its name, its blocks and its arrows."""
    name: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...] = ()
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        validate(self)

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise GraphError(f"No block {node_id!r}.")

    @property
    def start(self) -> StartNode:
        return next(n for n in self.nodes if isinstance(n, StartNode))

    def edge_from(self, node_id: str, output: str) -> Edge | None:
        return next((e for e in self.edges if e.source == node_id and e.output == output), None)


def validate(script: Script) -> None:
    """Every rule of the drawing (GraphError for the first one broken)."""
    if not isinstance(script.name, str) or not 0 < len(script.name.strip()) <= 120 or not script.name.isprintable():
        raise GraphError("A script needs a name of 1 to 120 printable characters.")
    if not isinstance(script.nodes, tuple) or not all(isinstance(n, Node) and n.kind for n in script.nodes):
        raise GraphError("A script's blocks are a tuple of blocks.")
    if not 1 <= len(script.nodes) <= MAX_NODES:
        raise GraphError(f"A script has 1 to {MAX_NODES} blocks.")
    ids = [n.id for n in script.nodes]
    if len(set(ids)) != len(ids):
        raise GraphError("Two blocks have the same id.")
    if sum(isinstance(n, StartNode) for n in script.nodes) != 1:
        raise GraphError("A script has exactly one start.")
    by_id = {n.id: n for n in script.nodes}
    seen: set[tuple[str, str]] = set()
    if not isinstance(script.edges, tuple) or not all(isinstance(e, Edge) for e in script.edges):
        raise GraphError("A script's arrows are a tuple of arrows.")
    for edge in script.edges:
        source, target = by_id.get(edge.source), by_id.get(edge.target)
        if source is None or target is None:
            raise GraphError("An arrow joins two blocks of the script.")
        if edge.output not in source.outputs:
            raise GraphError(f"A {source.kind} block has no output {edge.output!r}.")
        if (edge.source, edge.output) in seen:
            raise GraphError("One output leads to one block.")
        seen.add((edge.source, edge.output))
        if isinstance(target, StartNode):
            raise GraphError("Nothing leads back to the start.")
        if edge.source == edge.target and not isinstance(source, RepeatNode):
            raise GraphError("A block leads to itself only through a repeat.")
    # every cycle passes through a repeat's "again": without those arrows the drawing has no cycle
    following: dict[str, list[str]] = {n: [] for n in by_id}
    for edge in script.edges:
        if not (isinstance(by_id[edge.source], RepeatNode) and edge.output == AGAIN):
            following[edge.source].append(edge.target)
    state: dict[str, int] = {}
    for root in by_id:
        if root in state:
            continue
        stack = [(root, iter(following[root]))]
        state[root] = 1
        while stack:
            node, rest = stack[-1]
            nxt = next(rest, None)
            if nxt is None:
                state[node] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                raise GraphError("A loop goes back only through a repeat circle.")
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(following[nxt])))
