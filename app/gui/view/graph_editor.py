"""The script editor of version 4 (the person's design of 2026-10-25): the endless grid filling the screen, a panel
of blocks that slides out at the side, each kind with its own colour, and a block's settings at the right.

    toolbar   back · the script's name and whether it is saved · undo, redo · save · run, dry run, stop ·
              record a macro · zoom and "fit"
    grid      `GraphCanvas`: blocks dragged anywhere, an arrow dragged from an output to a block, the block that runs
              lit and the one a run failed in bright red
    blocks    the slide-out panel at the left: a click puts a block in the middle of the view, a drag puts it where
              it is dropped
    settings  the chosen block's own panel (`graph_panels`), at the right while a block is chosen

Opening the editor fills the screen: the window is maximised and the navigation and page header step aside until it
is left (the App does that, `App.show_screen`).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.decide import (AT_MATCH, CHECK, CLICK, DOUBLE, DRAG, FOR, HOLD, MACRO, MANUAL, PICTURE, PIXEL, REPEAT, RIGHT,
                        START, STOP, TEXT, TIMES, UNTIL, WAIT, CheckNode, ClickNode, MacroNode, RepeatNode,
                        StartNode, StopNode, WaitNode)
from app.gui import graph_edit as ge
from app.gui import theme as th
from app.gui.view import dialogs, fmt
from app.gui.view import widgets as w
from app.gui.view.graph_canvas import COLOURS, FAILED, RUNNING, VISITED, EdgeView, GraphCanvas, NodeView

RENAME_ATTEMPTS = 5
PALETTE = ((WAIT, "wait", "graph_block_wait", "graph_tip_wait"),
           (CHECK, "check", "graph_block_check", "graph_tip_check"),
           ("mixed_any", "mixed", "graph_block_mixed_any", "graph_tip_mixed_any"),
           ("mixed_all", "mixed", "graph_block_mixed_all", "graph_tip_mixed_all"),
           (REPEAT, "repeat", "graph_block_repeat", "graph_tip_repeat"),
           (CLICK, "click", "graph_block_click", "graph_tip_click"),
           (MACRO, "macro", "graph_block_macro", "graph_tip_macro"),
           (STOP, "stop", "graph_block_stop", "graph_tip_stop"))
OUTPUT_KEYS = {"next": "graph_out_next", "found": "graph_out_found", "missing": "graph_out_missing",
               "again": "graph_out_again", "done": "graph_out_done"}


# ---- a block in the person's words ------------------------------------------------------------------------------
def _area(ui, region) -> str:
    return ui.t("graph_area_all") if region is None else ui.t("graph_area").format(w=region.width, h=region.height)


def part_text(ui, part) -> str:
    q = part.condition.query
    if part.kind == PIXEL:
        return ui.t("graph_part_pixel").format(x=q.point[0], y=q.point[1])
    if part.kind == PICTURE:
        return ui.t("graph_part_picture").format(w=q.template.width, h=q.template.height)
    words = ", ".join(f"«{fmt.clip(line.text, 14)}»" for line in q.lines)
    return ui.t("graph_part_text").format(text=words)


def node_view(ui, node, macro_names: dict[str, str]) -> NodeView:
    t = ui.t
    outputs = tuple((out, t(OUTPUT_KEYS[out])) for out in node.outputs)
    if isinstance(node, StartNode):
        detail = t("graph_start_now") if not node.delay_s else t("graph_start_after").format(
            seconds=fmt.number(ui, node.delay_s, 1))
        return NodeView(node.id, START, "start", t("graph_block_start"), detail, at=node.at, outputs=outputs)
    if isinstance(node, WaitNode):
        return NodeView(node.id, WAIT, "wait", t("graph_block_wait"),
                        t("graph_wait_seconds").format(seconds=fmt.number(ui, node.seconds, 2)), at=node.at,
                        outputs=outputs)
    if isinstance(node, CheckNode):
        if node.mixed:
            title = t("graph_block_mixed_all" if node.mode == "all" else "graph_block_mixed_any")
            detail = t("graph_mixed_count").format(count=len(node.parts)) if node.parts else t("graph_check_empty")
            detail2 = " · ".join(t(f"graph_kind_{p.kind}") for p in node.parts)
            return NodeView(node.id, CHECK, "mixed", title, detail, detail2, at=node.at, outputs=outputs,
                            warning="!" if not node.parts else "")
        if not node.parts:
            return NodeView(node.id, CHECK, "check", t("graph_block_check"), t("graph_check_empty"), at=node.at,
                            outputs=outputs, warning="!")
        first = node.parts[0]
        detail = part_text(ui, first) + (f" +{len(node.parts) - 1}" if len(node.parts) > 1 else "")
        region = getattr(first.condition.query, "region", None)
        detail2 = "" if first.kind == PIXEL else _area(ui, region)
        return NodeView(node.id, CHECK, "check", t("graph_block_check"), detail, detail2, at=node.at, outputs=outputs)
    if isinstance(node, RepeatNode):
        detail = {MANUAL: t("graph_repeat_manual"), UNTIL: t("graph_repeat_until").format(time=node.until),
                  FOR: t("graph_repeat_for").format(minutes=fmt.number(ui, node.seconds / 60, 1)),
                  TIMES: t("graph_repeat_times").format(count=node.times)}[node.mode]
        return NodeView(node.id, REPEAT, "repeat", t("graph_block_repeat"), detail, at=node.at, outputs=outputs)
    if isinstance(node, ClickNode):
        how = {DOUBLE: t("graph_click_double"), RIGHT: t("graph_click_right"), DRAG: t("graph_click_drag"),
               HOLD: t("graph_click_hold").format(seconds=fmt.number(ui, node.hold_s, 1))}.get(node.how,
                                                                                          t("graph_click_tap"))
        if node.where == AT_MATCH:
            where = t("graph_click_at_match")
        elif node.point is None:
            where = t("graph_click_no_point")
        else:
            where = t("graph_click_at").format(x=node.point[0], y=node.point[1])
        return NodeView(node.id, CLICK, "click", t("graph_block_click"), how, where, at=node.at, outputs=outputs,
                        warning="" if node.ready else "!")
    if isinstance(node, MacroNode):
        name = macro_names.get(node.macro, "")
        detail = name or (t("graph_macro_gone") if node.macro else t("graph_macro_none"))
        return NodeView(node.id, MACRO, "macro", t("graph_block_macro"), detail, at=node.at, outputs=outputs,
                        warning="" if name else "!")
    assert isinstance(node, StopNode)
    detail = t("graph_stop_ok") if node.success else t("graph_stop_error")
    return NodeView(node.id, STOP, "stop", t("graph_block_stop"), detail, fmt.clip(node.message, 30), at=node.at,
                    outputs=outputs)


def script_lines(ui, script, macro_names: dict[str, str]) -> list[str]:
    """One line per block -- its title and what it does -- from the start along the arrows (each block once), then
    any block no arrow reaches: what a list of scripts shows of one."""
    order, seen, todo = [], set(), [script.start.id]
    while todo:
        node_id = todo.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        order.append(script.node(node_id))
        node = script.node(node_id)
        todo.extend(e.target for out in node.outputs for e in script.edges if e.source == node_id and e.output == out)
    order += [n for n in script.nodes if n.id not in seen]
    lines = []
    for node in order:
        view = node_view(ui, node, macro_names)
        lines.append(f"{view.title} — {view.detail}" if view.detail else view.title)
    return lines


# ---- the screen ------------------------------------------------------------------------------------------------
class GraphEditorScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        self.selected: str | None = None
        self.selected_edge: tuple[str, str] | None = None
        self.panel = None
        self._panel_for: tuple | None = None
        self._fitted_for = None
        self.pending_kind: dict[str, str] = {}     # an empty "If ... Then": the kind chosen before anything is picked
        self.pending_part: dict[str, str] = {}     # a mixed block: the kind of the part being added
        self._recording_here = False               # a recording started from this editor: its block follows
        self._build_toolbar()
        self._build_body()

    # ---- toolbar ---------------------------------------------------------------------------------------------
    def _icon_button(self, parent, glyph: str, tip: str, command) -> ttk.Button:
        button = ttk.Button(parent, style="Icon.TButton", command=command)
        paint = lambda: button.configure(image=self.ui.button_icon(glyph, th.ACTIVE.text))  # noqa: E731
        self.ui.on_theme(paint)
        paint()
        w.Tooltip(self.ui, button, lambda: self.ui.t(tip))
        return button

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.frame, style="Header.TFrame", padding=(14, 10, 14, 10))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)
        self._icon_button(bar, "back", "btn_back", self.go_back).grid(row=0, column=0, padx=(0, 8))
        self.btn_palette = self.ui.label(ttk.Button(bar, command=self.toggle_palette), "graph_btn_palette")
        self.btn_palette.grid(row=0, column=1, padx=(0, 12))
        titles = ttk.Frame(bar, style="Header.TFrame")
        titles.grid(row=0, column=2, sticky="w")
        self.lbl_title = ttk.Label(titles, text="", style="Heading.TLabel", cursor="hand2")
        self.lbl_title.pack(anchor="w")
        self.lbl_title.bind("<Button-1>", lambda _e: self.rename())
        self.lbl_state = ttk.Label(titles, text="", style="Muted.TLabel")
        self.lbl_state.pack(anchor="w")
        self.btn_target = ttk.Button(bar, style="Chip.TButton", command=self.choose_target)
        self.btn_target.grid(row=0, column=4, padx=(12, 0))
        w.Tooltip(self.ui, self.btn_target, lambda: self.ui.t("graph_tip_target"))
        right = ttk.Frame(bar, style="Header.TFrame")
        right.grid(row=0, column=3, sticky="e")
        self.btn_zoom_out = self._icon_button(right, "zoom_out", "tip_zoom_out", self._zoom_out)
        self.btn_zoom_out.pack(side="left")
        self.lbl_zoom = ttk.Label(right, text="100%", style="Muted.TLabel", width=5, anchor="center")
        self.lbl_zoom.pack(side="left")
        self._icon_button(right, "zoom_in", "tip_zoom_in", self._zoom_in).pack(side="left")
        self._icon_button(right, "fit", "tip_fit", self._fit).pack(side="left", padx=(0, 14))
        self.btn_undo = self._icon_button(right, "undo", "btn_undo", self._undo)
        self.btn_undo.pack(side="left")
        self.btn_redo = self._icon_button(right, "redo", "btn_redo", self._redo)
        self.btn_redo.pack(side="left", padx=(0, 10))
        self.btn_record = self.ui.label(ttk.Button(right, command=self.record_macro), "graph_btn_record")
        self.btn_record.pack(side="left", padx=(0, 6))
        self.btn_dry = self.ui.label(ttk.Button(right, command=lambda: self.run(dry_run=True)), "graph_btn_dry")
        self.btn_dry.pack(side="left", padx=(0, 6))
        self.btn_run = self.ui.label(ttk.Button(right, style="Primary.TButton", command=self.run), "graph_btn_run")
        self.btn_run.pack(side="left", padx=(0, 6))
        self.btn_stop = self.ui.label(ttk.Button(right, style="Danger.TButton", command=self.stop_run),
                                      "graph_btn_stop")
        self.btn_stop.pack(side="left", padx=(0, 10))
        self.btn_save = self.ui.label(ttk.Button(right, command=self.save), "btn_save")
        self.btn_save.pack(side="left")

    # ---- body: the grid, the blocks panel, the settings panel ------------------------------------------------
    def _build_body(self) -> None:
        body = ttk.Frame(self.frame, style="Plain.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self._body = body
        self.canvas_view = GraphCanvas(self.ui, body, on_select=self._select, on_select_edge=self._select_edge,
                                       on_activate=self._select, on_moved=self._moved, on_connect=self._connect,
                                       on_drop=self._dropped, on_delete=self.delete_selected)
        self.canvas_view.canvas.grid(row=0, column=0, sticky="nsew")
        self._build_palette(body)
        self._build_banner(body)
        self.side = ttk.Frame(body, style="Plain.TFrame", width=390)
        self.side.grid(row=0, column=1, sticky="ns")
        edge = tk.Frame(self.side, width=1, background=th.ACTIVE.border)
        edge.place(relx=0.0, rely=0.0, relheight=1.0, anchor="nw")
        self.ui.on_theme(lambda: edge.configure(background=th.ACTIVE.border))
        self.side.grid_propagate(False)
        self.side.columnconfigure(0, weight=1)
        self.side.rowconfigure(1, weight=1)
        self.side.grid_remove()
        head = ttk.Frame(self.side, style="Plain.TFrame", padding=(16, 12, 10, 6))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.lbl_side = ttk.Label(head, text="", style="Heading.TLabel")
        self.lbl_side.grid(row=0, column=0, sticky="w")
        self.btn_delete = self.ui.label(ttk.Button(head, style="SmallDanger.TButton", command=self.delete_selected),
                                        "btn_delete_step")
        self.btn_delete.grid(row=0, column=1, padx=(6, 6))
        self._icon_button(head, "close", "btn_close", lambda: self._select(None)).grid(row=0, column=2)
        outer, self.side_inner = w.scroll_area(self.ui, self.side, background=lambda: th.ACTIVE.bg)
        outer.grid(row=1, column=0, sticky="nsew", padx=(0, 4), pady=(0, 8))
        self.side_inner.columnconfigure(0, weight=1)
        self.lbl_message = ttk.Label(self.side, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self.lbl_message.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))

    def _build_banner(self, body) -> None:
        """Why the last run from here stopped, where, and what to do (the person's list of 2026-10-25, item 9)."""
        self.banner = tk.Frame(body, background=th.ACTIVE.error_bg, highlightthickness=1,
                               highlightbackground=th.ACTIVE.error)
        self.lbl_banner = tk.Label(self.banner, text="", justify="left", anchor="w", wraplength=760,
                                   background=th.ACTIVE.error_bg, foreground=th.ACTIVE.text, font=th.TYPE_BODY,
                                   padx=14, pady=10)
        self.lbl_banner.pack(side="left", fill="both", expand=True)
        self.btn_banner = ttk.Button(self.banner, text="", command=self._dismiss_banner)
        self.btn_banner.pack(side="right", padx=10, pady=10, anchor="n")
        self._dismissed = None

    def _dismiss_banner(self) -> None:
        self._dismissed = self.c.run_results
        self.banner.place_forget()

    def _render_banner(self) -> None:
        c = self.c
        failed = None
        ended_here = not c.run_active and bool(c.run_results) and c.run_macro_key == c.editor_key
        if ended_here and c.run_results is not self._dismissed:
            failed = next((r for r in c.run_results if r.state.value != "completed"), None)
        if failed is None:
            self.banner.place_forget()
            return
        lines = [self.ui.t("editor_run_failed")] + fmt.explain_lines(self.ui, failed.detail)
        self.lbl_banner.configure(text="\n".join(lines), background=th.ACTIVE.error_bg, foreground=th.ACTIVE.text)
        self.banner.configure(background=th.ACTIVE.error_bg, highlightbackground=th.ACTIVE.error)
        self.btn_banner.configure(text=self.ui.t("btn_dismiss"))
        self.banner.place(relx=0.5, y=14, anchor="n")
        self.banner.lift()

    def _build_palette(self, body) -> None:
        self.palette = tk.Frame(body, background=th.ACTIVE.surface, highlightthickness=1,
                                highlightbackground=th.ACTIVE.border)
        self.ui.on_theme(lambda: self.palette.configure(background=th.ACTIVE.surface,
                                                        highlightbackground=th.ACTIVE.border))
        self._palette_heading = tk.Label(self.palette, text="", anchor="w", background=th.ACTIVE.surface,
                                         foreground=th.ACTIVE.text_muted, font=th.TYPE_LABEL)
        self._palette_heading.pack(anchor="w", padx=12, pady=(10, 4))
        self._palette_rows = []
        for kind, colour, key, tip in PALETTE:
            row = tk.Frame(self.palette, background=th.ACTIVE.surface, cursor="hand2")
            row.pack(fill="x", padx=8, pady=2)
            stripe = tk.Frame(row, width=8, background=COLOURS[th.ACTIVE.dark][colour])
            stripe.pack(side="left", fill="y")
            label = tk.Label(row, text="", anchor="w", background=th.ACTIVE.surface, foreground=th.ACTIVE.text,
                             font=th.TYPE_BODY_STRONG, padx=10, pady=7)
            label.pack(side="left", fill="x", expand=True)
            self._palette_rows.append((row, stripe, label, kind, colour, key))
            for widget in (row, label, stripe):
                widget.bind("<ButtonPress-1>", lambda e, k=kind: self._palette_press(e, k))
                widget.bind("<B1-Motion>", self._palette_drag)
                widget.bind("<ButtonRelease-1>", self._palette_release)
            w.Tooltip(self.ui, label, lambda tip=tip: self.ui.t(tip))
        self.ui.on_theme(self._paint_palette)
        self._paint_palette()
        self._palette_language = self.ui.t.language
        self._palette_open = True
        self._drag: dict | None = None
        self.palette.place(x=12, y=12)

    def _paint_palette(self) -> None:
        self._palette_heading.configure(background=th.ACTIVE.surface, foreground=th.ACTIVE.text_muted,
                                        text=self.ui.t("graph_palette_title").upper())
        for row, stripe, label, _kind, colour, key in self._palette_rows:
            row.configure(background=th.ACTIVE.surface)
            stripe.configure(background=COLOURS[th.ACTIVE.dark][colour])
            label.configure(background=th.ACTIVE.surface, foreground=th.ACTIVE.text, text=self.ui.t(key))

    def toggle_palette(self) -> None:
        self._palette_open = not self._palette_open
        if self._palette_open:
            self.palette.place(x=12, y=12)
            self.palette.lift()
        else:
            self.palette.place_forget()

    def _palette_press(self, event, kind: str) -> None:
        self._drag = {"kind": kind, "x": event.x_root, "y": event.y_root, "ghost": None}

    def _palette_drag(self, event) -> None:
        drag = self._drag
        if not drag:
            return
        if drag["ghost"] is None and abs(event.x_root - drag["x"]) + abs(event.y_root - drag["y"]) > 6:
            ghost = tk.Toplevel(self.ui.root)
            ghost.overrideredirect(True)
            ghost.attributes("-alpha", 0.85)
            ghost.attributes("-topmost", True)
            key = next(k for kind, _c, k, _t in PALETTE if kind == drag["kind"])
            colour = next(c for kind, c, _k, _t in PALETTE if kind == drag["kind"])
            tk.Label(ghost, text=self.ui.t(key), background=COLOURS[th.ACTIVE.dark][colour], foreground="#FFFFFF",
                     font=th.TYPE_BODY_STRONG, padx=12, pady=6).pack()
            drag["ghost"] = ghost
        if drag["ghost"] is not None:
            drag["ghost"].geometry(f"+{event.x_root + 8}+{event.y_root + 8}")

    def _palette_release(self, event) -> None:
        drag, self._drag = self._drag, None
        if not drag:
            return
        if drag["ghost"] is not None:
            drag["ghost"].destroy()
            if self.canvas_view.drop(drag["kind"], event.x_root, event.y_root):
                return
        self.add_block(drag["kind"], self.canvas_view.centre())

    # ---- the drawing -----------------------------------------------------------------------------------------
    def add_block(self, kind: str, at) -> None:
        settings = {}
        if kind in ("mixed_any", "mixed_all"):
            settings = {"mode": "all" if kind == "mixed_all" else "any"}
            kind = ge.MIXED
        node_id = self.c.editor_apply_graph(ge.add_node, kind, at, **settings)
        if isinstance(node_id, str):
            # joined after the chosen block's first free output, when there is one: a block added is usually next
            if self.selected is not None:
                self._join_after(self.selected, node_id)
            self.selected, self.selected_edge = node_id, None
        self.ui.render()

    def _join_after(self, source: str, target: str) -> None:
        doc = self.c.editor.document
        try:
            node = doc.node(source)
        except Exception:
            return
        free = next((out for out in node.outputs if doc.edge_from(source, out) is None), None)
        if free is not None:
            self.c.editor_apply_graph(ge.connect, source, free, target)

    def _dropped(self, kind: str, at) -> None:
        self.add_block(kind, at)

    def _select(self, node_id) -> None:
        self.selected, self.selected_edge = node_id, None
        self.ui.render()

    def _select_edge(self, edge) -> None:
        self.selected, self.selected_edge = None, edge
        self.ui.render()

    def _moved(self, places) -> None:
        self.c.editor_apply_graph(ge.move_nodes, places)
        self.ui.render()

    def _connect(self, source: str, output: str, target: str) -> None:
        self.c.editor_apply_graph(ge.connect, source, output, target)
        self.ui.render()

    def delete_selected(self) -> None:
        if self.selected_edge is not None:
            self.c.editor_apply_graph(ge.disconnect, *self.selected_edge)
            self.selected_edge = None
        elif self.selected is not None and self.selected != self.c.editor.document.start.id:
            self.c.editor_apply_graph(ge.delete_nodes, {self.selected})
            self.selected = None
        self.ui.render()

    # ---- the zoom ---------------------------------------------------------------------------------------------
    def _zoom_in(self) -> None:
        self.canvas_view.zoom_in()
        self.lbl_zoom.configure(text=self.canvas_view.zoom_text())

    def _zoom_out(self) -> None:
        self.canvas_view.zoom_out()
        self.lbl_zoom.configure(text=self.canvas_view.zoom_text())

    def _fit(self) -> None:
        self.canvas_view.fit()
        self.lbl_zoom.configure(text=self.canvas_view.zoom_text())

    def choose_target(self) -> None:
        """The target the editor picks on, tests on and runs on: the application's chosen target."""
        if self.c.run_active or self.c.recording_active:
            return
        self.ui.choose_target()
        self.ui.render()

    # ---- saving, leaving --------------------------------------------------------------------------------------
    def leave(self) -> bool:
        """Whether the editor may be left: unsaved work always asks first (saved, or given up knowingly). True
        when it is closed now."""
        if self.c.editor_dirty:
            answer = dialogs.UnsavedChanges(self.ui, self.c.editor.document.name).show()
            if answer is None:
                return False
            if answer == "save" and not self._save_editor():
                return False
        self.c.clear_lit()
        self.c.close_editor()
        self.selected = self.selected_edge = None
        self._clear_panel()
        self._restore_olive()
        return True

    def go_back(self) -> None:
        """Leaving with unsaved work always asks; it is never lost silently."""
        if self.leave():
            self.ui.show_screen("scripts")
        self.ui.render()

    def save(self) -> None:
        self._save_editor()
        self.ui.render()

    def _save_editor(self) -> bool:
        if self.c.save_editor():
            return True
        if self.c.status.category == "changed_outside":
            name = self.c.editor.document.name
            if not self.ui.confirm(self.ui.t("dlg_changed_outside_title"),
                                   self.ui.t("dlg_changed_outside_text").format(name=name)):
                return False
            return self.c.save_editor(replace_outside=True)
        for _ in range(RENAME_ATTEMPTS):
            if not (self.c.editing_new and self.c.status.category == "exists"):
                return False
            name = dialogs.TextPrompt(self.ui, "dlg_script_name_taken", "field_name", self.c.free_script_name(),
                                      "btn_save").show()
            if not name:
                return False
            if self.c.rename_new_script(name) and self.c.save_editor():
                return True
        return False

    def rename(self) -> None:
        if self.c.editor is None:
            return
        name = dialogs.TextPrompt(self.ui, "dlg_rename_script", "field_name", self.c.editor.document.name,
                                  "btn_rename").show()
        if name and name.strip() != self.c.editor.document.name:
            if self.c.editing_new:
                self.c.rename_new_script(name)
            else:
                self.c.editor_apply_graph(ge.rename, name)
        self.ui.render()

    def _undo(self) -> None:
        self.c.editor_undo()
        self.ui.render()

    def _redo(self) -> None:
        self.c.editor_redo()
        self.ui.render()

    # ---- running from here -------------------------------------------------------------------------------------
    def run(self, *, dry_run: bool = False) -> None:
        """Run the script on the chosen target from the editor: saved first (asked), the blocks lit as they run."""
        if self.c.editor_dirty or self.c.editing_new:
            if not self.ui.confirm(self.ui.t("graph_save_before_run_title"), self.ui.t("graph_save_before_run")):
                return
            if not self._save_editor():
                self.ui.render()
                return
        whole = self.c.selected is not None and self.c.selected.whole_screen
        if self.c.run_from_editor(dry_run=dry_run) and whole:
            # on the whole screen Olive's own window would be what is looked at and refused to be clicked:
            # it steps out of the way for the run (Esc anywhere still stops it) and comes back when it ends
            self.ui.root.iconify()
            self._lowered = True
        self.ui.render()

    def _restore_olive(self) -> None:
        if getattr(self, "_lowered", False):
            self._lowered = False
            try:
                self.ui.root.deiconify()
            except tk.TclError:
                pass

    def stop_run(self) -> None:
        self.c.cancel()
        self.ui.render()

    def record_macro(self) -> None:
        """Record a macro on the chosen target (press again, or F10, to stop): it is named and saved in the library,
        and a block that runs it is put after the chosen block (the person's list of 2026-10-25)."""
        if self.c.recording_active:
            self.c.stop_recording()
            self._after_recording()
            return
        if not self.c.target_ready:
            self.ui.choose_target()
            if not self.c.target_ready:
                return
        if self.c.unsaved and not self.ui.confirm(self.ui.t("graph_discard_recording_title"),
                                                  self.ui.t("graph_discard_recording")):
            return
        if self.c.start_recording(discard_unsaved=True):
            self._recording_here = True
        self.ui.render()

    def _after_recording(self) -> None:
        """Once a recording started here has ended: its name asked, the macro saved, its block put in."""
        self._recording_here = False
        if not self.c.unsaved or self.c.macro is None:
            self.ui.render()
            return
        for _ in range(RENAME_ATTEMPTS):
            name = dialogs.TextPrompt(self.ui, "dlg_name_macro", "field_name", "", "btn_save").show()
            if not name:
                if self.ui.confirm(self.ui.t("graph_drop_recording_title"), self.ui.t("graph_drop_recording")):
                    self.c.discard_recording()
                    break
                continue
            key = self.c.save_recording_for_block(name)
            if key:
                node_id = self.c.editor_apply_graph(ge.add_node, MACRO, self._spot_after(), macro=key)
                if isinstance(node_id, str):
                    if self.selected is not None:
                        self._join_after(self.selected, node_id)
                    self.selected = node_id
                break
        self.ui.render()

    def _spot_after(self):
        """Where a new block goes: below the chosen block, or in the middle of the view."""
        if self.selected is not None:
            try:
                x, y = self.c.editor.document.node(self.selected).at
                return x, y + 150
            except Exception:
                pass
        return self.canvas_view.centre()

    def refresh_panel(self) -> None:
        self._panel_for = None
        if self.c.editing_graph:
            self._render_panel(self.c.editor.document)

    # ---- render -----------------------------------------------------------------------------------------------
    def render(self) -> None:
        if not self.c.editing_graph:
            return
        if self._palette_language != self.ui.t.language:      # the block names follow the language
            self._palette_language = self.ui.t.language
            self._paint_palette()
        doc = self.c.editor.document
        t = self.ui.t
        self.lbl_title.configure(text=t("editor_title").format(name=fmt.clip(doc.name, 60)))
        if self.c.editing_new:
            state = "editor_new_empty" if self.c.editor.empty else "editor_new_unsaved"
        else:
            state = "editor_unsaved" if self.c.editor_dirty else "editor_saved"
        self.lbl_state.configure(text=t(state))
        running = self.c.run_active
        savable = (self.c.editor_dirty or self.c.editing_new) and not self.c.editor.empty and not running
        self.btn_save.state(["!disabled"] if savable else ["disabled"])
        self.btn_undo.state(["!disabled"] if self.c.editor.can_undo and not running else ["disabled"])
        self.btn_redo.state(["!disabled"] if self.c.editor.can_redo and not running else ["disabled"])
        for button in (self.btn_run, self.btn_dry):
            button.state(["disabled"] if running or self.c.recording_active else ["!disabled"])
        self.btn_record.state(["disabled"] if running else ["!disabled"])
        self.btn_record.configure(text=t("graph_btn_record_stop" if self.c.recording_active else "graph_btn_record"))
        if self._recording_here and not self.c.recording_active:
            self.frame.after_idle(self._after_recording)      # ended by F10: name it and put its block in
            self._recording_here = False
        self.btn_stop.state(["!disabled"] if running else ["disabled"])
        target = self.c.selected.title if self.c.target_ready and self.c.selected is not None else ""
        self.btn_target.configure(text=t("graph_target").format(name=fmt.clip(target, 28)) if target
                                  else t("graph_target_none"))
        self.btn_target.state(["disabled"] if running or self.c.recording_active else ["!disabled"])
        if not running:
            self._restore_olive()
        if self.selected is not None and self.selected not in {n.id for n in doc.nodes}:
            self.selected = None
        names = self.c.macro_names()
        views = [node_view(self.ui, node, names) for node in doc.nodes]
        edges = [EdgeView(e.source, e.output, e.target) for e in doc.edges]
        self.canvas_view.show(views, edges, selected=self.selected, selected_edge=self.selected_edge)
        lit = self.c.graph_lit
        self.canvas_view.set_highlight({k: {"running": RUNNING, "failed": FAILED, "visited": VISITED}[v]
                                        for k, v in lit.items() if v in ("running", "failed")})
        self._render_banner()
        if self._fitted_for is not self.c.editor:
            self._fitted_for = self.c.editor
            self.frame.after(60, self._fit)
        self._render_panel(doc)

    def _clear_panel(self) -> None:
        for child in self.side_inner.winfo_children():
            child.destroy()
        self.panel, self._panel_for = None, None

    def _render_panel(self, doc) -> None:
        from app.gui.view import graph_panels
        node = None
        if self.selected is not None:
            try:
                node = doc.node(self.selected)
            except Exception:
                node = None
        if node is None:
            self._clear_panel()
            self.side.grid_remove()
            return
        self.side.grid()
        self.lbl_side.configure(text=node_view(self.ui, node, {}).title)
        self.btn_delete.state(["disabled"] if isinstance(node, StartNode) else ["!disabled"])
        if self._panel_for != (node.id, node):
            self._clear_panel()
            self.panel = graph_panels.panel_for(self, self.side_inner, node)
            self._panel_for = (node.id, node)

    def show_message(self, text: str, *, error: bool = False) -> None:
        self.lbl_message.configure(text=text, foreground=th.ACTIVE.error if error else th.ACTIVE.text_muted)
