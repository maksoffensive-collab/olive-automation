"""Where each block of a script is drawn -- automatically, or where the person put it -- and the arrows between.

A script runs its steps from top to bottom, each once; an IF runs ONE of its two branches -- each a list of blocks
of its own, an IF among them -- and then the run continues with the step after the IF (`app.decide.run`). The
picture says exactly that and nothing more:

  * blocks joined by arrows in run order, from Start to End;
  * an IF has two exits, "yes" and "no"; each leads through its branch's blocks to a small junction under the IF,
    and from the junction the run goes on; an empty branch is an arrow straight to the junction;
  * every arrow has a "+" in its middle: a new block goes exactly there (`Edge.slot`, a path into the script);
  * a WAIT UNTIL that runs out of time ends the run: a short dashed arrow to a "stop" marker beside it.

Placement: a block the person has placed (`Node.at`, or a position given while it is being dragged) is drawn
there; any other is placed automatically -- a column per list of blocks, an IF's two branches side by side under
it. Arrows are smooth curves from one block's bottom to the next one's top, so wherever a block is moved they
follow it. Coordinates are "scene" units at zoom 1.0; the canvas multiplies them by its zoom. Pure arithmetic, no
Tk: tested directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.decide import MAX_POSITION             # a block's place, each way: the scene ends there

STEP_W, STEP_H = 300.0, 74.0
GAP = 46.0                      # vertical room between blocks (the arrows and their "+" live here)
BRANCH_GAP = 44.0               # horizontal room between an IF's two branches
EMPTY_W = 150.0                 # the room an empty branch takes beside the other one
JUNCTION_R = 6.0
TERMINAL_W, TERMINAL_H = 170.0, 44.0
EXIT_W, EXIT_H = 84.0, 28.0
EXIT_GAP_X = 40.0               # from a wait's side to its exit marker
EXIT_ROOM = EXIT_W + EXIT_GAP_X  # what a wait's exit marker takes beside it
MARGIN = 28.0
PLUS_R = 10.0                   # the "+" on an arrow
YES_AT, NO_AT = 0.3, 0.7        # where along an IF's bottom its "yes" and "no" exits are

START, END, STEP, EXIT, PENDING, PLACEHOLDER = "start", "end", "step", "exit", "pending", "placeholder"
THEN, ELSE = "then", "else"
WAIT, IF, RUN, TAP = "wait_until", "if", "run_macro", "tap_match"     # app.gui.flow_edit's block kinds
START_KEY, END_KEY, PENDING_KEY = ("start",), ("end",), ("pending",)


@dataclass(frozen=True)
class Node:
    """What the layout needs to know about one block: where it is in the script, its kind, the place the person
    gave it (or None), and -- for an IF -- the blocks of its branches."""
    path: tuple
    kind: str
    at: tuple | None = None
    then: tuple = ()
    otherwise: tuple = ()


@dataclass(frozen=True)
class Part:
    """A part of a block that can be clicked to change just it: "condition", "timing", "macro", "op", "delay"."""
    name: str
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.w and self.y <= y <= self.y + self.h


@dataclass(frozen=True)
class Box:
    role: str                   # START | END | STEP | EXIT | PENDING | PLACEHOLDER
    path: tuple                 # the block's path (START_KEY / END_KEY / PENDING_KEY / () for the rest)
    x: float
    y: float
    w: float
    h: float
    kind: str = ""
    parts: tuple = ()

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom

    def part_at(self, x: float, y: float) -> str:
        return next((p.name for p in self.parts if p.contains(x, y)), "")


@dataclass(frozen=True)
class Edge:
    role: str                   # "flow" (a curve: p0, two control points, p3) | "exit" (a straight dash)
    points: tuple
    label: str = ""             # THEN | ELSE on the first arrow of a branch, "timeout" on an exit
    slot: tuple | None = None   # where a block added at this arrow's "+" goes; None: no "+" here

    @property
    def plus(self) -> tuple[float, float] | None:
        """Where the arrow's "+" is: the middle of the curve."""
        if self.slot is None or self.role != "flow":
            return None
        return bezier(self.points, 0.5)


