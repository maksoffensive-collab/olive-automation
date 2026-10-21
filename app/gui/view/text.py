"""Text on screen: what the chosen target's screen says, and WHERE -- read on request.

One fresh screenshot of ONE explicitly chosen, verified target, read by the text reader the composition root
configured, in the language the person chose -- or in the reader's own choice, which is then said. The screen only
looks: no control here clicks, types or sends anything, and the emergency stop still ends a reading under way.

What it shows is only what the reader reported. The screenshot that was read is shown on the left with a box around
every piece of text the reader found -- exactly its box, in that screenshot's own pixels, scaled for display by a
whole factor (`app.gui.region`) -- and the same pieces are listed on the right with their position and size. Choosing
a row highlights its box, and clicking a box chooses its row. Coordinates are the screenshot's pixels: (0, 0) is its
top-left corner, which is also the space the script's conditions and clicks use; a target that cannot be clicked
(the whole desktop) says so. There is no confidence figure (the reader may measure none, and then the screen says
so) and no sample text. Why reading is not possible -- no reader, no language, no target, a target that cannot be
captured, a failure -- is always said in words, never left as a disabled button.
"""
from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk

from app.gui import region as geo
from app.gui import theme as th
from app.gui.controller import (LANGS_FAILED, LANGS_IDLE, LANGS_LOADING, LANGS_READY, LANGS_UNKNOWN,
                                READ_CANCELLED, READ_DONE, READ_FAILED, READING)
from app.gui.view import dialogs
from app.gui.view import widgets as w
from app.providers.target import Capability

TEXT_HEIGHT = 3           # lines the "all text" area asks for at least
RESULTS_WIDTH = 340
RESULT_WRAP = RESULTS_WIDTH - 48              # the card's padding and edge: a sentence wraps inside it


def _enable(widget, on: bool) -> None:
    widget.state(["!disabled"] if on else ["disabled"])


class TextScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(28, 4, 28, 20))
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        self._shown: tuple | None = None       # what the result area holds, so it is rewritten only on change
        self._auto = ""                        # the "Automatic" choice, in the language it was last shown in
        self._photo_for = None                 # (request, png length) of the decoded screenshot
        self._photo_full: tk.PhotoImage | None = None
        self._photo: tk.PhotoImage | None = None
        self._map: tuple[int, int, int, int] | None = None     # (left, top, down, up) of the shown screenshot
        self.selected_block: int | None = None
        self._build()

    # ---- construction ------------------------------------------------------------------------------
    def _build(self) -> None:
        card = w.card(self.ui, self.frame, padding=(18, 14))
        card.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        card.columnconfigure(1, weight=1)
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), "text_target").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 10))
        self.lbl_target = ttk.Label(card, text="", style="PanelStrong.TLabel")
        self.lbl_target.grid(row=0, column=1, sticky="w", pady=(0, 10))
        self.btn_target = self.ui.label(ttk.Button(card, style="Card.TButton", command=self._choose_target),
                                        "btn_choose_target")
        self.btn_target.grid(row=0, column=2, sticky="e", pady=(0, 10))

        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), "text_language").grid(
            row=1, column=0, sticky="w", padx=(0, 12))
        languages = ttk.Frame(card, style="Surface.TFrame")
        languages.grid(row=1, column=1, columnspan=2, sticky="w")
        self.language_var = tk.StringVar(master=self.frame)   # this window's interpreter, never another one
        self.box_language = ttk.Combobox(languages, textvariable=self.language_var, width=36)
        self.box_language.pack(side="left")
        self.btn_languages = self.ui.label(ttk.Button(languages, style="Card.TButton", command=self._load_languages),
                                           "btn_text_languages")
        self.btn_languages.pack(side="left", padx=(8, 0))

        actions = ttk.Frame(card, style="Surface.TFrame")
        actions.grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.btn_read = self.ui.label(ttk.Button(actions, style="Card.Primary.TButton", compound="left",
                                                 command=self._read), "btn_read_text")
        paint = lambda: self.btn_read.configure(image=self.ui.button_icon(   # noqa: E731
            "camera", th.ACTIVE.text_on_primary, pad=8))
        self.ui.on_theme(paint)
        paint()
        self.btn_read.pack(side="left")
        self.btn_cancel = self.ui.label(ttk.Button(actions, style="Card.TButton", command=self._cancel),
                                        "btn_cancel_read")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.lbl_note = ttk.Label(card, text="", style="PanelMuted.TLabel", justify="left")
        self.lbl_note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        w.wrap_to(card, self.lbl_note, reserve=40, minimum=420)

        work = ttk.Frame(self.frame, style="Plain.TFrame")
        work.grid(row=1, column=0, sticky="nsew")
        work.columnconfigure(0, weight=1)
        work.rowconfigure(0, weight=1)

        preview = w.card(self.ui, work, padding=(16, 14))
        preview.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(1, weight=1)
        head = ttk.Frame(preview, style="Surface.TFrame")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(1, weight=1)
        w.section(self.ui, head, "text_preview_header").grid(row=0, column=0, sticky="w")
        self.lbl_preview_info = ttk.Label(head, text="", style="PanelCaption.TLabel")
        self.lbl_preview_info.grid(row=0, column=1, sticky="e")
        self.preview = tk.Canvas(preview, width=340, height=200, highlightthickness=1, borderwidth=0)
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda _e: self._draw_preview(force=True))
        self.preview.bind("<Button-1>", self._click_preview)
        self.lbl_coords = ttk.Label(preview, text="", style="PanelCaption.TLabel", justify="left")
        self.lbl_coords.grid(row=2, column=0, sticky="w", pady=(8, 0))
        w.wrap_to(preview, self.lbl_coords, reserve=36, minimum=300)

        result = w.card(self.ui, work, padding=(16, 14), )
        result.configure(width=RESULTS_WIDTH)
        result.grid(row=0, column=1, sticky="nsew")
        result.grid_propagate(False)
        result.columnconfigure(0, weight=1)
        result.rowconfigure(2, weight=1)
        head = ttk.Frame(result, style="Surface.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(0, weight=1)
        w.section(self.ui, head, "text_result_header").grid(row=0, column=0, sticky="w")
        self.btn_copy = self.ui.label(ttk.Button(head, style="Card.TButton", command=self._copy), "btn_copy_text")
        self.btn_copy.grid(row=0, column=1, sticky="e")
        self.lbl_result = ttk.Label(result, text="", style="PanelMuted.TLabel", wraplength=RESULT_WRAP,
                                    justify="left")
        self.lbl_result.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 8))

        holder = ttk.Frame(result, style="Surface.TFrame")
        holder.grid(row=2, column=0, columnspan=2, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.blocks = ttk.Treeview(holder, columns=("n", "text", "where"), show="headings", selectmode="browse",
                                   height=5)
        self.blocks.column("n", width=34, stretch=False, anchor="w")
        self.blocks.column("text", width=160, stretch=True, anchor="w")
        self.blocks.column("where", width=110, stretch=False, anchor="w")
        self.blocks.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.blocks.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.blocks.configure(yscrollcommand=w.autohide(bar))
        self.blocks.bind("<<TreeviewSelect>>", lambda _e: self._on_block_chosen())
        self.lbl_block = ttk.Label(result, text="", style="Panel.TLabel", wraplength=RESULT_WRAP, justify="left")
        self.lbl_block.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        w.section(self.ui, result, "text_all_header").grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 4))
        # Read-only, but selectable: the person can select and copy any part of what was read (Ctrl+C), or all
        # of it with the button.
        self.text = tk.Text(result, font=th.TYPE_BODY, wrap="word", state="disabled", relief="flat",
                            height=TEXT_HEIGHT, highlightthickness=1, padx=8, pady=6)
        self.text.grid(row=5, column=0, columnspan=2, sticky="ew")
        self.ui.on_theme(self._paint)
        self._paint()

    def _paint(self) -> None:
        c = th.ACTIVE
        self.text.configure(background=c.surface_alt, foreground=c.text, highlightbackground=c.border,
                            highlightcolor=c.primary, selectbackground=c.primary_soft, selectforeground=c.text,
                            inactiveselectbackground=c.primary_soft, insertbackground=c.text)
        self.preview.configure(background=c.surface_alt, highlightbackground=c.border)
        self._draw_preview(force=True)

    def relabel(self) -> None:
        """The column headings follow the language (called by the shell on a language change)."""
        for column, key in (("n", "col_number"), ("text", "col_text"), ("where", "col_where")):
            self.blocks.heading(column, text=self.ui.t(key), anchor="w")

    # ---- actions -------------------------------------------------------------------------------------
    def language(self) -> str:
        """The language to ask for: "" for the reader's own choice, otherwise exactly what is chosen or typed --
        never replaced by another one. Whether the reader can read it is the controller's (and the reader's)
        decision, and a refusal is shown, not guessed around."""
        value = self.language_var.get().strip()
        return "" if value in ("", self._auto) else value

    def _choose_target(self) -> bool:
        """Exactly ONE target, chosen explicitly and verified; nothing chosen means nothing is read."""
        chosen = dialogs.ChooseTargets(self.ui, self.c.entries, (), single=True).show()
        ok = isinstance(chosen, tuple) and len(chosen) == 1 and self.c.select(chosen[0])
        self.ui.render()
        return ok

    def _read(self) -> None:
        if not self.c.target_ready and not self._choose_target():
            return
        job = self.c.begin_read_text(self.language())
        if job is not None:
            self.selected_block = None
            self.ui.run_job(job, lambda outcome: self._read_done(outcome, job))
        self.ui.render()

    def _read_done(self, outcome, job) -> None:
        self.c.finish_read_text(outcome, job)
        self.ui.render()

    def _cancel(self) -> None:
        self.c.cancel_read_text()
        self.ui.render()

    def _load_languages(self) -> None:
        job = self.c.begin_text_languages()
        if job is not None:
            self.ui.run_job(job, self._languages_done)

    def _languages_done(self, outcome) -> None:
        self.c.finish_text_languages(outcome)
        self.ui.render()

    def _copy(self) -> None:
        """The selected part of what was read, or all of it."""
        reading = self.c.reading
        if reading is None or reading.state != READ_DONE or not reading.lines:
            return
        try:
            text = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""                                   # nothing selected: copy everything
        self.ui.copy_text(text or reading.text)
        self.c.note("ok", "text", self.ui.t("msg_text_copied"))
        self.ui.render()

    def _done_blocks(self) -> tuple:
        reading = self.c.reading
        return reading.blocks if reading is not None and reading.state == READ_DONE else ()

    def _on_block_chosen(self) -> None:
        selection = self.blocks.selection()
        index = int(selection[0]) if selection else None
        if index != self.selected_block:
            self.selected_block = index
            self._draw_preview(force=True)
            self._render_block()

    def choose_block(self, index: int | None) -> None:
        """Choose one recognised piece of text: its row, and its box on the screenshot."""
        if index is None:
            self.blocks.selection_remove(*self.blocks.selection())
        elif self.blocks.exists(str(index)):
            self.blocks.selection_set(str(index))
            self.blocks.see(str(index))
        self.selected_block = index
        self._draw_preview(force=True)
        self._render_block()

    def _click_preview(self, event) -> None:
        """A click on the screenshot chooses the piece of text under it (the smallest box, if boxes overlap)."""
        reading = self.c.reading
        if self._map is None or reading is None or not reading.width:
            return
        left, top, down, up = self._map
        x, y = geo.to_image_point_scaled(event.x - left, event.y - top, down, up, reading.width, reading.height)
        hits = [(b.width * b.height, i) for i, b in enumerate(self._done_blocks())
                if b.x <= x < b.x + b.width and b.y <= y < b.y + b.height]
        self.choose_block(min(hits)[1] if hits else None)

    # ---- rendering ---------------------------------------------------------------------------------------
    def render(self) -> None:
        c, t = self.c, self.ui.t
        if c.text_languages_state == LANGS_IDLE:
            self._load_languages()                      # asked the first time the screen is shown, not before
        reading_now = c.reading_text
        if c.target_ready and c.selected is not None:
            self.lbl_target.configure(text=c.selected.title or c.selected.process or c.target.target_id)
        else:
            self.lbl_target.configure(text=t("text_target_none"))

        auto = t("text_lang_auto")
        if self.language_var.get() in ("", self._auto):
            self.language_var.set(auto)                 # follows the interface language
        self._auto = auto
        listed = c.text_languages_state == LANGS_READY and bool(c.text_languages)
        self.box_language.configure(values=[auto, *c.text_languages])
        if c.ocr is None or reading_now:
            self.box_language.state(["disabled"])
        else:
            # A known list is chosen from; without one, a tag such as "en" or "ru" may be typed.
            self.box_language.state(["!disabled", "readonly" if listed else "!readonly"])
        _enable(self.btn_languages, c.ocr is not None and c.text_languages_state != LANGS_LOADING
                and not reading_now and not c.emergency_active)
        _enable(self.btn_target, not reading_now and not c.busy)
        # A chosen target that declares no capture cannot be read: the button is off and `note()` says why. With
        # no target chosen, Read stays on -- it asks for one first.
        uncapturable = c.target_ready and c.target is not None and not c.target.has_capability(Capability.SCREENSHOT)
        _enable(self.btn_read, c.ocr is not None and not reading_now and not c.busy and not c.emergency_active
                and not uncapturable)
        _enable(self.btn_cancel, reading_now)
        if reading_now and not self.progress.winfo_manager():
            self.progress.pack(side="left", padx=(12, 0))
            self.progress.start(15)
        elif not reading_now and self.progress.winfo_manager():
            self.progress.stop()
            self.progress.pack_forget()
        self.lbl_note.configure(text=self.note())
        self.relabel()
        self._render_result()
        self._draw_preview()

    def note(self) -> str:
        """Why reading is or is not possible right now, in words: the reader and its languages first, then the
        target."""
        c, t = self.c, self.ui.t
        if c.ocr is None:
            return t("text_note_no_reader")
        parts = []
        if c.emergency_active:
            parts.append(t("text_note_stopped"))
        elif c.busy:
            parts.append(t("text_note_busy"))
        state = c.text_languages_state
        if state == LANGS_LOADING:
            parts.append(t("text_note_languages_loading"))
        elif state == LANGS_READY and not c.text_languages:
            parts.append(t("text_note_no_languages"))
        elif state == LANGS_FAILED:
            parts.append(t("text_note_languages_failed"))
        elif state == LANGS_UNKNOWN:
            parts.append(t("text_note_languages_unknown"))
        if not c.target_ready:
            parts.append(t("text_note_no_target"))
        elif c.target is not None and not c.target.has_capability(Capability.SCREENSHOT):
            parts.append(t("text_note_no_screenshot"))
        return "\n".join(parts) or t("text_note_ready")

    def result_text(self) -> str:
        reading, t = self.c.reading, self.ui.t
        if reading is None:
            return t("text_result_empty")
        name = reading.target_title
        if reading.state == READING:
            return t("text_result_reading").format(name=name)
        if reading.state == READ_CANCELLED:
            return t("text_result_cancelled")
        if reading.state == READ_FAILED:
            return t(f"text_fail_{reading.reason}").format(name=name,
                                                          language=reading.language or t("text_lang_auto"))
        if not reading.lines:
            return t("text_result_none").format(name=name)
        text = t("text_result_meta").format(count=len(reading.lines), name=name,
                                            language=reading.read_in or t("text_lang_unreported"))
        return text if reading.confidence_measured else f"{text} · {t('text_confidence_unmeasured')}"

    def _render_result(self) -> None:
        reading = self.c.reading
        lines = reading.lines if reading is not None and reading.state == READ_DONE else ()
        blocks = self._done_blocks()
        shown = (reading.request if reading is not None else None, lines, blocks)
        if shown != self._shown:
            self._shown = shown
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", "\n".join(lines))
            self.text.configure(state="disabled")
            self.blocks.delete(*self.blocks.get_children())
            for index, block in enumerate(blocks):
                self.blocks.insert("", "end", iid=str(index),
                                   values=(index + 1, block.text, self.ui.t("text_where").format(x=block.x, y=block.y)))
            self.selected_block = None
            self._render_block()
        self.lbl_result.configure(text=self.result_text())
        _enable(self.btn_copy, bool(lines))
        reading_target = self.c.target
        coords = ""
        if blocks or (reading is not None and reading.width):
            coords = self.ui.t("text_coords_note")
            if reading_target is not None and not reading_target.has_capability(Capability.TAP):
                coords += " " + self.ui.t("text_coords_readonly")
        self.lbl_coords.configure(text=coords)

    def _render_block(self) -> None:
        blocks = self._done_blocks()
        index = self.selected_block
        if index is None or not 0 <= index < len(blocks):
            self.lbl_block.configure(text=self.ui.t("text_block_hint") if blocks else "")
            return
        block = blocks[index]
        cx, cy = block.center
        self.lbl_block.configure(text=self.ui.t("text_block_detail").format(
            text=block.text, x=block.x, y=block.y, w=block.width, h=block.height, cx=cx, cy=cy))

    def _draw_preview(self, force: bool = False) -> None:
        canvas, reading, t, c = self.preview, self.c.reading, self.ui.t, th.ACTIVE
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width <= 1 or height <= 1:                   # not laid out yet: its asked-for size, not a 1-pixel view
            width, height = int(canvas.cget("width")), int(canvas.cget("height"))
        key = (id(reading), self.selected_block, width, height, c.id, self.ui.t.language)
        if not force and key == getattr(self, "_preview_key", None):
            return
        self._preview_key = key
        canvas.delete("all")
        self._map = None
        if reading is not None and reading.state == READ_DONE and reading.png:
            self._draw_screenshot(reading, width, height)
            return
        self.lbl_preview_info.configure(text="")
        if reading is None:
            message = t("preview_text_idle")
        elif reading.state == READING:
            message = t("preview_text_reading")
        elif reading.state == READ_DONE:
            message = t("preview_text_no_image")
        else:
            message = self.result_text()
        canvas.create_text(width // 2, height // 2, text=message, fill=c.text_muted, font=th.TYPE_BODY,
                           justify="center", width=max(width - 40, 60))

    def _draw_screenshot(self, reading, width: int, height: int) -> None:
        canvas, c = self.preview, th.ACTIVE
        wanted = (reading.request, len(reading.png))
        if self._photo_for != wanted:
            try:
                self._photo_full = tk.PhotoImage(master=canvas, data=base64.b64encode(reading.png))
            except tk.TclError:
                self._photo_full = None
            self._photo_for, self._photo = wanted, None
            self._scaled_for = None
        if self._photo_full is None:
            canvas.create_text(width // 2, height // 2, text=self.ui.t("preview_text_no_image"), fill=c.text_muted)
            return
        iw, ih = reading.width or self._photo_full.width(), reading.height or self._photo_full.height()
        down, up = geo.fit_factors(iw, ih, width - 16, height - 16)
        if getattr(self, "_scaled_for", None) != (wanted, down, up):
            image = self._photo_full
            self._photo = image.subsample(down) if down > 1 else (image.zoom(up) if up > 1 else image)
            self._scaled_for = (wanted, down, up)
        shown_w, shown_h = self._photo.width(), self._photo.height()
        left, top = (width - shown_w) // 2, (height - shown_h) // 2           # letterboxed, never stretched
        self._map = (left, top, down, up)
        canvas.create_image(left, top, image=self._photo, anchor="nw")
        canvas.create_rectangle(left - 1, top - 1, left + shown_w, top + shown_h, outline=c.border_strong)
        for index, block in enumerate(reading.blocks):
            x0, y0, x1, y1 = geo.shown_box(block.x, block.y, block.width, block.height, down, up)
            x0, y0, x1, y1 = x0 + left, y0 + top, x1 + left, y1 + top
            chosen = index == self.selected_block
            if chosen:
                canvas.create_rectangle(x0, y0, x1, y1, fill=c.primary, stipple="gray25", outline="")
            canvas.create_rectangle(x0, y0, x1, y1, outline=c.primary if chosen else c.info,
                                    width=3 if chosen else 1.5)
            tag_w = 8 + 7 * len(str(index + 1))
            canvas.create_rectangle(x0, y0 - 15, x0 + tag_w, y0, fill=c.primary if chosen else c.info, outline="")
            canvas.create_text(x0 + tag_w / 2, y0 - 7.5, text=str(index + 1), fill=c.surface,
                               font=(th.FONT_SEMIBOLD, 8))
        scale = f"1:{down}" if down > 1 else (f"×{up}" if up > 1 else "1:1")
        self.lbl_preview_info.configure(text=self.ui.t("text_preview_info").format(w=iw, h=ih, scale=scale))
