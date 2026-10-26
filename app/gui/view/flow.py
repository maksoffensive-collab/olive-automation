"""Editing one block of a script: the block's form (used in the editor's properties panel, and in a dialog), its
condition editor, the Start block's delay, and the screen pickers.

Small on purpose -- a form per block. A WAIT UNTIL has one condition, a timeout and an interval; an IF has one
condition (the blocks of its "yes" and "no" branches are blocks of their own, added with the "+" on their arrows);
a recorded-macro block has its macro; a click on the match its kind of click and an offset. Each condition kind
shows only its own fields. The Start block has the delay before the first step.

Nothing here decides what is valid. On Apply the fields go to `app.gui.flow_edit`, which hands them to the domain
constructors and to the file format's own read-back check; a refusal is shown in the dialog, in the user's
language where the platform has the sentence, and the dialog stays open so nothing typed is lost.

Screens come only from the controller (`begin_screen` / `finish_screen`): a screenshot of the ONE target chosen
explicitly, taken and decoded off the UI thread. The picker shows it scaled by a whole factor and converts every
mark back to the screenshot's own pixels (`app.gui.region`); the window on the desktop is never touched, activated
or raised, and no pointer is moved. A template is cut from that screenshot in memory: there is no file dialog, so a
template can never come from an arbitrary path. It is written as `<macro>.assets/template-<sha256>.png` by the
library's save, through the v2 serializer, like every other template.
"""
from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk

from app.gui import flow_edit as fe
from app.gui import region as geo
from app.gui.controller import START_NOTICE_S
from app.gui import theme as th
from app.gui.macro_edit import EditError
from app.gui.view import dialogs, fmt
from dataclasses import replace

from app.observe import MAX_TEXT_LINES
from app.decide import MAX_START_DELAY_S, Decision, TapMatch
from app.playback.macro import REQUIRED_CAPABILITY, ActionOp
from app.vision.bitmap import encode_png

LABEL_WIDTH = 104                # the label column of a one-column (panel) form
PICKER_MAX = (880, 480)          # the shown screenshot fits in this, so the picker fits a 1120 x 680 window's screen
THUMB_MAX = (180, 70)
NEAR_MISS = 0.9                  # a picture not found but this close: "test it now" says how low the confidence must be
SEARCH_MARGIN = 20               # "search only around here": the template's place plus this many pixels each side
DEFAULTS = {
    fe.PIXEL: {"x": "0", "y": "0", "color": "0, 0, 0", "tolerance": "0"},
    fe.COLOR: {"color": "255, 0, 0", "tolerance": "10", "min_ratio": "0.5"},
    fe.TEMPLATE: {"min_confidence": "0.95", "max_results": "1", "any_size": "0"},
    fe.TEXT: {"language": "", "min_confidence": "0"},
}
# The typed fields of each kind, two to a row: (field, label key, width).
FIELDS = {
    fe.PIXEL: (("x", "field_x", 7), ("y", "field_y", 7), ("color", "field_color", 13),
               ("tolerance", "field_tolerance", 5)),
    fe.COLOR: (("color", "field_color", 13), ("tolerance", "field_tolerance", 5),
               ("min_ratio", "field_min_ratio", 5)),
    fe.TEMPLATE: (("min_confidence", "field_min_confidence", 5),),
    fe.TEXT: (("language", "field_language", 6), ("min_confidence", "field_min_confidence", 5)),
}
SWATCH_EMPTY = "#ffffff"          # the colour sample when what is typed is not a colour
TAP_LABELS = {ActionOp.TAP: "act_tap", ActionOp.DOUBLE_TAP: "act_double_tap",
              ActionOp.SECONDARY_TAP: "act_secondary_tap"}


def error_text(ui, exc: BaseException) -> str:
    """A refusal in the user's language when the platform has the sentence; the backend's own words otherwise."""
    key = getattr(exc, "key", "")
    params = dict(getattr(exc, "params", {}) or {})
    if "field" in params:
        params["field"] = ui.t(params["field"]).rstrip(":")
    if key:
        try:
            return ui.t(key).format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    return str(exc)


def ensure_target(ui, dialog) -> bool:
    """A screenshot needs ONE explicitly chosen, verified target: ask for it if there is none. Never a guess."""
    c = ui.controller
    if c.target_ready:
        return True
    chosen = dialogs.ChooseTargets(ui, c.entries, (), single=True).show()
    dialog.regrab()
    return bool(chosen) and c.select(chosen[0])


def photo(ui, png: bytes, max_size: tuple[int, int]) -> tuple[tk.PhotoImage, int]:
    """A PNG as a Tk image scaled down by a whole factor to fit `max_size`, and that factor."""
    image = tk.PhotoImage(master=ui.root, data=base64.b64encode(png))
    factor = geo.display_factor(image.width(), image.height(), *max_size)
    return (image.subsample(factor) if factor > 1 else image), factor


class _Dialog(dialogs.Modal):
    """A modal that can show a refusal without closing, and knows when it is gone (a screenshot may arrive
    after the person has already closed it)."""

    def __init__(self, ui, title_key: str, *, width: int = 420) -> None:
        super().__init__(ui, title_key, width=width)
        self.alive = True
        self.top.bind("<Destroy>", self._gone, add="+")
        self.lbl_error = ttk.Label(self.body, text="", style="Muted.TLabel", wraplength=640, justify="left",
                                   foreground=th.ACTIVE.error)
        self.lbl_error.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.buttons.grid(row=3, column=0, sticky="e", pady=(12, 0))

    def _gone(self, event) -> None:
        if event.widget is self.top:
            self.alive = False

    def show_error(self, text: str) -> None:
        if self.alive:
            self.lbl_error.configure(text=text, foreground=th.ACTIVE.error)

    def show_note(self, text: str) -> None:
        if self.alive:
            self.lbl_error.configure(text=text, foreground=th.ACTIVE.text_muted)

    def regrab(self) -> None:
        if self.alive:
            self.top.grab_set()


