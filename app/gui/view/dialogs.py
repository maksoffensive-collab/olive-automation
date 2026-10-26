"""Modal dialogs: naming things, choosing a folder, choosing where to run, and the questions that must never be
answered silently (unsaved changes, what to do with a finished recording).

Each one is small on purpose. The brief is explicit that creating a macro must not open a large form, so these
ask for the few things that cannot be guessed and nothing else. Every dialog returns a plain value (or None when
it was cancelled), so the caller has no dialog state to unwind.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.gui import theme as th
from app.gui.view import fmt
from app.gui.view import widgets as w

TARGET_LIST_HEIGHT = 380     # the least the target list shows: several targets, not only the first group
TARGET_TEXT_WIDTH = 350      # a target's details wrap inside the picker instead of running off its edge


class Modal:
    """A small centred dialog. Escape cancels, Return confirms, and the window it came from stays disabled until
    it is answered."""

    def __init__(self, ui, title_key: str, *, width: int = 420) -> None:
        self.ui = ui
        self.result = None
        self.top = tk.Toplevel(ui.root)
        self.top.title(ui.t(title_key))
        self.top.transient(ui.root)
        self.top.resizable(False, False)
        self.top.configure(background=th.ACTIVE.bg)
        self.body = ttk.Frame(self.top, style="Plain.TFrame", padding=(24, 20, 24, 20))
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        ui.label(ttk.Label(self.body, style="DialogTitle.TLabel"), title_key).grid(row=0, column=0, sticky="w",
                                                                                   pady=(0, 12))
        self.content = ttk.Frame(self.body, style="Plain.TFrame")
        self.content.grid(row=1, column=0, sticky="ew")
        self.content.columnconfigure(1, weight=1)
        self.buttons = ttk.Frame(self.body, style="Plain.TFrame")
        self.buttons.grid(row=2, column=0, sticky="e", pady=(22, 0))
        self._width = width
        self.top.bind("<Escape>", lambda _e: self.cancel())
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)

    def field(self, row: int, label_key: str, value: str = "", *, width: int = 30) -> tk.StringVar:
        self.ui.label(ttk.Label(self.content, style="FieldLabel.TLabel"), label_key).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        var = tk.StringVar(value=value)
        entry = ttk.Entry(self.content, textvariable=var, width=width)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        if row == 0:
            entry.focus_set()
            entry.selection_range(0, "end")
        return var

    def choice(self, row: int, label_key: str, values: list[str], value: str) -> tk.StringVar:
        self.ui.label(ttk.Label(self.content, style="FieldLabel.TLabel"), label_key).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        var = tk.StringVar(value=value)
        ttk.Combobox(self.content, textvariable=var, values=values, state="readonly").grid(
            row=row, column=1, sticky="ew", pady=4)
        return var

    def actions(self, confirm_key: str, *, confirm_style: str = "Primary.TButton") -> None:
        self.ui.label(ttk.Button(self.buttons, command=self.cancel), "btn_cancel_dialog").pack(side="left",
                                                                                                padx=(0, 8))
        confirm = self.ui.label(ttk.Button(self.buttons, style=confirm_style, command=self.confirm), confirm_key)
        confirm.pack(side="left")
        self.top.bind("<Return>", lambda _e: self.confirm())

    def confirm(self) -> None:
        self.result = self.value()
        self.top.destroy()

    def cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def value(self):
        return True

    def show(self):
        self.top.update_idletasks()
        x = self.ui.root.winfo_rootx() + max((self.ui.root.winfo_width() - self.top.winfo_width()) // 2, 20)
        y = self.ui.root.winfo_rooty() + 120
        self.top.wm_geometry(f"+{x}+{y}")
        self.top.grab_set()
        self.top.wait_window()
        return self.result


class TextPrompt(Modal):
    """One line of text: a new folder, a new name, the name of a copy."""

    def __init__(self, ui, title_key: str, field_key: str, value: str, confirm_key: str) -> None:
        super().__init__(ui, title_key)
        self.var = self.field(0, field_key, value)
        self.actions(confirm_key)

    def value(self):
        text = self.var.get().strip()
        return text or None


class NewMacro(Modal):
    """Name, folder and (optionally) a description. The target is shown, not asked for: a macro is always made
    for the target that is verified right now, and the dialog says which one."""

    def __init__(self, ui, folders: list[str], target_name: str, folder: str = "") -> None:
        super().__init__(ui, "dlg_new_macro")
        self.name = self.field(0, "field_name", "")
        self.folder = self.choice(1, "field_folder", [ui.t("folder_none")] + folders,
                                  folder or ui.t("folder_none"))
        self.description = self.field(2, "field_description", "")
        ui.label(ttk.Label(self.content, style="Muted.TLabel", wraplength=360, justify="left"),
                 "dlg_new_macro_target").grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(self.content, text=target_name, style="FieldLabel.TLabel").grid(row=4, column=0, columnspan=2,
                                                                                   sticky="w")
        self._none = ui.t("folder_none")
        self.actions("btn_create")

    def value(self):
        name = self.name.get().strip()
        if not name:
            return None
        folder = self.folder.get()
        return name, ("" if folder == self._none else folder), self.description.get().strip()


class NewScript(Modal):
    """Name and folder of a new script. Nothing is written yet: the editor opens on the empty script, and it becomes
    a file only once it has a step and is saved -- so creating a script never leaves an empty recorded macro."""

    def __init__(self, ui, folders: list[str], folder: str = "") -> None:
        super().__init__(ui, "dlg_new_script")
        self.name = self.field(0, "field_name", "")
        self.folder = self.choice(1, "field_folder", [ui.t("folder_none")] + folders, folder or ui.t("folder_none"))
        ui.label(ttk.Label(self.content, style="Muted.TLabel", wraplength=380, justify="left"),
                 "dlg_new_script_hint").grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self._none = ui.t("folder_none")
        self.actions("btn_create_script")

    def value(self):
        name = self.name.get().strip()
        if not name:
            return None
        folder = self.folder.get()
        return name, ("" if folder == self._none else folder)


class ChooseFolder(Modal):
    def __init__(self, ui, folders: list[str], current: str) -> None:
        super().__init__(ui, "dlg_move_to_folder")
        self._none = ui.t("folder_none")
        self.folder = self.choice(0, "field_folder", [self._none] + folders, current or self._none)
        self.actions("btn_move")

    def value(self):
        folder = self.folder.get()
        return "" if folder == self._none else folder


class ChooseTargets(Modal):
    """Where a macro will run. Nothing is ticked unless the user ticks it: an empty choice means no targets, and
    the run is refused rather than widened to everything."""

    def __init__(self, ui, entries, chosen: tuple[str, ...], *, single: bool = False) -> None:
        super().__init__(ui, "dlg_targets_single" if single else "dlg_targets", width=460)
        self.single, self.vars = single, {}
        self.top.resizable(False, True)
        outer, inner = w.scroll_area(ui, self.content, height=TARGET_LIST_HEIGHT)
        outer.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        # The list takes the dialog's height. With only its own small request it showed some 80 pixels -- the
        # first group (Full Windows) and nothing of the windows and instances below it.
        self.content.grid_configure(sticky="nsew")
        self.body.rowconfigure(1, weight=1)
        self.top.geometry("480x600")
        self.list_area = outer
        groups: dict[str, list] = {}
        for entry in entries:
            groups.setdefault(entry.group, []).append(entry)
        if not entries:
            w.empty_state(ui, inner, "targets_empty", "targets_empty_hint").pack(pady=30)
        self._choice = tk.StringVar(value=chosen[0] if (single and chosen) else "")
        self._chosen = tuple(chosen)
        self.rows: dict[str, dict] = {}
        for group, items in groups.items():
            ttk.Label(inner, text=fmt.group_label(ui, group), style="Head.TLabel").pack(anchor="w", pady=(12, 4))
            for entry in items:
                self._row(inner, entry)
        if not single and entries:
            row = ttk.Frame(self.buttons, style="Plain.TFrame")
            row.pack(side="left", padx=(0, 16))
            ui.label(ttk.Button(row, style="Link.TButton", command=lambda: self._all(True)),
                     "btn_select_all").pack(side="left")
            ui.label(ttk.Button(row, style="Link.TButton", command=lambda: self._all(False)),
                     "btn_select_none").pack(side="left", padx=(6, 0))
        self.actions("btn_use_targets")

    def _row(self, parent, entry) -> None:
        """One candidate, said three ways: what it is called, WHO offers it and what they say about it, and what
        choosing it means for this screen. The name alone is never enough -- the same program can appear as a
        device from one provider and as a window from another, under the very same name."""
        row = ttk.Frame(parent, style="Plain.TFrame")
        row.pack(fill="x", padx=2, pady=3)
        if self.single:
            control = ttk.Radiobutton(row, value=entry.ref, variable=self._choice, style="Choice.TRadiobutton")
            toggle = lambda: self._choice.set(entry.ref)                                   # noqa: E731
        else:
            var = tk.BooleanVar(value=entry.ref in self._chosen)
            self.vars[entry.ref] = var
            control = ttk.Checkbutton(row, variable=var, style="Choice.TCheckbutton")
            toggle = lambda v=var: v.set(not v.get())                                      # noqa: E731
        control.pack(side="left", padx=(0, 6), anchor="n")
        text = ttk.Frame(row, style="Plain.TFrame")
        text.pack(side="left", fill="x", expand=True)
        title = ttk.Label(text, text=entry.title or entry.process or entry.ref, style="FieldLabel.TLabel")
        title.pack(anchor="w")
        detail = ttk.Label(text, text=fmt.target_detail(self.ui, entry), style="Muted.TLabel",
                           wraplength=TARGET_TEXT_WIDTH, justify="left")
        detail.pack(anchor="w")
        hint = ttk.Label(text, text=fmt.target_hint(self.ui, entry), wraplength=TARGET_TEXT_WIDTH, justify="left",
                         style="TargetBackground.TLabel" if entry.background else "Muted.TLabel")
        hint.pack(anchor="w")
        for widget in (row, text, title, detail, hint):
            widget.bind("<Button-1>", lambda _e, fn=toggle: fn())
        self.rows[entry.ref] = {"title": title, "detail": detail, "hint": hint}

    def _all(self, value: bool) -> None:
        for var in self.vars.values():
            var.set(value)

    def value(self):
        if self.single:
            return (self._choice.get(),) if self._choice.get() else ()
        return tuple(ref for ref, var in self.vars.items() if var.get())


class RecordingResult(Modal):
    """What a finished recording became, and where it is about to be kept. The brief calls this out specifically:
    after Stop the user must SEE the result, not guess whether anything was captured."""

    def __init__(self, ui, action_count: int, event_count: int, folders: list[str], folder: str = "") -> None:
        super().__init__(ui, "dlg_recorded")
        summary = ui.t("dlg_recorded_summary").format(actions=action_count, events=event_count)
        ttk.Label(self.content, text=summary, style="FieldLabel.TLabel").grid(row=0, column=0, columnspan=2,
                                                                               sticky="w", pady=(0, 10))
        self.name = self.field(1, "field_name", "")
        self._none = ui.t("folder_none")
        self.folder = self.choice(2, "field_folder", [self._none] + folders, folder or self._none)
        self.actions("btn_save_macro")

    def value(self):
        name = self.name.get().strip()
        if not name:
            return None
        folder = self.folder.get()
        return name, ("" if folder == self._none else folder)


class UnsavedChanges(Modal):
    """Save / discard / cancel. Closing the editor must never lose work silently."""

    def __init__(self, ui, macro_name: str) -> None:
        super().__init__(ui, "dlg_unsaved")
        ttk.Label(self.content, text=macro_name, style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w")
        ui.label(ttk.Label(self.content, style="Muted.TLabel", wraplength=340, justify="left"),
                 "dlg_unsaved_hint").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ui.label(ttk.Button(self.buttons, command=self.cancel), "btn_cancel_dialog").pack(side="left", padx=(0, 8))
        ui.label(ttk.Button(self.buttons, command=lambda: self._answer("discard")),
                 "btn_dont_save").pack(side="left", padx=(0, 8))
        ui.label(ttk.Button(self.buttons, style="Primary.TButton", command=lambda: self._answer("save")),
                 "btn_save").pack(side="left")
        self.top.bind("<Return>", lambda _e: self._answer("save"))

    def _answer(self, value: str) -> None:
        self.result = value
        self.top.destroy()


class AddAction(Modal):
    """Only the action kinds the platform can really perform are offered (see macro_edit.INSERTABLE): a menu
    entry for something no provider can do would be a promise the run could not keep."""

    def __init__(self, ui, ops: list[tuple[str, str]], keys: list[str]) -> None:
        super().__init__(ui, "dlg_add_action")
        self._ops = {ui.t(label_key): op for op, label_key in ops}
        first = ui.t(ops[0][1]) if ops else ""
        self.kind = self.choice(0, "field_action_kind", list(self._ops), first)
        self.pause = self.field(1, "field_pause_seconds", "0")
        self.x = self.field(2, "field_x", "0", width=10)
        self.y = self.field(3, "field_y", "0", width=10)
        self.key = self.choice(4, "field_key", keys, keys[0] if keys else "")
        self.text = self.field(5, "field_text", "")
        self.actions("btn_add")

    def value(self):
        return {"op": self._ops.get(self.kind.get()), "pause": self.pause.get(), "x": self.x.get(),
                "y": self.y.get(), "key": self.key.get(), "text": self.text.get()}
