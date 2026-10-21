"""The editor: a script as connected blocks (or a recorded macro as a list of actions), and one properties panel.

Scripts. The canvas (`script_canvas`) draws the script as its run order -- Start, each block, End, an IF's two
branches under it, each a list of blocks of its own, a WAIT's time-out exit -- and the properties panel on the right
edits the chosen block in place with `flow.StepForm`: change the fields, then Apply. A click on a PART of a block
(its condition, its time limit, its macro, its kind of click, Start's delay) chooses the block and puts the cursor in
that part's field. Blocks are moved by dragging them anywhere (an IF together with its branches); the arrows follow,
and the place is kept in the file -- "Arrange automatically" gives every block its automatic place back. Where a
block is drawn never changes what the script does: the arrows say that.

Adding a block: the "+" in the middle of any arrow (exactly there: in the script, in a branch), the toolbar, the
palette or the empty script's placeholder. The new block is shown dashed where it will go, with an empty form in the
panel; it becomes part of the script only when Add is pressed and the form is valid -- a block that is not valid
cannot exist, so a missing parameter is shown on that pending block and in the panel, in the backend's own words. A
list view (the same blocks in a tree: an IF's "Yes:" and "No:" under it) is one click away; both views share one
selection. The Start block holds the delay before the first step.

Recorded macros (version 1). The steps are a table -- the one real macro in the repository has 419 actions, and
thousands of live widgets would make the screen slow for no gain -- with the action's fields in the panel.

Every change goes through `app.gui.macro_edit` / `app.gui.flow_edit`, which refuse anything they could not store
truthfully. Deleting a block does not ask for confirmation -- it happens, says what it removed, and Undo (Ctrl+Z)
brings it back -- which is what the brief asks for instead of a confirmation dialog on every small action. Undo,
redo, Save, Ctrl+S and "save before leaving" are the same for both kinds: the controller holds one edit session.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from tkinter import ttk

from app.decide import ELSE, THEN, Decision, RunMacro, TapMatch, WaitUntil, walk
from app.gui import flow_edit as fe
from app.gui import flow_layout as fl
from app.gui import macro_edit as me
from app.gui import theme as th
from app.gui.macro_edit import BlockKind, EditError
from app.gui.view import dialogs, fmt
from app.gui.view import widgets as w
from app.gui.view.flow import PRESET_TAP, StartForm, StepDialog, StepForm, error_text   # noqa: F401 (StepDialog:
#                                                   the dialog form of the same fields, still importable here)
from app.gui.view.script_canvas import BlockView, CanvasWords, ScriptCanvas
from app.observe import TextLinesQuery, TextQuery
from app.playback.macro import ActionOp
from app.providers.input import Key

ADDABLE = ((ActionOp.TAP, "act_tap"), (ActionOp.DOUBLE_TAP, "act_double_tap"),
           (ActionOp.SECONDARY_TAP, "act_secondary_tap"), (ActionOp.MOVE, "act_move"),
           (ActionOp.KEY_PRESS, "act_key_press"), (ActionOp.TYPE_TEXT, "act_type_text"))
PAUSE_OP = "pause"
TITLE_CHARS = 26          # the editor's own title line; the full name is in the library
DETAIL_CHARS = 74         # one block's detail on the canvas
BRANCH_CHARS = 30
PANEL_WIDTH = 380
# The palette: (what it adds, the block kind, a preset for the new block, an icon).
PALETTE = (("add_wait", fe.WAIT, "", "history"), ("add_tap", fe.IF, PRESET_TAP, "target"),
           ("add_if", fe.IF, "", "flow"), ("add_run", fe.RUN, "", "record"))
TAP_ENTRY = ("add_tap_match", fe.TAP, "", "target")       # only where a branch may begin with a click on the match
VIEW_CANVAS, VIEW_LIST = "canvas", "list"
READABLE_ZOOM = 0.8
RENAME_ATTEMPTS = 5       # how often a taken name is asked about again before giving up (the person can cancel)
START_IID = "start"


def _iid(path: tuple) -> str:
    """A block's row in the list: "s:2/then/0"."""
    return "s:" + "/".join(str(p) for p in path)


def _header_iid(list_path: tuple) -> str:
    """The "Yes:" / "No:" row of an IF's branch: "h:2/then"."""
    return "h:" + "/".join(str(p) for p in list_path)


def _parse_path(text: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in text.split("/") if p)


def _parse_iid(iid: str) -> tuple[str | None, tuple]:
    if iid == START_IID:
        return "start", fe.START
    if iid.startswith("s:"):
        return "step", _parse_path(iid[2:])
    if iid.startswith("h:"):
        return "header", _parse_path(iid[2:])
    return None, ()


class PanelHost:
    """What a step form needs from whatever hosts it -- here, the editor's properties panel, not a dialog."""

    def __init__(self, editor: "EditorScreen") -> None:
        self.editor, self.ui = editor, editor.ui

    @property
    def alive(self) -> bool:
        return self.editor.form is not None

    def show_error(self, text: str) -> None:
        self.editor.show_form_message(text, error=True)

    def show_note(self, text: str) -> None:
        self.editor.show_form_message(text, error=False)

    def regrab(self) -> None:
        """A dialog takes the input grab back after a nested one; a panel has nothing to take back."""


class EditorScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(28, 4, 28, 20))
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        self._signature = None
        self._selected_row: str | None = None
        self._choose_step: str | None = None      # the row to choose once the list is redrawn
        self.view = VIEW_CANVAS
        self.form = None                          # a StepForm, or the Start block's StartForm
        self._form_for: tuple | None = None       # ("step", path, block) | ("new", slot, kind, preset) | ("start", s)
        self._form_snapshot = None
        self._pending: tuple | None = None        # (slot, kind, preset, at) of a block being added
        self._pending_invalid = False
        self._focus_part = ""                     # the part of the chosen block whose field gets the cursor
        self._fitted_for: object = None          # fit the canvas once per opened script
        self._zoom_job: str | None = None        # that first fit, waiting for the canvas to be laid out
        self.host = PanelHost(self)
        self._build_toolbar()
        self._build_body()

    # ---- toolbar --------------------------------------------------------------------------------------
    def _icon_button(self, parent, glyph: str, tip: str, command, *, colour=None) -> ttk.Button:
        button = ttk.Button(parent, style="Icon.TButton", command=command)
        tint = colour or (lambda: th.ACTIVE.text)
        paint = lambda: button.configure(image=self.ui.button_icon(glyph, tint()))  # noqa: E731
        self.ui.on_theme(paint)
        paint()
        w.Tooltip(self.ui, button, lambda: self.ui.t(tip))
        return button

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.frame, style="Plain.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        bar.columnconfigure(1, weight=1)

        back = self._icon_button(bar, "back", "btn_back", self.go_back)
        back.grid(row=0, column=0, padx=(0, 10))

        titles = ttk.Frame(bar, style="Plain.TFrame")
        titles.grid(row=0, column=1, sticky="w")
        self.lbl_title = ttk.Label(titles, text="", style="Heading.TLabel")
        self.lbl_title.pack(anchor="w")
        self.lbl_state = ttk.Label(titles, text="", style="Muted.TLabel")
        self.lbl_state.pack(anchor="w")

        right = ttk.Frame(bar, style="Plain.TFrame")
        right.grid(row=0, column=2, sticky="e")
        self._v2_tools = ttk.Frame(right, style="Plain.TFrame")
        self._v2_tools.pack(side="left", padx=(0, 12))
        # a recorded macro: "Adapt to the size" (a script has it on its Start block)
        self._v1_tools = ttk.Frame(right, style="Plain.TFrame")
        self.adapt_var = tk.BooleanVar(value=False)
        self.chk_adapt = self.ui.label(ttk.Checkbutton(self._v1_tools, variable=self.adapt_var,
                                                        style="Choice.TCheckbutton", command=self._toggle_adapt),
                                       "adapt_size")
        self.chk_adapt.pack(side="left")
        w.Tooltip(self.ui, self.chk_adapt, lambda: self.ui.t("tip_adapt_macro"))
        self.btn_add_block = self.ui.label(ttk.Button(self._v2_tools, compound="left", command=self._add_menu),
                                           "btn_add_block")
        paint_add = lambda: self.btn_add_block.configure(  # noqa: E731
            image=self.ui.button_icon("plus", th.ACTIVE.primary, pad=6))
        self.ui.on_theme(paint_add)
        paint_add()
        self.btn_add_block.pack(side="left")

        self.btn_undo = self._icon_button(right, "undo", "btn_undo", self._undo)
        self.btn_undo.pack(side="left", padx=(0, 2))
        self.btn_redo = self._icon_button(right, "redo", "btn_redo", self._redo)
        self.btn_redo.pack(side="left", padx=(0, 10))
        self.btn_save = self.ui.label(ttk.Button(right, style="Primary.TButton", command=self.save), "btn_save")
        self.btn_save.pack(side="left")

    # ---- body -----------------------------------------------------------------------------------------
    def _build_body(self) -> None:
        body = ttk.Frame(self.frame, style="Plain.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self._body = body

        # the view of the steps: blocks or a list, and the zoom of the blocks
        self._view_bar = ttk.Frame(body, style="Plain.TFrame")
        self.btn_view_canvas = self._icon_button(self._view_bar, "flow", "tip_view_canvas",
                                                 lambda: self.set_view(VIEW_CANVAS))
        self.btn_view_canvas.pack(side="left")
        self.btn_view_list = self._icon_button(self._view_bar, "list", "tip_view_list",
                                               lambda: self.set_view(VIEW_LIST))
        self.btn_view_list.pack(side="left", padx=(0, 14))
        self._zoom_tools = ttk.Frame(self._view_bar, style="Plain.TFrame")
        self._zoom_tools.pack(side="left")
        self._icon_button(self._zoom_tools, "zoom_out", "tip_zoom_out", lambda: self.canvas_view.zoom_out()).pack(
            side="left")
        self.lbl_zoom = ttk.Label(self._zoom_tools, text="100%", style="Muted.TLabel", width=5, anchor="center")
        self.lbl_zoom.pack(side="left")
        self._icon_button(self._zoom_tools, "zoom_in", "tip_zoom_in", lambda: self.canvas_view.zoom_in()).pack(
            side="left")
        self._icon_button(self._zoom_tools, "fit", "tip_fit", lambda: self.canvas_view.fit()).pack(side="left")
        self.btn_arrange = self.ui.label(ttk.Button(self._zoom_tools, style="Link.TButton", command=self.arrange),
                                         "btn_arrange")
        self.btn_arrange.pack(side="left", padx=(14, 0))
        w.Tooltip(self.ui, self.btn_arrange, lambda: self.ui.t("tip_arrange"))

        self.canvas_view = ScriptCanvas(self.ui, body, on_select=self._canvas_select,
                                        on_activate=self._canvas_activate, on_placeholder=self._add_menu,
                                        on_delete=self._delete, on_plus=self._plus, on_moved=self._moved,
                                        on_zoom=lambda _s: self.lbl_zoom.configure(text=self.canvas_view.zoom_text()))
        w.Tooltip(self.ui, self.canvas_view.canvas, lambda: self.ui.t("canvas_hint"))
        self.canvas_view.frame.bind("<Destroy>", self._cancel_zoom, add="+")

        self._list = ttk.Frame(body, style="Plain.TFrame")
        self._list.columnconfigure(0, weight=1)
        self._list.rowconfigure(1, weight=1)
        head = ttk.Frame(self._list, style="Plain.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.columnconfigure(0, weight=1)
        # text set by render(): a script is made of steps, a recorded macro of actions, and the words say which
        self.lbl_steps_header = ttk.Label(head, style="SectionTitle.TLabel", text=self.ui.t("editor_steps_header"))
        self.lbl_steps_header.grid(row=0, column=0, sticky="w")
        self.btn_add = ttk.Button(head, style="Primary.TButton", command=self._add, text=self.ui.t("btn_add_step"))
        self.btn_add.grid(row=0, column=1, sticky="e")

        holder = ttk.Frame(self._list, style="Plain.TFrame")
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(holder, columns=("n", "kind", "detail"), show="headings", selectmode="browse",
                                 height=10)
        self.tree.column("#0", width=34, minwidth=24, stretch=False)
        self.tree.column("n", width=52, anchor="w", stretch=False)
        self.tree.column("kind", width=170, anchor="w", stretch=False)
        self.tree.column("detail", width=320, anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=w.autohide(bar))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        self.tree.bind("<Delete>", lambda _e: self._delete())
        self._headings()
        self.ui.on_theme(self._paint_tags)
        self._paint_tags()

        tools = ttk.Frame(self._list, style="Plain.TFrame")
        tools.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.btn_up = self.ui.label(ttk.Button(tools, command=lambda: self._move(-1)), "btn_move_up")
        self.btn_up.pack(side="left", padx=(0, 6))
        self.btn_down = self.ui.label(ttk.Button(tools, command=lambda: self._move(1)), "btn_move_down")
        self.btn_down.pack(side="left", padx=(0, 6))
        self.btn_duplicate = self.ui.label(ttk.Button(tools, command=self.duplicate_step), "btn_duplicate_step")
        self.btn_delete = self.ui.label(ttk.Button(tools, style="SmallDanger.TButton", command=self._delete),
                                        "btn_delete_step")
        self.btn_delete.pack(side="left")

        self._build_panel(body)

    def _headings(self) -> None:
        v2 = self.c.editing_v2
        for column, key in (("n", "col_step_number"), ("kind", "col_flow_step" if v2 else "col_step_kind"),
                            ("detail", "col_step_detail")):
            self.tree.heading(column, text=self.ui.t(key))
        # a script's blocks are a tree (an IF's branches under it); a recorded macro's actions a flat table
        self.tree.configure(show="tree headings" if v2 else "headings")
        self.lbl_steps_header.configure(text=self.ui.t("editor_flow_header" if v2 else "editor_steps_header"))
        self.btn_add.configure(text=self.ui.t("btn_add_flow_step" if v2 else "btn_add_step"))

    def _paint_tags(self) -> None:
        self.tree.tag_configure("pause", foreground=th.ACTIVE.text_muted)
        self.tree.tag_configure("locked", foreground=th.ACTIVE.warning)
        self.tree.tag_configure("branch", foreground=th.ACTIVE.block_if)

    def _build_panel(self, body: ttk.Frame) -> None:
        outer = ttk.Frame(body, style="Plain.TFrame", width=PANEL_WIDTH, height=420)
        outer.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(20, 0))
        outer.grid_propagate(False)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        holder, panel = w.scroll_area(self.ui, outer)
        holder.grid(row=0, column=0, sticky="nsew")
        # Apply / Revert stay in view below the scrolling form: a long condition never hides them.
        footer = ttk.Frame(outer, style="Plain.TFrame")
        footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self._footer = footer
        panel.columnconfigure(1, weight=1)
        self._panel = panel

        self.ui.label(ttk.Label(panel, style="SectionTitle.TLabel"), "editor_properties_header").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.lbl_kind = ttk.Label(panel, text="", style="Heading.TLabel", wraplength=340, justify="left")
        self.lbl_kind.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self.lbl_kind_hint = ttk.Label(panel, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self.lbl_kind_hint.grid(row=2, column=0, columnspan=2, sticky="w")
        # the chosen step in words (a script) -- the same sentence the list and the canvas show
        self.lbl_step = ttk.Label(panel, text="", style="Plain.TLabel", wraplength=340, justify="left")
        self.lbl_step.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ---- a recorded macro's action: its few fields
        self._fields: dict[str, tuple[ttk.Label, ttk.Widget]] = {}
        self.var_pause = tk.StringVar()
        self.var_x, self.var_y = tk.StringVar(), tk.StringVar()
        self.var_key, self.var_text = tk.StringVar(), tk.StringVar()
        self._field(panel, 4, "pause", "field_pause_seconds", ttk.Entry(panel, textvariable=self.var_pause))
        self._field(panel, 5, "x", "field_x", ttk.Entry(panel, textvariable=self.var_x))
        self._field(panel, 6, "y", "field_y", ttk.Entry(panel, textvariable=self.var_y))
        self._keys = {fmt.key_label(key.value): key for key in Key}
        self._field(panel, 7, "key", "field_key",
                    ttk.Combobox(panel, textvariable=self.var_key, values=list(self._keys), state="readonly"))
        self._field(panel, 8, "text", "field_text", ttk.Entry(panel, textvariable=self.var_text))
        self.btn_apply = self.ui.label(ttk.Button(panel, style="Primary.TButton", command=self._apply),
                                       "btn_apply")
        self.btn_apply.grid(row=9, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.lbl_note = ttk.Label(panel, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self.lbl_note.grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # ---- a script's block: its form, Apply / Revert, and what can be done with the block
        self._form_box = ttk.Frame(panel, style="Plain.TFrame")
        self._form_box.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self._form_box.columnconfigure(0, weight=1)
        self.lbl_form_error = ttk.Label(self._footer, text="", style="Error.TLabel", wraplength=360, justify="left")
        self.lbl_form_error.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._form_buttons = ttk.Frame(self._footer, style="Plain.TFrame")
        self._form_buttons.grid(row=1, column=0, sticky="w")
        self.btn_form_apply = ttk.Button(self._form_buttons, style="Primary.TButton", command=self.apply_form)
        self.btn_form_apply.pack(side="left", padx=(0, 8))
        self.btn_form_revert = ttk.Button(self._form_buttons, command=self.revert_form)
        self.btn_form_revert.pack(side="left")
        self._block_actions = ttk.Frame(panel, style="Plain.TFrame")
        self._block_actions.grid(row=14, column=0, columnspan=2, sticky="w", pady=(18, 0))
        ttk.Separator(self._block_actions).pack(fill="x", pady=(0, 12))
        actions = ttk.Frame(self._block_actions, style="Plain.TFrame")
        actions.pack(anchor="w")
        self.btn_step_up = self._icon_button(actions, "up", "btn_move_up", lambda: self._move(-1))
        self.btn_step_up.pack(side="left")
        self.btn_step_down = self._icon_button(actions, "down", "btn_move_down", lambda: self._move(1))
        self.btn_step_down.pack(side="left", padx=(0, 8))
        self.btn_step_duplicate = self.ui.label(ttk.Button(actions, command=self.duplicate_step),
                                                "btn_duplicate_step")
        self.btn_step_duplicate.pack(side="left", padx=(0, 8))
        self.btn_step_delete = self.ui.label(ttk.Button(actions, style="SmallDanger.TButton", command=self._delete),
                                             "btn_delete_step")
        self.btn_step_delete.pack(side="left")

        # ---- nothing chosen in a script: what a script is, and the blocks that can be added
        self._overview = ttk.Frame(panel, style="Plain.TFrame")
        self._overview.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._overview.columnconfigure(0, weight=1)
        self.lbl_overview = ttk.Label(self._overview, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self.lbl_overview.grid(row=0, column=0, sticky="w")
        self.ui.label(ttk.Label(self._overview, style="SectionTitle.TLabel"), "palette_header").grid(
            row=1, column=0, sticky="w", pady=(16, 6))
        self.palette_buttons: dict[str, ttk.Button] = {}
        for row, (key, kind, preset, glyph) in enumerate(PALETTE, start=2):
            card = w.card(self.ui, self._overview, padding=(12, 10))
            card.grid(row=row, column=0, sticky="ew", pady=(0, 8))
            card.columnconfigure(1, weight=1)
            icon = ttk.Label(card, style="Panel.TLabel")
            paint = lambda i=icon, g=glyph: i.configure(image=self.ui.icon(g, th.ACTIVE.primary))   # noqa: E731
            self.ui.on_theme(paint)
            paint()
            icon.grid(row=0, column=0, rowspan=2, padx=(0, 10))
            button = self.ui.label(ttk.Button(card, style="Card.Link.TButton",
                                              command=lambda k=kind, p=preset: self._add_step(k, preset=p)), key)
            button.grid(row=0, column=1, sticky="w")
            self.ui.label(ttk.Label(card, style="PanelCaption.TLabel", wraplength=280, justify="left"),
                          f"{key}_hint").grid(row=1, column=1, sticky="w")
            self.palette_buttons[key] = button

    def _field(self, panel, row: int, name: str, key: str, widget: ttk.Widget) -> None:
        label = self.ui.label(ttk.Label(panel, style="FieldLabel.TLabel"), key)
        label.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        self._fields[name] = (label, widget)

    def _show_fields(self, names) -> None:
        for name, (label, widget) in self._fields.items():
            if name in names:
                label.grid()
                widget.grid()
            else:
                label.grid_remove()
                widget.grid_remove()

    # ---- state ---------------------------------------------------------------------------------
    def _blocks(self):
        return self.c.editor.blocks() if self.c.editor is not None and not self.c.editing_v2 else ()

    def _current(self):
        """The chosen row as (index into the block list, Block), or (None, None)."""
        selection = self.tree.selection()
        blocks = self._blocks()
        if not selection:
            return None, None
        try:
            index = int(selection[0])
        except ValueError:
            return None, None
        return (index, blocks[index]) if 0 <= index < len(blocks) else (None, None)

    # ---- actions --------------------------------------------------------------------------------
    def go_back(self) -> None:
        """Leaving with unsaved work always asks; it is never lost silently."""
        if self.c.editor_dirty:
            name = self.c.editor.document.name
            answer = dialogs.UnsavedChanges(self.ui, name).show()
            if answer is None:
                return
            if answer == "save" and not self._save_editor():
                return
        scripts = self.c.editing_v2
        self.c.close_editor()
        self._reset_form()
        self.ui.show_screen("scripts" if scripts else "macros")
        self.ui.render()

    def save(self) -> None:
        self._save_editor()
        self.ui.render()

    def _save_editor(self) -> bool:
        """Save. A file changed outside since it was opened here is replaced only after asking. A new script whose
        name a file made meanwhile has taken is offered another name instead of being refused for good: the work in
        it is never lost, and the other file is never written over without being asked."""
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

    _save = save

    def _toggle_adapt(self) -> None:
        if self.c.editor is not None and not self.c.editing_v2:
            self.c.editor_apply(me.set_adapt, self.adapt_var.get())
        self.ui.render()

    def _undo(self) -> None:
        self.c.editor_undo()
        self.ui.render()

    def _redo(self) -> None:
        self.c.editor_redo()
        self.ui.render()

    def set_view(self, view: str) -> None:
        """Canvas or list: two views of the same steps and the same selection."""
        if view not in (VIEW_CANVAS, VIEW_LIST) or view == self.view:
            return
        self.view = view
        self.ui.render()

    # ---- v2: which block is chosen ---------------------------------------------------------------
    def _selected(self) -> tuple[str | None, tuple]:
        """("step", path) | ("header", the branch's list path) | ("start", fe.START) | (None, ())."""
        selection = self.tree.selection()
        if not selection or self.c.editor is None:
            return None, ()
        what, path = _parse_iid(selection[0])
        if what == "step":
            try:
                fe.item_at(self.c.editor.document, path)
            except EditError:
                return None, ()
        return what, path

    def _selected_path(self) -> tuple | None:
        """The chosen block's path, or None (nothing, a branch's row, or Start)."""
        what, path = self._selected()
        return path if what == "step" else None

    def _step_index(self) -> int | None:
        """The chosen block's top-level number when it is a top-level step (what the toolbar's tests ask)."""
        path = self._selected_path()
        return path[0] if path is not None and len(path) == 1 else None

    def _select_path(self, path: tuple | None) -> None:
        if path is None:
            self.tree.selection_remove(*self.tree.selection())
            return
        iid = START_IID if tuple(path) == fe.START else _iid(tuple(path))
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _select_step(self, index) -> None:
        self._select_path(None if index is None else fe._path(index))

    def _canvas_select(self, path: tuple, part: str = "") -> None:
        path = tuple(path)
        if path == fe.END:
            path = None
        current = START_IID if self._selected()[0] == "start" else self._selected_path()
        wanted = START_IID if path == fe.START else path
        self._focus_part = part
        if wanted == current and self._pending is None:
            self._focus_chosen_part()
            return
        if not self._may_leave_form():
            return
        self._pending = None
        self._select_path(path)
        self.ui.render()
        if path is not None:
            self.canvas_view.ensure_visible(path)
        self._focus_chosen_part()

    def _canvas_activate(self, path: tuple, part: str = "") -> None:
        self._canvas_select(path, part)
        self._focus_form()

    def _focus_chosen_part(self) -> None:
        """The cursor into the field of the part of the block that was clicked."""
        part, self._focus_part = self._focus_part, ""
        form = self.form
        if form is None or not part:
            return
        widget = None
        if part == "condition" and getattr(form, "condition", None) is not None:
            widget = form.condition.focus_widget()
        elif part == "timing":
            widget = getattr(form, "timeout_entry", None)
        elif part == "macro" and getattr(form, "chooser", None) is not None:
            widget = form.chooser.box
        elif part == "op" and getattr(form, "tap", None) is not None:
            widget = form.tap.op_box
        elif part == "delay":
            widget = getattr(form, "entry", None)
        if widget is not None:
            try:
                widget.focus_set()
            except tk.TclError:
                pass

    def _focus_form(self) -> None:
        if self.form is not None:
            try:
                self.form.frame.focus_set()
            except tk.TclError:
                pass

    def edit_step(self) -> None:
        """Edit the chosen block: its fields are already in the properties panel, so this brings them forward."""
        if self._selected_path() is not None and self.c.editing_v2:
            self._pending = None
            self.ui.render()
            self._focus_form()

    # ---- v2: adding a block ------------------------------------------------------------------------
    def _insert_slot(self) -> tuple:
        """Where a block added from the toolbar goes: after the chosen block, at the start of the chosen branch,
        or at the end of the script."""
        what, path = self._selected()
        if what == "step":
            return path[:-1] + (path[-1] + 1,)
        if what == "header":
            blocks = fe.item_at(self.c.editor.document, path[:-1]).branch(path[-1])
            return path + (1 if blocks and isinstance(blocks[0], TapMatch) else 0,)    # a click stays first
        return (len(self.c.editor.document.steps),)

    def _entries(self, slot: tuple, at=None) -> list:
        """The kinds of block that may go at `slot`, as menu entries."""
        document = self.c.editor.document
        entries = []
        for key, kind, preset, _glyph in PALETTE + (TAP_ENTRY,):
            if kind == fe.TAP and not fe.can_insert(document, slot, fe.TAP):
                continue
            allowed = fe.can_insert(document, slot, kind)
            entries.append((key, lambda k=kind, p=preset: self._start_adding(slot, k, p, at), allowed))
        return entries

    def _add_menu(self) -> None:
        if self.c.editor is None or not self.c.editing_v2:
            return
        w.popup(self.ui, self.btn_add_block, self._entries(self._insert_slot()))

    def _add_menu_from(self, widget) -> None:
        w.popup(self.ui, widget, self._entries(self._insert_slot()))

    def _plus(self, slot: tuple, screen: tuple, scene: tuple) -> None:
        """The "+" in the middle of an arrow: a new block exactly there. On a script laid out by hand it is drawn
        at the "+" itself, and the blocks below make room."""
        if self.c.editor is None or not self._may_leave_form():
            return
        at = None
        if fe.placed(self.c.editor.document):
            at = fe.on_scene((scene[0] - fl.STEP_W / 2, scene[1] - fl.STEP_H / 2))
        w.popup(self.ui, self.canvas_view.canvas, self._entries(tuple(slot), at), at=screen)

    def _add_step(self, kind: str, *, preset: str = "") -> None:
        """Start adding a block after the chosen one (or at the end). It is shown where it will go, and its form is
        in the panel; it becomes part of the script only with Add (`apply_form`)."""
        if self.c.editor is None or not self.c.editing_v2:
            return
        self._start_adding(self._insert_slot(), kind, preset, None)

    def _start_adding(self, slot: tuple, kind: str, preset: str, at) -> None:
        if not self._may_leave_form():
            return
        self._pending, self._pending_invalid = (tuple(slot), kind, preset, at), False
        self.ui.render()
        self._focus_form()

    def apply_form(self) -> bool:
        """Apply the panel's form: insert the new block, change the chosen one, or set Start's delay. A refusal is
        shown in the panel (and on the pending block); nothing is changed."""
        if self.form is None or self._form_for is None:
            return False
        if self._form_for[0] == "start":
            try:
                seconds = self.form.seconds()
            except EditError as exc:
                self.show_form_message(error_text(self.ui, exc), error=True)
                return False
            if not self.c.editor_apply(fe.set_start, seconds, self.form.adapt.get()):
                self.show_form_message(self.ui.status_text(), error=True)
                return False
            self._form_for = None
            self.ui.render()
            return True
        try:
            step = self.form.build()
            # points and areas picked on the screen: the script's size, or scaled into it
            step, size = fe.fit_to_script(self.c.editor.document, step, self.form.picked)
        except EditError as exc:
            self._pending_invalid = self._form_for[0] == "new"
            self.show_form_message(error_text(self.ui, exc), error=True)
            self.ui.render()
            return False
        if self._form_for[0] == "new":
            slot = self._form_for[1]
            at = self._pending[3] if self._pending is not None else None
            room = None
            if at is not None:
                step = replace(step, at=at)
                room = fl.make_room(self._nodes(), self.canvas_view.layout, at)
            if not self.c.editor_apply(fe.sized(fe.insert_placed), slot, step, room, size=size):
                self.show_form_message(self.ui.status_text(), error=True)
                return False
            self._pending = None
            self._choose_step = _iid(slot)        # its row exists only after the redraw below
        else:
            path = self._form_for[1]
            if not self.c.editor_apply(fe.sized(fe.replace_item), path, step, size=size):
                self.show_form_message(self.ui.status_text(), error=True)
                return False
        self._form_for = None                  # rebuilt from the block as it now is
        self.ui.render()
        return True

    def revert_form(self) -> None:
        """Discard what was typed (or stop adding the new block)."""
        if self._pending is not None:
            self._pending = None
        self._form_for = None
        self.ui.render()

    def duplicate_step(self) -> None:
        path = self._selected_path()
        if path is not None and self.c.editor_apply(fe.duplicate_item, path):
            self._choose_step = _iid(path[:-1] + (path[-1] + 1,))   # the copy has no row until the redraw
        self.ui.render()

    def _moved(self, moves: dict) -> None:
        """Blocks were dragged and dropped. The first time in a script laid out automatically, every other block
        keeps the place it is drawn at now -- so moving one block moves only that one."""
        if self.c.editor is None:
            return
        positions = {tuple(p): xy for p, xy in moves.items()}
        if not fe.placed(self.c.editor.document):
            layout = self.canvas_view.layout
            for path in list(layout.order) + [fl.START_KEY, fl.END_KEY]:
                box = layout.box_of(path)
                if box is not None:
                    positions.setdefault(path, (int(round(box.x)), int(round(box.y))))
        self.c.editor_apply(fe.place, positions)
        self.ui.render()

    def arrange(self) -> None:
        """Every block back in its automatic place: one edit, undone like any other."""
        if self.c.editor is None or not self.c.editing_v2 or not fe.placed(self.c.editor.document):
            return
        if self.c.editor_apply(fe.arrange):
            self.c.note("info", "editor", self.ui.t("editor_arranged"))
            self._form_for = None
        self.ui.render()

    def _add(self) -> None:
        if self.c.editing_v2:
            self._add_menu_from(self.btn_add)
            return
        index, block = self._current()
        at = block.action_index + 1 if block is not None else len(self.c.editor.document.actions)
        answer = dialogs.AddAction(self.ui, list(ADDABLE), list(self._keys)).show()
        if not answer or not answer.get("op"):
            return
        op = answer["op"]
        kwargs = {}
        if op in (ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP, ActionOp.MOVE):
            kwargs = {"x": self._int(answer["x"]), "y": self._int(answer["y"])}
        elif op is ActionOp.KEY_PRESS:
            kwargs = {"key": self._keys.get(answer["key"], Key.ENTER)}
        else:
            kwargs = {"text": answer["text"]}
        if None in kwargs.values():
            self.ui.render()
            return
        self.c.editor_apply(me.insert, at, op, delay_ms=self._ms(answer["pause"]) or 0, **kwargs)
        self.ui.render()

    def _delete(self) -> None:
        if self.c.editing_v2:
            path = self._selected_path()
            if path is None:
                return
            removed = fmt.step_title(self.ui, fe.item_at(self.c.editor.document, path))
            if self.c.editor_apply(fe.delete_item, path):
                self.c.note("info", "editor", self.ui.t("editor_removed").format(what=removed))
                self._form_for = None
            self.ui.render()
            return
        index, block = self._current()
        if block is None:
            return
        removed = fmt.block_line(self.ui, block)
        if block.kind is BlockKind.PAUSE:
            done = self.c.editor_apply(me.set_pause, block.action_index, 0)
        else:
            done = self.c.editor_apply(me.delete, block.action_index)
        if done:
            self.c.note("info", "editor", self.ui.t("editor_removed").format(what=removed))
        self.ui.render()

    def _move(self, delta: int) -> None:
        if self.c.editing_v2:
            path = self._selected_path()
            if path is not None and self.c.editor_apply(fe.move_item, path, delta):
                moved = path[:-1] + (path[-1] + delta,)
                self.tree.selection_set(_iid(moved))
                self._choose_step = _iid(moved)
                self._form_for = None
            self.ui.render()
            return
        index, block = self._current()
        if block is None or block.kind is BlockKind.PAUSE:
            return
        self.c.editor_apply(me.move, block.action_index, delta)
        self.ui.render()

    def _apply(self) -> None:
        index, block = self._current()
        if block is None:
            return
        if block.kind is BlockKind.PAUSE:
            ms = self._ms(self.var_pause.get())
            if ms is not None:
                self.c.editor_apply(me.set_pause, block.action_index, ms)
        else:
            action = block.action
            if action.x is not None:
                x, y = self._int(self.var_x.get()), self._int(self.var_y.get())
                if x is not None and y is not None:
                    self.c.editor_apply(me.set_point, block.action_index, x, y)
            elif action.key is not None:
                self.c.editor_apply(me.set_key, block.action_index, self._keys.get(self.var_key.get(), action.key))
            elif action.text is not None:
                self.c.editor_apply(me.set_text, block.action_index, self.var_text.get())
        self.ui.render()

    def _int(self, text: str):
        try:
            return int(str(text).strip())
        except (TypeError, ValueError):
            self.c.note("error", "editor", self.ui.t("editor_needs_number"))
            return None

    def _ms(self, text: str):
        try:
            return int(round(float(str(text).strip().replace(",", ".")) * 1000))
        except (TypeError, ValueError):
            self.c.note("error", "editor", self.ui.t("editor_needs_number"))
            return None

    def _on_select(self) -> None:
        if self.c.editing_v2:
            what, path = self._selected()
            chosen = ("step", path) if what == "step" else ("start",) if what == "start" else None
            if self._form_target() is not None and self._form_target() != chosen:
                self._may_leave_form()          # typed changes of the block that was chosen are not dropped silently
            if self._pending is not None and what is not None:
                self._pending = None
            self._render_step_properties()
            self._render_canvas()
        else:
            self._render_properties()

    # ---- the form in the panel -----------------------------------------------------------------
    def show_form_message(self, text: str, *, error: bool) -> None:
        self.lbl_form_error.configure(text=text, style="Error.TLabel" if error else "Muted.TLabel")

    def _form_values(self):
        """Everything typed or picked in the form, to tell whether it was changed."""
        form = self.form
        if form is None:
            return None
        values = []
        for name in ("condition", "chooser", "tap"):
            part = getattr(form, name, None)
            if part is None:
                continue
            for attribute in ("kind_var", "expect_var", "label_var", "op_var", "dx", "dy", "var"):
                var = getattr(part, attribute, None)
                if var is not None:
                    values.append(var.get())
            if name == "condition":
                values.append(part.kind)
                values.append(part.region)
                values.append(id(part.template))
                values += [(k, n, v.get()) for k, fields in part.vars.items() for n, v in fields.items()]
                values.append(tuple((line["text"].get(), line["present"].get()) for line in part.lines))
        for name in ("timeout", "interval", "value", "unit", "adapt"):
            var = getattr(form, name, None)
            if var is not None:
                values.append(var.get())
        return tuple(values)

    def _form_target(self) -> tuple | None:
        """What the open form edits: ("step", path), ("start",), or None (nothing, or a block being added)."""
        if self._form_for is None or self._form_for[0] not in ("step", "start"):
            return None
        return ("step", self._form_for[1]) if self._form_for[0] == "step" else ("start",)

    def _form_changed(self) -> bool:
        return self.form is not None and self._form_for is not None and self._form_for[0] in ("step", "start") \
            and self._form_values() != self._form_snapshot

    def _may_leave_form(self) -> bool:
        """Before another block is chosen: changes typed into the panel but not applied are offered to be applied
        -- never dropped without a word."""
        if not self._form_changed():
            return True
        if self.ui.confirm(self.ui.t("dlg_unapplied_title"), self.ui.t("dlg_unapplied_text")):
            return self.apply_form()
        self._form_for = None                  # discarded on purpose
        return True

    def _reset_form(self) -> None:
        for child in self._form_box.winfo_children():
            child.destroy()
        self.form, self._form_for, self._form_snapshot = None, None, None
        self.lbl_form_error.configure(text="")

    @staticmethod
    def _same_fields(old, new) -> bool:
        """Whether two versions of a block differ only in what its form does not show (where it is drawn, an IF's
        branches): then the open form -- and what was typed into it -- is kept."""
        if type(old) is not type(new):
            return False
        if isinstance(old, Decision):
            return old.condition == new.condition
        return replace(old, at=None) == replace(new, at=None)

    def _ensure_form(self, wanted: tuple | None) -> None:
        if wanted == self._form_for and self.form is not None:
            return
        if wanted is not None and self.form is not None and self._form_for is not None \
                and wanted[0] == self._form_for[0] == "step" and wanted[1] == self._form_for[1] \
                and self._same_fields(self._form_for[2], wanted[2]):
            self._form_for = wanted            # moved, or its branches changed: the same fields, kept as typed --
            self.form.step = wanted[2]         # and built from the block as it is NOW (its place, its branches)
            return
        self._reset_form()
        if wanted is None:
            return
        if wanted[0] == "new":
            _, _slot, kind, preset = wanted
            self.form = StepForm(self.host, self._form_box, kind, stacked=True, preset=preset)
        elif wanted[0] == "start":
            self.form = StartForm(self.host, self._form_box, wanted[1], wanted[2], wanted[3])
        else:
            _, _path, step = wanted
            self.form = StepForm(self.host, self._form_box, fe.kind_of(step), step, stacked=True)
        self.form.frame.grid(row=0, column=0, sticky="ew")
        self._form_for = wanted
        self._form_snapshot = self._form_values()

    # ---- rendering -------------------------------------------------------------------------------
    def render(self) -> None:
        if self.c.editor is None:
            return
        document = self.c.editor.document
        self.lbl_title.configure(text=self.ui.t("editor_title").format(
            name=fmt.clip(document.name, TITLE_CHARS)))
        if getattr(self.c.editor, "empty", False):
            state = "editor_new_empty"
        elif self.c.editing_new:
            state = "editor_new_unsaved"
        else:
            state = "editor_unsaved" if self.c.editor_dirty else "editor_saved"
        self.lbl_state.configure(text=self.ui.t(state))
        can_save = self.c.editor_dirty and not getattr(self.c.editor, "empty", False)
        self.btn_save.state(["!disabled"] if can_save else ["disabled"])
        self._headings()                                   # headings follow the language too
        self.btn_undo.state(["!disabled"] if self.c.editor.can_undo else ["disabled"])
        self.btn_redo.state(["!disabled"] if self.c.editor.can_redo else ["disabled"])
        if self.c.editing_v2:
            self._v1_tools.pack_forget()
            self._v2_tools.pack(side="left", padx=(0, 12), before=self.btn_undo)
            self._render_steps()
            self._place_views()
            self._render_step_properties()
            self._render_canvas()
        else:
            self._v2_tools.pack_forget()
            self._v1_tools.pack(side="left", padx=(0, 12), before=self.btn_undo)
            self.adapt_var.set(document.adapt)
            self.chk_adapt.state(["!disabled"] if document.size is not None or document.adapt else ["disabled"])
            self._view_bar.grid_remove()
            self.canvas_view.frame.grid_remove()
            self._list.grid(row=0, column=0, rowspan=2, sticky="nsew")
            self._reset_form()
            self._pending = None
            self._render_rows()
            self._render_properties()

    def _place_views(self) -> None:
        self._view_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        if self.view == VIEW_CANVAS:
            self._list.grid_remove()
            self.canvas_view.frame.grid(row=1, column=0, sticky="nsew")
            self._zoom_tools.pack(side="left")
        else:
            self.canvas_view.frame.grid_remove()
            self._list.grid(row=1, column=0, rowspan=1, sticky="nsew")
            self._zoom_tools.pack_forget()
        self.btn_view_canvas.configure(style="NavActive.TButton" if self.view == VIEW_CANVAS else "Icon.TButton")
        self.btn_view_list.configure(style="NavActive.TButton" if self.view == VIEW_LIST else "Icon.TButton")
        placed = fe.placed(self.c.editor.document)
        self.btn_arrange.state(["!disabled"] if placed else ["disabled"])

    # ---- v2 rendering ----------------------------------------------------------------------------
    def _render_steps(self) -> None:
        document = self.c.editor.document
        blocks = list(walk(document.steps))
        signature = ("v2", fmt.delay_text(self.ui, document.start_delay_s)) + tuple(
            (path, fmt.step_line(self.ui, item)) for path, item in blocks)
        wanted, self._choose_step = self._choose_step, None
        if signature == self._signature:
            self._choose(wanted)
            return
        self._signature = signature
        chosen = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        t = self.ui.t
        self.tree.insert("", "end", iid=START_IID, values=("", t("block_start"),
                                                          fmt.delay_text(self.ui, document.start_delay_s)))
        for number, (path, item) in enumerate(blocks, start=1):
            parent = "" if len(path) == 1 else _header_iid(path[:-1])
            self.tree.insert(parent, "end", iid=_iid(path), open=True,
                             values=(str(number), fmt.step_title(self.ui, item), fmt.step_detail(self.ui, item)))
            if isinstance(item, Decision):
                for branch, words in ((THEN, "tree_then"), (ELSE, "tree_else")):
                    items = item.branch(branch)
                    detail = t("tree_branch_count").format(count=len(items)) if items else t("tree_branch_empty")
                    self.tree.insert(_iid(path), "end", iid=_header_iid(path + (branch,)), open=True,
                                     values=("", t(words), detail), tags=("branch",))
        if self._choose(wanted):
            return
        if chosen and self.tree.exists(chosen[0]):
            self.tree.selection_set(chosen[0])
        elif blocks and self._pending is None:
            self.tree.selection_set(_iid(blocks[0][0]))

    def _choose(self, row: str | None) -> bool:
        """Choose `row` and bring it into view, if the list shows it."""
        if row is None or not self.tree.exists(row):
            return False
        self.tree.selection_set(row)
        self.tree.see(row)
        return True

    def _kind_hint(self, kind: str) -> str:
        return self.ui.t({fe.WAIT: "hint_block_wait", fe.IF: "hint_block_if", fe.RUN: "hint_block_run",
                          fe.TAP: "hint_block_tap_match"}[kind])

    def _render_step_properties(self) -> None:
        self._show_fields(())
        self.btn_apply.grid_remove()
        self.lbl_note.grid_remove()                      # its empty row would leave a gap
        self.btn_duplicate.pack(side="left", padx=(0, 6), before=self.btn_delete)
        document = self.c.editor.document
        what, path = self._selected()
        t = self.ui.t
        if self._pending is not None:
            slot, kind, preset, _at = self._pending
            self._ensure_form(("new", slot, kind, preset))
            self.lbl_kind.configure(text=t("editor_new_block").format(what=t(self._palette_key(kind, preset))))
            self.lbl_kind_hint.configure(text=self._kind_hint(kind))
            self.lbl_step.configure(text="")
            self.btn_form_apply.configure(text=t("btn_add_to_script"))
            self.btn_form_revert.configure(text=t("btn_cancel_dialog"))
            self._show_parts(form=True, actions=False)
            enabled = False
        elif what == "start":
            self._ensure_form(("start", document.start_delay_s, document.adapt, document.size))
            self.lbl_kind.configure(text=t("block_start"))
            self.lbl_kind_hint.configure(text=t("hint_block_start"))
            self.lbl_step.configure(text=fmt.delay_text(self.ui, document.start_delay_s))
            self.btn_form_apply.configure(text=t("btn_apply"))
            self.btn_form_revert.configure(text=t("btn_revert"))
            self._show_parts(form=True, actions=False)
            enabled = False
        elif what == "step":
            step = fe.item_at(document, path)
            self._ensure_form(("step", path, step))
            self.lbl_kind.configure(text=fmt.step_title(self.ui, step))   # the same words as its block
            self.lbl_kind_hint.configure(text=self._kind_hint(fe.kind_of(step)))
            self.lbl_step.configure(text=fmt.step_detail(self.ui, step))
            self.btn_form_apply.configure(text=t("btn_apply"))
            self.btn_form_revert.configure(text=t("btn_revert"))
            self._show_parts(form=True, actions=True)
            enabled = True
        else:
            self._ensure_form(None)
            if what == "header":
                owner = fe.item_at(document, path[:-1])
                items = owner.branch(path[-1])
                self.lbl_kind.configure(text=t("tree_then" if path[-1] == THEN else "tree_else").rstrip(":"))
                self.lbl_kind_hint.configure(text=t("if_branches_hint"))
                self.lbl_step.configure(text=fmt.branch_line(self.ui, items))
                self.lbl_overview.configure(text="")
            else:
                steps = document.steps
                self.lbl_kind.configure(text=t("editor_overview_title") if steps else t("editor_empty_title"))
                self.lbl_kind_hint.configure(text="")
                self.lbl_step.configure(text="")
                self.lbl_overview.configure(text=t("editor_overview") if steps else t("editor_empty_hint"))
            self._show_parts(form=False, actions=False)
            self._overview.grid()
            enabled = False
        for button in (self.btn_up, self.btn_down, self.btn_delete, self.btn_duplicate, self.btn_step_up,
                       self.btn_step_down, self.btn_step_duplicate, self.btn_step_delete):
            button.state(["!disabled"] if enabled else ["disabled"])
        if enabled:
            siblings = len(fe._blocks_of(document, path[:-1]))
            for button in (self.btn_up, self.btn_step_up):
                button.state(["disabled"] if path[-1] == 0 else ["!disabled"])
            for button in (self.btn_down, self.btn_step_down):
                button.state(["disabled"] if path[-1] == siblings - 1 else ["!disabled"])
        self._focus_chosen_part()

    def _show_parts(self, *, form: bool, actions: bool) -> None:
        for part in (self._form_box, self._form_buttons, self.lbl_form_error):
            part.grid() if form else part.grid_remove()
        self._block_actions.grid() if actions else self._block_actions.grid_remove()
        self._overview.grid_remove()

    @staticmethod
    def _palette_key(kind: str, preset: str) -> str:
        return next(key for key, k, p, _glyph in PALETTE + (TAP_ENTRY,) if k == kind and p == preset)

    def _nodes(self, items=None, prefix: tuple = ()) -> tuple:
        """The script's shape for the layout: each block's path, kind, place and (an IF) branches."""
        if items is None:
            items = self.c.editor.document.steps
        nodes = []
        for index, item in enumerate(items):
            path = prefix + (index,)
            if isinstance(item, Decision):
                nodes.append(fl.Node(path, fe.IF, item.at, self._nodes(item.then, path + (THEN,)),
                                     self._nodes(item.otherwise, path + (ELSE,))))
            else:
                nodes.append(fl.Node(path, fe.kind_of(item), item.at))
        return tuple(nodes)

    def block_views(self) -> dict:
        """Each block as the canvas shows it -- the same words as the list, plus what the canvas can mark."""
        t = self.ui.t
        views = {}
        for number, (path, step) in enumerate(walk(self.c.editor.document.steps), start=1):
            kind = fe.kind_of(step)
            badge = warning = ""
            condition = getattr(step, "condition", None)
            if condition is not None and isinstance(condition.query, (TextQuery, TextLinesQuery)):
                badge = t("badge_text")
                if self.c.ocr is None:
                    warning = t("block_warn_no_reader")
            detail2 = ""
            if isinstance(step, WaitUntil):
                detail = fmt.condition_line(self.ui, step.condition)
                detail2 = t("block_wait_limit").format(seconds=fmt.number(self.ui, step.timeout_s, 2))
            elif isinstance(step, Decision):
                detail = fmt.condition_line(self.ui, step.condition)
                if not step.then and not step.otherwise:
                    warning = t("block_warn_empty_if")
            elif isinstance(step, RunMacro):
                detail = t("step_run_detail").format(name=step.macro.name or t("unnamed"),
                                                     actions=fmt.action_count(self.ui, len(step.macro.actions)))
            elif isinstance(step, TapMatch):
                detail = fmt.tap_line(self.ui, step)
            else:
                detail = fmt.step_detail(self.ui, step)
            views[path] = BlockView(kind=kind, title=fmt.step_title(self.ui, step),
                                    detail=fmt.clip(detail, DETAIL_CHARS), detail2=detail2, number=str(number),
                                    badge=badge, warning=warning)
        return views

    def _render_canvas(self) -> None:
        if self.c.editor is None or not self.c.editing_v2:
            return
        t = self.ui.t
        document = self.c.editor.document
        words = CanvasWords(start=t("canvas_start"), end=t("canvas_end"), yes=t("canvas_yes"), no=t("canvas_no"),
                            timeout=t("canvas_timeout"), stop=t("canvas_stop"), placeholder=t("canvas_placeholder"),
                            pending_hint=t("canvas_pending_hint"),
                            start_detail=fmt.delay_text(self.ui, document.start_delay_s))
        pending = pending_title = None
        if self._pending is not None:
            slot, kind, preset, at = self._pending
            pending = (slot, kind, at)
            pending_title = t("editor_new_block").format(what=t(self._palette_key(kind, preset)))
        what, path = self._selected()
        selected = fe.START if what == "start" else path if what == "step" else None
        self.canvas_view.show(self._nodes(), self.block_views(),
                              selected=selected if self._pending is None else None, words=words,
                              start_at=document.start_at, end_at=document.end_at, pending=pending,
                              pending_title=pending_title or "", pending_invalid=self._pending_invalid)
        opened = id(self.c.editor)
        if self._fitted_for != opened and self.view == VIEW_CANVAS:
            self._fitted_for = opened
            self._cancel_zoom()
            self._zoom_job = self.canvas_view.frame.after_idle(self._initial_zoom)
        self.lbl_zoom.configure(text=self.canvas_view.zoom_text())

    def _cancel_zoom(self, _event=None) -> None:
        """The first fit is not wanted any more (another script, or the canvas is going away): an idle callback
        whose widget is gone would only fail later, in Tcl."""
        job, self._zoom_job = self._zoom_job, None
        if job is not None:
            try:
                self.canvas_view.frame.after_cancel(job)
            except tk.TclError:
                pass

    def _initial_zoom(self) -> None:
        """A newly opened script starts at 100% when it fits; otherwise fitted, but never so small that its words
        cannot be read (80% at least -- the rest is a scroll away, and Fit to View shows it all)."""
        self._zoom_job = None
        view = self.canvas_view
        layout, canvas = view.layout, view.canvas
        fitted = fl.fit_scale(layout.width, layout.height, max(canvas.winfo_width(), 1),
                              max(canvas.winfo_height(), 1), high=1.0)
        view.set_scale(max(READABLE_ZOOM, fitted))
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)
        self.lbl_zoom.configure(text=view.zoom_text())

    # ---- v1 rendering ----------------------------------------------------------------------------
    def _render_rows(self) -> None:
        blocks = self._blocks()
        signature = tuple((b.kind.value, b.number, b.delay_ms,
                           None if b.action is None else fmt.action_detail(self.ui, b.action))
                          for b in blocks)
        if signature == self._signature:
            return
        self._signature = signature
        chosen = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for index, block in enumerate(blocks):
            if block.kind is BlockKind.PAUSE:
                values = ("", fmt.block_title(self.ui, block), fmt.block_detail(self.ui, block))
                tags = ("pause",)
            else:
                values = (f"{block.number:02d}", fmt.action_title(self.ui, block.action),
                          fmt.action_detail(self.ui, block.action))
                tags = () if block.structural else ("locked",)
            self.tree.insert("", "end", iid=str(index), values=values, tags=tags)
        if chosen and self.tree.exists(chosen[0]):
            self.tree.selection_set(chosen[0])
        elif blocks:
            self.tree.selection_set("0")

    def _render_properties(self) -> None:
        self.btn_duplicate.pack_forget()                 # v2 only: a recorded action is not duplicated
        for part in (self._form_box, self._form_buttons, self.lbl_form_error, self._block_actions, self._overview):
            part.grid_remove()
        self.lbl_step.configure(text="")
        self.lbl_kind_hint.configure(text="")
        self.btn_apply.grid()
        self.lbl_note.grid()
        index, block = self._current()
        movable = False
        if block is None:
            self.lbl_kind.configure(text=self.ui.t("editor_no_selection"))
            self._show_fields(())
            self.btn_apply.state(["disabled"])
            self.lbl_note.configure(text="")
        elif block.kind is BlockKind.PAUSE:
            self.lbl_kind.configure(text=self.ui.t("act_pause"))
            self._show_fields(("pause",))
            self.var_pause.set(fmt.number(self.ui, block.delay_ms / 1000.0, 2))
            self.btn_apply.state(["!disabled"])
            self.lbl_note.configure(text="")
        else:
            action = block.action
            self.lbl_kind.configure(text=fmt.action_title(self.ui, action))
            if action.x is not None:
                self._show_fields(("x", "y"))
                self.var_x.set(str(action.x))
                self.var_y.set(str(action.y))
            elif action.key is not None:
                self._show_fields(("key",))
                self.var_key.set(fmt.key_label(action.key.value))
            else:
                self._show_fields(("text",))
                self.var_text.set(action.text or "")
            self.btn_apply.state(["!disabled"])
            self.lbl_note.configure(text="" if block.structural else self.ui.t("editor_locked_hint"))
            movable = block.structural
        for button in (self.btn_up, self.btn_down, self.btn_delete):
            button.state(["!disabled"] if (movable or (block is not None and
                                                       block.kind is BlockKind.PAUSE and
                                                       button is self.btn_delete)) else ["disabled"])
