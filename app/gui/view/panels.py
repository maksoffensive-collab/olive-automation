"""Settings and Diagnostics.

Settings holds the choices a person makes about the application itself: language, appearance, and what the
current provider is. Diagnostics is the technical screen -- provider, target identity, capabilities, the live
screenshot preview and the event log -- kept off the everyday path (Ctrl+Shift+D) so that ordinary use never
turns into reading a log, while an advanced user is still one shortcut away from everything.
"""
from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk

from app.gui import theme as th
from app.gui.controller import CONCURRENCY_CHOICES, TargetStatus
from app.gui.i18n import LANGUAGE_NAMES, LANGUAGES
from app.gui.view import widgets as w
from app.providers.target import Capability

CAPABILITIES = (Capability.SCREENSHOT, Capability.TAP, Capability.DOUBLE_TAP, Capability.SECONDARY_TAP,
                Capability.POINTER_MOVE, Capability.POINTER_BUTTON, Capability.KEY, Capability.TEXT,
                Capability.RECORD)


class SettingsScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(28, 4, 28, 20))
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        # the page title is the header's
        outer, body = w.scroll_area(self.ui, self.frame)
        outer.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        self._theme_cards: list = []
        self._build_general(body)
        self._build_appearance(body)
        self._build_running(body)
        self._build_provider(body)
        self._build_advanced(body)

    def _card(self, body, row: int, header: str) -> ttk.Frame:
        card = w.card(self.ui, body, padding=(20, 16))
        card.grid(row=row, column=0, sticky="ew", pady=(0, 14), padx=(0, 6))
        card.columnconfigure(1, weight=1)
        self.ui.label(ttk.Label(card, style="Head.TLabel"), header).grid(row=0, column=0, columnspan=2,
                                                                         sticky="w")
        return card

    def _build_general(self, body) -> None:
        card = self._card(body, 0, "settings_general_header")
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), "settings_language").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        box = ttk.Combobox(card, textvariable=self.ui.language_var, state="readonly", width=16,
                           values=[LANGUAGE_NAMES[code] for code in LANGUAGES])
        box.grid(row=1, column=1, sticky="w", pady=(10, 0))
        box.bind("<<ComboboxSelected>>", self.ui.on_language_chosen)
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel", wraplength=620, justify="left"),
                      "settings_note").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_appearance(self, body) -> None:
        card = self._card(body, 1, "settings_appearance_header")
        row = ttk.Frame(card, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 8))
        for index, theme_id in enumerate(th.THEME_ORDER):
            self._theme_card(row, theme_id).grid(row=0, column=index, padx=(0 if index == 0 else 12, 0))
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), "appearance_note").grid(
            row=2, column=0, columnspan=2, sticky="w")

    def _theme_card(self, parent, theme_id: str) -> tk.Frame:
        preview = th.THEMES[theme_id]
        wrap = tk.Frame(parent, highlightthickness=2, cursor="hand2")
        canvas = tk.Canvas(wrap, width=150, height=92, highlightthickness=0, background=preview.bg)
        canvas.pack(padx=10, pady=(10, 8))
        canvas.create_rectangle(0, 0, 150, 16, fill=preview.topbar_bg, outline="")
        canvas.create_rectangle(0, 16, 38, 92, fill=preview.sidebar_bg, outline="")
        canvas.create_rectangle(9, 27, 31, 37, fill=preview.sidebar_active, outline="")
        canvas.create_rectangle(48, 26, 96, 60, fill=preview.surface, outline=preview.border)
        canvas.create_rectangle(102, 26, 142, 60, fill=preview.surface, outline=preview.border)
        canvas.create_rectangle(48, 68, 142, 82, fill=preview.surface, outline=preview.border)
        canvas.create_oval(54, 72, 64, 82, fill=preview.primary, outline="")
        check = canvas.create_text(138, 8, text="✓", fill=preview.primary, font=(th.FONT, 10, "bold"),
                                   state="hidden")
        name = self.ui.label(ttk.Label(wrap, style="PanelBig.TLabel"), f"theme_{theme_id}")
        name.pack(anchor="w", padx=10)
        desc = self.ui.label(ttk.Label(wrap, style="PanelMuted.TLabel"), f"theme_{theme_id}_desc")
        desc.pack(anchor="w", padx=10, pady=(0, 4))
        # Deliberately NOT registered for retranslation: this line belongs to whichever card is chosen, and a
        # blanket "set every registered label to its key" on a language change would light it up on all three.
        active = ttk.Label(wrap, text="", style="PanelMuted.TLabel")
        active.pack(anchor="w", padx=10, pady=(0, 10))

        def refresh() -> None:
            chosen = th.ACTIVE.id == theme_id
            wrap.configure(background=th.ACTIVE.surface,
                           highlightbackground=th.ACTIVE.primary if chosen else th.ACTIVE.border)
            canvas.itemconfigure(check, state="normal" if chosen else "hidden")
            active.configure(text=self.ui.t("theme_active") if chosen else "")

        for widget in (wrap, canvas, name, desc, active):
            widget.bind("<Button-1>", lambda _e, t=theme_id: self.ui.apply_theme(t))
        self.ui.on_theme(refresh)
        self._theme_cards.append(refresh)      # also re-run on every render, so a language change reaches it
        refresh()
        return wrap

    def _build_running(self, body) -> None:
        """The concurrency ceiling. It is a ceiling only: whether two targets may actually overlap is the
        targets' own answer, so this cannot make anything run in parallel that said it must not."""
        card = self._card(body, 2, "settings_running_header")
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), "settings_concurrency").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        self.concurrency_var = tk.StringVar(value=str(self.c.concurrency))
        box = ttk.Combobox(card, textvariable=self.concurrency_var, state="readonly", width=6,
                           values=[str(n) for n in CONCURRENCY_CHOICES])
        box.grid(row=1, column=1, sticky="w", pady=(10, 0))
        box.bind("<<ComboboxSelected>>", lambda _e: self.ui.apply_concurrency(self.concurrency_var.get()))
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel", wraplength=620, justify="left"),
                      "settings_concurrency_note").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_provider(self, body) -> None:
        card = self._card(body, 3, "settings_provider_header")
        self.lbl_provider = ttk.Label(card, text="-", style="PanelBig.TLabel")
        self.lbl_provider.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 4))
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel", wraplength=620, justify="left"),
                      "settings_provider_note").grid(row=2, column=0, columnspan=2, sticky="w")

    def _build_advanced(self, body) -> None:
        card = self._card(body, 4, "settings_advanced_header")
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel", wraplength=620, justify="left"),
                      "advanced_hint").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 8))
        self.ui.label(ttk.Button(card, command=lambda: self.ui.show_screen("diagnostics")),
                      "btn_open_diagnostics").grid(row=2, column=0, sticky="w")
        self.ui.label(ttk.Label(card, style="PanelMuted.TLabel", wraplength=620, justify="left"),
                      "settings_reset_hint").grid(row=3, column=0, columnspan=2, sticky="w", pady=(16, 8))
        self.ui.label(ttk.Button(card, command=self._reset), "btn_reset_view").grid(row=4, column=0, sticky="w")

    def _reset(self) -> None:
        """Clears the search, the scope, the chosen macro and the chosen targets. It deletes nothing, which is
        why it sits here rather than next to anything that does."""
        self.c.reset_view()
        macros = self.ui.screens.get("macros")
        if macros is not None:
            macros.search_var.set("")
        self.ui.render()

    def render(self) -> None:
        self.lbl_provider.configure(text=self.c.services.provider_id)
        self.concurrency_var.set(str(self.c.concurrency))
        for refresh in self._theme_cards:
            refresh()


class DiagnosticsScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(28, 4, 28, 20))
        self.frame.columnconfigure(0, weight=1)
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(2, weight=1)

        card = w.card(self.ui, self.frame)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12), padx=(0, 10))
        card.columnconfigure(1, weight=1)
        self.fields = {}
        for index, key in enumerate(("diag_provider", "diag_target", "diag_target_id", "diag_verification",
                                     "diag_capabilities", "diag_last_screenshot", "diag_last_run")):
            self.ui.label(ttk.Label(card, style="PanelMuted.TLabel"), key).grid(row=index, column=0, sticky="w",
                                                                                 padx=(0, 12), pady=2)
            value = ttk.Label(card, text="-", style="Panel.TLabel", wraplength=320, justify="left")
            value.grid(row=index, column=1, sticky="w", pady=2)
            self.fields[key] = value

        preview_card = w.card(self.ui, self.frame)
        preview_card.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(1, weight=1)
        self.ui.label(ttk.Label(preview_card, style="Head.TLabel"), "diag_preview").grid(row=0, column=0,
                                                                                          sticky="w")
        self.preview = tk.Canvas(preview_card, height=190, background=th.ACTIVE.surface_alt,
                                 highlightthickness=1, highlightbackground=th.ACTIVE.border)
        self.preview.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.preview.bind("<Configure>", lambda _e: self.draw_preview(force=True))
        self.ui.on_theme(lambda: (self.preview.configure(background=th.ACTIVE.surface_alt,
                                                         highlightbackground=th.ACTIVE.border),
                                  self.draw_preview(force=True)))

        self.ui.label(ttk.Label(self.frame, style="Muted.TLabel"), "diag_log_header").grid(
            row=2, column=0, columnspan=2, sticky="nw")
        self.log = tk.Text(self.frame, font=(th.MONO, 9), background=th.ACTIVE.surface, height=8,
                           foreground=th.ACTIVE.text, relief="flat", wrap="word", state="disabled",
                           highlightthickness=1, highlightbackground=th.ACTIVE.border)
        self.log.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        self.frame.rowconfigure(3, weight=1)
        self.ui.on_theme(lambda: self.log.configure(background=th.ACTIVE.surface, foreground=th.ACTIVE.text,
                                                    highlightbackground=th.ACTIVE.border))
        self._log_text = None
        self._preview_key = None

    def render(self) -> None:
        c = self.c
        target, entry = c.target, c.selected
        self.fields["diag_provider"].configure(text=c.services.provider_id)
        self.fields["diag_target"].configure(
            text=(entry.title or entry.process) if entry else self.ui.t("diag_none"))
        self.fields["diag_target_id"].configure(text=target.target_id if target else self.ui.t("diag_none"))
        verification = target.verification if target else None
        self.fields["diag_verification"].configure(
            text=", ".join(verification.methods) if verification else self.ui.t("diag_none"))
        if target is not None:
            available = {c.value for c in target.capabilities}
            text = "\n".join(f"{cap.value:<16}{'yes' if cap.value in available else '—'}"
                             for cap in CAPABILITIES)
        else:
            text = self.ui.t("diag_none")
        self.fields["diag_capabilities"].configure(text=text)
        shot = c.preview
        self.fields["diag_last_screenshot"].configure(
            text=f"{shot.width} x {shot.height}" if shot is not None else self.ui.t("diag_none"))
        runs = c.history.list()
        self.fields["diag_last_run"].configure(text=runs[0].outcome.upper() if runs else self.ui.t("diag_none"))

        lines: list[str] = []
        for title, rows in c.diagnostics():
            lines += [f"[{title}]"] + [f"  {k:<20}{v}" for k, v in rows] + [""]
        if c.preflight_lines:
            lines += ["[Preflight]"] + [f"  {x}" for x in c.preflight_lines] + [""]
        if c.dry_run_lines:
            lines += ["[Dry run result]"] + [f"  {x}" for x in c.dry_run_lines[:200]] + [""]
        lines += ["[Log]"] + [f"  {x}" for x in list(c.log)[-200:]]
        text = "\n".join(lines)
        if text != self._log_text:
            self._log_text = text
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("1.0", text)
            self.log.configure(state="disabled")
        self.draw_preview()

    def draw_preview(self, force: bool = False) -> None:
        canvas, shot = self.preview, self.c.preview
        key = (id(shot), canvas.winfo_width(), canvas.winfo_height(), self.c.target_status, th.ACTIVE.id)
        if not force and key == self._preview_key:
            return
        self._preview_key = key
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 2), max(canvas.winfo_height(), 2)
        if shot is None:
            text = {TargetStatus.NONE: "preview_pick_target", TargetStatus.SELECTED: "preview_verifying",
                    TargetStatus.VERIFIED: "preview_capturing", TargetStatus.INVALID: "preview_invalid",
                    TargetStatus.STALE: "preview_stale"}[self.c.target_status]
            if self.c.target_status is TargetStatus.VERIFIED and not self.c.can_screenshot():
                text = "preview_unsupported"
            canvas.create_text(width // 2, height // 2, text=self.ui.t(text), fill=th.ACTIVE.text_muted,
                               font=(th.FONT, 10), justify="center")
            return
        try:
            full = tk.PhotoImage(master=canvas, data=base64.b64encode(shot.png))
            factor = max(1, -(-shot.width // max(width - 8, 1)), -(-shot.height // max(height - 8, 1)))
            self._photo = full.subsample(factor) if factor > 1 else full
            canvas.create_image(width // 2, height // 2, image=self._photo)
        except tk.TclError:
            canvas.create_text(width // 2, height // 2, text=self.ui.t("preview_unavailable"),
                               fill=th.ACTIVE.text_muted)
