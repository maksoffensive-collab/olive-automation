"""Tk application shell: the window, the styles, the screens and the one render loop. No automation logic.

Layout, left to right and top to bottom:

    navigation rail | page header: title + what this page is / where you are     state chip, target chip
    (Macros,        |-------------------------------------------------------------------------------------
     Scripts,       | the current screen
     Text on        |
     screen,        |                         (transient notifications float at the bottom centre)
     History ...    |-------------------------------------------------------------------------------------
     Settings)      | stop bar: only while something that can send input runs, or a stop is set

The stop bar carries the Emergency Stop while something that can send input is under way, and the stopped state with
its separate "Reset emergency stop" once a stop is set; with nothing under way and no stop it is not shown (Esc is
inert then too). Routine results appear as notifications that leave by themselves; an error stays until it is closed;
the emergency stop is never a notification. Screens live in `app.gui.view`; each is a small class that renders
`AppController` and calls back into this object through a narrow interface:

    ui.t(key)                 translate                ui.icon(kind, colour)   a themed glyph
    ui.label(widget, key)     text that follows the    ui.on_theme(fn)         re-colour on a theme change
                              language
    ui.render()               redraw everything        ui.show_screen(name)    navigate
    ui.confirm(title, text)   ask a yes/no question    ui.open_folder(path)    reveal a file (injected)
    ui.run_job(job, done)     run `job` off the UI thread; `done(result or exception)` is called back ON it
    ui.copy_text(text)        put text on the clipboard

Colours always come from `theme.ACTIVE` at call time. `_style()` re-configures every ttk style (and re-renders the
rounded control pictures of `view.skin`) from it and is safe to call again, which is what makes switching theme
instant; the raw tk widgets that ttk styling cannot reach register a callback with `on_theme` instead. The view
imports no provider and no operating-system module -- even revealing a macro in the file manager arrives as an
injected callable, so the GUI layer stays free of the OS.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from app.gui import theme as th
from app.gui.controller import (KIND_MACROS, KIND_SCRIPTS, STOP_IDLE, STOP_STOPPED, AppController, RunState, Status,
                                TargetStatus)
from app.gui.history import RunHistory
from app.gui.i18n import LANGUAGE_NAMES, LANGUAGES, Translator
from app.gui.library import MacroLibrary
from app.gui.macro_list import Scope
from app.gui.meta import MacroMeta
from app.gui.prefs import PreferencesStore
from app.gui.services import AutomationServices
from app.gui.view import dialogs, fmt, skin
from app.gui.view import widgets as w
from app.gui.view.editor import EditorScreen
from app.gui.view.history import HistoryScreen
from app.gui.view.macros import MacrosScreen
from app.gui.view.panels import DiagnosticsScreen, SettingsScreen
from app.gui.view.text import TextScreen
from app.observe import OcrProvider

POLL_MS = 100
CRUMB_CHARS = 28          # a breadcrumb part: a name is clipped, the header keeps its width whatever the names
TARGET_CHARS = 26
SCREENS = ("macros", "scripts", "editor", "history", "text", "settings", "diagnostics")
NAV = (("macros", "nav_macros", "record"), ("scripts", "nav_scripts", "flow"), ("text", "nav_text", "text"),
       ("history", "nav_history", "history"))
NAV_FOOT = (("settings", "nav_settings", "settings"),)
SUBTITLE = {"macros": "sub_macros", "scripts": "sub_scripts", "text": "text_subtitle", "history": "sub_history",
            "settings": "sub_settings", "diagnostics": "sub_diagnostics"}
TITLE = {"macros": "nav_macros", "scripts": "nav_scripts", "text": "nav_text", "history": "nav_history",
         "settings": "nav_settings", "diagnostics": "nav_diagnostics"}
LIBRARY_SCREENS = {"macros": KIND_MACROS, "scripts": KIND_SCRIPTS}
# Routine sentences that are not worth a notification: what the screen itself already shows.
QUIET = frozenset({("info", "discovery"), ("info", "editor"), ("info", "ready"), ("ok", "verified")})


class App:
    def __init__(self, services: AutomationServices, library: MacroLibrary, *, root: tk.Tk | None = None,
                 auto_refresh: bool = True, confirm: Callable[[str, str], bool] | None = None,
                 preview_runner: Callable[[Callable[[], None]], None] | None = None,
                 prefs: PreferencesStore | None = None, meta: MacroMeta | None = None,
                 history: RunHistory | None = None,
                 open_folder: Callable[[object], bool] | None = None,
                 ocr: OcrProvider | None = None) -> None:
        self.prefs = prefs or PreferencesStore(None)
        saved = self.prefs.load()
        th.set_theme(saved.get("theme", th.DEFAULT_THEME))
        self.controller = AppController(services, library, meta=meta, history=history, ocr=ocr)
        self.controller.set_concurrency(saved.get("concurrency", ""))
        self.root = root or tk.Tk()
        self.t = Translator(saved.get("language", "en"))
        self._confirm = confirm or (lambda title, text: messagebox.askyesno(title, text, parent=self.root))
        self._open_folder = open_folder
        self._theme_hooks: list[Callable[[], None]] = []
        self._texts: list[tuple[tk.Widget, str]] = []
        self._icons = w.Icons(self.root)
        self._skin = skin.Skin(self.root)
        self._results: queue.Queue = queue.Queue()
        self._jobs: queue.Queue = queue.Queue()            # (done, outcome) of run_job work, drained by _tick
        self._preview_busy = False
        self._preview_runner = preview_runner or self._run_in_thread
        self._was_busy = False
        self._after: str | None = None
        self._rendered = -1
        self._shown_status: Status | None = self.controller.status     # the sentence already shown (not toasted)
        self._rung = self.controller.attention                        # the sounds already played
        self.screen = "macros"
        self._build()
        self.controller.refresh_macros()
        self.render()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._after = self.root.after(POLL_MS, self._tick)
        if auto_refresh:
            self.root.after(50, self._on_refresh)

    # ---- the interface the screens use -------------------------------------------------------------
    def label(self, widget, key: str):
        widget.configure(text=self.t(key))
        self._texts.append((widget, key))
        return widget

    register_text = label

    def on_theme(self, fn: Callable[[], None]) -> None:
        self._theme_hooks.append(fn)

    def icon(self, kind: str, colour: str, size: int = w.ICON_SIZE, *, pad: int = 0) -> tk.PhotoImage:
        return self._icons.get(kind, colour, size, pad=pad)

    def button_icon(self, kind: str, colour: str, *, pad: int = 0) -> tuple:
        """A button's glyph with its own disabled look: without it Tk draws the enabled glyph on the disabled
        face -- a white play sign on a pale button reads as an empty white box."""
        return (self.icon(kind, colour, pad=pad), "disabled", self.icon(kind, th.ACTIVE.disabled_fg, pad=pad))

    def confirm(self, title: str, text: str) -> bool:
        return bool(self._confirm(title, text))

    def copy_text(self, text: str) -> None:
        """Put `text` on the clipboard (Tk's own clipboard, which is the system's)."""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def open_folder(self, path) -> None:
        """Reveal a macro in the file manager. The GUI layer must not touch the operating system itself, so the
        composition root injects this; without it the action says so instead of doing nothing."""
        if self._open_folder is None or not self._open_folder(path):
            self.controller.note("warn", "storage", self.t("folder_open_failed"))
        self.render()

    # ---- construction --------------------------------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        root.title(self.t("app_title"))
        root.geometry("1360x840")
        root.minsize(1120, 680)
        root.configure(background=th.ACTIVE.bg)
        self.on_theme(lambda: root.configure(background=th.ACTIVE.bg))
        self._style()
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        self.language_var = tk.StringVar(value=LANGUAGE_NAMES[self.t.language])
        self._build_nav()

        main = ttk.Frame(root, style="Plain.TFrame")
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        self._build_header(main)

        container = ttk.Frame(main, style="Plain.TFrame")
        container.grid(row=1, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self.screens = {}
        for name, screen in (("macros", MacrosScreen(self, container, kind=KIND_MACROS)),
                             ("scripts", MacrosScreen(self, container, kind=KIND_SCRIPTS)),
                             ("editor", EditorScreen(self, container)),
                             ("history", HistoryScreen(self, container)), ("text", TextScreen(self, container)),
                             ("settings", SettingsScreen(self, container)),
                             ("diagnostics", DiagnosticsScreen(self, container))):
            screen.frame.grid(row=0, column=0, sticky="nsew")
            screen.frame.grid_remove()        # laid out only while shown: see show_screen
            self.screens[name] = screen

        self._build_stop_bar(main)
        # The latest sentence, for assistive technology and for tests; what a person sees is the notification.
        self.lbl_status = ttk.Label(main, text="", style="Muted.TLabel")
        self.toaster = w.Toaster(self, container)
        self._bind_keys()
        self.show_screen("macros")

    def _build_nav(self) -> None:
        nav = ttk.Frame(self.root, style="Sidebar.TFrame", padding=(12, 18, 13, 14), width=th.NAV_WIDTH)
        nav.grid(row=0, column=0, sticky="ns")
        nav.pack_propagate(False)
        edge = tk.Frame(nav, width=1, background=th.ACTIVE.border)
        edge.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        self.on_theme(lambda: edge.configure(background=th.ACTIVE.border))

        brand = ttk.Frame(nav, style="Sidebar.TFrame")
        brand.pack(fill="x", padx=8, pady=(0, 24))
        self._mark = tk.Canvas(brand, width=30, height=30, highlightthickness=0)
        self._mark.pack(side="left", padx=(0, 10))
        self.on_theme(self._draw_mark)
        self._draw_mark()
        names = ttk.Frame(brand, style="Sidebar.TFrame")
        names.pack(side="left")
        ttk.Label(names, text="Olive", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(names, text="Automation", style="BrandSub.TLabel").pack(anchor="w")

        self.label(ttk.Label(nav, style="NavSection.TLabel"), "nav_section").pack(anchor="w", padx=10,
                                                                                  pady=(0, 6))
        self.nav_buttons: dict[str, tuple[ttk.Button, str]] = {}
        for name, key, glyph in NAV:
            self._nav_button(nav, name, key, glyph).pack(fill="x", pady=1)
        ttk.Frame(nav, style="Sidebar.TFrame").pack(fill="both", expand=True)
        for name, key, glyph in NAV_FOOT:
            self._nav_button(nav, name, key, glyph).pack(fill="x", pady=1)
        self.on_theme(self._paint_nav)

    def _nav_button(self, parent, name: str, key: str, glyph: str) -> ttk.Button:
        button = ttk.Button(parent, style="Nav.TButton", compound="left", takefocus=True,
                            command=lambda n=name: self.show_screen(n))
        self.label(button, key)
        self.nav_buttons[name] = (button, glyph)
        return button

    def _draw_mark(self) -> None:
        c = th.ACTIVE
        self._mark.configure(background=c.sidebar_bg)
        self._mark.delete("all")
        self._mark.create_oval(1, 1, 29, 29, fill=c.primary_soft, outline="")
        self._mark.create_oval(8, 8, 22, 22, fill=c.primary, outline="")
        self._mark.create_polygon(15, 3, 21, 9, 15, 13, 9, 9, fill=c.accent, outline="", smooth=True)

    def _build_header(self, main) -> None:
        bar = ttk.Frame(main, style="Header.TFrame", padding=(28, 20, 28, 10))
        bar.grid(row=0, column=0, sticky="ew")
        self.header = bar
        bar.columnconfigure(0, weight=1)
        titles = ttk.Frame(bar, style="Header.TFrame")
        titles.grid(row=0, column=0, sticky="nw")
        self.lbl_title = ttk.Label(titles, text="", style="HeaderTitle.TLabel")
        self.lbl_title.pack(anchor="w")
        # what the page is for -- or, once something is chosen, where you are (the breadcrumb, never a file path)
        self.lbl_where = ttk.Label(titles, text="", style="HeaderSub.TLabel", justify="left")
        self.lbl_where.pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(bar, style="Header.TFrame")
        right.grid(row=0, column=1, sticky="ne", padx=(16, 0), pady=(4, 0))
        self.lbl_system = ttk.Label(right, text="", style="Chip.TLabel", compound="left")
        self.lbl_system.pack(side="left", padx=(0, 8))
        self.btn_target = ttk.Button(right, style="Chip.TButton", compound="left", command=self._choose_target)
        self.btn_target.pack(side="left")
        w.Tooltip(self, self.btn_target, lambda: self.t("tip_header_target"))
        w.wrap_to(bar, self.lbl_where, others=(right,), reserve=90, minimum=300)

    def _build_stop_bar(self, main) -> None:
        """The Emergency Stop and the stopped state, in a red band across the workspace. Built once;
        `_render_stop_bar` shows it only while it means something (see `AppController.stop_phase`)."""
        bar = ttk.Frame(main, style="StopBar.TFrame", padding=(28, 10))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.stop_bar = bar
        icon = ttk.Label(bar, style="StopBar.TLabel")
        icon.grid(row=0, column=0, padx=(0, 10))
        paint = lambda: icon.configure(image=self.icon("warning", th.ACTIVE.error))     # noqa: E731
        self.on_theme(paint)
        paint()
        self.lbl_stop = ttk.Label(bar, text="", style="StopBar.TLabel", justify="left")
        self.lbl_stop.grid(row=0, column=1, sticky="w")
        # A separate control, never the stop button pressed again: a second press of the stop must keep the stop.
        # It sits to the LEFT of the stop, and the stop keeps its place at the right edge in every phase the bar is
        # shown in, so a hurried second or third click on the stop always lands on the stop -- never on the reset
        # that appears once everything has ended.
        self.btn_reset_stop = self.label(ttk.Button(bar, command=self._on_reset_stop), "btn_reset_stop")
        self.btn_reset_stop.grid(row=0, column=2, sticky="e", padx=(12, 0))
        w.Tooltip(self, self.btn_reset_stop, lambda: self.t("reset_stop_hint"))
        self.btn_estop = self.label(ttk.Button(bar, style="Danger.TButton", command=self._on_estop),
                                    "btn_estop")
        self.btn_estop.grid(row=0, column=3, sticky="e", padx=(8, 0))
        w.Tooltip(self, self.btn_estop, lambda: self.t("estop_hint"))
        w.wrap_to(bar, self.lbl_stop, others=(self.btn_reset_stop, self.btn_estop, icon), reserve=110, minimum=260)
        bar.grid_remove()

    def _bind_keys(self) -> None:
        """Shortcuts are an addition, never the only way: every one of these has a visible control too."""
        binds = {"<Control-s>": self._key_save, "<Control-S>": self._key_save,
                 "<Control-z>": self._key_undo, "<Control-Z>": self._key_undo,
                 "<Control-Shift-Z>": self._key_redo, "<Control-y>": self._key_redo,
                 "<F2>": self._key_rename, "<Control-d>": self._key_duplicate,
                 "<Control-Shift-D>": self._toggle_diagnostics, "<Control-Shift-d>": self._toggle_diagnostics,
                 "<Escape>": self._key_estop}
        for index, (name, _key, _glyph) in enumerate(NAV, start=1):
            binds[f"<Control-Key-{index}>"] = lambda _e, n=name: self._key_nav(n)
        for sequence, handler in binds.items():
            self.root.bind_all(sequence, handler)

    # ---- styles --------------------------------------------------------------------------------------
    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        c = th.ACTIVE
        body = th.TYPE_BODY
        self._skin.apply(style)
        style.configure(".", font=body, background=c.bg, foreground=c.text, bordercolor=c.border,
                        lightcolor=c.bg, darkcolor=c.bg, troughcolor=c.surface_alt, focuscolor=c.focus,
                        selectbackground=c.primary_soft, selectforeground=c.text, insertcolor=c.text,
                        fieldbackground=c.surface)
        for name, background in (("TFrame", c.bg), ("Plain.TFrame", c.bg), ("Header.TFrame", c.topbar_bg),
                                 ("Sidebar.TFrame", c.sidebar_bg), ("Surface.TFrame", c.surface),
                                 ("Inset.TFrame", c.surface_alt), ("Panel.TFrame", c.bg), ("Card.TFrame", c.bg),
                                 ("SoftCard.TFrame", c.bg), ("Toast.TFrame", c.bg),
                                 ("StopBar.TFrame", c.danger_bg), ("Recording.TFrame", c.danger_bg),
                                 ("StatusBar.TFrame", c.surface_alt)):
            style.configure(name, background=background)
        for name, background, foreground, typeface in (
                ("TLabel", c.bg, c.text, body), ("Plain.TLabel", c.bg, c.text, body),
                ("Muted.TLabel", c.bg, c.text_muted, body), ("Caption.TLabel", c.bg, c.text_muted, th.TYPE_CAPTION),
                ("Error.TLabel", c.bg, c.error, body), ("Warning.TLabel", c.bg, c.warning, body),
                ("Success.TLabel", c.bg, c.success, body),
                ("TargetBackground.TLabel", c.bg, c.success, body),
                ("Panel.TLabel", c.surface, c.text, body), ("PanelMuted.TLabel", c.surface, c.text_muted, body),
                ("PanelCaption.TLabel", c.surface, c.text_muted, th.TYPE_CAPTION),
                ("PanelStrong.TLabel", c.surface, c.text, th.TYPE_BODY_STRONG),
                ("PanelError.TLabel", c.surface, c.error, body),
                ("PanelBig.TLabel", c.surface, c.text, th.TYPE_HEADING),
                ("PanelEmptyTitle.TLabel", c.surface, c.text, th.TYPE_HEADING),
                ("Head.TLabel", c.surface, c.text_muted, th.TYPE_LABEL),
                ("SidebarHead.TLabel", c.sidebar_bg, c.text_muted, th.TYPE_LABEL),
                ("SidebarMuted.TLabel", c.sidebar_bg, c.text_muted, body),
                ("NavSection.TLabel", c.sidebar_bg, c.text_muted, th.TYPE_LABEL),
                ("Brand.TLabel", c.sidebar_bg, c.text, th.TYPE_BRAND),
                ("BrandSub.TLabel", c.sidebar_bg, c.text_muted, th.TYPE_CAPTION),
                ("SectionTitle.TLabel", c.bg, c.text_muted, th.TYPE_LABEL),
                ("Heading.TLabel", c.bg, c.text, th.TYPE_HEADING),
                ("PageTitle.TLabel", c.bg, c.text, th.TYPE_TITLE),
                ("EmptyTitle.TLabel", c.bg, c.text, th.TYPE_HEADING),
                ("DialogTitle.TLabel", c.bg, c.text, th.TYPE_HEADING),
                ("FieldLabel.TLabel", c.bg, c.text, th.TYPE_BODY_STRONG),
                ("Mono.TLabel", c.surface, c.text_muted, (th.MONO, 9)),
                ("HeaderTitle.TLabel", c.topbar_bg, c.text, th.TYPE_TITLE),
                ("HeaderSub.TLabel", c.topbar_bg, c.text_muted, th.TYPE_SUBTITLE),
                ("StopBar.TLabel", c.danger_bg, c.error, th.TYPE_BODY_STRONG),
                ("RecordingText.TLabel", c.danger_bg, c.error, th.TYPE_BODY_STRONG),
                ("Toast.TLabel", c.overlay, c.text, body),
                ("StatusBar.TLabel", c.surface_alt, c.text, body)):
            style.configure(name, background=background, foreground=foreground, font=typeface)
        style.configure("Chip.TLabel", background=c.topbar_bg, foreground=c.text, font=body, padding=(12, 6))

        for name, foreground, typeface, padding, background in (
                ("TButton", c.text, th.TYPE_BODY_STRONG, (14, 7), c.bg),
                ("Primary.TButton", c.text_on_primary, th.TYPE_BODY_STRONG, (16, 7), c.bg),
                ("Danger.TButton", c.text_on_primary, th.TYPE_BODY_STRONG, (16, 7), c.danger_bg),
                ("SmallDanger.TButton", c.error, th.TYPE_BODY_STRONG, (12, 6), c.bg),
                ("Icon.TButton", c.text, body, 6, c.bg),
                ("Toast.Icon.TButton", c.text, body, 4, c.overlay),
                ("Nav.TButton", c.sidebar_text_muted, body, (10, 8), c.sidebar_bg),
                ("NavActive.TButton", c.primary, th.TYPE_BODY_STRONG, (10, 8), c.sidebar_bg),
                ("Chip.TButton", c.text, body, (12, 5), c.topbar_bg)):
            style.configure(name, foreground=foreground, font=typeface, padding=padding, background=background,
                            anchor="w" if name.startswith("Nav") else "center", borderwidth=0, relief="flat")
            style.map(name, foreground=[("disabled", c.disabled_fg)], background=[("disabled", background),
                                                                                  ("active", background)])
        style.map("Nav.TButton", foreground=[("disabled", c.disabled_fg), ("active", c.sidebar_text)])
        style.layout("Link.TButton", [("Button.background", {"sticky": "nsew", "children": [
            ("Button.focus", {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])
        style.configure("Link.TButton", padding=(2, 2), background=c.bg, foreground=c.primary,
                        font=(th.FONT, 10, "underline"), focuscolor=c.focus, borderwidth=0)
        style.map("Link.TButton", background=[("active", c.bg)],
                  foreground=[("disabled", c.disabled_fg), ("active", c.primary_hover)])
        # The same buttons on a white card or a list row: only the colour behind their rounded corners differs.
        for prefix, background in (("Card", c.surface), ("Row", c.surface), ("RowSel", c.primary_soft)):
            for base in ("TButton", "Primary.TButton", "Icon.TButton", "SmallDanger.TButton", "Link.TButton"):
                style.configure(f"{prefix}.{base}", background=background)
                style.map(f"{prefix}.{base}", background=[("disabled", background), ("active", background)])
        for name in ("Row.TButton", "RowSel.TButton"):
            style.configure(name, padding=(12, 4))
        # The create card's button: on the white card, and a little narrower so a longer translation still fits.
        for name in ("Create.Primary.TButton", "Create.Danger.TButton"):
            style.configure(name, background=c.surface, padding=(10, 7))
            style.map(name, background=[("disabled", c.surface), ("active", c.surface)])

        style.configure("TEntry", padding=(10, 7), foreground=c.text, insertcolor=c.text, background=c.bg,
                        selectbackground=c.primary_soft, selectforeground=c.text)
        style.map("TEntry", foreground=[("disabled", c.disabled_fg)])
        style.configure("TCombobox", padding=(10, 6), foreground=c.text, background=c.bg, arrowsize=0,
                        selectbackground=c.surface, selectforeground=c.text)
        style.map("TCombobox", foreground=[("disabled", c.disabled_fg)],
                  selectbackground=[("readonly", c.surface)], selectforeground=[("readonly", c.text)])
        for name, background in (("TCheckbutton", c.bg), ("Choice.TCheckbutton", c.bg),
                                 ("TRadiobutton", c.bg), ("Choice.TRadiobutton", c.bg)):
            style.configure(name, background=background, foreground=c.text, padding=(2, 4), font=body,
                            focuscolor=c.focus)
            style.map(name, background=[("active", background)], foreground=[("disabled", c.disabled_fg)])

        style.configure("Treeview", background=c.surface, fieldbackground=c.surface, foreground=c.text,
                        rowheight=30, borderwidth=1, bordercolor=c.border, lightcolor=c.surface,
                        darkcolor=c.surface, font=body)
        style.map("Treeview", background=[("selected", c.primary_soft)], foreground=[("selected", c.text)])
        style.configure("Treeview.Heading", background=c.surface_alt, foreground=c.text_muted,
                        font=th.TYPE_LABEL, relief="flat", padding=(8, 6), bordercolor=c.border,
                        lightcolor=c.surface_alt, darkcolor=c.surface_alt)
        style.map("Treeview.Heading", background=[("active", c.primary_soft)])
        for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            style.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            style.configure(f"{orient}.TScrollbar", background=c.scrollbar, troughcolor=c.bg, bordercolor=c.bg,
                            lightcolor=c.scrollbar, darkcolor=c.scrollbar, arrowcolor=c.text_muted, gripcount=0,
                            arrowsize=10)
            style.map(f"{orient}.TScrollbar", background=[("active", c.input_border)],
                      lightcolor=[("active", c.input_border)], darkcolor=[("active", c.input_border)])
        style.configure("Horizontal.TProgressbar", background=c.primary, troughcolor=c.surface_alt,
                        bordercolor=c.surface_alt, lightcolor=c.primary, darkcolor=c.primary, thickness=8)
        style.configure("TSeparator", background=c.border)
        for option, value in (("background", c.surface), ("foreground", c.text),
                              ("selectBackground", c.primary_soft), ("selectForeground", c.text),
                              ("font", "{Segoe UI} 10")):
            self.root.option_add(f"*TCombobox*Listbox.{option}", value)

    # ---- navigation and global actions ----------------------------------------------------------------
    def show_screen(self, name: str) -> None:
        if name not in self.screens:
            return
        if self.screen == "editor" and name != "editor":
            if self.controller.editor_dirty:
                self.screens["editor"].go_back()      # never leave unsaved work behind silently
                if self.controller.editor_dirty:
                    return                            # the question was cancelled: stay where we are
            self.controller.close_editor()            # nothing unsaved: do not keep a macro open behind us
        if name in LIBRARY_SCREENS:
            self.controller.set_library_kind(LIBRARY_SCREENS[name])
        self.screen = name
        # Only the page being looked at is in the layout. Raising it over the others (as before) kept all seven
        # pages laid out, so every resize -- maximising on a 3440 x 1440 display, say -- laid out all of them.
        for other, screen in self.screens.items():
            if other != name:
                screen.frame.grid_remove()
        self.screens[name].frame.grid()
        self._paint_nav()
        self.render()

    def _active_nav(self) -> str:
        if self.screen == "editor":
            return "scripts" if self.controller.editing_v2 else "macros"
        if self.screen == "diagnostics":
            return "settings"
        return self.screen

    def _paint_nav(self) -> None:
        active = self._active_nav()
        for name, (button, glyph) in self.nav_buttons.items():
            on = name == active
            button.configure(style="NavActive.TButton" if on else "Nav.TButton",
                             image=self.icon(glyph, th.ACTIVE.sidebar_icon_active if on else th.ACTIVE.sidebar_icon,
                                             pad=10))

    def _choose_target(self) -> None:
        """The one target that recording, reading text and the screen pickers use. Explicit, verified, never all:
        the list is refreshed first, and nothing is chosen unless the person chooses it."""
        c = self.controller
        if c.busy:
            return
        c.refresh()
        current = (c.selected.ref,) if c.selected is not None and c.target_ready else ()
        chosen = dialogs.ChooseTargets(self, c.entries, current, single=True).show()
        if chosen:
            c.select(chosen[0])
        self.render()

    def _toggle_diagnostics(self, _event=None) -> str:
        self.show_screen("macros" if self.screen == "diagnostics" else "diagnostics")
        return "break"

    def _key_nav(self, name: str) -> str:
        self.show_screen(name)
        return "break"

    def _on_refresh(self) -> None:
        self.controller.refresh()
        self.render()

    def _on_estop(self) -> None:
        """Only ever stops. Pressed again while a stop is set, it keeps (re-asserts) the stop -- it never clears it;
        that is `_on_reset_stop`, a different button."""
        self.controller.emergency_stop()
        self.render()

    def _on_reset_stop(self) -> None:
        """The deliberate reset. The controller refuses it, with the reason, while anything is still ending."""
        self.controller.clear_emergency_stop()
        self.render()

    def _key_estop(self, event=None) -> str | None:
        """Esc is the Emergency Stop while something that can send input is under way (a recording, a playback, a
        run) and this window is the one in front. Only in the main window -- in a dialog, Esc still just closes the
        dialog -- and it never CLEARS a stop: that stays a deliberate click on the button."""
        try:
            if event is not None and event.widget.winfo_toplevel() is not self.root:
                return None
        except (AttributeError, tk.TclError):         # a widget Tk names only by its path (a combobox list)
            return None
        if not self.controller.busy or self.controller.emergency_active:
            return None
        self.controller.emergency_stop()
        self.render()
        return "break"

    def _key_save(self, _event=None) -> str:
        if self.screen == "editor":
            self.screens["editor"].save()
        return "break"

    def _key_undo(self, _event=None) -> str:
        if self.screen == "editor":
            self.controller.editor_undo()
            self.render()
        return "break"

    def _key_redo(self, _event=None) -> str:
        if self.screen == "editor":
            self.controller.editor_redo()
            self.render()
        return "break"

    def _key_rename(self, _event=None) -> str:
        if self.screen in LIBRARY_SCREENS and self.controller.selected_key:
            self.screens[self.screen]._rename(self.controller.selected_key)
        return "break"

    def _key_duplicate(self, _event=None) -> str:
        if self.screen in LIBRARY_SCREENS and self.controller.selected_key:
            self.screens[self.screen]._duplicate(self.controller.selected_key)
        elif self.screen == "editor" and self.controller.editing_v2:
            self.screens["editor"].duplicate_step()
        return "break"

    # ---- theme and language ----------------------------------------------------------------------------
    def _save_prefs(self) -> None:
        self.prefs.save({"theme": th.ACTIVE.id, "language": self.t.language,
                         "concurrency": str(self.controller.concurrency)})

    def apply_concurrency(self, value: object) -> None:
        self.controller.set_concurrency(value)
        self._save_prefs()
        self.render()

    def apply_theme(self, theme_id: str) -> None:
        if theme_id == th.ACTIVE.id:
            return
        th.set_theme(theme_id)
        self._save_prefs()
        self.toaster.dismiss_all()                    # their colours belong to the previous theme
        self._style()
        alive = []
        for hook in self._theme_hooks:
            try:
                hook()
            except tk.TclError:
                continue                              # its widget was rebuilt (a list row, an empty state): drop it
            alive.append(hook)
        self._theme_hooks = alive
        self.render()

    def on_language_chosen(self, _event=None) -> None:
        reverse = {name: code for code, name in LANGUAGE_NAMES.items()}
        self.apply_language(reverse.get(self.language_var.get(), self.t.language))

    def apply_language(self, language: str) -> None:
        self.t.set(language)
        self._save_prefs()
        self.language_var.set(LANGUAGE_NAMES[language])
        self.root.title(self.t("app_title"))
        for widget, key in self._texts:
            try:
                widget.configure(text=self.t(key))
            except tk.TclError:
                pass                                   # a widget that has since been destroyed
        for screen in self.screens.values():
            for attribute in ("_signature", "_list_signature", "_nav_signature"):
                if hasattr(screen, attribute):
                    setattr(screen, attribute, None)   # rebuild rows so their text follows the language too
            relabel = getattr(screen, "relabel", None)
            if callable(relabel):
                relabel()
        self.render()

    # ---- screenshot preview (a worker thread, applied on the UI thread) ---------------------------------
    @staticmethod
    def _run_in_thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True, name="preview").start()

    def _start_preview(self) -> None:
        if self._preview_busy:
            return
        job = self.controller.begin_preview()
        if job is None:
            return

        def work() -> None:
            try:
                outcome = job()
            except BaseException as exc:
                outcome = exc
            self._results.put((job, outcome))

        self._preview_busy = True
        try:
            self._preview_runner(work)
        except BaseException:
            self._preview_busy = False
            raise

    def run_job(self, job: Callable[[], object], done: Callable[[object], None]) -> None:
        """Run `job` on a worker thread (a screenshot, decoding it, asking a condition) and call `done` with what
        it returned or raised -- always on the UI thread, from the render loop, never from the worker. If no worker
        can be started, `done` gets that error the same way, and the job is abandoned: what it registered (with the
        emergency stop and with closing) is released, so nothing stays "under way" and closing waits for nothing."""
        def work() -> None:
            try:
                outcome = job()
            except BaseException as exc:
                outcome = exc
            self._jobs.put((done, outcome))

        try:
            self._preview_runner(work)
        except Exception as exc:
            abandon = getattr(job, "abandon", None)
            if callable(abandon):
                abandon()
            self._jobs.put((done, exc))

    def _drain_jobs(self) -> None:
        while True:
            try:
                done, outcome = self._jobs.get_nowait()
            except queue.Empty:
                return
            done(outcome)

    # ---- the render loop ---------------------------------------------------------------------------------
    def _tick(self) -> None:
        try:
            self.controller.poll()
            discarded = self.controller.preview_discarded
            while True:
                try:
                    job, outcome = self._results.get_nowait()
                except queue.Empty:
                    break
                self._preview_busy = False
                self.controller.finish_preview(outcome, job)
            self._drain_jobs()
            if (self.controller.preview_discarded != discarded and not self._preview_busy
                    and self.controller.can_screenshot() and not self.controller.busy):
                # that result belonged to a target that is no longer chosen: capture the CURRENT one instead,
                # so a stale image is never what the user is left looking at.
                self._start_preview()
            busy = self.controller.busy
            if self._was_busy and not busy and self.controller.can_screenshot():
                self._start_preview()
            self._was_busy = busy
            if self.controller.version != self._rendered or busy:
                self.render()
        finally:
            if self._after is not None:
                self._after = self.root.after(POLL_MS, self._tick)

    def render(self) -> None:
        controller = self.controller
        self._rendered = controller.version
        self._render_stop_bar()                           # first: no screen's own rendering may keep the stop away
        self._render_header()
        self.screens[self.screen].render()
        self.lbl_status.configure(text=self.status_text())
        self._notify()
        self._ring()

    def _ring(self) -> None:
        """One sound for each time the controller asks for the person's attention (a delayed run about to begin):
        heard even when this window is behind the program the run will act on. Nothing pops up -- a window of
        Olive's own could take the keyboard focus from that program just before the run acts on it."""
        if self.controller.attention == self._rung:
            return
        self._rung = self.controller.attention
        try:
            self.root.bell()
        except tk.TclError:
            pass

    def _notify(self) -> None:
        """A new sentence from the controller becomes a notification -- except the emergency stop, which is the
        stop bar's to say, and the routine ones the page already shows."""
        status = self.controller.status
        if status is self._shown_status:
            return
        self._shown_status = status
        if status.category == "emergency_stop":
            self.toaster.dismiss_all()                # what they said was about work the stop has ended
            return
        if (status.level, status.category) in QUIET:
            return
        self.toaster.show(status.level, self.status_text(status), key=status.category)

    def _render_stop_bar(self) -> None:
        """Hidden when idle with no stop; the stop button whenever it is shown (while work is under way, while it
        is ending and once it has, where pressing it only keeps the stop); and, once a stop is set, the separate
        reset to its left -- offered while work is still ending, but disabled until it has."""
        phase = self.controller.stop_phase
        if phase == STOP_IDLE:
            self.stop_bar.grid_remove()
            return
        self.stop_bar.grid()
        self.lbl_stop.configure(text=self.t(f"stop_bar_{phase}"))
        # The stop stays where it was even once everything has ended: pressing it then only keeps the stop.
        if self.controller.emergency_active:
            self.btn_reset_stop.grid()
            self.btn_reset_stop.state(["!disabled"] if phase == STOP_STOPPED else ["disabled"])
        else:
            self.btn_reset_stop.grid_remove()

    def status_text(self, status: Status | None = None) -> str:
        """The status bar sentence -- or `status`, another sentence of the controller -- in the current language.
        A message with no key (a provider's own technical wording) is shown as it came, rather than being hidden."""
        status = status if status is not None else self.controller.status
        if status.key:
            try:
                return self.t(status.key).format(**status.values)
            except (KeyError, IndexError, ValueError):
                return status.text
        return status.text

    def _render_header(self) -> None:
        c, t = self.controller, self.t
        title, subtitle = self._titles()
        self.lbl_title.configure(text=title)
        self.lbl_where.configure(text=subtitle)
        if c.emergency_active:
            text, colour = t("system_stopped"), th.ACTIVE.error
        elif c.recording_active:
            text, colour = t("system_recording"), th.ACTIVE.error
        elif c.run_active:
            running = next((r for r in c.run_results if r.state is RunState.RUNNING), None)
            text = t("system_running").format(
                name=fmt.clip(c.run_macro_name, TARGET_CHARS),
                done=sum(1 for r in c.run_results if r.state not in (RunState.WAITING, RunState.RUNNING)),
                total=len(c.run_results)) if running else t("system_running_generic")
            colour = th.ACTIVE.warning
        elif c.reading_text:
            text, colour = t("system_reading"), th.ACTIVE.info
        else:
            text, colour = t("system_ready"), th.ACTIVE.success
        self.lbl_system.configure(text=text, image=self.icon("dot", colour, pad=4))
        if c.target_ready and c.selected is not None:
            name = fmt.clip(c.selected.title or c.selected.process or c.target.target_id, TARGET_CHARS)
            text, colour = t("header_target").format(name=name), th.ACTIVE.success
        elif c.target_status in (TargetStatus.INVALID, TargetStatus.STALE):
            text, colour = t("header_target_stale"), th.ACTIVE.error
        else:
            text, colour = t("header_no_target"), th.ACTIVE.neutral
        self.btn_target.configure(text=text, image=self.icon("target", colour, pad=6))
        self.btn_target.state(["disabled"] if c.busy else ["!disabled"])

    def _titles(self) -> tuple[str, str]:
        c, t = self.controller, self.t
        if self.screen == "editor":
            return t("editor_title_script" if c.editing_v2 else "editor_title_macro"), self._breadcrumb()
        title = t(TITLE[self.screen])
        crumb = self._breadcrumb()
        return title, crumb if crumb != title else t(SUBTITLE[self.screen])

    def _breadcrumb(self) -> str:
        """Where the user is, so going back is never a guess: the section, then the folder or view, then the
        chosen macro or script. It names places in the application, never a file path."""
        if self.screen in ("history", "text", "settings", "diagnostics"):
            return self.t(TITLE[self.screen])
        scripts = self.screen == "scripts" or (self.screen == "editor" and self.controller.editing_v2)
        parts = [self.t("nav_scripts" if scripts else "nav_macros")]
        flt = self.controller.filter
        if flt.scope is Scope.FOLDER and flt.folder:
            parts.append(fmt.clip(flt.folder, CRUMB_CHARS))
        elif flt.scope is not Scope.ALL:
            parts.append(self.t({Scope.FAVORITES: "scope_favorites",
                                 Scope.RECENT: "scope_recent"}[flt.scope]))
        if self.screen == "editor" and self.controller.editor is not None:
            parts.append(fmt.clip(self.controller.editor.document.name or self.t("unnamed"), CRUMB_CHARS))
        else:
            info = self.controller.shown_macro()
            if info is not None:
                parts.append(fmt.clip(info.name, CRUMB_CHARS))
        return "  /  ".join(parts)

    # ---- lifecycle -------------------------------------------------------------------------------------
    def close(self) -> None:
        self.controller.shutdown()
        if self._after is not None:
            self.root.after_cancel(self._after)
            self._after = None
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