@dataclass(frozen=True)
class Layout:
    boxes: tuple[Box, ...]
    edges: tuple[Edge, ...]
    junctions: tuple[tuple[tuple, float, float], ...]       # (the IF's path, x, y) of each junction
    order: tuple[tuple, ...]                                # every block's path, in run order
    width: float
    height: float
    auto: Mapping = field(default_factory=dict)             # where each block would be placed automatically

    def box_of(self, path: tuple) -> Box | None:
        return next((b for b in self.boxes if b.path == tuple(path) and b.role != EXIT), None)

    def hit(self, x: float, y: float) -> Box | None:
        """The block under a scene point: a step, Start, End, the pending new block or the placeholder of an empty
        script. Later boxes are drawn over earlier ones, so they are found first."""
        for box in reversed(self.boxes):
            if box.role in (STEP, START, END, PENDING, PLACEHOLDER) and box.contains(x, y):
                return box
        return None

    def plus_at(self, x: float, y: float, *, radius: float = PLUS_R + 3) -> tuple | None:
        """The slot of the "+" under a scene point, or None."""
        for edge in self.edges:
            point = edge.plus
            if point is not None and (point[0] - x) ** 2 + (point[1] - y) ** 2 <= radius * radius:
                return edge.slot
        return None

    def descendants(self, path: tuple) -> tuple[tuple, ...]:
        """The paths of the blocks inside an IF's branches, at any depth (in run order)."""
        path = tuple(path)
        return tuple(p for p in self.order if len(p) > len(path) and p[:len(path)] == path)


