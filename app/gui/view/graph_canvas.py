"""The version-4 script as blocks on an endless grid, joined by arrows (the person's design of 2026-10-25).

The canvas only draws and reports what the person does; it knows nothing of conditions or macros. The editor hands
it one `NodeView` per block (already in the person's words) and the arrows, and is told:

    on_select(node_id | None)         a block was clicked (None: the empty grid)
    on_select_edge((source, output))  an arrow was clicked
    on_activate(node_id)              a block was double-clicked
    on_moved({node_id: (x, y)})       blocks were dragged and dropped where they are now (scene units)
    on_connect(source, output, to)    an arrow was dragged from an output to a block
    on_drop(kind, (x, y))             a block of the palette was dropped on the grid
    on_delete()                       Delete was pressed

The grid is endless: only what is in view is drawn, and it pans and zooms with the blocks. Drag the empty grid (or
use the middle button) to pan; the wheel zooms around the pointer. Drag a block to move it; drag from a block's
output (the small labelled dot at its foot) to another block to join them.

While a run is on, the editor lights the block that runs (`set_highlight`): a bright frame that moves from block to
block, bright red for the block a run failed in.
"""
from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from app.gui import theme as th

GRID = 24                         # scene units per square of the grid
GRID_MAJOR = 5                    # every fifth line is stronger
CARD_W, CARD_H = 220, 78          # a block
HEADER_H = 24                     # its coloured band
CIRCLE_R = 30                     # a repeat circle
PORT_R = 6                        # an output's dot
DRAG_START = 4
ZOOM_MIN, ZOOM_MAX = 0.3, 2.5

RUNNING, VISITED, FAILED = "running", "visited", "failed"

# one colour per kind of block, light and dark
COLOURS = {
    False: {"start": "#2D6A4F", "wait": "#A96500", "check": "#1E5A8A", "mixed": "#7B3FA0", "repeat": "#0E7C86",
            "click": "#C2410C", "macro": "#8A3B5C", "stop": "#B42318"},
    True: {"start": "#74C69D", "wait": "#E3B35A", "check": "#8EC1EA", "mixed": "#C9A3E6", "repeat": "#6FD3DB",
           "click": "#F59E6B", "macro": "#E8A0BF", "stop": "#F28B82"},
}
GLOW = {False: "#F5B400", True: "#FFD54F"}            # the block that runs
ERROR_FILL = {False: "#FFD6D6", True: "#5A1414"}      # the block a run failed in
ERROR_EDGE = {False: "#E00000", True: "#FF5252"}
OUTPUT_COLOURS = {"found": ("#2E7D32", "#81C784"), "missing": ("#C62828", "#EF9A9A"),
                  "again": ("#0E7C86", "#6FD3DB"), "done": ("#5F6B63", "#AAB5AE"), "next": ("#5F6B63", "#AAB5AE")}


def block_colour(colour_key: str) -> str:
    return COLOURS[th.ACTIVE.dark].get(colour_key, th.ACTIVE.primary)


def output_colour(output: str) -> str:
    light, dark = OUTPUT_COLOURS.get(output, OUTPUT_COLOURS["next"])
    return dark if th.ACTIVE.dark else light


@dataclass(frozen=True)
class NodeView:
    """One block as the canvas shows it: everything already in the person's words."""
    id: str
    kind: str                         # start | wait | check | repeat | click | macro | stop
    colour: str                       # a key of COLOURS (a mixed check: "mixed")
    title: str
    detail: str = ""
    detail2: str = ""
    at: tuple[int, int] = (0, 0)
    outputs: tuple[tuple[str, str], ...] = ()     # (output, its label)
    warning: str = ""


@dataclass(frozen=True)
class EdgeView:
    source: str
    output: str
    target: str