# ---- the condition ------------------------------------------------------------------------------------------
class ConditionEditor:
    """Kind, expectation, label, the kind's own fields, the region, the template -- and "test it now".

    `dialog` is whatever hosts the form -- a dialog, or the editor's properties panel: anything with `ui`, `alive`,
    `show_error`, `show_note` and `regrab`. `columns` is how many typed fields share a row (1 in a narrow panel)."""

    def __init__(self, dialog: _Dialog, parent, condition, *, columns: int = 2) -> None:
        self.dialog, self.ui, self.c = dialog, dialog.ui, dialog.ui.controller
        self.columns = max(1, columns)
        form = fe.condition_form(condition)
        self.picked: dict = {}     # {part: size}: the point, the area, the picture, each the size it was picked on
        self.kind = form.kind
        self.region, self.template, self._kept = form.region, form.template, form.kept
        self._ignore_case = form.ignore_case
        # a text condition says "is there" / "is not there" per line; one saved as NOT (these lines) shows that too
        self._text_negated = form.kind == fe.TEXT and not form.expected
        self.vars = {kind: {name: tk.StringVar(value=value) for name, value in DEFAULTS[kind].items()}
                     for kind in fe.CONDITION_KINDS}
        for name, value in form.fields.items():
            self.vars[form.kind].setdefault(name, tk.StringVar()).set(value)
        t = self.ui.t
        # text: one row per text, each with its own "is there" / "is not there"
        self._presence = {t("line_present"): True, t("line_absent"): False}
        self.lines = [self._line(text, present) for text, present in (form.lines or [("", True)])]
        self._line_entries: list = []
        self._swatch = None
        colour = self.vars[fe.PIXEL]["color"]
        watch = colour.trace_add("write", lambda *_: self._paint_swatch())
        self._thumb = None
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.frame.columnconfigure(1, weight=1)

        def unwatch(event) -> None:             # the watch holds this editor: it goes with the editor's frame
            if event.widget is self.frame and colour.trace_info():
                try:
                    colour.trace_remove("write", watch)
                except tk.TclError:
                    pass
        self.frame.bind("<Destroy>", unwatch, add="+")

        # a colour-area condition is no longer offered for new conditions; one saved earlier still opens as itself
        offered = fe.OFFERED_KINDS if self.kind in fe.OFFERED_KINDS else fe.OFFERED_KINDS + (self.kind,)
        self._kinds = {t(f"cond_{k}"): k for k in offered}
        self.kind_var = tk.StringVar(value=t(f"cond_{self.kind}"))
        self._expects = {t("expect_present"): True, t("expect_absent"): False}
        self.expect_var = tk.StringVar(value=t("expect_present" if form.expected else "expect_absent"))
        self.label_var = tk.StringVar(value=form.label)

        stacked = self.columns == 1
        # one column (a narrow panel): every field starts at the same x and nothing is wider than the panel
        self.note_width = 330 if stacked else 380
        top = ttk.Frame(self.frame, style="Plain.TFrame")
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.ui.label(ttk.Label(top, style="FieldLabel.TLabel"), "field_condition_kind").grid(
            row=0, column=0, sticky="w", padx=(0, 6))
        kind_box = ttk.Combobox(top, textvariable=self.kind_var, values=list(self._kinds), state="readonly",
                                width=15)
        self.kind_box = kind_box
        kind_box.grid(row=0, column=1, sticky="w", padx=(0, 12))
        kind_box.bind("<<ComboboxSelected>>", lambda _e: self._on_kind())
        expect_box = ttk.Combobox(top, textvariable=self.expect_var, values=list(self._expects),
                                  state="readonly", width=18)
        if stacked:                               # under the kind, in the same column as every other field
            top.columnconfigure(0, minsize=LABEL_WIDTH)
            expect_box.grid(row=1, column=1, sticky="w", pady=(6, 0))
            self.frame.columnconfigure(0, minsize=LABEL_WIDTH)
        else:
            expect_box.grid(row=0, column=2, sticky="w")
        self.expect_box = expect_box              # text says "is there" / "is not there" per line instead
        self.ui.label(ttk.Label(self.frame, style="FieldLabel.TLabel"), "field_label").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
        ttk.Entry(self.frame, textvariable=self.label_var, width=24 if stacked else 30).grid(
            row=1, column=1, sticky="ew", padx=(0 if stacked else 8, 0), pady=(8, 0))
        self.specific = ttk.Frame(self.frame, style="Plain.TFrame")
        self.specific.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        where = ttk.Frame(self.frame, style="Plain.TFrame")
        where.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        where.columnconfigure(0, weight=1)
        self.lbl_target = ttk.Label(where, text="", style="Muted.TLabel", wraplength=220 if stacked else 300,
                                    justify="left")
        self.lbl_target.grid(row=0, column=0, sticky="w")
        self.ui.label(ttk.Button(where, style="Link.TButton", command=self._change_target),
                      "btn_change_target").grid(row=0, column=1, sticky="e")
        self.ui.label(ttk.Button(where, command=self._test), "btn_test_condition").grid(row=1, column=0,
                                                                                        sticky="w", pady=(4, 0))
        self.lbl_result = ttk.Label(where, text="", style="Muted.TLabel", wraplength=self.note_width,
                                    justify="left")
        self.lbl_result.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._rebuild()
        self.refresh_target()

    # ---- the form ---------------------------------------------------------------------------------
    def form(self) -> fe.ConditionForm:
        fields = {name: var.get() for name, var in self.vars[self.kind].items()}
        region = None if self.kind == fe.PIXEL else self.region
        template = self.template if self.kind == fe.TEMPLATE else None
        text = self.kind == fe.TEXT
        expected = True if text and not self._text_negated else self._expects.get(self.expect_var.get(), True)
        lines = [(line["text"].get(), self._presence.get(line["present"].get(), True)) for line in self.lines] \
            if text else []
        return fe.ConditionForm(self.kind, expected, self.label_var.get(), fields, region, template, lines,
                                self._kept, self._ignore_case)

    # ---- text lines -------------------------------------------------------------------------------
    def _line(self, text: str = "", present: bool = True) -> dict:
        t = self.ui.t
        return {"text": tk.StringVar(value=text),
                "present": tk.StringVar(value=t("line_present") if present else t("line_absent"))}

    def _focus_line(self, index: int) -> None:
        if 0 <= index < len(self._line_entries):
            entry = self._line_entries[index]
            entry.focus_set()
            entry.icursor("end")

    def new_line(self, after: int) -> str:
        """Enter in a line: a new, empty line right under it, with the cursor in it."""
        if len(self.lines) >= MAX_TEXT_LINES:
            self.dialog.show_error(self.ui.t("err_flow_too_many_lines").format(count=MAX_TEXT_LINES))
            return "break"
        self.lines.insert(after + 1, self._line())
        self._rebuild()
        self._focus_line(after + 1)
        return "break"

    def remove_line(self, index: int) -> None:
        """The last line is emptied rather than removed: there is always one to write in."""
        if len(self.lines) > 1:
            del self.lines[index]
        else:
            self.lines[0]["text"].set("")
        self._rebuild()
        self._focus_line(min(index, len(self.lines) - 1))

    def _backspace(self, index: int):
        """Backspace in an empty line removes it and goes back to the end of the one above."""
        if self.lines[index]["text"].get() or len(self.lines) == 1:
            return None
        del self.lines[index]
        self._rebuild()
        self._focus_line(max(index - 1, 0))
        return "break"

    def _lines_block(self, frame, row: int) -> int:
        t = self.ui.t
        box = ttk.Frame(frame, style="Plain.TFrame")
        box.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        box.columnconfigure(1, weight=1)
        self._line_entries = []
        for i, line in enumerate(self.lines):
            ttk.Label(box, text="\u2022", style="FieldLabel.TLabel").grid(row=i, column=0, sticky="w",
                                                                          padx=(0, 6))
            entry = ttk.Entry(box, textvariable=line["text"], width=14)
            entry.grid(row=i, column=1, sticky="ew", pady=2)
            entry.bind("<Return>", lambda _e, i=i: self.new_line(i))
            entry.bind("<KP_Enter>", lambda _e, i=i: self.new_line(i))
            entry.bind("<BackSpace>", lambda _e, i=i: self._backspace(i))
            ttk.Combobox(box, textvariable=line["present"], values=list(self._presence), state="readonly",
                         width=8).grid(row=i, column=2, sticky="w", padx=(6, 0))
            ttk.Button(box, text="\u00d7", style="Link.TButton", width=2,
                       command=lambda i=i: self.remove_line(i)).grid(row=i, column=3, padx=(2, 0))
            self._line_entries.append(entry)
        under = ttk.Frame(box, style="Plain.TFrame")
        under.grid(row=len(self.lines), column=0, columnspan=4, sticky="w", pady=(2, 0))
        ttk.Button(under, text=t("btn_add_line"), style="Link.TButton",
                   command=lambda: self.new_line(len(self.lines) - 1)).pack(side="left", anchor="n")
        ttk.Label(under, text=t("text_lines_hint"), style="Muted.TLabel", wraplength=self.note_width - 90,
                  justify="left").pack(side="left", padx=(8, 0))
        return row + 1

    # ---- the pixel's colour sample ------------------------------------------------------------------
    def _paint_swatch(self) -> None:
        if self._swatch is None or not self._swatch.winfo_exists():
            return
        try:
            r, g, b = fe.parse_color(self.vars[fe.PIXEL]["color"].get())
            colour = f"#{r:02x}{g:02x}{b:02x}" if all(0 <= c <= 255 for c in (r, g, b)) else SWATCH_EMPTY
        except EditError:
            colour = SWATCH_EMPTY
        self._swatch.configure(background=colour)

    def condition(self):
        return fe.build_condition(self.form())            # raises EditError, in the backend's terms

    def focus_widget(self):
        """Where the cursor goes when the condition of a block is clicked: the first text, or the kind."""
        if self.kind == fe.TEXT and self._line_entries:
            return self._line_entries[0]
        return self.kind_box

    def _on_kind(self) -> None:
        self.kind = self._kinds.get(self.kind_var.get(), fe.TEXT)
        self._rebuild()

    def _fields(self):
        """The typed fields of the current kind. A text reader without confidence gets no confidence field: a
        minimum it cannot apply must not be offered as if it worked (a saved value is kept, untouched)."""
        fields = FIELDS[self.kind]
        if self.kind == fe.TEXT and not self.c.text_confidence_reported:
            fields = tuple(f for f in fields if f[0] != "min_confidence")
        return fields

    def _confidence_note(self) -> str:
        if self.c.ocr is None:
            return self.ui.t("text_no_reader")
        if self.c.text_confidence_reported:
            return ""
        note = self.ui.t("text_no_confidence")
        try:
            saved = float(self.vars[fe.TEXT]["min_confidence"].get().replace(",", "."))
        except ValueError:
            saved = 0.0
        if saved > 0:
            note += " " + self.ui.t("text_confidence_ignored").format(value=fmt.number(self.ui, saved, 3))
        return note

    def _rebuild(self) -> None:
        for child in self.specific.winfo_children():
            child.destroy()
        self._swatch = None
        t, frame = self.ui.t, self.specific
        row = 0
        stacked = self.columns == 1
        if stacked:                               # one column: labels share one width, fields fill the rest
            frame.columnconfigure(0, minsize=LABEL_WIDTH)
            frame.columnconfigure(1, weight=1)
        if self.kind == fe.TEXT:
            if self._text_negated:
                self.expect_box.grid()
            else:
                self.expect_box.grid_remove()     # each line says whether its text must be there
            row = self._lines_block(frame, row)
        else:
            self.expect_box.grid()
        first = row
        for i, (name, key, width) in enumerate(self._fields()):
            r, col = divmod(i, self.columns)
            r += first
            ttk.Label(frame, text=t(key), style="FieldLabel.TLabel").grid(row=r, column=col * 2, sticky="w",
                                                                         padx=(0 if col == 0 else 12, 6), pady=3)
            ttk.Entry(frame, textvariable=self.vars[self.kind][name], width=width).grid(
                row=r, column=col * 2 + 1, sticky="ew" if stacked else "w", pady=3)
            row = r + 1
        if self.kind == fe.TEXT:
            note = self._confidence_note()
            if note:
                ttk.Label(frame, text=note, style="Muted.TLabel", wraplength=self.note_width,
                          justify="left").grid(
                    row=row, column=0, columnspan=4, sticky="w", pady=(2, 0))
                row += 1
        buttons = ttk.Frame(frame, style="Plain.TFrame")
        if self.kind == fe.PIXEL:
            ttk.Button(buttons, text=t("btn_pick_point"), command=lambda: self._pick("point", self._set_pixel)
                       ).pack(side="left")
            ttk.Button(buttons, text=t("btn_eyedropper"),
                       command=lambda: self._pick("color", self._set_pixel_color)).pack(side="left", padx=(6, 0))
            self._swatch = tk.Label(buttons, width=3, borderwidth=1, relief="solid", background=SWATCH_EMPTY)
            self._swatch.pack(side="left", padx=(8, 0), ipady=2)
            self._paint_swatch()
        if self.kind == fe.COLOR:
            ttk.Button(buttons, text=t("btn_pick_color"), command=lambda: self._pick("point", self._set_color)
                       ).pack(side="left", padx=(0, 6))
        if self.kind == fe.TEMPLATE:
            ttk.Button(buttons, text=t("btn_capture_template"),
                       command=lambda: self._pick("template", self._set_template)).pack(side="left")
        if buttons.winfo_children():
            buttons.grid(row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))
            row += 1
        if self.kind == fe.TEMPLATE:
            self._template_row(frame, row)
            row += 1
            ttk.Checkbutton(frame, text=t("template_any_size"), style="Choice.TCheckbutton",
                            variable=self.vars[fe.TEMPLATE]["any_size"], onvalue="1", offvalue="0").grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))
            row += 1
        if self.kind != fe.PIXEL:
            area = ttk.Frame(frame, style="Plain.TFrame")
            area.grid(row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))
            ttk.Label(area, text=t("field_region"), style="FieldLabel.TLabel").pack(side="left")
            ttk.Label(area, text=fmt.region_text(self.ui, self.region), style="Muted.TLabel").pack(side="left",
                                                                                                    padx=(6, 8))
            ttk.Button(area, style="Link.TButton", text=t("btn_pick_region"),
                       command=lambda: self._pick("region", self._set_region)).pack(side="left")
            ttk.Button(area, style="Link.TButton", text=t("btn_whole_screen"),
                       command=lambda: self._set_region(None, None)).pack(side="left", padx=(4, 0))

    def _template_row(self, frame, row: int) -> None:
        box = ttk.Frame(frame, style="Plain.TFrame")
        box.grid(row=row, column=0, columnspan=4, sticky="w", pady=(6, 0))
        if self.template is None:
            ttk.Label(box, text=self.ui.t("template_none"), style="Muted.TLabel").pack(side="left")
            return
        self._thumb, _ = photo(self.ui, encode_png(self.template), THUMB_MAX)
        tk.Label(box, image=self._thumb, borderwidth=1, relief="solid",
                 background=th.ACTIVE.surface).pack(side="left")
        ttk.Label(box, text=self.ui.t("template_size").format(w=self.template.width, h=self.template.height),
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))

    # ---- screens ----------------------------------------------------------------------------------
    def refresh_target(self) -> None:
        c = self.c
        if c.target_ready and c.selected is not None:
            name = c.selected.title or c.selected.process
            self.lbl_target.configure(text=self.ui.t("screen_of").format(name=name))
        else:
            self.lbl_target.configure(text=self.ui.t("screen_no_target"))

    def _change_target(self) -> None:
        chosen = dialogs.ChooseTargets(self.ui, self.c.entries, (), single=True).show()
        self.dialog.regrab()
        if chosen:
            self.c.select(chosen[0])
        self.refresh_target()

    def _pick(self, mode: str, apply) -> None:
        if not ensure_target(self.ui, self.dialog):
            self.refresh_target()
            return
        self.refresh_target()
        job = self.c.begin_screen()
        if job is None:
            self.dialog.show_error(self.ui.status_text())
            return
        self.dialog.show_note(self.ui.t("screen_capturing"))
        self.ui.run_job(job, lambda outcome: self._picked(outcome, job, mode, apply))

    def _picked(self, outcome, job, mode: str, apply) -> None:
        if not self.dialog.alive:
            return
        screen = self.c.finish_screen(outcome, job)
        if screen is None:
            self.dialog.show_error(self.ui.status_text())
            return
        self.dialog.show_note("")
        answer = ScreenPicker(self.ui, screen, mode, self.region if mode == "region" else None).show()
        self.dialog.regrab()
        if answer is not None and self.dialog.alive:
            apply(screen, answer)

    def _note_size(self, screen, *parts: str) -> None:
        """The size of the picture these parts (fe.PARTS) were just picked on: the script's size, or scaled into it
        -- each part from its own. "The whole screen" picks nothing on a picture (no `screen`)."""
        for part in parts:
            if screen is None:
                self.picked.pop(part, None)
            else:
                self.picked[part] = (screen.width, screen.height)

    def _set_region(self, screen, answer) -> None:
        self.region = None if answer is None else answer["roi"]
        self._note_size(screen, "region")
        self._rebuild()

    def _set_pixel(self, screen, answer) -> None:
        self._note_size(screen, "point")
        x, y = answer["point"]
        values = self.vars[fe.PIXEL]
        values["x"].set(str(x))
        values["y"].set(str(y))
        values["color"].set(fe.format_color(screen.frame.bitmap.pixel(x, y)))

    def _set_pixel_color(self, screen, answer) -> None:
        """The eyedropper: only the colour of the pixel clicked; the point stays where it is."""
        x, y = answer["point"]
        self.vars[fe.PIXEL]["color"].set(fe.format_color(screen.frame.bitmap.pixel(x, y)))

    def _set_color(self, screen, answer) -> None:
        x, y = answer["point"]
        self.vars[fe.COLOR]["color"].set(fe.format_color(screen.frame.bitmap.pixel(x, y)))

    def _set_template(self, screen, answer) -> None:
        self._note_size(screen, "template")
        roi = answer["roi"]
        self.template = screen.frame.bitmap.crop(roi.x, roi.y, roi.width, roi.height)
        if answer.get("limit"):
            self.region = geo.expand(roi, SEARCH_MARGIN, screen.width, screen.height)
            self._note_size(screen, "region")
        self._rebuild()

    # ---- asking it now ----------------------------------------------------------------------------
    def _test(self) -> None:
        try:
            # asked exactly as a run of the script would ask it: in the script's size, scaled to the screen's
            document = self.c.editor.document if self.c.editor is not None else None
            condition, picked_on = fe.probe_condition(document, self.condition(), self.picked)
        except EditError as exc:
            self.dialog.show_error(error_text(self.ui, exc))
            return
        if not ensure_target(self.ui, self.dialog):
            self.refresh_target()
            return
        self.refresh_target()
        job = self.c.begin_probe(condition, picked_on=picked_on)
        if job is None:
            self.dialog.show_error(self.ui.status_text())
            return
        self.dialog.show_note("")
        self.lbl_result.configure(text=self.ui.t("probe_running"))
        self.ui.run_job(job, lambda outcome: self._tested(outcome, job))

    def _tested(self, outcome, job) -> None:
        if not self.dialog.alive:
            return
        result = self.c.finish_probe(outcome, job)
        if result is None:
            # this probe's own reason: under the emergency stop the status bar keeps saying the stop instead
            self.lbl_result.configure(text=self.ui.status_text(self.c.probe_status))
            return
        center = result.center
        at = f"({center.x}, {center.y})" if center is not None else "—"
        detail = result.observation.detail
        closest = detail.get("best") if not (result.observation.found or result.satisfied) else None
        threshold = detail.get("min_confidence")
        if closest is not None:
            # a picture that was not found: where it came closest, so a threshold can be set knowingly -- and, when
            # it came near, the confidence at which it would have been found
            scale = detail.get("scale", 1.0)
            size = self.ui.t("probe_size").format(percent=round(scale * 100)) if scale != 1.0 else ""
            if closest["confidence"] >= NEAR_MISS:
                size += self.ui.t("probe_lower_to").format(value=fmt.confidence(self.ui, closest["confidence"], places=2))
            self.lbl_result.configure(text=self.ui.t("probe_false_closest").format(
                confidence=fmt.confidence(self.ui, closest["confidence"], threshold), size=size,
                at=f"({closest['x'] + closest['width'] // 2}, {closest['y'] + closest['height'] // 2})"))
            return
        if detail.get("confidence_measured", True) is False:
            # no confidence was measured: say so, rather than show the reader's placeholder as a number
            key = "probe_true_unmeasured" if result.satisfied else "probe_false_unmeasured"
            self.lbl_result.configure(text=self.ui.t(key).format(at=at))
            return
        self.lbl_result.configure(text=self.ui.t("probe_true" if result.satisfied else "probe_false").format(
            confidence=fmt.confidence(self.ui, result.confidence, threshold), at=at))