def bezier(points: tuple, t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = points
    u = 1 - t
    a, b, c, d = u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3
    return a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3


def curve(p0: tuple[float, float], p3: tuple[float, float]) -> tuple:
    """A smooth arrow leaving `p0` downwards and arriving at `p3` from above, wherever `p3` is: straight down when
    one block is right under the other, an S when it is beside, a loop when it is above."""
    (x0, y0), (x3, y3) = p0, p3
    dy = y3 - y0
    if dy >= 24:
        d = max(22.0, min(140.0, dy * 0.5))
    else:
        d = 60.0 + min(220.0, -dy * 0.3)
    return (p0, (x0, y0 + d), (x3, y3 - d), p3)


# ---- automatic placement ------------------------------------------------------------------------------------
# A column of blocks is measured from its centre line: how far it reaches to the left, how far to the right (a
# WAIT's exit marker reaches further left than its block), and how tall it is. An IF's two branches are columns
# placed side by side under it, with BRANCH_GAP between what they hold -- exit markers included -- so nothing an
# automatic layout draws ever covers anything else.
def _extent(nodes: Sequence[Node]) -> tuple[float, float, float]:
    """(left, right, height) of a column of blocks around its centre (an empty one: no height, a little room)."""
    if not nodes:
        return EMPTY_W / 2, EMPTY_W / 2, 0.0
    left = right = height = 0.0
    for i, node in enumerate(nodes):
        lo, hi, h = _extent_node(node)
        left, right = max(left, lo), max(right, hi)
        height += h + (GAP if i else 0.0)
    return left, right, height


def _extent_node(node: Node) -> tuple[float, float, float]:
    half = STEP_W / 2
    if node.kind == WAIT:
        return half + EXIT_ROOM, half, STEP_H
    if node.kind != IF:
        return half, half, STEP_H
    tl, tr, th = _extent(node.then)
    el, er, eh = _extent(node.otherwise)
    inner = max(th, eh)
    left = max(half, BRANCH_GAP / 2 + tr + tl)
    right = max(half, BRANCH_GAP / 2 + el + er)
    height = STEP_H + (GAP + inner + GAP * 0.7 if inner else GAP) + 2 * JUNCTION_R
    return left, right, height


def _place(nodes: Sequence[Node], cx: float, top: float, out: dict) -> float:
    """Automatic places of a column of blocks centred on `cx` from `top`; returns the column's bottom."""
    y = top
    for i, node in enumerate(nodes):
        if i:
            y += GAP
        _lo, _hi, h = _extent_node(node)
        out[node.path] = (cx - STEP_W / 2, y)
        if node.kind == IF:
            _tl, tr, _th = _extent(node.then)
            el, _er, _eh = _extent(node.otherwise)
            branches_top = y + STEP_H + GAP
            _place(node.then, cx - BRANCH_GAP / 2 - tr, branches_top, out)
            _place(node.otherwise, cx + BRANCH_GAP / 2 + el, branches_top, out)
        y += h
    return y


def _parts(kind: str, x: float, y: float, w: float, h: float) -> tuple:
    if kind == START:
        return (Part("delay", x, y, w, h),)
    detail = {WAIT: "condition", IF: "condition", RUN: "macro", TAP: "op"}.get(kind, "")
    if not detail:
        return ()
    if kind == WAIT:
        return (Part("condition", x + 50, y + 30, w - 64, 20), Part("timing", x + 50, y + 50, w - 64, h - 52))
    return (Part(detail, x + 50, y + 30, w - 64, h - 32),)


def _walk(nodes: Sequence[Node]):
    for node in nodes:
        yield node
        yield from _walk(node.then)
        yield from _walk(node.otherwise)


def layout(nodes: Sequence[Node], *, start_at: tuple | None = None, end_at: tuple | None = None,
           overrides: Mapping | None = None, pending: tuple | None = None) -> Layout:
    """The blocks and arrows of a script whose top-level blocks are `nodes` (an IF's `then` / `otherwise` hold
    its branches). `overrides` -- {path: (x, y)} -- places blocks while they are being dragged; `pending` --
    (slot, kind, at or None) -- shows a new block that is not added yet, where it WILL go (no "+" is offered then:
    one new block at a time)."""
    overrides = dict(overrides or {})
    nodes = tuple(nodes)
    shown = nodes
    if pending is not None:
        shown = _with_pending(nodes, pending)
    # 1. where everything would go automatically
    column_left, _right, _height = _extent(shown)
    cx = MARGIN + max(column_left, EXIT_ROOM + STEP_W / 2)
    auto: dict = {}
    start_auto = (cx - TERMINAL_W / 2, MARGIN)
    column_top = MARGIN + TERMINAL_H + GAP
    if shown:
        bottom = _place(shown, cx, column_top, auto)
    else:
        auto[PLACEHOLDER] = (cx - STEP_W / 2, column_top)
        bottom = column_top + STEP_H
    end_auto = (cx - TERMINAL_W / 2, bottom + GAP)

    # 2. where everything IS: a place given while dragging, then the person's own place, then the automatic one
    def where(node: Node) -> tuple[float, float]:
        if node.path in overrides:
            return overrides[node.path]
        if node.at is not None:
            return node.at
        return auto[node.path]

    boxes: list[Box] = []
    edges: list[Edge] = []
    junctions: list = []
    start_xy = overrides.get(START_KEY) or start_at or start_auto
    end_xy = overrides.get(END_KEY) or end_at or end_auto
    start = Box(START, START_KEY, start_xy[0], start_xy[1], TERMINAL_W, TERMINAL_H, START,
                _parts(START, start_xy[0], start_xy[1], TERMINAL_W, TERMINAL_H))
    end = Box(END, END_KEY, end_xy[0], end_xy[1], TERMINAL_W, TERMINAL_H, END)
    placed: dict = {}
    for node in _walk(shown):
        x, y = where(node)
        role = PENDING if node.path == PENDING_KEY else STEP
        placed[node.path] = Box(role, node.path, x, y, STEP_W, STEP_H, node.kind,
                                _parts(node.kind, x, y, STEP_W, STEP_H) if role == STEP else ())

    # 3. junctions: under each IF, below everything its branches hold
    def subtree_bottom(node: Node) -> float:
        own = placed[node.path].bottom
        if node.kind != IF:
            return own
        return junction(node)[1] + JUNCTION_R

    junction_xy: dict = {}

    def junction(node: Node) -> tuple[float, float]:
        if node.path in junction_xy:
            return junction_xy[node.path]
        box = placed[node.path]
        inner = [subtree_bottom(child) for child in node.then + node.otherwise]
        lowest = max(inner, default=box.bottom)
        y = max(box.bottom, lowest) + (GAP * 0.7 if inner else GAP) + JUNCTION_R
        junction_xy[node.path] = (box.cx, y)
        return junction_xy[node.path]

    # 4. arrows, in run order; every one between two blocks has a "+" where a new block goes
    no_plus = pending is not None

    def chain(blocks: Sequence[Node], list_path: tuple, source: tuple, label: str, target: tuple) -> None:
        previous, real = source, 0
        for k, node in enumerate(blocks):
            box = placed[node.path]
            # no "+" into a click on the match: nothing may go before it, it clicks right after the look
            slot = None if no_plus or (k == 0 and node.kind == TAP) else list_path + (real,)
            edges.append(Edge("flow", curve(previous, (box.cx, box.y)), label if k == 0 else "", slot))
            if node.kind == IF and node.path != PENDING_KEY:
                jx, jy = junction(node)
                junctions.append((node.path, jx, jy))
                chain(node.then, node.path + (THEN,), (box.x + box.w * YES_AT, box.bottom), THEN,
                      (jx, jy - JUNCTION_R))
                chain(node.otherwise, node.path + (ELSE,), (box.x + box.w * NO_AT, box.bottom), ELSE,
                      (jx, jy - JUNCTION_R))
                previous = (jx, jy + JUNCTION_R)
            else:
                previous = (box.cx, box.bottom)
            if node.kind == WAIT and node.path != PENDING_KEY:
                _exit(box, boxes, edges)
            if node.path != PENDING_KEY:
                real += 1
        slot = None if no_plus else list_path + (real,)
        edges.append(Edge("flow", curve(previous, target), label if not blocks else "", slot))

    boxes.append(start)
    if shown:
        chain(shown, (), (start.cx, start.bottom), "", (end.cx, end.y))
    else:
        x, y = auto[PLACEHOLDER]
        holder = Box(PLACEHOLDER, (), x, y, STEP_W, STEP_H)
        boxes.append(holder)
        edges.append(Edge("flow", curve((start.cx, start.bottom), (holder.cx, holder.y))))
        edges.append(Edge("flow", curve((holder.cx, holder.bottom), (end.cx, end.y))))
    boxes.extend(placed[node.path] for node in _walk(shown))
    boxes.append(end)
    order = tuple(node.path for node in _walk(nodes))
    right = max([b.right for b in boxes] + [x + JUNCTION_R for _p, x, _y in junctions])
    lowest = max([b.bottom for b in boxes] + [y + JUNCTION_R for _p, _x, y in junctions])
    return Layout(tuple(boxes), tuple(edges), tuple(junctions), order, right + MARGIN + 24.0, lowest + MARGIN,
                  {path: xy for path, xy in auto.items() if isinstance(path, tuple)} | {START_KEY: start_auto,
                                                                                      END_KEY: end_auto})


def _exit(box: Box, boxes: list, edges: list) -> None:
    """A WAIT's time-out exit: a stop marker on its left (on its right when there is no room on the left)."""
    if box.x - EXIT_GAP_X - EXIT_W >= 0:
        marker = Box(EXIT, box.path, box.x - EXIT_GAP_X - EXIT_W, box.cy - EXIT_H / 2, EXIT_W, EXIT_H, WAIT)
        edges.append(Edge("exit", ((box.x, box.cy), (marker.right, marker.cy)), "timeout"))
    else:
        marker = Box(EXIT, box.path, box.right + EXIT_GAP_X, box.cy - EXIT_H / 2, EXIT_W, EXIT_H, WAIT)
        edges.append(Edge("exit", ((box.right, box.cy), (marker.x, marker.cy)), "timeout"))
    boxes.append(marker)


def _with_pending(nodes: tuple, pending: tuple) -> tuple:
    """The block list with the pending new block shown at its slot."""
    slot, kind, at = pending
    new = Node(PENDING_KEY, kind, at)

    def insert(blocks: tuple, list_path: tuple) -> tuple:
        out = []
        for i, node in enumerate(blocks):
            if list_path + (i,) == tuple(slot):
                out.append(new)
            if node.kind == IF:
                node = Node(node.path, node.kind, node.at, insert(node.then, node.path + (THEN,)),
                            insert(node.otherwise, node.path + (ELSE,)))
            out.append(node)
        if list_path + (len(blocks),) == tuple(slot):
            out.append(new)
        return tuple(out)
    return insert(nodes, ())


def make_room(nodes: Sequence[Node], layout_now: Layout, at: tuple[float, float]) -> dict:
    """Where blocks go so that a new block placed at `at` (by hand) does not cover any: every block -- and End --
    whose top is at or below the new block's top and that overlaps it sideways moves down by one block and a gap.
    Returns {path: (x, y)} for the blocks that move."""
    x, y = at
    moves = {}
    for box in layout_now.boxes:
        if box.role not in (STEP, END) or box.y < y - 1:
            continue
        if box.right < x - 10 or box.x > x + STEP_W + 10:
            continue
        moves[box.path] = (box.x, box.y + STEP_H + GAP)
    return moves


def fit_scale(content_w: float, content_h: float, view_w: float, view_h: float, *, low: float = 0.3,
              high: float = 1.5, margin: float = 0.94) -> float:
    """The zoom at which the WHOLE content fits the view (with a little air), within [low, high]: Fit to View is
    computed from the real size of the script, never a fixed zoom."""
    if content_w <= 0 or content_h <= 0 or view_w <= 0 or view_h <= 0:
        return 1.0
    return max(low, min(high, margin * min(view_w / content_w, view_h / content_h)))


ZOOM_MIN, ZOOM_MAX = 0.3, 2.0
WHEEL_STEP = 1.1                 # one notch of the wheel: 10% closer or further, around the pointer
ZOOM_STEPS = (0.3, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def zoom_step(current: float, direction: int) -> float:
    """The next zoom level up (direction > 0) or down from `current`, on a fixed ladder (the toolbar's buttons)."""
    if direction > 0:
        return next((z for z in ZOOM_STEPS if z > current + 1e-6), ZOOM_STEPS[-1])
    return next((z for z in reversed(ZOOM_STEPS) if z < current - 1e-6), ZOOM_STEPS[0])


def wheel_zoom(current: float, notches: int) -> float:
    """The zoom after `notches` of the wheel (positive: closer), continuous, within [ZOOM_MIN, ZOOM_MAX]."""
    return max(ZOOM_MIN, min(ZOOM_MAX, current * (WHEEL_STEP ** notches)))
