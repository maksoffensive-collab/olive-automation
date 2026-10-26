"""The library screens: recorded macros, and scripts -- which one is chosen, what it does, where it will run, Run.

The same screen class shows either part of the library (`kind`), because both are files in the one library and run
through the one batch; what differs is what a person creates there:

  Macros   "Record a macro" -- the recorder remembers clicks and keys in ONE chosen window or device
           (an empty hand-made macro is still possible, as a secondary link)
  Scripts  "New script" -- opens the script editor on an empty script; nothing is written until it has a step

Three columns, each answering one question and nothing else:
  left    what can I do here, and which ones am I looking at  (the create action; all / favourites / recent / folder)
  centre  which one do I want                                 (search, sort, and the list itself)
  right   what is this one and where does it run              (description, its steps, its targets, Run)

The right column is what makes the chain visible: choosing an item in the centre immediately changes the panel
that carries the Run button, so "this button runs THIS macro on THOSE targets" is readable without being told. A
row has no Run of its own -- a run starts only from that panel, under the targets it will use. Run is disabled
until both halves are answered, and its tooltip says which half is missing.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.gui import theme as th
from app.gui.controller import KIND_MACROS, KIND_SCRIPTS, RunState
from app.gui.macro_list import Scope, Sort
from app.gui.view import dialogs, fmt
from app.gui.view import widgets as w

PREVIEW_STEPS = 8
PREVIEW_CHARS = 40     # one preview line in the 330 px panel; the editor shows each step in full
RUN_LINES = 5          # how many target lines a running batch shows before "show all"
RECENT_RUNS = 3
SORTS = ((Sort.MODIFIED, "sort_modified"), (Sort.MODIFIED_OLD, "sort_modified_old"), (Sort.NAME, "sort_name"),
         (Sort.NAME_DESC, "sort_name_desc"), (Sort.LAST_RUN, "sort_last_run"), (Sort.ACTIONS, "sort_actions"))
SCOPE_KEY = {Scope.ALL: "scope_all", Scope.FAVORITES: "scope_favorites", Scope.RECENT: "scope_recent"}
SCOPE_KEY_SCRIPTS = {Scope.ALL: "scope_all_scripts", Scope.FAVORITES: "scope_favorites",
                     Scope.RECENT: "scope_recent"}
NAV_WIDTH = 196
DETAILS_WIDTH = 316


class MacrosScreen:
    def __init__(self, ui, parent: tk.Widget, *, kind: str = KIND_MACROS) -> None:
        self.ui, self.c, self.kind = ui, ui.controller, kind
        self.scripts = kind == KIND_SCRIPTS
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(24, 4, 24, 20))
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.nav_items: dict[str, dict] = {}
        self._row_widgets: dict[str, dict] = {}
        self._list_signature = None
        self._nav_signature = None
        self._show_all_targets = False
        self._preview: tuple | None = None           # (key, modified, language, text): one item, read once
        self.btn_record: ttk.Button | None = None
        self._build_nav()
        self._build_centre()
        self._build_details()
        # Rows are raw tk widgets built once per list change, so their text colour would otherwise still be the
        # PREVIOUS theme's after a switch: forget the signature and let the next render rebuild them.
        self.ui.on_theme(self._forget_rows)

    def _forget_rows(self) -> None:
        self._list_signature = None
        self._nav_signature = None
        self._show_all_targets = False

    def _key(self, macro_key: str, script_key: str) -> str:
        return script_key if self.scripts else macro_key

    # ---- left: what can I do here, and which ones am I looking at --------------------------------------
    def _build_nav(self) -> None:
        nav = ttk.Frame(self.frame, style="Plain.TFrame", width=NAV_WIDTH)
        nav.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        nav.grid_propagate(False)
        nav.rowconfigure(5, weight=1)
        nav.columnconfigure(0, weight=1)          # the column is a fixed width: keep children inside it

        create = w.card(self.ui, nav, padding=(14, 14, 14, 12))
        create.grid(row=0, column=0, sticky="ew")
        create.columnconfigure(0, weight=1)
        if self.scripts:
            button = self.ui.label(ttk.Button(create, style="Create.Primary.TButton", compound="left",
                                              command=self._new_script), "btn_new_script")
            self._paint_icon(button, "plus", lambda: th.ACTIVE.text_on_primary)
            button.grid(row=0, column=0, sticky="ew")
            self.ui.label(ttk.Label(create, style="PanelCaption.TLabel", wraplength=156, justify="left"),
                          "new_script_hint").grid(row=1, column=0, sticky="w", pady=(8, 0))
        else:
            self.btn_record = ttk.Button(create, style="Create.Primary.TButton", compound="left",
                                         command=self._toggle_recording)
            self.btn_record.grid(row=0, column=0, sticky="ew")
            self.ui.label(ttk.Label(create, style="PanelCaption.TLabel", wraplength=156, justify="left"),
                          "record_hint").grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.ui.label(ttk.Button(create, style="Link.TButton", command=self._new_macro),
                          "btn_new_empty_macro").grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.ui.on_theme(self._paint_record)

        self.ui.label(ttk.Label(nav, style="SectionTitle.TLabel"), "nav_view_header").grid(
            row=1, column=0, sticky="w", pady=(20, 6))
        self._scopes = ttk.Frame(nav, style="Plain.TFrame")
        self._scopes.grid(row=2, column=0, sticky="ew")
        keys = SCOPE_KEY_SCRIPTS if self.scripts else SCOPE_KEY
        for scope in (Scope.ALL, Scope.FAVORITES, Scope.RECENT):
            self._nav_item(self._scopes, scope.value, keys[scope], lambda s=scope: self._pick_scope(s))
        self.ui.on_theme(self._paint_nav)

        self.ui.label(ttk.Label(nav, style="SectionTitle.TLabel"), "nav_folders_header").grid(row=3, column=0,
                                                                                               sticky="w",
                                                                                               pady=(18, 6))
        holder, self._folders_box = w.scroll_area(self.ui, nav)
        holder.grid(row=5, column=0, sticky="nsew")
        self.ui.label(ttk.Button(nav, style="Link.TButton", command=self._new_folder),
                      "btn_new_folder").grid(row=6, column=0, sticky="w", pady=(8, 0))

    def _paint_icon(self, button: ttk.Button, glyph: str, colour) -> None:
        paint = lambda: button.configure(image=self.ui.button_icon(glyph, colour(), pad=8))    # noqa: E731
        self.ui.on_theme(paint)
        paint()

    def _paint_record(self) -> None:
        if self.btn_record is None:
            return
        recording = self.c.recording_active
        self.btn_record.configure(style="Create.Danger.TButton" if recording else "Create.Primary.TButton",
                                  image=self.ui.button_icon("stop" if recording else "record",
                                                            th.ACTIVE.text_on_primary, pad=8))

    def _nav_item(self, parent, name: str, key: str, command, *, label: str | None = None) -> None:
        """One place to look in: a row with an accent bar when chosen. A real keyboard stop (Tab, then Enter or
        Space), not only a mouse target."""
        row = tk.Frame(parent, background=th.ACTIVE.bg, cursor="hand2", takefocus=1, highlightthickness=1,
                       highlightbackground=th.ACTIVE.bg, highlightcolor=th.ACTIVE.focus)
        row.pack(fill="x", pady=1)
        bar = tk.Frame(row, width=3, background=th.ACTIVE.bg)
        bar.pack(side="left", fill="y")
        text = tk.Label(row, text=label if label is not None else self.ui.t(key), anchor="w",
                        font=th.TYPE_BODY, background=th.ACTIVE.bg, foreground=th.ACTIVE.text, padx=10, pady=7)
        text.pack(side="left", fill="x", expand=True)
        count = tk.Label(row, text="", font=th.TYPE_CAPTION, background=th.ACTIVE.bg,
                         foreground=th.ACTIVE.text_muted, padx=10)
        count.pack(side="right")
        for widget in (row, bar, text, count):
            widget.bind("<Button-1>", lambda _e, fn=command: fn())
        for sequence in ("<Return>", "<space>"):
            row.bind(sequence, lambda _e, fn=command: fn())
        if label is None:
            self.ui.register_text(text, key)
        self.nav_items[name] = {"row": row, "bar": bar, "text": text, "count": count, "command": command}

    def _paint_nav(self) -> None:
        flt = self.c.filter
        active = f"folder:{flt.folder}" if flt.scope is Scope.FOLDER else flt.scope.value
        c = th.ACTIVE
        for name, item in self.nav_items.items():
            on = name == active
            background = c.primary_soft if on else c.bg
            item["row"].configure(background=background, highlightbackground=background,
                                  highlightcolor=c.focus)
            item["bar"].configure(background=c.primary if on else background)
            item["text"].configure(background=background, font=th.TYPE_BODY_STRONG if on else th.TYPE_BODY,
                                   foreground=c.primary if on else c.text)
            item["count"].configure(background=background, foreground=c.text_muted)

    def _pick_scope(self, scope: Scope, folder: str = "") -> None:
        self.c.set_scope(scope, folder)
        self.ui.render()

    # ---- centre: search, sort, the list ------------------------------------------------------------
    def _build_centre(self) -> None:
        centre = ttk.Frame(self.frame, style="Plain.TFrame")
        centre.grid(row=0, column=1, sticky="nsew")
        centre.columnconfigure(0, weight=1)
        centre.rowconfigure(4, weight=1)

        header = ttk.Frame(centre, style="Plain.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        titles = ttk.Frame(header, style="Plain.TFrame")
        titles.grid(row=0, column=0, sticky="w")
        self.lbl_scope = ttk.Label(titles, text="", style="Heading.TLabel")
        self.lbl_scope.pack(anchor="w")
        self.lbl_count = ttk.Label(titles, text="", style="Muted.TLabel")
        self.lbl_count.pack(anchor="w")
        sort_box = ttk.Frame(header, style="Plain.TFrame")
        sort_box.grid(row=0, column=1, sticky="e")
        self._sort_var = tk.StringVar()
        self._sort_box = ttk.Combobox(sort_box, textvariable=self._sort_var, state="readonly", width=17,
                                      values=[self.ui.t(key) for _, key in SORTS])
        self._sort_box.pack(side="right")
        self._sort_box.bind("<<ComboboxSelected>>", self._on_sort)

        search = ttk.Frame(centre, style="Plain.TFrame")
        search.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        search.columnconfigure(1, weight=1)
        self._search_icon = ttk.Label(search, style="Muted.TLabel")
        self._search_icon.grid(row=0, column=0, padx=(0, 8))
        paint_search = lambda: self._search_icon.configure(                       # noqa: E731
            image=self.ui.icon("search", th.ACTIVE.text_muted))
        self.ui.on_theme(paint_search)
        paint_search()                            # the hook only fires on a CHANGE, so paint it once now
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var, width=12)   # it grows with the column
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self._on_search())
        w.Tooltip(self.ui, self.search_entry, lambda: self.ui.t(self._key("tip_search", "tip_search_scripts")))
        self.ui.label(ttk.Button(search, style="Link.TButton", command=self._clear_search),
                      "btn_clear").grid(row=0, column=2, padx=(8, 0))

        self._chips = ttk.Frame(centre, style="Plain.TFrame")
        self._chips.grid(row=2, column=0, sticky="w", pady=(8, 0))

        self._recording = ttk.Frame(centre, style="Recording.TFrame", padding=(14, 10))
        self._recording.columnconfigure(0, weight=1)
        self.lbl_rec = ttk.Label(self._recording, text="", style="RecordingText.TLabel")
        self.lbl_rec.grid(row=0, column=0, sticky="w")
        self.ui.label(ttk.Button(self._recording, style="Danger.TButton", command=self._toggle_recording),
                      "btn_stop_recording").grid(row=0, column=1, sticky="e")

        holder, self._list_box = w.scroll_area(self.ui, centre)
        holder.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        self._empty = None

    def _on_sort(self, _event=None) -> None:
        labels = [self.ui.t(key) for _, key in SORTS]
        chosen = self._sort_var.get()
        if chosen in labels:
            self.c.set_sort(SORTS[labels.index(chosen)][0])
            self.ui.render()

    def _on_search(self) -> None:
        self.c.set_query(self.search_var.get())
        self.ui.render()

    def _clear_search(self) -> None:
        self.search_var.set("")

    # ---- right: what is chosen, and Run -------------------------------------------------------------
    def _build_details(self) -> None:
        panel = ttk.Frame(self.frame, style="Panel.TFrame", padding=18, width=DETAILS_WIDTH)
        panel.grid(row=0, column=2, sticky="nsew", padx=(18, 0))
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        self._details_panel = panel

        head = ttk.Frame(panel, style="Surface.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self._head = head
        self.lbl_kind = ttk.Label(head, text="", style="Head.TLabel")
        self.lbl_kind.grid(row=0, column=0, sticky="w")
        self.lbl_name = ttk.Label(head, text="", style="PanelBig.TLabel", wraplength=230, justify="left")
        self.lbl_name.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.btn_star = ttk.Button(head, style="Card.Icon.TButton", command=self._toggle_favorite)
        self.btn_star.grid(row=0, column=1, rowspan=2, sticky="ne")
        w.Tooltip(self.ui, self.btn_star, lambda: self.ui.t("tip_favorite"))
        self.lbl_meta = ttk.Label(head, text="", style="PanelMuted.TLabel")
        self.lbl_meta.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        holder, body = w.scroll_area(self.ui, panel, background=lambda: th.ACTIVE.surface)
        holder.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        self._body, self._body_holder = body, holder

        self._about = w.section(self.ui, body, "details_about")
        self._about.pack(anchor="w")
        self.lbl_description = ttk.Label(body, text="", style="Panel.TLabel", wraplength=250, justify="left")
        self.lbl_description.pack(anchor="w", pady=(4, 2))
        self._btn_description = self.ui.label(ttk.Button(body, style="Card.Link.TButton",
                                                         command=self._edit_description), "btn_edit_description")
        self._btn_description.pack(anchor="w")

        self._steps_head = w.section(self.ui, body, "details_steps")
        self._steps_head.pack(anchor="w", pady=(16, 4))
        self.lbl_steps = ttk.Label(body, text="", style="Panel.TLabel", justify="left")
        self.lbl_steps.pack(anchor="w")

        self._runs_head = w.section(self.ui, body, "details_recent_runs")
        self._runs_head.pack(anchor="w", pady=(16, 4))
        self.lbl_runs = ttk.Label(body, text="", style="Panel.TLabel", justify="left")
        self.lbl_runs.pack(anchor="w")

        footer = ttk.Frame(panel, style="Surface.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        footer.columnconfigure(0, weight=1)
        self._footer = footer
        ttk.Separator(footer).grid(row=0, column=0, sticky="ew", pady=(0, 12))
        w.section(self.ui, footer, "details_targets").grid(row=1, column=0, sticky="w")
        self.lbl_targets = ttk.Label(footer, text="", style="Panel.TLabel", wraplength=270, justify="left")
        self.lbl_targets.grid(row=2, column=0, sticky="w", pady=(4, 0))
        # A run can cover many targets, so the batch gets ONE bar and one "show all" toggle here, never a row of
        # widgets per target: both are gridded only while a run is under way.
        self.btn_show_all = self.ui.label(ttk.Button(footer, style="Card.Link.TButton",
                                                     command=self._toggle_targets), "btn_show_all")
        self.ui.label(ttk.Button(footer, style="Card.Link.TButton", command=self._choose_targets),
                      "btn_change_targets").grid(row=4, column=0, sticky="w", pady=(2, 10))
        self.run_bar = ttk.Progressbar(footer, mode="determinate", maximum=1, value=0)
        self.btn_run = ttk.Button(footer, text="", style="Card.Primary.TButton", compound="left", command=self._run)
        self.btn_run.grid(row=6, column=0, sticky="ew")
        paint_run = lambda: self.btn_run.configure(image=self.ui.button_icon(  # noqa: E731
            "play", th.ACTIVE.text_on_primary, pad=8))
        self.ui.on_theme(paint_run)
        paint_run()
        w.Tooltip(self.ui, self.btn_run, lambda: self.ui.t(self.c.why_cannot_run())
                  if self.c.why_cannot_run() else "")
        extra = ttk.Frame(footer, style="Surface.TFrame")
        extra.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        self.btn_edit = self.ui.label(ttk.Button(extra, style="Card.TButton", command=self._edit_selected),
                                      self._key("btn_edit", "btn_open_editor"))
        self.btn_dry = self.ui.label(ttk.Button(extra, style="Card.TButton", command=lambda: self._run(dry_run=True)),
                                     "btn_dry_run")
        self._extra_side: bool | None = None
        self._layout_extra()
        w.Tooltip(self.ui, self.btn_dry, lambda: self.ui.t("tip_dry_run"))
        self.btn_stop = self.ui.label(ttk.Button(footer, style="Card.SmallDanger.TButton", command=self._stop_run),
                                      "btn_stop_run")

        self._nothing = w.empty_state(self.ui, panel, self._key("details_none_title", "details_none_title_script"),
                                      self._key("details_none_hint", "details_none_hint_script"),
                                      icon="flow" if self.scripts else "macros", surface=True, wrap=280)
        self._nothing.grid(row=0, column=0, rowspan=3, sticky="nsew")

    # ---- actions ------------------------------------------------------------------------------------
    def _selected(self):
        return self.c.selected_macro()

    def _new_macro(self) -> None:
        """An EMPTY recorded macro, to fill in by hand in the editor (the secondary path of the Macros screen)."""
        target = self.c.selected.title if (self.c.selected and self.c.target_ready) else ""
        if not target:
            if not self._pick_record_target():
                return
            target = self.c.selected.title if self.c.selected else ""
        answer = dialogs.NewMacro(self.ui, self.c.folders(), target,
                                  self.c.filter.folder if self.c.filter.scope is Scope.FOLDER else "").show()
        if answer:
            name, folder, description = answer
            self.c.create_macro(name, folder=folder, description=description)
        self.ui.render()

    def _new_script(self) -> None:
        """A NEW script: a name (and folder), then straight into the editor. Nothing is written until the script
        has a step and is saved."""
        answer = dialogs.NewScript(self.ui, self.c.folders(),
                                   self.c.filter.folder if self.c.filter.scope is Scope.FOLDER else "").show()
        if answer and self.c.new_script(answer[0], folder=answer[1]):
            self.ui.show_screen("editor")
        self.ui.render()

    def _new_folder(self) -> None:
        name = dialogs.TextPrompt(self.ui, "dlg_new_folder", "field_name", "", "btn_create").show()
        if name:
            self.c.create_folder(name)
        self.ui.render()

    def _folder_menu(self, folder: str) -> None:
        w.popup(self.ui, self.nav_items[f"folder:{folder}"]["row"], [
            ("menu_rename", lambda: self._rename_folder(folder), True),
            ("menu_delete_folder", lambda: self._delete_folder(folder), True)])

    def _rename_folder(self, folder: str) -> None:
        name = dialogs.TextPrompt(self.ui, "dlg_rename_folder", "field_name", folder, "btn_rename").show()
        if name:
            self.c.rename_folder(folder, name)
        self.ui.render()

    def _delete_folder(self, folder: str) -> None:
        self.c.delete_folder(folder)
        self.ui.render()

    def _toggle_favorite(self) -> None:
        info = self._selected()
        if info is not None:
            self.c.toggle_favorite(info.key)
            self.ui.render()

    def _edit_description(self) -> None:
        info = self._selected()
        if info is None:
            return
        text = dialogs.TextPrompt(self.ui, "dlg_description", "field_description",
                                  self.c.description(info.key), "btn_save").show()
        if text is not None:
            self.c.set_description(info.key, text)
        self.ui.render()

    def _edit_selected(self) -> None:
        info = self._selected()
        if info is not None and self.c.open_editor(info.key):
            self.ui.show_screen("editor")
        self.ui.render()

    def _choose_targets(self) -> None:
        # Opens with this macro's targets ticked -- or the ones last chosen for another macro, as a suggestion
        # that only becomes this macro's choice once confirmed here.
        chosen = dialogs.ChooseTargets(self.ui, self.c.entries, self.c.suggested_run_refs()).show()
        if chosen is not None:
            self.c.set_run_refs(chosen)
        self.ui.render()

    def _pick_record_target(self) -> bool:
        """Recording needs exactly ONE verified target, so it asks for one rather than guessing."""
        chosen = dialogs.ChooseTargets(self.ui, self.c.entries, (), single=True).show()
        if not chosen:
            return False
        return self.c.select(chosen[0])

    def _toggle_recording(self) -> None:
        if self.c.recording_active:
            self.c.stop_recording()
            self.ui.render()
            self._offer_to_save_recording()
            return
        if not self.c.target_ready and not self._pick_record_target():
            self.ui.render()
            return
        self.c.start_recording()
        self.ui.render()

    def _offer_to_save_recording(self) -> None:
        """After Stop the recording must be visible and keepable, not a state the user has to guess at."""
        macro = self.c.macro
        if macro is None or not self.c.unsaved:
            return
        answer = dialogs.RecordingResult(self.ui, len(macro.actions), len(macro.recording),
                                         self.c.folders()).show()
        if answer is None:
            self.c.discard_recording()
        else:
            name, folder = answer
            if self.c.save(name, folder=folder):
                self.c.refresh_macros()
                info = next((i for i in self.c.macros if i.path == self.c.macro_path), None)
                if info is not None:
                    self.c.select_macro(info.key)
                    self.c.set_scope(Scope.FOLDER, folder) if folder else self.c.set_scope(Scope.ALL)
        self.ui.render()

    def _run(self, *, dry_run: bool = False) -> None:
        self.c.start_run(dry_run=dry_run)
        self.ui.render()

    def _stop_run(self) -> None:
        self.c.cancel()
        self.ui.render()

    def _toggle_targets(self) -> None:
        self._show_all_targets = not self._show_all_targets
        self.ui.render()

    def _row_menu(self, key: str) -> None:
        info = self.c.macro_by_key(key)
        if info is None:
            return
        starred = self.c.is_favorite(key)
        items = [("menu_edit", lambda: self._menu_edit(key), info.valid),
                 ("menu_duplicate", lambda: self._duplicate(key), info.valid)]
        if not self.scripts:
            items.append(("menu_convert_v2", lambda: self._convert(key), info.valid and info.version == 1))
        items += [("menu_favorite_remove" if starred else "menu_favorite_add",
                   lambda: (self.c.toggle_favorite(key), self.ui.render()), True),
                  ("menu_move", lambda: self._move(key), True),
                  ("menu_rename", lambda: self._rename(key), info.valid),
                  ("menu_open_folder", lambda: self.ui.open_folder(info.path), True),
                  None,
                  ("menu_delete", lambda: self._delete(key), True)]
        w.popup(self.ui, self._row_widgets[key]["more"], items)

    def _menu_edit(self, key: str) -> None:
        self.c.select_macro(key)
        self._edit_selected()

    def _duplicate(self, key: str) -> None:
        info = self.c.macro_by_key(key)
        suggestion = self.ui.t("copy_of").format(name=info.name) if info else ""
        name = dialogs.TextPrompt(self.ui, "dlg_duplicate", "field_name", suggestion, "btn_create_copy").show()
        if name:
            self.c.duplicate_macro(key, name)
        self.ui.render()

    def _convert(self, key: str) -> None:
        """A new script beside the recorded macro, under a name the user confirms: the macro file is never
        replaced, and a name that is taken is refused like every other save. The new script is shown where scripts
        live."""
        info = self.c.macro_by_key(key)
        suggestion = self.ui.t("v2_name_of").format(name=info.name) if info else ""
        name = dialogs.TextPrompt(self.ui, "dlg_convert_v2", "field_name", suggestion, "btn_convert").show()
        if name and self.c.convert_to_v2(key, name):
            self.ui.show_screen("scripts")
        self.ui.render()

    def _move(self, key: str) -> None:
        info = self.c.macro_by_key(key)
        folder = dialogs.ChooseFolder(self.ui, self.c.folders(), info.folder if info else "").show()
        if folder is not None:
            self.c.move_macro(key, folder)
        self.ui.render()

    def _rename(self, key: str) -> None:
        info = self.c.macro_by_key(key)
        name = dialogs.TextPrompt(self.ui, "dlg_rename", "field_name", info.name if info else "",
                                  "btn_rename").show()
        if name:
            self.c.rename_macro(key, name)
        self.ui.render()

    def _delete(self, key: str) -> None:
        info = self.c.macro_by_key(key)
        if info is None:
            return
        if self.ui.confirm(self.ui.t("dlg_delete_title"), self.ui.t("dlg_delete_text").format(name=info.name)):
            self.c.delete_macro(key)
        self.ui.render()

    def select_row(self, key: str) -> None:
        self.c.select_macro(key)
        self.ui.render()

    # ---- rendering -----------------------------------------------------------------------------------
    def render(self) -> None:
        self._render_nav()
        self._render_header()
        self._render_rows()
        self._render_details()

    def _render_nav(self) -> None:
        counts = self.c.macro_counts()
        folders = self.c.folders()
        signature = (tuple(folders), tuple(sorted(counts.items())))
        if signature != self._nav_signature:
            self._nav_signature = signature
            for name in [n for n in self.nav_items if n.startswith("folder:")]:
                self.nav_items.pop(name)
            for child in self._folders_box.winfo_children():
                child.destroy()
            if not folders:
                self.ui.label(ttk.Label(self._folders_box, style="Muted.TLabel", wraplength=160,
                                        justify="left"), self._key("folders_empty", "folders_empty_script")
                              ).pack(anchor="w", padx=4, pady=4)
            for folder in folders:
                self._nav_item(self._folders_box, f"folder:{folder}", "",
                               lambda f=folder: self._pick_scope(Scope.FOLDER, f), label=folder)
                self.nav_items[f"folder:{folder}"]["row"].bind("<Button-3>",
                                                               lambda _e, f=folder: self._folder_menu(f))
        for name, item in self.nav_items.items():
            total = counts.get(name[len("folder:"):] if name.startswith("folder:") else name, 0)
            item["count"].configure(text=str(total) if total else "")
        self._paint_nav()
        if self.btn_record is not None:
            self.btn_record.configure(text=self.ui.t("btn_stop_recording" if self.c.recording_active
                                                     else "btn_record"))
            self._paint_record()
            self.btn_record.state(["!disabled"] if (self.c.recording_active or not self.c.busy) else ["disabled"])

    def _render_header(self) -> None:
        flt = self.c.filter
        keys = SCOPE_KEY_SCRIPTS if self.scripts else SCOPE_KEY
        self.lbl_scope.configure(text=flt.folder if flt.scope is Scope.FOLDER else self.ui.t(keys[flt.scope]))
        shown = self.c.visible_macros()
        self.lbl_count.configure(text=fmt.counted(self.ui, self._key("count_macros", "count_scripts"), len(shown)))
        labels = [self.ui.t(key) for _, key in SORTS]
        self._sort_box.configure(values=labels)
        current = next((self.ui.t(key) for sort, key in SORTS if sort is flt.sort), labels[0])
        if self._sort_var.get() != current:
            self._sort_var.set(current)
        for child in self._chips.winfo_children():
            child.destroy()
        if flt.query:
            self._chip("chip_search", flt.query, self._clear_search)
        if flt.scope is Scope.FOLDER:
            self._chip("chip_folder", flt.folder, lambda: self._pick_scope(Scope.ALL))
        if self.c.recording_active and not self.scripts:
            self._recording.grid(row=3, column=0, sticky="ew", pady=(10, 0))
            self.lbl_rec.configure(text=self.ui.t("rec_running").format(
                time=fmt.duration(self.ui, self.c.elapsed_s), count=self.c.event_count))
        else:
            self._recording.grid_remove()

    def _chip(self, key: str, value: str, clear) -> None:
        c = th.ACTIVE
        chip = tk.Frame(self._chips, background=c.primary_soft)
        chip.pack(side="left", padx=(0, 6))
        tk.Label(chip, text=f"{self.ui.t(key)}: {value}", background=c.primary_soft,
                 foreground=c.text, font=th.TYPE_CAPTION, padx=10, pady=4).pack(side="left")
        close = tk.Label(chip, text="×", background=c.primary_soft, foreground=c.text_muted,
                         font=(th.FONT, 11), padx=6, pady=2, cursor="hand2")
        close.pack(side="left")
        close.bind("<Button-1>", lambda _e: (clear(), self.ui.render()))

    def _render_rows(self) -> None:
        shown = self.c.visible_macros()
        signature = tuple((i.key, i.name, i.actions, i.modified, i.folder, i.valid, i.version,
                           self.c.is_favorite(i.key)) for i in shown)
        if signature != self._list_signature:
            self._list_signature = signature
            for child in self._list_box.winfo_children():
                child.destroy()
            self._row_widgets = {}
            for info in shown:
                self._build_row(info)
            if not shown:
                self._render_empty()
        c = th.ACTIVE
        for key, row in self._row_widgets.items():
            chosen = key == self.c.selected_key
            background = c.primary_soft if chosen else c.surface
            row["frame"].configure(background=background, highlightbackground=c.primary if chosen else c.border)
            row["accent"].configure(background=c.primary if chosen else background)
            for label in row["labels"]:
                label.configure(background=background)
            row["star"].configure(image=self.ui.icon("star_on" if self.c.is_favorite(key) else "star_off",
                                                     c.warning if self.c.is_favorite(key) else c.text_muted),
                                  background=background)
            prefix = "RowSel" if chosen else "Row"          # the buttons' corners take the row's own colour
            row["edit"].configure(style=f"{prefix}.TButton")
            row["more"].configure(style=f"{prefix}.Icon.TButton")

    def _render_empty(self) -> None:
        searching = bool(self.c.filter.query)
        if searching:
            buttons = ()
        elif self.scripts:
            buttons = (("btn_new_script", self._new_script, "Primary.TButton"),)
        else:
            buttons = (("btn_record", self._toggle_recording, "Primary.TButton"),)
        w.empty_state(self.ui, self._list_box,
                      "macros_no_results" if searching else self._key("macros_empty", "scripts_empty"),
                      "macros_no_results_hint" if searching else self._key("macros_empty_hint", "scripts_empty_hint"),
                      buttons, icon="search" if searching else ("flow" if self.scripts else "record")).pack(pady=48)

    def _build_row(self, info) -> None:
        c = th.ACTIVE
        frame = tk.Frame(self._list_box, background=c.surface, cursor="hand2", takefocus=1, highlightthickness=1,
                         highlightbackground=c.border, highlightcolor=c.focus)
        frame.pack(fill="x", pady=(0, 8))
        accent = tk.Frame(frame, width=4, background=c.surface)
        accent.pack(side="left", fill="y")
        kind = tk.Label(frame, background=c.surface, padx=10,
                        image=self.ui.icon("flow" if info.is_v2 else "record",
                                           c.error if not info.valid else c.primary))
        kind.pack(side="left")
        star = tk.Label(frame, background=c.surface, cursor="hand2", padx=8)
        star.pack(side="right")
        star.bind("<Button-1>", lambda _e, k=info.key: (self.c.toggle_favorite(k), self.ui.render()))
        w.Tooltip(self.ui, star, lambda: self.ui.t("tip_favorite"))

        # The buttons are packed BEFORE the name, so they keep their room and a long name is clipped instead of
        # squeezing Edit and the "..." menu out of the row.
        buttons = tk.Frame(frame, background=c.surface)
        buttons.pack(side="right", padx=(8, 0))
        middle = tk.Frame(frame, background=c.surface)
        middle.pack(side="left", fill="x", expand=True, pady=10)
        name = tk.Label(middle, anchor="w", font=(th.FONT_SEMIBOLD, 11), background=c.surface, foreground=c.text)
        name.pack(anchor="w", fill="x")
        w.elide(name, info.name)                          # a long name ends in "...", never sliced at an edge
        parts = [fmt.macro_size(self.ui, info)]
        if info.folder:
            parts.append(info.folder)
        parts.append(self.ui.t("row_changed").format(when=fmt.relative_time(self.ui, info.modified)))
        detail = tk.Label(middle, anchor="w", font=th.TYPE_CAPTION, background=c.surface, foreground=c.text_muted)
        detail.pack(anchor="w", fill="x")
        w.elide(detail, "  ·  ".join(parts) if info.valid else self.ui.t("row_invalid"))
        if not info.valid:
            detail.configure(foreground=c.error)

        more = ttk.Button(buttons, style="Row.Icon.TButton", command=lambda k=info.key: self._row_menu(k))
        more.pack(side="right")
        self.ui.on_theme(lambda b=more: b.configure(image=self.ui.icon("dots", th.ACTIVE.text_muted)))
        more.configure(image=self.ui.icon("dots", c.text_muted))
        w.Tooltip(self.ui, more, lambda: self.ui.t("tip_more"))
        # No Run here on purpose: a row button would start this macro at once on whatever targets were chosen
        # earlier. A run starts only from the details panel, under the targets it will use.
        edit = ttk.Button(buttons, style="Row.TButton", command=lambda k=info.key: self._menu_edit(k))
        edit.configure(text=self.ui.t("btn_edit"))
        edit.pack(side="right", padx=(0, 6))
        if not info.valid:
            edit.state(["disabled"])

        for widget in (frame, middle, name, detail, kind):
            widget.bind("<Button-1>", lambda _e, k=info.key: self.select_row(k))
            widget.bind("<Double-Button-1>", lambda _e, k=info.key: self._menu_edit(k))
        frame.bind("<Return>", lambda _e, k=info.key: self.select_row(k))
        frame.bind("<space>", lambda _e, k=info.key: self.select_row(k))
        self._row_widgets[info.key] = {"frame": frame, "accent": accent, "star": star, "more": more, "edit": edit,
                                       "labels": [middle, name, detail, buttons, kind]}

    def _render_details(self) -> None:
        """With nothing chosen the panel explains what to do rather than showing an empty frame -- and the Run
        button is not merely disabled, it is not there to be misread."""
        info = self.c.shown_macro()
        if info is None:
            for part in (self._head, self._body_holder, self._footer):
                part.grid_remove()
            self._nothing.grid(row=0, column=0, rowspan=3, sticky="nsew")
            for button in (self.btn_run, self.btn_edit, self.btn_dry):
                button.state(["disabled"])      # hidden AND disabled: the two can never disagree
            return
        self._nothing.grid_remove()
        self._head.grid(row=0, column=0, sticky="ew")
        self._body_holder.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        self._footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.lbl_kind.configure(text=self.ui.t("kind_script" if info.is_v2 else "kind_macro"))
        self.lbl_name.configure(text=info.name)
        self.btn_star.configure(image=self.ui.icon("star_on" if self.c.is_favorite(info.key) else "star_off",
                                                   th.ACTIVE.warning if self.c.is_favorite(info.key)
                                                   else th.ACTIVE.text_muted))
        meta = [fmt.macro_size(self.ui, info)]
        if info.folder:
            meta.append(info.folder)
        self.lbl_meta.configure(text="  ·  ".join(meta))
        description = self.c.description(info.key)
        self.lbl_description.configure(text=description or self.ui.t("details_no_description"))
        self.lbl_steps.configure(text=self._steps_preview(info))
        self.lbl_runs.configure(text=self._runs_preview(info))
        self._render_targets()
        self._layout_extra()

    def _layout_extra(self) -> None:
        """Edit and Dry run side by side when both fit the panel, one under the other when they do not (a
        longer translation): a button is never cut off."""
        room = DETAILS_WIDTH - 2 * 18 - 4
        side = self.btn_edit.winfo_reqwidth() + self.btn_dry.winfo_reqwidth() + 8 <= room
        if side == self._extra_side:
            return
        self._extra_side = side
        for button in (self.btn_edit, self.btn_dry):
            button.pack_forget()
        if side:
            self.btn_edit.pack(side="left", expand=True, fill="x", padx=(0, 4))
            self.btn_dry.pack(side="left", expand=True, fill="x", padx=(4, 0))
        else:
            self.btn_edit.pack(fill="x")
            self.btn_dry.pack(fill="x", pady=(8, 0))

    def _steps_preview(self, info) -> str:
        """A few real steps, so a macro can be understood without opening the editor. The file is read once per
        chosen macro (and again only when it changes on disk), not on every redraw: a v2 macro's templates are
        read, and hash-checked, as part of that one read."""
        if not info.valid:
            return self.ui.t("row_invalid")
        wanted = (info.key, info.stamp, self.ui.t.language)       # the version of the file the row describes
        if self._preview is not None and self._preview[:3] == wanted:
            return self._preview[3]
        text = self._read_preview(info)
        self._preview = wanted + (text,)
        return text

    def _read_preview(self, info) -> str:
        try:
            macro = self.c.library.load(info.path)
            if hasattr(macro, "nodes"):                # version 4: its blocks, in the order the arrows reach them
                from app.gui.view.graph_editor import script_lines
                rows = script_lines(self.ui, macro, self.c.macro_names())
            elif hasattr(macro, "steps"):
                rows = [fmt.step_line(self.ui, step) for step in macro.steps]
            else:
                from app.gui.macro_edit import blocks_of, document_of
                rows = [fmt.block_line(self.ui, block) for block in blocks_of(document_of(macro))]
        except Exception:
            return self.ui.t("details_steps_unavailable")
        if not rows:
            return self.ui.t("details_no_steps")
        lines = [f"{i + 1:02d}  {fmt.clip(row, PREVIEW_CHARS)}" for i, row in enumerate(rows[:PREVIEW_STEPS])]
        if len(rows) > PREVIEW_STEPS:
            lines.append(self.ui.t("details_more_steps").format(count=len(rows) - PREVIEW_STEPS))
        places = getattr(getattr(macro, "recording", None), "places", ())
        if places:                                 # a Full Windows recording: the windows its clicks go into
            lines += fmt.places_lines(self.ui, places, PREVIEW_CHARS)
        return "\n".join(lines)

    def _runs_preview(self, info) -> str:
        runs = self.c.history.for_macro(info.key, limit=RECENT_RUNS)
        if not runs:
            return self.ui.t("details_no_runs")
        return "\n".join(f"{fmt.relative_time(self.ui, r.at)} {fmt.clock_time(r.at)} — "
                         f"{fmt.outcome_label(self.ui, r.outcome)}" for r in runs)

    def _render_targets(self) -> None:
        titles = self.c.run_target_titles()
        if not titles:
            text = self.ui.t("targets_none_chosen")
            if self.c.previous_run_refs:
                text += "\n" + self.ui.t("targets_not_carried")
            self.lbl_targets.configure(text=text)
        else:
            # the group comes along: a name on its own cannot say whether the run goes to a device or a window
            by_ref = {entry.ref: entry for entry in self.c.entries}
            lines = []
            for ref, title in zip(self.c.run_refs, titles):
                entry = by_ref.get(ref)
                group = fmt.group_label(self.ui, entry.group) if entry is not None and entry.group else ""
                lines.append(f"{title}  ·  {group}" if group and group != title else title)
            shown = "\n".join(lines[:4])
            if len(lines) > 4:
                shown += "\n" + self.ui.t("details_more_targets").format(count=len(lines) - 4)
            self.lbl_targets.configure(text=f"{fmt.target_count(self.ui, len(titles))}\n{shown}")
        running = self.c.run_active
        if running:
            self.btn_run.grid_remove()
            self.btn_stop.grid(row=6, column=0, sticky="ew")
            self.lbl_targets.configure(text=self._run_progress())
            results = self.c.run_results
            done = sum(1 for r in results if r.state not in (RunState.WAITING, RunState.RUNNING))
            self.run_bar.configure(maximum=max(len(results), 1), value=done)
            self.run_bar.grid(row=5, column=0, sticky="ew", pady=(2, 8))
            if len(results) > 1:
                self.btn_show_all.configure(text=self.ui.t("btn_show_less" if self._show_all_targets
                                                           else "btn_show_all"))
                self.btn_show_all.grid(row=3, column=0, sticky="w", pady=(4, 0))
            else:
                self.btn_show_all.grid_remove()       # one target has nothing to expand
        else:
            self.btn_stop.grid_remove()
            self.run_bar.grid_remove()
            self.btn_show_all.grid_remove()
            self._show_all_targets = False
            self.btn_run.grid(row=6, column=0, sticky="ew")
            self.btn_run.configure(text=self.ui.t("btn_run_on").format(count=len(titles)) if titles
                                   else self.ui.t("btn_run"))
            self.btn_run.state(["!disabled"] if self.c.can_run() else ["disabled"])
        for button, enabled in ((self.btn_edit, not self.c.busy), (self.btn_dry, self.c.can_run())):
            button.state(["!disabled"] if enabled else ["disabled"])

    def _run_progress(self) -> str:
        """A run may cover many targets at once, so this is a SUMMARY plus the few lines worth reading: what is
        under way and what went wrong. The full list is one click away rather than eighteen permanent widgets."""
        results = self.c.run_results
        finished = [r for r in results if r.state not in (RunState.WAITING, RunState.RUNNING)]
        counts = [(state, sum(1 for r in results if r.state is state))
                  for state in (RunState.RUNNING, RunState.FAILED, RunState.STOPPED, RunState.REFUSED,
                                RunState.WAITING)]
        lines = [self.ui.t("run_progress_done").format(done=len(finished), total=len(results)),
                 "  ·  ".join(f"{n} {self.ui.t('run_state_' + state.value)}" for state, n in counts if n)]
        # Only the lines a person would act on: what is under way, and what went wrong. Everything else is in the
        # counts above, and the full list is behind the toggle.
        interesting = [r for r in results if r.state is RunState.RUNNING] + \
                      [r for r in results if r.state in (RunState.FAILED, RunState.REFUSED)]
        shown = list(results) if self._show_all_targets else interesting[:RUN_LINES]
        for result in shown:
            if result.state is RunState.RUNNING and result.starts_in:
                lines.append(self.ui.t("run_waiting_to_start").format(name=result.title,
                                                                      time=fmt.countdown(result.starts_in)))
            elif result.state is RunState.RUNNING:
                lines.append(self.ui.t("run_running").format(name=result.title, done=result.completed,
                                                             total=result.total))
            else:
                lines.append(f"{result.title} — {self.ui.t('run_state_' + result.state.value)}")
        if not self._show_all_targets and len(interesting) > RUN_LINES:
            lines.append(self.ui.t("details_more_targets").format(count=len(interesting) - RUN_LINES))
        return "\n".join(lines)