class GraphCanvas:
    def __init__(self, ui, parent, *, on_select: Callable, on_select_edge: Callable, on_activate: Callable,
                 on_moved: Callable, on_connect: Callable, on_drop: Callable, on_delete: Callable) -> None:
        self.ui = ui
        self._cb = dict(select=on_select, edge=on_select_edge, activate=on_activate, moved=on_moved,
                        connect=on_connect, drop=on_drop, delete=on_delete)
        self.canvas = c = tk.Canvas(parent, highlightthickness=0, background=th.ACTIVE.canvas_bg, takefocus=1)
        self.scale = 1.0
        self.origin = (-120.0, -60.0)         # the scene point at the canvas's top-left corner
        self.nodes: dict[str, NodeView] = {}
        self.edges: list[EdgeView] = []
        self.selected: str | None = None
        self.selected_edge: tuple[str, str] | None = None
        self.highlight: dict[str, str] = {}
        self._press: dict | None = None
        c.bind("<ButtonPress-1>", self._press_1)
        c.bind("<B1-Motion>", self._motion_1)
        c.bind("<ButtonRelease-1>", self._release_1)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<ButtonPress-2>", self._pan_start)
        c.bind("<B2-Motion>", self._pan_move)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Configure>", lambda _e: self.redraw())
        c.bind("<Delete>", lambda _e: self._cb["delete"]())
        ui.on_theme(lambda: (c.configure(background=th.ACTIVE.canvas_bg), self.redraw()))

    # ---- what to show -----------------------------------------------------------------------------------------
    def show(self, nodes, edges, *, selected: str | None = None, selected_edge=None) -> None:
        dragged = self._press.get("node") if self._press and self._press.get("moving") else None
        kept = self.nodes.get(dragged) if dragged else None
        self.nodes = {n.id: n for n in nodes}
        if kept is not None and dragged in self.nodes:          # a drag under way keeps where the pointer has it
            self.nodes[dragged] = kept
        self.edges = list(edges)
        self.selected, self.selected_edge = selected, selected_edge
        self.redraw()

    def set_highlight(self, highlight: dict[str, str]) -> None:
        if highlight != self.highlight:
            self.highlight = dict(highlight)
            self.redraw()

    # ---- the view: scene units <-> canvas pixels ---------------------------------------------------------------
    def to_view(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.origin[0]) * self.scale, (y - self.origin[1]) * self.scale

    def to_scene(self, x: float, y: float) -> tuple[float, float]:
        return x / self.scale + self.origin[0], y / self.scale + self.origin[1]

    def zoom_text(self) -> str:
        return f"{round(self.scale * 100)}%"

    def set_scale(self, scale: float, *, anchor: tuple[float, float] | None = None) -> None:
        scale = max(ZOOM_MIN, min(ZOOM_MAX, scale))
        if anchor is None:
            anchor = (self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        sx, sy = self.to_scene(*anchor)
        self.scale = scale
        self.origin = (sx - anchor[0] / scale, sy - anchor[1] / scale)
        self.redraw()

    def zoom_in(self) -> None:
        self.set_scale(self.scale * 1.2)

    def zoom_out(self) -> None:
        self.set_scale(self.scale / 1.2)

    def fit(self) -> None:
        """Every block in view (at most at 100%)."""
        if not self.nodes:
            return
        boxes = [self._box(n) for n in self.nodes.values()]
        x0, y0 = min(b[0] for b in boxes) - 60, min(b[1] for b in boxes) - 60
        x1, y1 = max(b[2] for b in boxes) + 60, max(b[3] for b in boxes) + 60
        w, h = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        self.scale = max(ZOOM_MIN, min(1.0, w / (x1 - x0), h / (y1 - y0)))
        self.origin = ((x0 + x1) / 2 - w / 2 / self.scale, (y0 + y1) / 2 - h / 2 / self.scale)
        self.redraw()

    def centre(self) -> tuple[int, int]:
        """The scene point in the middle of the view (where a block clicked in the palette goes)."""
        x, y = self.to_scene(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)
        return round(x / GRID) * GRID, round(y / GRID) * GRID

    def ensure_visible(self, node_id: str) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        x0, y0, x1, y1 = self._box(node)
        w, h = self.canvas.winfo_width() / self.scale, self.canvas.winfo_height() / self.scale
        ox, oy = self.origin
        if x0 < ox or x1 > ox + w or y0 < oy or y1 > oy + h:
            self.origin = ((x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2)
            self.redraw()

    # ---- geometry of a block (scene units) --------------------------------------------------------------------
    @staticmethod
    def _box(node: NodeView) -> tuple[float, float, float, float]:
        x, y = node.at
        if node.kind == "repeat":
            return x - CIRCLE_R, y - CIRCLE_R, x + CIRCLE_R, y + CIRCLE_R
        if node.kind == "start":
            return x - 90, y - 22, x + 90, y + 22
        return x - CARD_W / 2, y - CARD_H / 2, x + CARD_W / 2, y + CARD_H / 2

    def _input(self, node: NodeView) -> tuple[float, float]:
        x0, y0, x1, _ = self._box(node)
        return (x0 + x1) / 2, y0

    def _outputs(self, node: NodeView) -> list[tuple[str, str, float, float]]:
        """(output, label, x, y) of each output dot."""
        x0, _, x1, y1 = self._box(node)
        n = len(node.outputs)
        if node.kind == "repeat" and n:
            cx, cy = node.at
            spots = [(cx - CIRCLE_R - 4, cy), (cx + CIRCLE_R + 4, cy)] if n == 2 else [(cx - CIRCLE_R - 4, cy)]
            return [(out, label, sx, sy) for (out, label), (sx, sy) in zip(node.outputs, spots)]
        return [(out, label, x0 + (x1 - x0) * (i + 1) / (n + 1), y1) for i, (out, label) in enumerate(node.outputs)]

    # ---- drawing ----------------------------------------------------------------------------------------------
    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._grid()
        for edge in self.edges:
            self._edge(edge)
        for node in self.nodes.values():
            self._node(node)
        if self._press and self._press.get("wire"):
            self._wire()

    def _grid(self) -> None:
        c, theme = self.canvas, th.ACTIVE
        w, h = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        x0, y0 = self.to_scene(0, 0)
        x1, y1 = self.to_scene(w, h)
        step = GRID if GRID * self.scale >= 8 else GRID * GRID_MAJOR
        minor, major = theme.canvas_dot, theme.border
        i = math.floor(x0 / step)
        while i * step <= x1:
            vx = (i * step - self.origin[0]) * self.scale
            strong = (i * step) % (GRID * GRID_MAJOR) == 0
            c.create_line(vx, 0, vx, h, fill=major if strong else minor, tags=("grid",))
            i += 1
        j = math.floor(y0 / step)
        while j * step <= y1:
            vy = (j * step - self.origin[1]) * self.scale
            strong = (j * step) % (GRID * GRID_MAJOR) == 0
            c.create_line(0, vy, w, vy, fill=major if strong else minor, tags=("grid",))
            j += 1

    def _font(self, size: int, bold: bool = False):
        return (th.FONT_SEMIBOLD if bold else th.FONT, max(6, round(size * self.scale)))

    def _node(self, node: NodeView) -> None:
        c, theme, s = self.canvas, th.ACTIVE, self.scale
        x0, y0, x1, y1 = (v for v in self._box(node))
        vx0, vy0 = self.to_view(x0, y0)
        vx1, vy1 = self.to_view(x1, y1)
        colour = block_colour(node.colour)
        state = self.highlight.get(node.id)
        chosen = node.id == self.selected
        outline = ERROR_EDGE[theme.dark] if state == FAILED else GLOW[theme.dark] if state == RUNNING else \
            (theme.focus if chosen else theme.border_strong)
        width = (5 if state in (RUNNING, FAILED) else 3 if chosen else 1) * max(s, 0.6)
        fill = ERROR_FILL[theme.dark] if state == FAILED else theme.surface
        tag = ("node", f"node:{node.id}")
        if state == RUNNING:                                   # a soft glow around the block that runs
            pad = 8 * s
            if node.kind == "repeat":
                c.create_oval(vx0 - pad, vy0 - pad, vx1 + pad, vy1 + pad, fill="", outline=GLOW[theme.dark],
                              width=2 * s, dash=(4, 3), tags=tag)
            else:
                c.create_rectangle(vx0 - pad, vy0 - pad, vx1 + pad, vy1 + pad, fill="", outline=GLOW[theme.dark],
                                   width=2 * s, dash=(4, 3), tags=tag)
        if node.kind == "repeat":
            c.create_oval(vx0, vy0, vx1, vy1, fill=ERROR_FILL[theme.dark] if state == FAILED else colour,
                          outline=outline, width=width, tags=tag)
            cx, cy = self.to_view(*node.at)
            c.create_text(cx, cy - 2 * s, text="↻", fill="#FFFFFF", font=self._font(16, True), tags=tag)
            c.create_text(cx, vy1 + 12 * s, text=node.detail, fill=theme.text, font=self._font(9), tags=tag)
        elif node.kind == "start":
            c.create_rectangle(vx0, vy0, vx1, vy1, fill=colour, outline=outline, width=width, tags=tag)
            c.create_text((vx0 + vx1) / 2, (vy0 + vy1) / 2 - (5 * s if node.detail else 0), text=node.title,
                          fill="#FFFFFF", font=self._font(11, True), tags=tag)
            if node.detail:
                c.create_text((vx0 + vx1) / 2, (vy0 + vy1) / 2 + 9 * s, text=node.detail, fill="#FFFFFF",
                              font=self._font(8), tags=tag)
        else:
            c.create_rectangle(vx0, vy0, vx1, vy1, fill=fill, outline=outline, width=width, tags=tag)
            c.create_rectangle(vx0, vy0, vx1, vy0 + HEADER_H * s, fill=colour, outline="", tags=tag)
            c.create_text(vx0 + 10 * s, vy0 + HEADER_H * s / 2, text=node.title, anchor="w", fill="#FFFFFF",
                          font=self._font(10, True), tags=tag)
            if node.warning:
                c.create_text(vx1 - 10 * s, vy0 + HEADER_H * s / 2, text="!", anchor="e", fill="#FFFFFF",
                              font=self._font(11, True), tags=tag)
            c.create_text(vx0 + 10 * s, vy0 + (HEADER_H + 15) * s, text=self._clip(node.detail, 30), anchor="w",
                          fill=theme.text, font=self._font(9), tags=tag)
            if node.detail2:
                c.create_text(vx0 + 10 * s, vy0 + (HEADER_H + 33) * s, text=self._clip(node.detail2, 32),
                              anchor="w", fill=theme.text_muted, font=self._font(8), tags=tag)
        for output, label, px, py in self._outputs(node):
            vx, vy = self.to_view(px, py)
            r = PORT_R * s
            ptag = ("port", f"port:{node.id}:{output}")
            c.create_oval(vx - r, vy - r, vx + r, vy + r, fill=output_colour(output), outline=theme.surface,
                          width=max(1, 2 * s), tags=ptag)
            if node.kind == "repeat":
                c.create_text(vx + (-9 if px < node.at[0] else 9) * s, vy + 12 * s, text=label,
                              anchor="e" if px < node.at[0] else "w", fill=output_colour(output),
                              font=self._font(8, True), tags=ptag)
            else:                                              # beside its dot, out of the way of its arrow
                left = len(node.outputs) > 1 and px < (x0 + x1) / 2
                c.create_text(vx + (-9 if left else 9) * s, vy + 9 * s, text=label, anchor="e" if left else "w",
                              fill=output_colour(output), font=self._font(8, True), tags=ptag)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _edge(self, edge: EdgeView) -> None:
        source, target = self.nodes.get(edge.source), self.nodes.get(edge.target)
        if source is None or target is None:
            return
        spot = next(((px, py) for out, _l, px, py in self._outputs(source) if out == edge.output), None)
        if spot is None:
            return
        tx, ty = self._input(target)
        chosen = self.selected_edge == (edge.source, edge.output)
        self._curve(spot, (tx, ty), output_colour(edge.output), chosen, ("edge", f"edge:{edge.source}:{edge.output}"),
                    sideways=source.kind == "repeat")

    def _curve(self, a, b, colour, chosen, tags, *, sideways=False) -> None:
        (ax, ay), (bx, by) = a, b
        bend = max(40.0, abs(by - ay) / 2)
        if sideways:                                          # out of a circle's side, round into the block's top
            c1 = (ax + (-1 if bx <= ax else 1) * bend, ay)
        else:
            c1 = (ax, ay + bend)
        c2 = (bx, by - bend)
        points = []
        for i in range(17):
            t = i / 16
            x = (1 - t) ** 3 * ax + 3 * (1 - t) ** 2 * t * c1[0] + 3 * (1 - t) * t ** 2 * c2[0] + t ** 3 * bx
            y = (1 - t) ** 3 * ay + 3 * (1 - t) ** 2 * t * c1[1] + 3 * (1 - t) * t ** 2 * c2[1] + t ** 3 * by
            points.extend(self.to_view(x, y))
        s = self.scale
        self.canvas.create_line(*points, fill=colour, width=(3.5 if chosen else 2) * max(s, 0.6), smooth=True,
                                arrow=tk.LAST, arrowshape=(10 * s, 12 * s, 4 * s), tags=tags)

    def _wire(self) -> None:
        press = self._press
        self._curve(press["from"], self.to_scene(*press["now"]), output_colour(press["output"]), True, ("wire",))

    # ---- what is under the pointer ----------------------------------------------------------------------------
    def _hit(self, x: float, y: float):
        """("port", node, output) | ("node", node) | ("edge", source, output) | None, at canvas pixels (x, y)."""
        c = self.canvas
        for item in reversed(c.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            for tag in c.gettags(item):
                if tag.startswith("port:"):
                    _, node, output = tag.split(":", 2)
                    return "port", node, output
        for item in reversed(c.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            for tag in c.gettags(item):
                if tag.startswith("node:"):
                    return "node", tag[5:]
        for item in reversed(c.find_overlapping(x - 4, y - 4, x + 4, y + 4)):
            for tag in c.gettags(item):
                if tag.startswith("edge:"):
                    _, source, output = tag.split(":", 2)
                    return "edge", source, output
        return None

    def node_at(self, x: float, y: float) -> str | None:
        """The block at canvas pixels (x, y), by its drawn box."""
        sx, sy = self.to_scene(x, y)
        for node in reversed(list(self.nodes.values())):
            x0, y0, x1, y1 = self._box(node)
            if x0 <= sx <= x1 and y0 <= sy <= y1:
                return node.id
        return None

    # ---- the pointer ------------------------------------------------------------------------------------------
    def _press_1(self, event) -> None:
        self.canvas.focus_set()
        hit = self._hit(event.x, event.y)
        if hit and hit[0] == "port":
            node = self.nodes[hit[1]]
            spot = next((px, py) for out, _l, px, py in self._outputs(node) if out == hit[2])
            self._press = {"wire": True, "source": hit[1], "output": hit[2], "from": spot, "now": (event.x, event.y)}
        elif hit and hit[0] == "node":
            node = self.nodes[hit[1]]
            self._press = {"node": hit[1], "x": event.x, "y": event.y, "at": node.at, "moving": False}
        elif hit and hit[0] == "edge":
            self._press = None
            self._cb["edge"]((hit[1], hit[2]))
        else:
            self._press = {"pan": True, "x": event.x, "y": event.y, "origin": self.origin, "moved": False}

    def _motion_1(self, event) -> None:
        press = self._press
        if not press:
            return
        if press.get("wire"):
            press["now"] = (event.x, event.y)
            self.redraw()
        elif press.get("node"):
            dx, dy = event.x - press["x"], event.y - press["y"]
            if not press["moving"] and abs(dx) + abs(dy) < DRAG_START:
                return
            press["moving"] = True
            ax, ay = press["at"]
            at = (round((ax + dx / self.scale) / (GRID / 2)) * (GRID // 2),
                  round((ay + dy / self.scale) / (GRID / 2)) * (GRID // 2))
            node = self.nodes[press["node"]]
            self.nodes[node.id] = NodeView(node.id, node.kind, node.colour, node.title, node.detail, node.detail2,
                                           at, node.outputs, node.warning)
            self.redraw()
        elif press.get("pan"):
            dx, dy = event.x - press["x"], event.y - press["y"]
            if abs(dx) + abs(dy) >= DRAG_START:
                press["moved"] = True
            ox, oy = press["origin"]
            self.origin = (ox - dx / self.scale, oy - dy / self.scale)
            self.redraw()

    def _release_1(self, event) -> None:
        press, self._press = self._press, None
        if not press:
            return
        if press.get("wire"):
            target = self.node_at(event.x, event.y)
            self.redraw()
            if target is not None:
                self._cb["connect"](press["source"], press["output"], target)
        elif press.get("node"):
            if press["moving"]:
                self._cb["moved"]({press["node"]: self.nodes[press["node"]].at})
            else:
                self._cb["select"](press["node"])
        elif press.get("pan") and not press["moved"]:
            self._cb["select"](None)

    def _double(self, event) -> None:
        hit = self._hit(event.x, event.y)
        if hit and hit[0] == "node":
            self._cb["activate"](hit[1])

    def _pan_start(self, event) -> None:
        self._press = {"pan": True, "x": event.x, "y": event.y, "origin": self.origin, "moved": True}

    def _pan_move(self, event) -> None:
        self._motion_1(event)

    def _wheel(self, event) -> str:
        steps = event.delta / 120
        self.set_scale(self.scale * (1.1 ** steps), anchor=(event.x, event.y))
        return "break"

    # ---- a block dropped from the palette -----------------------------------------------------------------------
    def drop(self, kind: str, screen_x: int, screen_y: int) -> bool:
        """A palette block released at screen point (x, y): True when it was over the grid (and was added there)."""
        c = self.canvas
        x, y = screen_x - c.winfo_rootx(), screen_y - c.winfo_rooty()
        if not (0 <= x < c.winfo_width() and 0 <= y < c.winfo_height()):
            return False
        sx, sy = self.to_scene(x, y)
        self._cb["drop"](kind, (round(sx / (GRID / 2)) * (GRID // 2), round(sy / (GRID / 2)) * (GRID // 2)))
        return True
