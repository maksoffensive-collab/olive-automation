"""Run history: what ran, where, and how it ended — in the user's words.

Grouped by day, newest first. A row says the outcome in ordinary language ("Stopped", "Finished"); the technical
detail a run produced (a provider's error code, a preflight reason) is kept, but it lives behind "show details"
so that ordinary use never turns into reading a log. The tree groups by day rather than building a widget per
run, because the history holds up to 500 entries.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.gui import history as store
from app.gui import theme as th
from app.gui.view import fmt
from app.gui.view import widgets as w

OUTCOME_ANY = "any"
FILTERS = (OUTCOME_ANY, store.COMPLETED, store.PARTIAL, store.FAILED, store.STOPPED, store.REFUSED)
RESULT_LINES = 6          # per-target lines shown before "show details" opens the whole list


class HistoryScreen:
    def __init__(self, ui, parent: tk.Widget) -> None:
        self.ui, self.c = ui, ui.controller
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(28, 4, 28, 20))
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)
        self._signature = None
        self._records: dict[str, store.RunRecord] = {}
        self._build()

    def _build(self) -> None:
        header = ttk.Frame(self.frame, style="Plain.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        titles = ttk.Frame(header, style="Plain.TFrame")
        titles.grid(row=0, column=0, sticky="w")
        # the page title is the header's; this line says how many runs the list holds
        self.lbl_count = ttk.Label(titles, text="", style="Heading.TLabel")
        self.lbl_count.pack(anchor="w")
        self.ui.label(ttk.Button(header, style="SmallDanger.TButton", command=self._clear),
                      "btn_clear_history").grid(row=0, column=1, sticky="e")

        tools = ttk.Frame(self.frame, style="Plain.TFrame")
        tools.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        tools.columnconfigure(1, weight=1)
        self._search_icon = ttk.Label(tools, style="Muted.TLabel")      # the same search field as the Macros page
        self._search_icon.grid(row=0, column=0, padx=(0, 8))
        paint_search = lambda: self._search_icon.configure(                       # noqa: E731
            image=self.ui.icon("search", th.ACTIVE.text_muted))
        self.ui.on_theme(paint_search)
        paint_search()                            # the hook only fires on a CHANGE, so paint it once now
        self.search_var = tk.StringVar()
        entry = ttk.Entry(tools, textvariable=self.search_var)
        entry.grid(row=0, column=1, sticky="ew")
        w.Tooltip(self.ui, entry, lambda: self.ui.t("tip_search_history"))
        self.search_var.trace_add("write", lambda *_: self.ui.render())
        self.filter_var = tk.StringVar()
        self._filter_box = ttk.Combobox(tools, textvariable=self.filter_var, state="readonly", width=20)
        self._filter_box.grid(row=0, column=2, padx=(8, 0))
        self._filter_box.bind("<<ComboboxSelected>>", lambda _e: self.ui.render())

        holder = ttk.Frame(self.frame, style="Plain.TFrame")
        holder.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(holder, columns=("time", "macro", "targets", "progress", "outcome"),
                                 show="tree headings", selectmode="browse")
        self.tree.column("#0", width=120, stretch=False)
        for name, width, stretch in (("time", 84, False), ("macro", 200, True), ("targets", 100, False),
                                     ("progress", 110, False), ("outcome", 120, False)):
            self.tree.column(name, width=width, anchor="w", stretch=stretch)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=w.autohide(bar))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._render_detail())
        self.ui.on_theme(self._paint_tags)
        self._paint_tags()

        self._detail = ttk.Frame(self.frame, style="Panel.TFrame", padding=(18, 14))
        self._detail.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self._detail.columnconfigure(0, weight=1)
        self.lbl_detail = ttk.Label(self._detail, text="", style="PanelStrong.TLabel", wraplength=900,
                                    justify="left")
        self.lbl_detail.grid(row=0, column=0, sticky="w")
        # when it ran, to the second -- the list shows the time of day, this says the date too
        self.lbl_when = ttk.Label(self._detail, text="", style="PanelMuted.TLabel")
        self.lbl_when.grid(row=4, column=0, sticky="w", pady=(6, 0))
        # Targets of one run need not agree with each other, so the aggregate alone cannot say which one failed.
        self.lbl_results = ttk.Label(self._detail, text="", style="Panel.TLabel", wraplength=900, justify="left")
        self.btn_detail = self.ui.label(ttk.Button(self._detail, style="Link.TButton", command=self._toggle),
                                        "btn_show_details")
        self.btn_detail.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.lbl_technical = ttk.Label(self._detail, text="", style="Mono.TLabel", wraplength=900,
                                       justify="left")
        self._showing_detail = False
        self._empty = w.empty_state(self.ui, self.frame, "history_empty", "history_empty_hint", icon="history")

    def _paint_tags(self) -> None:
        self.tree.tag_configure("day", foreground=th.ACTIVE.primary_hover)
        for outcome, colour in ((store.COMPLETED, th.ACTIVE.success), (store.PARTIAL, th.ACTIVE.warning),
                                (store.STOPPED, th.ACTIVE.warning), (store.FAILED, th.ACTIVE.error),
                                (store.REFUSED, th.ACTIVE.error)):
            self.tree.tag_configure(outcome, foreground=colour)

    # ---- actions ---------------------------------------------------------------------------------
    def _clear(self) -> None:
        if self.ui.confirm(self.ui.t("dlg_clear_history_title"), self.ui.t("dlg_clear_history_text")):
            self.c.history.clear()
            self._signature = None
            self.ui.render()

    def _toggle(self) -> None:
        self._showing_detail = not self._showing_detail
        self._render_detail()

    def _selected(self):
        selection = self.tree.selection()
        return self._records.get(selection[0]) if selection else None

    # ---- rendering -------------------------------------------------------------------------------
    def render(self) -> None:
        labels = [self.ui.t(f"filter_{name}") for name in FILTERS]
        self._filter_box.configure(values=labels)
        if self.filter_var.get() not in labels:
            self.filter_var.set(labels[0])
        wanted = FILTERS[labels.index(self.filter_var.get())]
        query = self.search_var.get().casefold().strip()
        records = [r for r in self.c.history.list()
                   if (wanted == OUTCOME_ANY or r.outcome == wanted)
                   and (not query or query in r.macro.casefold())]
        self.lbl_count.configure(text=fmt.counted(self.ui, "count_runs", len(records)))
        signature = tuple((r.at, r.macro, r.outcome, r.completed) for r in records)
        if signature != self._signature:
            self._signature = signature
            self._fill(records)
        self._render_detail()

    def _fill(self, records) -> None:
        chosen = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self._records = {}
        days: dict[str, str] = {}
        for index, record in enumerate(records):
            day = store.day_key(record.at)
            if day not in days:
                days[day] = self.tree.insert("", "end", iid=f"day:{day}", text=fmt.day_label(self.ui, day),
                                             open=True, tags=("day",))
            iid = f"run:{index}"
            self._records[iid] = record
            progress = self.ui.t("history_progress").format(done=record.completed, total=record.actions_total)
            self.tree.insert(days[day], "end", iid=iid, text="",
                             values=(fmt.clock_time_precise(record.at), record.macro,
                                     fmt.target_count(self.ui, record.target_count), progress,
                                     fmt.outcome_label(self.ui, record.outcome)),
                             tags=(record.outcome,))
        for column, key in (("#0", "col_day"), ("time", "col_time"), ("macro", "col_macro"),
                            ("targets", "col_targets"), ("progress", "col_progress"), ("outcome", "col_outcome")):
            self.tree.heading(column, text=self.ui.t(key), anchor="w")   # headings sit over their own values
        if chosen and self.tree.exists(chosen[0]):
            self.tree.selection_set(chosen[0])
        if records:
            self._empty.grid_remove()
        else:
            self._empty.grid(row=2, column=0, sticky="nsew", pady=40)

    def _render_detail(self) -> None:
        record = self._selected()
        if record is None:
            self._detail.grid_remove()
            return
        self._detail.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.lbl_when.configure(text=self.ui.t("history_when").format(when=fmt.full_time(record.at)))
        targets = ", ".join(record.targets) or self.ui.t("targets_none_chosen")
        self.lbl_detail.configure(text=self.ui.t("history_detail").format(
            macro=record.macro, outcome=fmt.outcome_label(self.ui, record.outcome), targets=targets,
            duration=fmt.duration(self.ui, record.duration_s)))
        self._render_results(record)
        self.btn_detail.configure(text=self.ui.t("btn_hide_details" if self._showing_detail
                                                 else "btn_show_details"))
        if self._showing_detail and record.detail:
            self.lbl_technical.configure(text=record.detail)
            self.lbl_technical.grid(row=3, column=0, sticky="w", pady=(6, 0))
        else:
            self.lbl_technical.grid_remove()
            if self._showing_detail:
                self.lbl_technical.configure(text=self.ui.t("history_no_detail"))
                self.lbl_technical.grid(row=3, column=0, sticky="w", pady=(6, 0))

    def _render_results(self, record) -> None:
        """Per target. Ordinarily only the ones that did not simply finish -- those are what a person is looking
        for -- with the whole list under "show details"."""
        if not record.results:
            self.lbl_results.grid_remove()            # a run recorded before runs could cover several targets
            return
        shown = list(record.results) if self._showing_detail \
            else [r for r in record.results if r.outcome != store.COMPLETED][:RESULT_LINES]
        lines = []
        for r in shown:
            lines.append(self.ui.t("history_result_line").format(target=r.target,
                                                                 outcome=fmt.outcome_label(self.ui, r.outcome),
                                                                 done=r.completed, total=r.total))
            if r.outcome != store.COMPLETED:          # why it stopped, where, and what to do about it
                lines.extend("    " + line for line in fmt.explain_lines(self.ui, r.detail))
        hidden = len(record.results) - len(shown)
        if lines and hidden > 0:
            lines.append(self.ui.t("details_more_targets").format(count=hidden))
        if not lines:
            self.lbl_results.grid_remove()
            return
        self.lbl_results.configure(text="\n".join(lines))
        self.lbl_results.grid(row=1, column=0, sticky="w", pady=(6, 0))
