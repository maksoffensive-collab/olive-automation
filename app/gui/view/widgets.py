"""Shared view pieces: icons, cards, chips, tooltips, scroll areas, menus and empty states.

Every widget here takes a `ui` object (the `App`) and uses only these four things from it, so a screen never
reaches into the application's internals: `ui.t(key)` to translate, `ui.on_theme(fn)` to be re-coloured when the
theme changes, `ui.label(widget, key)` to be re-translated when the language changes, and `ui.root` as the Tk
master. Colours are always read from `theme.ACTIVE` at call time, never captured.

Icons are hand-drawn monochrome glyphs rasterised into transparent `PhotoImage`s: the project ships no icon font
and no image assets, and the redesign brief rules out emoji and stray Unicode symbols as UI icons. Unset pixels
stay transparent, so one glyph works on any themed background.
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Callable

from app.gui import theme as th

ICON_SIZE = 16


def _points(kind: str, size: int) -> set[tuple[int, int]]:
    """ON pixels of one glyph on a `size` x `size` grid, built from the same few primitives at one weight."""
    pts: set[tuple[int, int]] = set()
    s = size
    mid = (s - 1) / 2

    def rect(x0, y0, x1, y1, *, fill=True):
        for x in range(int(x0), int(x1) + 1):
            for y in range(int(y0), int(y1) + 1):
                if fill or x in (int(x0), int(x1)) or y in (int(y0), int(y1)):
                    pts.add((x, y))

    def ring(cx, cy, r, width=1.4, fill=False):
        for x in range(s):
            for y in range(s):
                d = math.hypot(x - cx, y - cy)
                if (d <= r) if fill else (r - width <= d <= r):
                    pts.add((x, y))

    def line(x0, y0, x1, y1, width=2):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(int(steps) + 1):
            x = round(x0 + (x1 - x0) * i / steps)
            y = round(y0 + (y1 - y0) * i / steps)
            for dx in range(width):
                for dy in range(width):
                    pts.add((x + dx, y + dy))

    def star(cx, cy, outer, inner, fill):
        corners = []
        for i in range(10):
            angle = -math.pi / 2 + i * math.pi / 5
            radius = outer if i % 2 == 0 else inner
            corners.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        if fill:
            for x in range(s):
                for y in range(s):
                    if _inside(corners, x, y):
                        pts.add((x, y))
        else:
            for a, b in zip(corners, corners[1:] + corners[:1]):
                line(a[0], a[1], b[0], b[1], width=1)

    if kind == "macros":                       # stacked layers
        for i, y0 in enumerate((3, 7, 11)):
            rect(2 + i, y0, s - 3 - i, y0 + 1)
    elif kind == "text":                       # a capital T over a line of text
        rect(3, 2, s - 4, 3)
        rect(mid - 0.5, 2, mid + 0.5, s - 6)
        rect(2, s - 3, s - 3, s - 3)
    elif kind == "history":                    # a clock
        ring(mid, mid, mid - 1.5)
        line(mid, mid - 4, mid, mid, width=1)
        line(mid, mid, mid + 3, mid, width=1)
    elif kind == "settings":                   # a gear
        ring(mid, mid, 5, width=2)
        ring(mid, mid, 2, fill=True)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            rect(round(mid + 6.4 * math.cos(rad)) - 1, round(mid + 6.4 * math.sin(rad)) - 1,
                 round(mid + 6.4 * math.cos(rad)) + 1, round(mid + 6.4 * math.sin(rad)) + 1)
    elif kind == "play":                       # a triangle pointing right
        for x in range(4, s - 4):
            half = (s - 5 - x) * 0.75
            rect(x, mid - half, x, mid + half)
    elif kind == "record":
        ring(mid, mid, 5, fill=True)
    elif kind == "stop":
        rect(4, 4, s - 5, s - 5)
    elif kind == "star_on":
        star(mid, mid, mid - 0.5, (mid - 0.5) * 0.45, True)
    elif kind == "star_off":
        star(mid, mid, mid - 0.5, (mid - 0.5) * 0.45, False)
    elif kind == "search":                     # a magnifier
        ring(6.5, 6.5, 4.5)
        line(9.5, 9.5, s - 3, s - 3, width=2)
    elif kind == "folder":
        rect(2, 5, s - 3, s - 4, fill=False)
        rect(2, 3, 7, 4)
    elif kind == "plus":
        rect(mid - 0.5, 3, mid + 0.5, s - 4)
        rect(3, mid - 0.5, s - 4, mid + 0.5)
    elif kind == "dots":
        for cx in (3, mid, s - 4):
            ring(cx, mid, 1.2, fill=True)
    elif kind == "up":
        line(mid, 4, mid, s - 4, width=2)
        line(mid - 4, mid - 1, mid, 4, width=2)
        line(mid + 4, mid - 1, mid, 4, width=2)
    elif kind == "down":
        line(mid, 4, mid, s - 4, width=2)
        line(mid - 4, mid + 1, mid, s - 4, width=2)
        line(mid + 4, mid + 1, mid, s - 4, width=2)
    elif kind == "back":
        line(3, mid, s - 4, mid, width=2)
        line(3, mid, 8, mid - 5, width=2)
        line(3, mid, 8, mid + 5, width=2)
    elif kind == "check":
        line(3, mid, 6, s - 5, width=2)
        line(6, s - 5, s - 4, 4, width=2)
    elif kind == "warning":
        line(mid, 3, 3, s - 4, width=2)
        line(mid, 3, s - 4, s - 4, width=2)
        line(3, s - 4, s - 4, s - 4, width=2)
    elif kind == "target":                     # a monitor
        rect(2, 2, s - 3, s - 6, fill=False)
        rect(mid - 2, s - 5, mid + 2, s - 4)
    elif kind == "pause":                      # two bars
        rect(5, 4, 6, s - 5)
        rect(s - 7, 4, s - 6, s - 5)
    elif kind == "undo":
        line(3, mid, 9, mid, width=2)
        line(3, mid, 7, mid - 4, width=2)
        line(3, mid, 7, mid + 4, width=2)
        line(9, mid, 11, mid + 4, width=2)
    elif kind == "redo":
        line(s - 4, mid, s - 10, mid, width=2)
        line(s - 4, mid, s - 8, mid - 4, width=2)
        line(s - 4, mid, s - 8, mid + 4, width=2)
        line(s - 10, mid, s - 12, mid + 4, width=2)
    elif kind == "flow":                       # three connected blocks: a script
        rect(3, 1, s - 4, 4, fill=False)
        rect(mid - 0.5, 5, mid + 0.5, 6)
        rect(3, 7, s - 4, 9, fill=False)
        rect(mid - 0.5, 10, mid + 0.5, 11)
        rect(3, 12, s - 4, s - 2, fill=False)
    elif kind == "close":                      # a cross
        line(4, 4, s - 5, s - 5, width=2)
        line(s - 5, 4, 4, s - 5, width=2)
    elif kind == "zoom_in":
        ring(6.5, 6.5, 4.5)
        line(9.5, 9.5, s - 3, s - 3, width=2)
        rect(4.5, 6, 8.5, 7)
        rect(6, 4.5, 7, 8.5)
    elif kind == "zoom_out":
        ring(6.5, 6.5, 4.5)
        line(9.5, 9.5, s - 3, s - 3, width=2)
        rect(4.5, 6, 8.5, 7)
    elif kind == "fit":                        # four corners pointing outwards
        for x0, y0, dx, dy in ((2, 2, 1, 1), (s - 3, 2, -1, 1), (2, s - 3, 1, -1), (s - 3, s - 3, -1, -1)):
            line(x0, y0, x0 + 4 * dx, y0, width=1)
            line(x0, y0, x0, y0 + 4 * dy, width=1)
        rect(mid - 2, mid - 2, mid + 2, mid + 2, fill=False)
    elif kind == "list":                       # three lines
        for y0 in (4, 8, 12):
            rect(2, y0, 3, y0 + 1)
            rect(5, y0, s - 3, y0 + 1)
    elif kind == "copy":
        rect(5, 2, s - 3, s - 6, fill=False)
        rect(2, 5, s - 6, s - 3, fill=False)
    elif kind == "camera":                     # take a screenshot
        rect(2, 5, s - 3, s - 3, fill=False)
        rect(5, 3, 9, 4)
        ring(mid, mid + 1.5, 3)
    elif kind == "info":
        ring(mid, mid, mid - 1)
        rect(mid - 0.5, 4, mid + 0.5, 5)
        rect(mid - 0.5, 7, mid + 0.5, s - 5)
    elif kind == "error":
        ring(mid, mid, mid - 1)
        line(5, 5, s - 6, s - 6, width=1)
        line(s - 6, 5, 5, s - 6, width=1)
    elif kind == "dot":                        # a status light
        ring(mid, mid, 3.6, fill=True)
    elif kind == "chevron_down":
        line(4, 6, mid, s - 6, width=2)
        line(s - 5, 6, mid, s - 6, width=2)
    return {(x, y) for x, y in pts if 0 <= x < s and 0 <= y < s}


def _inside(corners, x, y) -> bool:
    """Even-odd test, so a star can be filled without a polygon rasteriser."""
    inside = False
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0 + 1e-9) + x0:
            inside = not inside
    return inside


class Icons:
    """One cache per window. Images are tied to their Tk interpreter, so the root is always explicit."""

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._cache: dict[tuple[str, str, int], tk.PhotoImage] = {}

    def get(self, kind: str, color: str, size: int = ICON_SIZE, *, pad: int = 0) -> tk.PhotoImage:
        """The glyph in `color`. `pad` adds transparent room to its right, which is how a ttk button keeps a gap
        between its icon and its text without padding the text itself (the text stays exactly the translation)."""
        key = (kind, color, size, pad)
        image = self._cache.get(key)
        if image is None:
            image = tk.PhotoImage(master=self._root, width=size + pad, height=size)
            for x, y in _points(kind, size):
                image.put(color, (x, y))
            self._cache[key] = image
        return image


# ---- small behaviours -------------------------------------------------------------------------------------
class Tooltip:
    """Explains an icon-only control (the redesign brief requires one for every wordless button). The text is a
    callable so it can be re-read in the current language, and re-state a disabled button's reason."""

    def __init__(self, ui, widget: tk.Widget, text: Callable[[], str]) -> None:
        self._ui, self._widget, self._text, self._tip = ui, widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        text = (self._text() or "").strip()
        if self._tip is not None or not text:
            return
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.configure(background=th.ACTIVE.border_strong)
        label = tk.Label(self._tip, text=text, justify="left", background=th.ACTIVE.overlay,
                         foreground=th.ACTIVE.text, font=th.TYPE_CAPTION, padx=10, pady=6, wraplength=300)
        label.pack(padx=1, pady=1)
        x = self._widget.winfo_rootx() + 12
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        self._tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def popup(ui, widget: tk.Widget, items: list, *, at: tuple[int, int] | None = None) -> None:
    """A "..." menu. `items` are (label key, command, enabled) triples, or None for a separator. Built fresh on
    every click so labels follow the language and entries follow the current state. It opens under `widget`, or at
    the screen point `at` (where the pointer is)."""
    menu = tk.Menu(widget, tearoff=0, background=th.ACTIVE.overlay, foreground=th.ACTIVE.text,
                   activebackground=th.ACTIVE.primary_soft, activeforeground=th.ACTIVE.text,
                   disabledforeground=th.ACTIVE.disabled_fg, borderwidth=1, relief="solid", font=th.TYPE_BODY)
    for item in items:
        if item is None:
            menu.add_separator()
            continue
        key, command, enabled = item
        menu.add_command(label=ui.t(key), command=command, state="normal" if enabled else "disabled")
    try:
        x, y = at if at is not None else (widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


def scroll_area(ui, parent: tk.Widget, *, background: Callable[[], str] | None = None, height: int = 80):
    """A vertically scrollable region. Returns (outer, inner): grid `outer`, build into `inner`. `height` is the
    least it asks for; it grows with its cell."""
    colour = background or (lambda: th.ACTIVE.bg)
    outer = ttk.Frame(parent)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)
    # A small request (Tk's default canvas asks for 10 x 7 cm): the area grows with its cell, and never makes
    # the window wider than it needs to be.
    canvas = tk.Canvas(outer, highlightthickness=0, background=colour(), width=120, height=height)
    canvas.grid(row=0, column=0, sticky="nsew")
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    bar.grid(row=0, column=1, sticky="ns")
    canvas.configure(yscrollcommand=autohide(bar))
    inner = ttk.Frame(canvas, style=_flat_style(colour()))
    window = canvas.create_window((0, 0), window=inner, anchor="nw")

    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
    canvas.bind("<Enter>", lambda _e: canvas.bind_all(
        "<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
    ui.on_theme(lambda: canvas.configure(background=colour()))
    return outer, inner


def _flat_style(colour: str) -> str:
    """The flat frame style whose background is `colour` (a scroll area's inside must not be a second card)."""
    c = th.ACTIVE
    return {c.bg: "Plain.TFrame", c.surface: "Surface.TFrame", c.sidebar_bg: "Sidebar.TFrame",
            c.surface_alt: "Inset.TFrame"}.get(colour, "Plain.TFrame")


def autohide(bar: ttk.Scrollbar) -> Callable[[str, str], None]:
    """A scroll command that also shows the bar only while there is something to scroll: an always-visible bar
    over content that fits says "there is more" when there is not. The bar must already be gridded once."""
    def set_view(first, last) -> None:
        bar.set(first, last)
        try:
            if float(first) <= 0.0 and float(last) >= 1.0:
                bar.grid_remove()
            else:
                bar.grid()
        except tk.TclError:
            pass                                     # the bar is being destroyed with its window
    return set_view


def wrap_to(container: tk.Widget, label: ttk.Label, *, others=(), reserve: int = 0, minimum: int = 240) -> None:
    """Keep `label`'s wrap width equal to the room `container` actually leaves it (its width minus the other
    widgets beside it and `reserve`), so a long sentence wraps instead of asking for a wider window. The label
    starts at `minimum`, which is all it asks for when the window is at its smallest."""
    label.configure(wraplength=minimum)

    def update(_event=None) -> None:
        try:
            room = container.winfo_width() - sum(o.winfo_reqwidth() for o in others) - reserve
            room = max(minimum, room)
            if abs(int(str(label.cget("wraplength")) or 0) - room) > 4:
                label.configure(wraplength=room)
        except tk.TclError:
            pass
    container.bind("<Configure>", update, add="+")


ELLIPSIS = "…"


def fit_text(widget: tk.Widget, text: str, room: int) -> str:
    """`text` as it fits in `room` pixels of `widget`'s font: whole, or cut at a letter and ended with an
    ellipsis -- so a cut is visible as a cut, never a word sliced off at an edge."""
    font = widget.cget("font")

    def width(s: str) -> int:
        return int(widget.tk.call("font", "measure", font, s))
    if room <= 0 or width(text) <= room:
        return text
    low, high = 0, len(text)
    while low < high:                                   # the longest prefix that still fits with the ellipsis
        middle = (low + high + 1) // 2
        if width(text[:middle].rstrip() + ELLIPSIS) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + ELLIPSIS


def elide(label: tk.Label, text: str) -> None:
    """Show `text` in `label`, shortened with an ellipsis whenever the room the layout gives the label is too
    narrow for it (re-fitted as that room changes). The label must be laid out with fill: its room comes from the
    layout, not from its text."""
    label.configure(text=text)

    def fit(_event=None) -> None:
        try:
            room = label.winfo_width() - 2 * int(str(label.cget("padx")) or 0) - 2
            if room > 1:                                # laid out: before that, the whole text
                shown = fit_text(label, text, room)
                if shown != label.cget("text"):
                    label.configure(text=shown)
        except tk.TclError:
            pass
    label.bind("<Configure>", fit, add="+")


def card(ui, parent: tk.Widget, *, padding: int | tuple = 16) -> ttk.Frame:
    """A rounded white card with a hairline edge (see `skin`): the one container used for grouped content."""
    return ttk.Frame(parent, style="Panel.TFrame", padding=padding)


def section(ui, parent: tk.Widget, key: str) -> ttk.Label:
    """An upper-case section heading inside a card ("ЦЕЛИ ЗАПУСКА")."""
    return ui.label(ttk.Label(parent, style="Head.TLabel"), key)


def empty_state(ui, parent: tk.Widget, title_key: str, hint_key: str, buttons=(), *, icon: str = "",
                surface: bool = False, wrap: int = 420) -> ttk.Frame:
    """What an empty area says: what happened, why, and what to do next -- never a blank rectangle. `surface`:
    it sits on a card rather than on the page."""
    frame = ttk.Frame(parent, style="Surface.TFrame" if surface else "Plain.TFrame")
    if icon:
        glyph = ttk.Label(frame, style="PanelMuted.TLabel" if surface else "Muted.TLabel")

        def paint() -> None:
            glyph.configure(image=ui.icon(icon, th.ACTIVE.primary, 32))
        ui.on_theme(paint)
        paint()
        glyph.pack(pady=(0, 10))
    ui.label(ttk.Label(frame, style="PanelEmptyTitle.TLabel" if surface else "EmptyTitle.TLabel",
                       anchor="center", justify="center", wraplength=wrap), title_key).pack(pady=(0, 6))
    ui.label(ttk.Label(frame, style="PanelMuted.TLabel" if surface else "Muted.TLabel", anchor="center",
                       justify="center", wraplength=wrap), hint_key).pack()
    if buttons:
        row = ttk.Frame(frame, style="Plain.TFrame")
        row.pack(pady=(14, 0))
        for key, command, style in buttons:
            ui.label(ttk.Button(row, style=style, command=command), key).pack(side="left", padx=4)
    return frame


def hairline(parent: tk.Widget) -> ttk.Separator:
    return ttk.Separator(parent, orient="horizontal")


# ---- transient notifications ------------------------------------------------------------------------------------
TOAST_MS = {"ok": 4500, "info": 4500, "warn": 8000}   # an error stays until it is closed
TOAST_MS_PER_CHAR = 45                                # a longer sentence stays longer: it must be readable
TOAST_MS_MAX = 15000
TOAST_LIMIT = 2
TOAST_ICON = {"ok": "check", "info": "info", "warn": "warning", "error": "error"}


def toast_duration(level: str, text: str) -> int | None:
    """How long a notification stays: None (until closed) for an error; otherwise long enough to read it."""
    base = TOAST_MS.get(level)
    if base is None:
        return None
    return min(TOAST_MS_MAX, max(base, len(text) * TOAST_MS_PER_CHAR))


class Toast:
    """One notification card: a coloured edge and icon for its level, the sentence, and a close button."""

    def __init__(self, toaster: "Toaster", level: str, text: str, key: str = "") -> None:
        self.toaster, self.level, self.text, self.key = toaster, level, text, key
        self.duration_ms = toast_duration(level, text)
        ui = toaster.ui
        c = th.ACTIVE
        self.frame = ttk.Frame(ui.root, style="Toast.TFrame", padding=(10, 10, 6, 10))
        self.frame.columnconfigure(2, weight=1)
        tk.Frame(self.frame, width=4, background=th.level_colour(level)).grid(row=0, column=0, sticky="ns",
                                                                             padx=(2, 10))
        ttk.Label(self.frame, style="Toast.TLabel",
                  image=ui.icon(TOAST_ICON.get(level, "info"), th.level_colour(level))).grid(row=0, column=1,
                                                                                         sticky="n", pady=2)
        self.label = ttk.Label(self.frame, text=text, style="Toast.TLabel", wraplength=440, justify="left")
        self.label.grid(row=0, column=2, sticky="w", padx=(8, 8))
        close = ttk.Button(self.frame, style="Toast.Icon.TButton", command=self.dismiss,
                           image=ui.icon("close", c.text_muted))
        close.grid(row=0, column=3, sticky="ne")
        Tooltip(ui, close, lambda: ui.t("tip_close_notification"))
        self._after: str | None = None
        self.restart()

    def restart(self) -> None:
        """(Re)start the countdown -- a repeated message keeps its card and gets its full time again."""
        root = self.toaster.ui.root
        if self._after is not None:
            root.after_cancel(self._after)
            self._after = None
        if self.duration_ms is not None:
            self._after = root.after(self.duration_ms, self.dismiss)

    def dismiss(self) -> None:
        if self._after is not None:
            try:
                self.toaster.ui.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        if self in self.toaster.toasts:
            self.toaster.toasts.remove(self)
        try:
            self.frame.destroy()
        except tk.TclError:
            pass
        self.toaster.restack()


class Toaster:
    """Transient notifications for routine results, stacked at the bottom centre of the workspace -- above the
    content, never over the navigation, the header or the stop bar. Success and information leave by themselves
    once they could be read; a warning stays longer; an error stays until it is closed. The same sentence twice
    keeps one card. The emergency stop is never a toast: it has its own bar."""

    def __init__(self, ui, anchor: tk.Widget) -> None:
        self.ui, self.anchor = ui, anchor
        self.toasts: list[Toast] = []

    def show(self, level: str, text: str, *, key: str = "") -> Toast | None:
        """Show a notification. `key` names what it is about (the controller's category): a newer sentence about
        the same thing REPLACES the older one -- "Reading the text..." becomes "Read 3 lines" -- instead of piling
        up next to it."""
        text = (text or "").strip()
        if not text:
            return None
        for toast in list(self.toasts):
            if toast.level == level and toast.text == text:
                toast.restart()
                self.restack()
                return toast
            if key and toast.key == key:
                toast.dismiss()
        toast = Toast(self, level, text, key)
        self.toasts.append(toast)
        while len(self.toasts) > TOAST_LIMIT:
            self.toasts[0].dismiss()
        self.restack()
        return toast

    def dismiss_all(self) -> None:
        for toast in list(self.toasts):
            toast.dismiss()

    def restack(self) -> None:
        offset = -18
        for toast in reversed(self.toasts):
            try:
                toast.frame.update_idletasks()
                toast.frame.place(in_=self.anchor, relx=0.5, rely=1.0, y=offset, anchor="s")
                toast.frame.lift()
                offset -= toast.frame.winfo_reqheight() + 8
            except tk.TclError:
                pass