# ---- branches and recorded macros ---------------------------------------------------------------------------
class MacroChooser:
    """A recorded (v1) macro from the library, or -- when editing -- the one the step already holds."""

    def __init__(self, dialog: _Dialog, parent, current) -> None:
        self.ui, self.c, self.current = dialog.ui, dialog.ui.controller, current
        t = self.ui.t
        self._options: dict[str, str | None] = {}
        if current is not None:
            self._options[t("keep_macro").format(name=current.name or t("unnamed"),
                                                 actions=fmt.action_count(self.ui, len(current.actions)))] = None
        for info in self.c.recorded_macros():
            label = f"{info.name}  ·  {info.folder}" if info.folder else info.name
            while label in self._options:
                label += " "
            self._options[label] = info.key
        self.var = tk.StringVar(value=next(iter(self._options), ""))
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.box = None
        if not self._options:
            ttk.Label(self.frame, text=t("no_recorded_macros"), style="Muted.TLabel", wraplength=280,
                      justify="left").pack(anchor="w")
        else:
            self.box = ttk.Combobox(self.frame, textvariable=self.var, values=list(self._options), state="readonly",
                                    width=32)
            self.box.pack(anchor="w")

    def macro(self):
        if not self._options:
            return None
        key = self._options.get(self.var.get())
        return self.current if key is None else self.c.load_recorded(key)


