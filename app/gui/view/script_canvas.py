"""The script as connected blocks on a canvas -- drawn from `app.gui.flow_layout`, moved by hand.

The canvas only draws and reports what the person does; it knows nothing of conditions or macros. The editor hands
it the script's shape (`flow_layout.Node`s, with any place the person gave a block) and one `BlockView` per block
(already in the person's words), and is told:

    on_select(path, part)        a block was clicked -- and which part of it ("condition", "timing", "macro", "op",
                                 "delay" for Start, or "" for the rest), so the editor can open just that part;
    on_activate(path, part)      a double click: bring that part's field forward;
    on_plus(slot, screen, scene) the "+" in the middle of an arrow: a new block goes there;
    on_moved({path: (x, y)})     blocks were dragged and dropped: where they are now (an IF moves with everything
                                 its branches hold); the arrows followed them while they moved;
    on_placeholder()             the "add the first step" box of an empty script.

Navigation: drag a block to move it; drag the empty canvas (or use the middle button) to pan; the wheel zooms
around the pointer (Shift+wheel scrolls sideways); `zoom_in`, `zoom_out`, `zoom_reset` and `fit` (Fit to View)
are the toolbar's buttons. Keyboard: Up / Down choose the previous / next block in run order, Delete asks the editor
to delete it, + / - / 0 zoom. The background is a grid of squares that zooms and pans with the blocks.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Callable

from app.gui import flow_layout as fl
from app.gui import theme as th
from app.gui.view import widgets as w

DRAG_START = 4                  # pixels the pointer moves before a press on a block becomes a drag
GRID_CELL = 24                  # scene units per square of the background grid
GRID_MAJOR = 5                  # every fifth line of the grid is stronger


@dataclass(frozen=True)
class BlockView:
    """One block as the canvas shows it: everything already translated and shortened by the editor."""
    kind: str                         # fl.WAIT | fl.IF | fl.RUN | fl.TAP
    title: str
    detail: str
    detail2: str = ""                 # a second line (a WAIT's time limit)
    number: str = ""                  # its number in run order
    badge: str = ""                   # a short tag, e.g. "OCR" for a text condition
    warning: str = ""                 # why this block may not run as expected here ("" = nothing to say)


@dataclass(frozen=True)
class CanvasWords:
    """The few fixed words the canvas draws itself (translated by the editor)."""
    start: str = "Start"
    end: str = "End"
    yes: str = "yes"
    no: str = "no"
    timeout: str = "time out"
    stop: str = "stop"
    placeholder: str = "+ Add the first step"
    pending_hint: str = ""
    start_detail: str = ""            # under "Start": the start delay, or that there is none


@dataclass
class _Scene:
    nodes: tuple = ()
    views: dict = field(default_factory=dict)
    selected: tuple | None = None
    start_at: tuple | None = None
    end_at: tuple | None = None
    pending: tuple | None = None      # (slot, kind, at)
    pending_title: str = ""
    pending_invalid: bool = False
    words: CanvasWords = field(default_factory=CanvasWords)


def rounded(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, r: float, **kw) -> int:
    """A rounded rectangle (Tk's canvas has none): a smoothed polygon through the corner control points."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
              x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return canvas.create_polygon(points, smooth=True, **kw)


def _font(size: float, weight: str = "") -> tuple:
    family = th.FONT_SEMIBOLD if weight == "bold" else th.FONT
    return (family, max(6, int(round(size))))


def key(path: tuple) -> str:
    """A path as a canvas tag: (2, "then", 0) -> "b:2/then/0"."""
    return "b:" + "/".join(str(p) for p in path)


class ScriptCanvas:
    def __init__(self, ui, parent, *, on_select: Callable[[tuple, str], None],
                 on_activate: Callable[[tuple, str], None] | None = None,
                 on_placeholder: Callable[[], None] | None = None,
                 on_delete: Callable[[], None] | None = None,
                 on_zoom: Callable[[float], None] | None = None,
                 on_plus: Callable[[tuple, tuple, tuple], None] | None = None,
                 on_moved: Callable[[dict], None] | None = None) -> None:
        self.ui = ui
        self._on_select, self._on_activate = on_select, on_activate
        self._on_placeholder, self._on_delete, self._on_zoom = on_placeholder, on_delete, on_zoom
        self._on_plus, self._on_moved = on_plus, on_moved
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.frame, width=340, height=300, highlightthickness=1, takefocus=1,
                                borderwidth=0, cursor="arrow")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar = ttk.Scrollbar(self.frame, orient="horizontal", command=self.canvas.xview)
        hbar.grid(row=1, column=0, sticky="ew")
        self._grid_for: tuple | None = None       # (cell, width, height, theme) the grid picture was made for
        self._grid_picture: tk.PhotoImage | None = None
        self._grid_item: int | None = None
        self._grid_size = GRID_CELL * GRID_MAJOR
        self.canvas.configure(xscrollcommand=self._following(w.autohide(hbar)),
                              yscrollcommand=self._following(w.autohide(vbar)))
        self.scale = 1.0
        self.scene = _Scene()
        self.layout: fl.Layout = fl.layout(())
        self._drawn = None
        self._press: tuple | None = None          # what the button went down on
        self._drag: dict | None = None            # the blocks being moved, while they are
        self._hover: tuple | None = None          # (path, part) or ("plus", slot) under the pointer
        c = self.canvas
        c.bind("<ButtonPress-1>", self._press_1)
        c.bind("<B1-Motion>", self._motion_1)
        c.bind("<ButtonRelease-1>", self._release_1)
        c.bind("<Double-Button-1>", self._double)
        c.bind("<ButtonPress-2>", lambda e: (c.scan_mark(e.x, e.y), None)[1])
        c.bind("<B2-Motion>", lambda e: c.scan_dragto(e.x, e.y, gain=1))
        c.bind("<Motion>", self._hover_at)
        c.bind("<Leave>", lambda _e: self._set_hover(None))
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Configure>", lambda _e: self.redraw(force=True))
        for sequence, handler in (("<Up>", lambda _e: self._step(-1)), ("<Down>", lambda _e: self._step(1)),
                                  ("<Delete>", lambda _e: self._on_delete and self._on_delete()),
                                  ("<plus>", lambda _e: self.zoom_in()), ("<equal>", lambda _e: self.zoom_in()),
                                  ("<KP_Add>", lambda _e: self.zoom_in()), ("<minus>", lambda _e: self.zoom_out()),
                                  ("<KP_Subtract>", lambda _e: self.zoom_out()),
                                  ("<Key-0>", lambda _e: self.zoom_reset())):
            c.bind(sequence, handler)
        ui.on_theme(lambda: self.redraw(force=True))

    # ---- what to show ---------------------------------------------------------------------------------
    def show(self, nodes, views, *, selected: tuple | None, words: CanvasWords, start_at: tuple | None = None,
             end_at: tuple | None = None, pending: tuple | None = None, pending_title: str = "",
             pending_invalid: bool = False) -> None:
        self.scene = _Scene(tuple(nodes), dict(views), None if selected is None else tuple(selected), start_at,
                            end_at, pending, pending_title, pending_invalid, words)
        self.layout = self._scene_layout()
        self.redraw()

    def _scene_layout(self) -> fl.Layout:
        """The blocks where the script has them (no drag under way)."""
        s = self.scene
        return fl.layout(s.nodes, start_at=s.start_at, end_at=s.end_at, pending=s.pending)

    # ---- zoom -------------------------------------------------------------------------------------------
    def zoom_text(self) -> str:
        return f"{round(self.scale * 100)}%"

    def set_scale(self, scale: float, *, anchor: tuple[int, int] | None = None) -> None:
        scale = max(fl.ZOOM_MIN, min(fl.ZOOM_MAX, scale))
        if abs(scale - self.scale) < 1e-9:
            return
        c = self.canvas
        ax, ay = anchor if anchor is not None else (c.winfo_width() // 2, c.winfo_height() // 2)
        scene_x, scene_y = c.canvasx(ax) / self.scale, c.canvasy(ay) / self.scale
        self.scale = scale
        self.redraw(force=True)
        self._scroll_to(scene_x * scale - ax, scene_y * scale - ay)      # the point under the pointer stays put
        if self._on_zoom:
            self._on_zoom(scale)

    def zoom_in(self) -> None:
        self.set_scale(fl.zoom_step(self.scale, +1))

    def zoom_out(self) -> None:
        self.set_scale(fl.zoom_step(self.scale, -1))

    def zoom_reset(self) -> None:
        self.set_scale(1.0)

    def fit(self) -> None:
        """Fit to View: the zoom at which the whole script -- every block and branch -- is visible."""
        c = self.canvas
        view_w = c.winfo_width() if c.winfo_width() > 1 else int(c.cget("width"))
        view_h = c.winfo_height() if c.winfo_height() > 1 else int(c.cget("height"))
        self.scale = fl.fit_scale(self.layout.width, self.layout.height, view_w, view_h)
        self.redraw(force=True)
        c.xview_moveto(0)
        c.yview_moveto(0)
        if self._on_zoom:
            self._on_zoom(self.scale)

    def _scroll_to(self, left: float, top: float) -> None:
        c = self.canvas
        region = c.cget("scrollregion").split()
        if len(region) != 4:
            return
        width, height = float(region[2]), float(region[3])
        if width > 0:
            c.xview_moveto(max(0.0, left) / width)
        if height > 0:
            c.yview_moveto(max(0.0, top) / height)

    def ensure_visible(self, path) -> None:
        box = self.layout.box_of(tuple(path)) if path is not None else None
        if box is None:
            return
        c, s = self.canvas, self.scale
        left, top = c.canvasx(0), c.canvasy(0)
        right, bottom = c.canvasx(c.winfo_width()), c.canvasy(c.winfo_height())
        new_left, new_top = left, top
        if box.y * s < top or box.bottom * s > bottom:
            new_top = box.y * s - 40
        if box.x * s < left or box.right * s > right:
            new_left = box.x * s - 40
        if (new_left, new_top) != (left, top):
            self._scroll_to(new_left, new_top)

    # ---- input --------------------------------------------------------------------------------------------
    def _scene_point(self, event) -> tuple[float, float]:
        return self.canvas.canvasx(event.x) / self.scale, self.canvas.canvasy(event.y) / self.scale

    def block_at(self, x: float, y: float) -> fl.Box | None:
        """The block under a SCENE point (zoom 1.0 units)."""
        return self.layout.hit(x, y)

    def _press_1(self, event) -> None:
        self.canvas.focus_set()
        x, y = self._scene_point(event)
        slot = self.layout.plus_at(x, y)
        if slot is not None and self._on_plus:
            self._press = ("plus", slot)
            return
        box = self.block_at(x, y)
        if box is None:
            self._press = ("pan",)
            self.canvas.scan_mark(event.x, event.y)
            self.canvas.configure(cursor="fleur")
            return
        if box.role == fl.PLACEHOLDER:
            self._press = None
            if self._on_placeholder:
                self._on_placeholder()
            return
        if box.role in (fl.STEP, fl.START, fl.END):
            self._press = ("block", box, box.part_at(x, y), event.x, event.y)
            return
        self._press = None

    def _motion_1(self, event) -> None:
        press = self._press
        if press is None:
            return
        if press[0] == "pan":
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            return
        if press[0] != "block" or self._on_moved is None:
            return
        _, box, _part, x0, y0 = press
        if self._drag is None:
            if abs(event.x - x0) < DRAG_START and abs(event.y - y0) < DRAG_START:
                return
            self._begin_drag(box, x0, y0)
        self._drag_to(event.x, event.y)

    def _begin_drag(self, box: fl.Box, x0: int, y0: int) -> None:
        """An IF moves with every block its branches hold; any other block moves alone."""
        group = [box.path] + (list(self.layout.descendants(box.path)) if box.kind == fl.IF else [])
        origin = {}
        for path in group:
            shape = self.layout.box_of(path)
            if shape is not None:
                origin[path] = (shape.x, shape.y)
        self._drag = {"origin": origin, "x0": x0, "y0": y0, "last": (x0, y0), "now": dict(origin)}
        self.canvas.configure(cursor="fleur")
        self._set_hover(None)

    def _drag_to(self, x: int, y: int) -> None:
        drag, s, c = self._drag, self.scale, self.canvas
        dx, dy = (x - drag["x0"]) / s, (y - drag["y0"]) / s
        # never past the scene's edges (0..MAX_POSITION): the group stops there, together, in its own shape
        xs = [ox for ox, _oy in drag["origin"].values()]
        ys = [oy for _ox, oy in drag["origin"].values()]
        dx = min(max(dx, -min(xs)), max(0.0, fl.MAX_POSITION - max(xs)))
        dy = min(max(dy, -min(ys)), max(0.0, fl.MAX_POSITION - max(ys)))
        now = {path: (ox + dx, oy + dy) for path, (ox, oy) in drag["origin"].items()}
        first = next(iter(drag["origin"]))
        step_x = (now[first][0] - drag["now"][first][0]) * s
        step_y = (now[first][1] - drag["now"][first][1]) * s
        for path in now:
            c.move(key(path), step_x, step_y)
        drag["now"] = now
        self.layout = fl.layout(self.scene.nodes, start_at=self.scene.start_at, end_at=self.scene.end_at,
                                overrides=now, pending=self.scene.pending)
        self._links()                                  # the arrows follow, every move of the pointer

    def _release_1(self, event) -> None:
        press, drag = self._press, self._drag
        self._press = self._drag = None
        self.canvas.configure(cursor="arrow")
        if press is None:
            return
        if press[0] == "pan":
            return
        if drag is not None:
            moved = {path: (int(round(x)), int(round(y))) for path, (x, y) in drag["now"].items()}
            if moved != {p: (int(round(x)), int(round(y))) for p, (x, y) in drag["origin"].items()}:
                drawn = self._drawn
                self._on_moved(moved)
                if self._drawn is not drawn:
                    return                             # drawn again, from the script as it now is
            # not moved after all, or the move was not taken: the blocks go back where the script has them
            self.layout = self._scene_layout()
            self.redraw(force=True)
            return
        if press[0] == "plus":
            x, y = self._scene_point(event)
            if self.layout.plus_at(x, y) == press[1] and self._on_plus:
                edge = next(e for e in self.layout.edges if e.slot == press[1] and e.plus is not None)
                self._on_plus(press[1], (event.x_root, event.y_root), edge.plus)
            return
        _, box, part, _x0, _y0 = press
        self._on_select(box.path, part)

    def _double(self, event) -> None:
        box = self.block_at(*self._scene_point(event))
        if box is not None and box.role in (fl.STEP, fl.START) and self._on_activate:
            x, y = self._scene_point(event)
            self._on_activate(box.path, box.part_at(x, y))

    def _hover_at(self, event) -> None:
        if self._press is not None:
            return
        x, y = self._scene_point(event)
        slot = self.layout.plus_at(x, y) if self._on_plus else None
        if slot is not None:
            self._set_hover(("plus", slot))
            return
        box = self.block_at(x, y)
        if box is not None and box.role in (fl.STEP, fl.START):
            self._set_hover((box.path, box.part_at(x, y)))
        else:
            self._set_hover(None)

    def _set_hover(self, hover) -> None:
        if hover == self._hover:
            return
        was_plus = self._hover is not None and self._hover[0] == "plus"
        self._hover = hover
        if was_plus or (hover is not None and hover[0] == "plus"):
            self._links()                              # a "+" is drawn lit while the pointer is on it
        self._paint_hover()

    def _wheel(self, event) -> str:
        if event.state & 0x0001:                      # Shift: sideways
            self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        notches = event.delta / 120.0 if event.delta else 0.0
        if notches:                                   # the wheel alone zooms, around the pointer
            self.set_scale(fl.wheel_zoom(self.scale, notches), anchor=(event.x, event.y))
        return "break"

    def _step(self, delta: int) -> str:
        order = self.layout.order
        if not order:
            return "break"
        current = self.scene.selected
        if current in order:
            index = max(0, min(len(order) - 1, order.index(current) + delta))
        else:
            index = 0
        self._on_select(order[index], "")
        return "break"

    # ---- drawing ------------------------------------------------------------------------------------------
    def redraw(self, *, force: bool = False) -> None:
        c = self.canvas
        # by value: an unchanged script is not drawn again on every render of the window
        signature = (self.scene, self.scale, c.winfo_width(), c.winfo_height(), th.ACTIVE.id)
        if not force and signature == self._drawn:
            return
        self._drawn = signature
        theme = th.ACTIVE
        s = self.scale
        c.delete("all")
        c.configure(background=theme.canvas_bg, highlightbackground=theme.border, highlightcolor=theme.focus)
        border = 2 * int(c.cget("highlightthickness"))
        width = max(self.layout.width * s, c.winfo_width() - border)      # the visible area, inside the frame
        height = max(self.layout.height * s, c.winfo_height() - border)
        c.configure(scrollregion=(0, 0, width, height))
        self._grid()
        for box in self.layout.boxes:
            self._box(box)
        self._links()
        self._paint_hover()

    def _links(self) -> None:
        """The arrows, their "+" and the junctions: drawn again on every move while blocks are dragged, so they
        follow the blocks (the blocks themselves are moved, not redrawn)."""
        c = self.canvas
        c.delete("links")
        for edge in self.layout.edges:
            self._edge(edge)
        for path, x, y in self.layout.junctions:
            self._junction(x, y)
        c.tag_lower("links")
        c.tag_raise("plus")
        c.tag_lower("grid")

    def _grid(self) -> None:
        """The squares behind the blocks: ONE picture of the visible area, tiled by Tk itself from a single tile
        (thin lines, a stronger one every fifth) and moved along with the view (`_place_grid`). It zooms with the
        blocks: a square is GRID_CELL scene units."""
        self._grid_item = None
        c, theme = self.canvas, th.ACTIVE
        cell = GRID_CELL * self.scale
        while cell < 10:                                  # far away: bigger squares rather than a grey wash
            cell *= GRID_MAJOR
        cell = max(1, round(cell))
        size = cell * GRID_MAJOR
        width, height = max(c.winfo_width(), 1) + size, max(c.winfo_height(), 1) + size
        signature = (cell, width, height, theme.id)
        if self._grid_for != signature:
            tile = tk.PhotoImage(master=c, width=size, height=size)
            tile.put(theme.canvas_bg, to=(0, 0, size, size))
            for i in range(1, GRID_MAJOR):
                tile.put(theme.canvas_dot, to=(i * cell, 0, i * cell + 1, size))
                tile.put(theme.canvas_dot, to=(0, i * cell, size, i * cell + 1))
            tile.put(theme.border_strong, to=(0, 0, 1, size))
            tile.put(theme.border_strong, to=(0, 0, size, 1))
            picture = tk.PhotoImage(master=c, width=width, height=height)
            picture.tk.call(picture, "copy", tile, "-to", 0, 0, width, height)      # tiled in C, not in Python
            self._grid_picture, self._grid_for = picture, signature
        self._grid_size = size
        self._grid_item = c.create_image(0, 0, image=self._grid_picture, anchor="nw", tags=("grid",))
        self._place_grid()

    def _place_grid(self) -> None:
        """Keep the grid picture under the visible part of the canvas, on the grid (the view scrolled or moved)."""
        if self._grid_item is None:
            return
        c, size = self.canvas, self._grid_size
        c.coords(self._grid_item, (c.canvasx(0) // size) * size, (c.canvasy(0) // size) * size)

    def _following(self, scrollbar_set):
        """A scroll command that also moves the grid picture with the view."""
        def scrolled(first, last):
            scrollbar_set(first, last)
            self._place_grid()
        return scrolled

    def _edge(self, edge: fl.Edge) -> None:
        c, s, theme = self.canvas, self.scale, th.ACTIVE
        points = [v * s for point in edge.points for v in point]
        width = max(1.0, 1.8 * s)
        arrow = (8 * s, 10 * s, 4 * s)
        if edge.role == "exit":
            c.create_line(*points, fill=theme.error, width=max(1.0, 1.2 * s), dash=(5, 3), arrow="last",
                          arrowshape=arrow, tags=("links",))
            if s >= 0.6:
                x, y = points[2], points[3]
                c.create_text(x + (fl.EXIT_W * s / 2 if x > points[0] else -fl.EXIT_W * s / 2),
                              y + fl.EXIT_H * s / 2 + 9 * s, text=self.scene.words.timeout, fill=theme.error,
                              font=_font(8 * s), tags=("links",))
            return
        colour = theme.block_if if edge.label else theme.border_strong
        c.create_line(*points, smooth="raw", splinesteps=24, fill=colour, width=width, arrow="last",
                      arrowshape=arrow, tags=("links",))
        if edge.label:
            words = self.scene.words
            c.create_text(points[0] + 6 * s, points[1] + 12 * s, anchor="w", fill=theme.block_if,
                          text=words.yes if edge.label == fl.THEN else words.no, font=_font(8.5 * s, "bold"),
                          tags=("links",))
        point = edge.plus
        if point is not None:
            px, py = point[0] * s, point[1] * s
            r = max(7.0, fl.PLUS_R * s)
            hot = self._hover == ("plus", edge.slot)
            c.create_oval(px - r, py - r, px + r, py + r, fill=theme.primary if hot else theme.surface,
                          outline=theme.primary, width=max(1.0, 1.4 * s), tags=("links", "plus"))
            c.create_text(px, py - 1, text="+", fill=theme.surface if hot else theme.primary,
                          font=_font(max(9.0, 12 * s), "bold"), tags=("links", "plus"))

    def _junction(self, x: float, y: float) -> None:
        c, s, theme = self.canvas, self.scale, th.ACTIVE
        r = max(2.5, fl.JUNCTION_R * s)
        c.create_oval(x * s - r, y * s - r, x * s + r, y * s + r, fill=theme.border_strong, outline="",
                      tags=("links",))

    def _paint_hover(self) -> None:
        """The part of a block the pointer is over, outlined: a click there opens just that part."""
        c, s, theme = self.canvas, self.scale, th.ACTIVE
        c.delete("hover")
        hover = self._hover
        cursor = "arrow"
        if hover is not None and hover[0] == "plus":
            cursor = "hand2"
        elif hover is not None:
            path, part = hover
            box = self.layout.box_of(path)
            shape = next((p for p in box.parts if p.name == part), None) if box is not None else None
            if shape is not None:
                rounded(c, (shape.x - 4) * s, shape.y * s, (shape.x + shape.w) * s, (shape.y + shape.h) * s, 6 * s,
                        fill="", outline=theme.primary, width=max(1.0, 1.2 * s), dash=(3, 2), tags=("hover",))
                cursor = "hand2"
        if self._drag is None:
            c.configure(cursor=cursor)

    def _box(self, box: fl.Box) -> None:
        c, s, theme = self.canvas, self.scale, th.ACTIVE
        x0, y0, x1, y1 = box.x * s, box.y * s, box.right * s, box.bottom * s
        words = self.scene.words
        tags = ("block", key(box.path))
        if box.role in (fl.START, fl.END):
            chosen = self.scene.selected == box.path
            if chosen:
                rounded(c, x0 - 4 * s, y0 - 4 * s, x1 + 4 * s, y1 + 4 * s, 24 * s, fill=theme.primary_soft, outline="",
                        tags=tags)
            rounded(c, x0, y0, x1, y1, 21 * s, fill=theme.primary_soft, outline=theme.primary,
                    width=max(1.0, (2.2 if chosen else 1.0) * s), tags=tags)
            title = words.start if box.role == fl.START else words.end
            detail = words.start_detail if box.role == fl.START else ""
            if detail and s >= 0.5:
                c.create_text((x0 + x1) / 2, y0 + 15 * s, text=title, fill=theme.primary,
                              font=_font(10 * s, "bold"), tags=tags)
                c.create_text((x0 + x1) / 2, y0 + 31 * s, text=detail, fill=theme.text_muted, font=_font(8.5 * s),
                              width=(x1 - x0) - 12 * s, tags=tags)
            else:
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=title, fill=theme.primary,
                              font=_font(10 * s, "bold"), tags=tags)
            return
        if box.role == fl.EXIT:
            rounded(c, x0, y0, x1, y1, 15 * s, fill=theme.error_bg, outline=theme.error, width=max(1.0, s), tags=tags)
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=words.stop, fill=theme.error, font=_font(9 * s, "bold"),
                          tags=tags)
            return
        if box.role == fl.PLACEHOLDER:
            rounded(c, x0, y0, x1, y1, 12 * s, fill=theme.surface, outline=theme.primary, width=max(1.0, 1.5 * s),
                    dash=(6, 4), tags=("block",))
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=words.placeholder, fill=theme.primary,
                          font=_font(10.5 * s, "bold"), tags=("block",))
            return
        if box.role == fl.PENDING:
            colour = theme.error if self.scene.pending_invalid else theme.primary
            rounded(c, x0, y0, x1, y1, 12 * s, fill=theme.surface, outline=colour, width=max(1.0, 2 * s), dash=(6, 4),
                    tags=tags)
            c.create_text(x0 + 20 * s, y0 + 24 * s, text=self.scene.pending_title, anchor="w", fill=colour,
                          font=_font(10.5 * s, "bold"), tags=tags)
            if s >= 0.55:
                c.create_text(x0 + 20 * s, y0 + 48 * s, text=words.pending_hint, anchor="w", fill=theme.text_muted,
                              width=(x1 - x0) - 36 * s, font=_font(9 * s), tags=tags)
            return
        block = self.scene.views.get(box.path)
        if block is None:
            return
        colour = {fl.WAIT: theme.block_wait, fl.IF: theme.block_if, fl.TAP: theme.primary}.get(block.kind,
                                                                                               theme.block_run)
        chosen = box.path == self.scene.selected
        if chosen:
            rounded(c, x0 - 5 * s, y0 - 5 * s, x1 + 5 * s, y1 + 5 * s, 16 * s, fill=theme.primary_soft, outline="",
                    tags=tags)
        else:
            rounded(c, x0, y0 + 2 * s, x1, y1 + 2 * s, 12 * s, fill=theme.border, outline="", tags=tags)   # shadow
        rounded(c, x0, y0, x1, y1, 12 * s, fill=theme.surface, outline=theme.primary if chosen else theme.border_strong,
                width=max(1.0, (2.4 if chosen else 1.0) * s), tags=tags)
        rounded(c, x0 + 1 * s, y0 + 1 * s, x0 + 9 * s, y1 - 1 * s, 4 * s, fill=colour, outline="", tags=tags)
        cx, cy = x0 + 30 * s, y0 + 21 * s
        r = 13 * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=colour, outline="", tags=tags)
        c.create_text(cx, cy, text=block.number, fill=theme.surface, font=_font(8.5 * s, "bold"), tags=tags)
        text_x = x0 + 52 * s
        c.create_text(text_x, y0 + 21 * s, text=block.title, anchor="w", fill=theme.text, font=_font(10.5 * s, "bold"),
                      tags=tags)
        if s >= 0.55:
            width = (x1 - text_x) - 14 * s
            if block.detail2:
                c.create_text(text_x, y0 + 40 * s, text=block.detail, anchor="w", fill=theme.text_muted, width=width,
                              font=_font(9 * s), tags=tags)
                c.create_text(text_x, y0 + 59 * s, text=block.detail2, anchor="w", fill=theme.text_muted,
                              width=width, font=_font(8.5 * s), tags=tags)
            else:
                c.create_text(text_x, y0 + 36 * s, text=block.detail, anchor="nw", fill=theme.text_muted, width=width,
                              font=_font(9 * s), tags=tags)
        if block.badge:
            bw = 40 * s
            rounded(c, x1 - bw - 10 * s, y0 + 10 * s, x1 - 10 * s, y0 + 28 * s, 9 * s, fill=theme.info_bg, outline="",
                    tags=tags)
            c.create_text(x1 - 10 * s - bw / 2, y0 + 19 * s, text=block.badge, fill=theme.info,
                          font=_font(8 * s, "bold"), tags=tags)
        if block.warning:
            wx, wy = x1 - 22 * s, y1 - 18 * s
            c.create_oval(wx - 9 * s, wy - 9 * s, wx + 9 * s, wy + 9 * s, fill=theme.warning_bg, outline=theme.warning,
                          tags=tags)
            c.create_text(wx, wy, text="!", fill=theme.warning, font=_font(9 * s, "bold"), tags=tags)