class TapForm:
    """A click on the match: tap, double tap or secondary tap, and an offset from the middle of what was found."""

    def __init__(self, dialog: _Dialog, parent, tap: TapMatch | None) -> None:
        self.dialog, self.ui, self.c = dialog, dialog.ui, dialog.ui.controller
        t = self.ui.t
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self._ops = {t(TAP_LABELS[op]): op for op in fe.TAP_OPS}
        op = tap.op if tap is not None else ActionOp.TAP
        offset = tap.offset if tap is not None else (0, 0)
        self.op_var = tk.StringVar(value=t(TAP_LABELS[op]))
        self.dx, self.dy = tk.StringVar(value=str(offset[0])), tk.StringVar(value=str(offset[1]))
        self.op_box = ttk.Combobox(self.frame, textvariable=self.op_var, values=list(self._ops), state="readonly",
                                   width=18)
        self.op_box.grid(row=0, column=0, columnspan=4, sticky="w")
        self.op_box.bind("<<ComboboxSelected>>", lambda _e: self._note())
        for col, (key, var) in enumerate((("field_offset_x", self.dx), ("field_offset_y", self.dy))):
            ttk.Label(self.frame, text=t(key), style="FieldLabel.TLabel").grid(row=1, column=col * 2, sticky="w",
                                                                              padx=(0 if col == 0 else 10, 4),
                                                                              pady=(6, 0))
            ttk.Entry(self.frame, textvariable=var, width=6).grid(row=1, column=col * 2 + 1, sticky="w",
                                                                  pady=(6, 0))
        self.lbl_note = ttk.Label(self.frame, text="", style="Muted.TLabel", wraplength=300, justify="left")
        self.lbl_note.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._note()

    def _note(self) -> None:
        """Said, not hidden: a tap the chosen target does not declare is refused when the run starts there."""
        op = self._ops.get(self.op_var.get(), ActionOp.TAP)
        target, entry = self.c.target, self.c.selected
        needed = REQUIRED_CAPABILITY.get(op)
        if self.c.target_ready and needed is not None and not target.has_capability(needed):
            name = (entry.title or entry.process) if entry is not None else target.target_id
            self.lbl_note.configure(text=self.ui.t("tap_unsupported_here").format(name=name),
                                    foreground=th.ACTIVE.warning)
        else:
            self.lbl_note.configure(text="")

    def tap(self, at=None) -> TapMatch:
        made = fe.build_branch(fe.TAP, op=self._ops.get(self.op_var.get(), ActionOp.TAP), dx=self.dx.get(),
                               dy=self.dy.get())
        return replace(made, at=at)


class StartForm:
    """The Start block: how long a run waits before its first step -- in seconds, minutes or hours, at most 24 hours
    (a wait like any other: bounded). A dry run does not wait."""

    def __init__(self, dialog, parent, delay_s: float, adapt: bool = True, size: tuple | None = None) -> None:
        self.dialog, self.ui = dialog, dialog.ui
        t = self.ui.t
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self._units = {t("unit_word_seconds"): 1, t("unit_word_minutes"): 60, t("unit_word_hours"): 3600}
        # shown in the largest unit it is a whole number of
        unit = next((k for k, size in (("unit_word_hours", 3600), ("unit_word_minutes", 60))
                     if delay_s >= size and delay_s % size == 0), "unit_word_seconds")
        value = delay_s / self._units[t(unit)]
        self.value = tk.StringVar(value=fmt.number(self.ui, value, 2))
        self.unit = tk.StringVar(value=t(unit))
        ttk.Label(self.frame, text=t("field_start_delay"), style="FieldLabel.TLabel").grid(row=0, column=0,
                                                                                          sticky="w", padx=(0, 8))
        self.entry = ttk.Entry(self.frame, textvariable=self.value, width=7)
        self.entry.grid(row=0, column=1, sticky="w")
        ttk.Combobox(self.frame, textvariable=self.unit, values=list(self._units), state="readonly", width=9).grid(
            row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Label(self.frame, text=t("start_delay_hint").format(hours=round(MAX_START_DELAY_S / 3600),
                                                                 notice=START_NOTICE_S),
                  style="Muted.TLabel", wraplength=320, justify="left").grid(row=1, column=0, columnspan=3,
                                                                             sticky="w", pady=(6, 0))
        # "Adapt to the size": the script's points and areas follow a change of the target's size (same proportions)
        self.adapt = tk.BooleanVar(value=adapt)
        ttk.Checkbutton(self.frame, text=t("adapt_size"), variable=self.adapt, style="Choice.TCheckbutton").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        where = t("adapt_size_known").format(w=size[0], h=size[1]) if size else t("adapt_size_unknown")
        ttk.Label(self.frame, text=where, style="Muted.TLabel", wraplength=320, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def seconds(self) -> float:
        """The delay in seconds; EditError when what is typed is not a number."""
        value = fe.parse_float(self.value.get() or "0", "field_start_delay")
        return value * self._units.get(self.unit.get(), 1)


# ---- the step -------------------------------------------------------------------------------------------------
PRESET_TAP = "tap"        # a new IF that clicks where its condition finds the thing: "click on what is found"


class StepForm:
    """The fields of one block -- a WAIT UNTIL, an IF, a recorded macro or a click on the match -- built into
    `parent`.

    Hosted by the editor's properties panel (`stacked`: one narrow column) or by `StepDialog` (side by side).
    `build()` returns the validated block, or raises `EditError` in the backend's own words; nothing here decides
    what is valid. What the form does not show is kept from the block being changed: an IF's branches, where a
    block is drawn. `preset` only pre-fills a NEW block: `PRESET_TAP` starts an IF whose THEN begins with a click on
    the match."""

    def __init__(self, host, parent, kind: str, step=None, *, stacked: bool = False, preset: str = "") -> None:
        self.host, self.ui, self.kind, self.step, self.preset = host, host.ui, kind, step, preset
        ui, t = host.ui, host.ui.t
        self.frame = ttk.Frame(parent, style="Plain.TFrame")
        self.frame.columnconfigure(0, weight=1)
        row = 0
        if kind in (fe.WAIT, fe.IF):
            ttk.Label(self.frame, text=t("section_condition"), style="SectionTitle.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            self.condition = ConditionEditor(host, self.frame, step.condition if step is not None else None,
                                             columns=1 if stacked else 2)
            self.condition.frame.grid(row=row + 1, column=0, sticky="new" if stacked else "nw")
            row += 2
        if kind == fe.WAIT:
            timing = ttk.Frame(self.frame, style="Plain.TFrame")
            timing.grid(row=row, column=0, sticky="w", pady=(14, 0))
            self.timeout = tk.StringVar(value=fmt.number(ui, step.timeout_s if step is not None else 20, 3))
            self.interval = tk.StringVar(value=fmt.number(ui, step.interval_s if step is not None else 0.5, 3))
            if stacked:                           # one under the other, aligned with the condition's fields
                timing.columnconfigure(0, minsize=LABEL_WIDTH)
            for i, (key, var) in enumerate((("field_timeout", self.timeout), ("field_interval", self.interval))):
                r, col = (i, 0) if stacked else (0, i)
                ttk.Label(timing, text=t(key), style="FieldLabel.TLabel").grid(
                    row=r, column=col * 2, sticky="w", padx=(0 if col == 0 else 14, 6), pady=(0, 4 if stacked else 0))
                entry = ttk.Entry(timing, textvariable=var, width=7)
                entry.grid(row=r, column=col * 2 + 1, sticky="w", pady=(0, 4 if stacked else 0))
                if i == 0:
                    self.timeout_entry = entry           # a click on a block's time limit puts the cursor here
            ttk.Label(timing, text=t("timing_hint"), style="Muted.TLabel", wraplength=330 if stacked else 420,
                      justify="left").grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
            row += 1
        if kind == fe.IF:
            ttk.Label(self.frame, text=t("if_branches_hint"), style="Muted.TLabel", wraplength=330 if stacked else 420,
                      justify="left").grid(row=row, column=0, sticky="w", pady=(14, 0))
            row += 1
        if kind == fe.RUN:
            ttk.Label(self.frame, text=t("section_recorded_macro"), style="SectionTitle.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            self.chooser = MacroChooser(host, self.frame, step.macro if step is not None else None)
            self.chooser.frame.grid(row=row + 1, column=0, sticky="w")
            row += 2
        if kind == fe.TAP:
            ttk.Label(self.frame, text=t("section_tap"), style="SectionTitle.TLabel").grid(
                row=row, column=0, sticky="w", pady=(0, 6))
            self.tap = TapForm(host, self.frame, step)
            self.tap.frame.grid(row=row + 1, column=0, sticky="w")
            ttk.Label(self.frame, text=t("tap_first_hint"), style="Muted.TLabel", wraplength=330 if stacked else 420,
                      justify="left").grid(row=row + 2, column=0, sticky="w", pady=(10, 0))
            row += 3

    @property
    def picked(self) -> dict:
        part = getattr(self, "condition", None)
        return dict(getattr(part, "picked", {}))

    def build(self):
        at = self.step.at if self.step is not None else None
        if self.kind == fe.WAIT:
            return replace(fe.build_wait(self.condition.condition(), self.timeout.get(), self.interval.get()), at=at)
        if self.kind == fe.IF:
            if isinstance(self.step, Decision):         # the branches are blocks of their own: kept as they are
                then, otherwise = self.step.then, self.step.otherwise
            else:
                then, otherwise = ((TapMatch(),) if self.preset == PRESET_TAP else ()), ()
            return replace(fe.build_decision(self.condition.condition(), then, otherwise), at=at)
        if self.kind == fe.TAP:
            return self.tap.tap(at)
        return replace(fe.build_run(self.chooser.macro()), at=at)


class StepDialog(_Dialog):
    """Add or change one step in a dialog. Returns the new step (a validated domain object), or None when cancelled.
    The editor itself edits steps in its properties panel with the same `StepForm`."""

    def __init__(self, ui, kind: str, step=None, *, preset: str = "") -> None:
        super().__init__(ui, f"dlg_step_{kind}", width=720)
        self.kind = kind
        self.content.columnconfigure(0, weight=1)
        self.form = StepForm(self, self.content, kind, step, preset=preset)
        self.form.frame.grid(row=0, column=0, sticky="nsew")
        for name in ("condition", "timeout", "interval", "chooser", "tap"):
            if hasattr(self.form, name):
                setattr(self, name, getattr(self.form, name))
        self.actions("btn_apply")

    def _build(self):
        return self.form.build()

    def confirm(self) -> None:
        try:
            step = self._build()
        except EditError as exc:
            self.show_error(error_text(self.ui, exc))
            return
        self.result = step
        self.top.destroy()


# ---- the screen picker ----------------------------------------------------------------------------------------
class ScreenPicker(_Dialog):
    """One screenshot of the chosen target, shown scaled; drag a rectangle (region, template) or click a point.

    Returns {"roi": Roi} / {"point": (x, y)} / {"roi": Roi, "limit": bool}, all in the screenshot's own pixels,
    or None when cancelled."""

    def __init__(self, ui, screen, mode: str, region=None) -> None:
        super().__init__(ui, {"region": "dlg_pick_region", "point": "dlg_pick_point", "color": "dlg_pick_color",
                              "template": "dlg_capture_template"}[mode], width=900)
        self.screen, self.mode = screen, mode
        self.pointlike = mode in ("point", "color")       # a click picks one pixel (the eyedropper: its colour)
        self.roi, self.point = region, None
        self._start = None
        self.image, self.factor = photo(ui, screen.png, PICKER_MAX)
        shown_w, shown_h = geo.shown_size(screen.width, screen.height, self.factor)
        ttk.Label(self.content, text=ui.t(f"pick_hint_{mode}"), style="Muted.TLabel", wraplength=860,
                  justify="left").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.canvas = tk.Canvas(self.content, width=shown_w, height=shown_h, highlightthickness=1,
                                highlightbackground=th.ACTIVE.border, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="w")
        self.canvas.create_image(0, 0, image=self.image, anchor="nw")
        self._mark = None
        self.lbl_info = ttk.Label(self.content, text=ui.t("pick_scale").format(
            w=screen.width, h=screen.height, factor=self.factor), style="Muted.TLabel")
        self.lbl_info.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.limit = tk.BooleanVar(value=False)
        self.swatch = None
        if self.pointlike:
            self.swatch = tk.Label(self.content, width=4, borderwidth=1, relief="solid", background=SWATCH_EMPTY)
            self.swatch.grid(row=3, column=0, sticky="w", pady=(6, 0), ipady=3)
            self.canvas.bind("<Motion>", self._hover)
        if mode == "template":
            extra = ttk.Frame(self.content, style="Plain.TFrame")
            extra.grid(row=3, column=0, sticky="w", pady=(6, 0))
            self._preview = ttk.Label(extra, style="Muted.TLabel")
            self._preview.pack(side="left")
            ttk.Checkbutton(extra, text=ui.t("template_limit_search"), variable=self.limit,
                            style="Choice.TCheckbutton").pack(side="left", padx=(12, 0))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        if region is not None and not self.pointlike:
            self._draw_rect(*geo.to_shown_rect(region, self.factor))
        self.actions("btn_use_selection")

    def _draw_rect(self, x0, y0, x1, y1) -> None:
        if self._mark is not None:
            self.canvas.delete(self._mark)
        self._mark = self.canvas.create_rectangle(x0, y0, x1, y1, outline=th.ACTIVE.error, width=2)

    def _colour_at(self, event) -> tuple[tuple[int, int], tuple[int, int, int]]:
        s = self.screen
        point = geo.to_image_point(event.x, event.y, self.factor, s.width, s.height)
        return point, s.frame.bitmap.pixel(*point)

    def _show_colour(self, point, colour) -> None:
        self.swatch.configure(background="#%02x%02x%02x" % tuple(colour))
        self.lbl_info.configure(text=self.ui.t("pick_point_info").format(x=point[0], y=point[1],
                                                                          color=fe.format_color(colour)))

    def _hover(self, event) -> None:
        """The colour under the pointer, while it moves: what a click would take."""
        if self.point is None:                             # a picked pixel stays shown until another click
            self._show_colour(*self._colour_at(event))

    def _press(self, event) -> None:
        self._start = (event.x, event.y)
        if self.pointlike:
            self._release(event)

    def _drag(self, event) -> None:
        if self._start is not None and not self.pointlike:
            self._draw_rect(self._start[0], self._start[1], event.x, event.y)

    def _release(self, event) -> None:
        s = self.screen
        if self.pointlike:
            self.point, colour = self._colour_at(event)
            if self._mark is not None:
                self.canvas.delete(self._mark)
            sx, sy = event.x, event.y
            self._mark = self.canvas.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, outline=th.ACTIVE.error, width=2)
            self._show_colour(self.point, colour)
            return
        if self._start is None:
            return
        roi = geo.to_image_roi(self._start[0], self._start[1], event.x, event.y, self.factor, s.width, s.height)
        self._start = None
        if roi is None:
            return
        self.roi = roi
        self._draw_rect(*geo.to_shown_rect(roi, self.factor))
        self.lbl_info.configure(text=fmt.region_text(self.ui, roi))
        if self.mode == "template":
            crop = s.frame.bitmap.crop(roi.x, roi.y, roi.width, roi.height)
            self._thumb, _ = photo(self.ui, encode_png(crop), THUMB_MAX)
            self._preview.configure(image=self._thumb)

    def confirm(self) -> None:
        if self.pointlike and self.point is None or not self.pointlike and self.roi is None:
            self.show_error(self.ui.t("pick_nothing"))
            return
        self.result = ({"point": self.point} if self.pointlike
                       else {"roi": self.roi, "limit": bool(self.limit.get())})
        self.top.destroy()
