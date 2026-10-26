"""The settings of one block of a version-4 script, in the editor's panel at the right (the person's design of
2026-10-25). Each block kind has its own panel; a change is applied at once (a typed value when Enter is pressed or
the field is left), as one edit that can be undone.

What is picked on the screen is picked ON THE TARGET ITSELF where it has a place on this computer's screen: a picture
of it laid exactly over it (`OverlayPicker`) -- press the button, click the window, the point is taken. A device
shows its picture in a dialog instead (`flow.ScreenPicker`). Nothing is ever sent to the target while picking.

    start     the delay before the run (seconds)
    wait      how long (seconds)
    check     "If ... Then": a pixel's colour (picked: its point and colour at once), a picture (taken from the screen,
              where to look), or texts (each its own area; true when any is there) -- and "test it now"
    mixed     its parts, each any of those, and whether at least one or all must hold
    repeat    until stopped by hand / until a time of day / for a time / a number of times
    click     a click, a double click, a right click, a hold, a drag; at a picked point or where it was found
    macro     which recorded macro (or record a new one)
    stop      finish, or stop with an error and its words
"""
from __future__ import annotations

import base64
import math
import tkinter as tk
from dataclasses import replace
from tkinter import ttk

from app.decide import (ALL, ANY, AT_MATCH, AT_POINT, CLICK_KINDS, DRAG, FOR, HOLD, MANUAL, PICTURE, PIXEL, TEXT,
                        TIMES, UNTIL, CheckNode, ClickNode, Condition, DecisionError, GraphError, MacroNode, Part,
                        RepeatNode, StartNode, StopNode, WaitNode)
from app.gui import graph_edit as ge
from app.gui import region as geo
from app.gui import theme as th
from app.gui.view import fmt
from app.gui.view.overlay_picker import OverlayPicker
from app.observe import InvalidQuery, TemplateQuery, scaled_part
from app.vision.bitmap import VisionError, encode_png

THUMB = (300, 90)
PIXEL_TOLERANCE = 10            # a new pixel's colour may differ this much per channel (a live screen is not exact)
TEXT_LIMIT = 10


def _number(text: str) -> float | None:
    try:
        value = float(str(text).strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class Panel:
    """What every block's panel has: the editor it belongs to, its block, and how to apply a change."""

    def __init__(self, editor, parent, node) -> None:
        self.editor, self.ui, self.c, self.node = editor, editor.ui, editor.c, node
        self.frame = ttk.Frame(parent, style="Plain.TFrame", padding=(14, 0, 14, 10))
        self.frame.grid(row=0, column=0, sticky="nsew")
        self.frame.columnconfigure(1, weight=1)
        self.row = 0
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    # ---- layout helpers -----------------------------------------------------------------------------------------
    def hint(self, key: str, **values) -> ttk.Label:
        label = ttk.Label(self.frame, text=self.ui.t(key).format(**values), style="Muted.TLabel", wraplength=330,
                          justify="left")
        label.grid(row=self.row, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.row += 1
        return label

    def heading(self, key: str) -> None:
        ttk.Label(self.frame, text=self.ui.t(key), style="FieldLabel.TLabel").grid(
            row=self.row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.row += 1

    def entry(self, label_key: str, value: str, on_commit, *, width: int = 10) -> tk.StringVar:
        ttk.Label(self.frame, text=self.ui.t(label_key), style="TLabel").grid(row=self.row, column=0,
                                                                                 sticky="w", pady=3, padx=(0, 8))
        var = tk.StringVar(value=value)
        box = ttk.Entry(self.frame, textvariable=var, width=width)
        box.grid(row=self.row, column=1, sticky="w", pady=3)
        commit = lambda _e=None: self.safely(on_commit, var.get())   # noqa: E731
        box.bind("<Return>", commit)
        box.bind("<FocusOut>", commit)
        self.row += 1
        return var

    def radios(self, options, value: str, on_change) -> tk.StringVar:
        var = tk.StringVar(value=value)
        for key, label_key in options:
            ttk.Radiobutton(self.frame, text=self.ui.t(label_key), value=key, variable=var,
                            style="Choice.TRadiobutton", command=lambda: on_change(var.get())).grid(
                row=self.row, column=0, columnspan=2, sticky="w", pady=1)
            self.row += 1
        return var

    def button(self, label_key: str, command, *, style: str = "TButton", column: int = 0) -> ttk.Button:
        made = ttk.Button(self.frame, text=self.ui.t(label_key), style=style, command=command)
        made.grid(row=self.row, column=column, columnspan=2 if column == 0 else 1, sticky="w", pady=(6, 2))
        self.row += 1
        return made

    # ---- applying ---------------------------------------------------------------------------------------------
    def apply(self, node) -> bool:
        if node == self.node:
            return True
        result = self.c.editor_apply_graph(ge.replace_node, node)
        if result is None:
            self.editor.show_message(self.ui.status_text(), error=True)
            return False
        self.editor.show_message("")
        self.node = node
        self.ui.render()
        return True

    def safely(self, action, *args) -> None:
        """`action(*args)`; a value the block or condition refuses (an invisible character pasted into a text,
        say) is said in the panel instead of raised from a Tk callback."""
        try:
            action(*args)
        except (GraphError, DecisionError, InvalidQuery, VisionError, ValueError) as exc:
            self.refuse("graph_err_value", reason=str(exc))

    def refuse(self, key: str, **values) -> None:
        self.editor.show_message(self.ui.t(key).format(**values), error=True)

    # ---- picking on the target ---------------------------------------------------------------------------------
    def pick(self, mode: str, done) -> None:
        """A fresh picture of the chosen target, then a point or a rectangle picked on it: `done(screen, answer)`."""
        if not self.c.target_ready:
            self.ui.choose_target()
            if not self.c.target_ready:
                self.refuse("graph_need_target")
                return
        whole = self.c.selected is not None and self.c.selected.whole_screen
        if whole:
            # a picture of the whole screen would be a picture of Olive itself: it steps out of the way first
            self.ui.root.iconify()
            self.ui.root.after(450, lambda: self._capture(mode, done, whole))
            return
        self._capture(mode, done, whole)

    def _capture(self, mode: str, done, whole: bool) -> None:
        job = self.c.begin_screen()
        if job is None:
            self._back(whole)
            self.editor.show_message(self.ui.status_text(), error=True)
            return
        self.editor.show_message(self.ui.t("screen_capturing"))
        self.ui.run_job(job, lambda outcome: self._picked(outcome, job, mode, done, whole))

    def _back(self, whole: bool) -> None:
        if whole:
            try:
                self.ui.root.deiconify()
            except tk.TclError:
                pass

    def _picked(self, outcome, job, mode: str, done, whole: bool = False) -> None:
        screen = self.c.finish_screen(outcome, job)
        if screen is None:
            self._back(whole)
            self.editor.show_message(self.ui.status_text(), error=True)
            return
        self.editor.show_message("")
        rect = self.c.target_screen_rect()
        try:
            if rect is not None and (rect[2], rect[3]) == (screen.width, screen.height):
                answer = OverlayPicker(self.ui, screen, rect, mode).show()
            else:
                from app.gui.view.flow import ScreenPicker
                answer = ScreenPicker(self.ui, screen, "color" if mode == "point" else mode).show()
        finally:
            self._back(whole)
        if answer is not None:
            self.safely(done, screen, answer)


# ---- start, wait, stop, repeat ------------------------------------------------------------------------------------
class StartPanel(Panel):
    def build(self) -> None:
        self.hint("graph_hint_start")
        self.entry("graph_field_delay", fmt.number(self.ui, self.node.delay_s, 1), self._delay)

    def _delay(self, text: str) -> None:
        value = _number(text)
        if value is None or not 0 <= value <= 24 * 3600:
            return self.refuse("graph_err_seconds", low=0, high=86400)
        self.apply(replace(self.node, delay_s=value))


class WaitPanel(Panel):
    def build(self) -> None:
        self.hint("graph_hint_wait")
        self.entry("graph_field_seconds", fmt.number(self.ui, self.node.seconds, 2), self._seconds)

    def _seconds(self, text: str) -> None:
        value = _number(text)
        if value is None or not 0.05 <= value <= 24 * 3600:
            return self.refuse("graph_err_seconds", low="0,05", high=86400)
        self.apply(replace(self.node, seconds=value))


class StopPanel(Panel):
    def build(self) -> None:
        self.hint("graph_hint_stop")
        self.radios((("ok", "graph_stop_ok"), ("error", "graph_stop_error")),
                    "ok" if self.node.success else "error", self._kind)
        self.entry("graph_field_message", self.node.message, self._message, width=30)

    def _kind(self, value: str) -> None:
        self.apply(replace(self.node, success=value == "ok"))

    def _message(self, text: str) -> None:
        text = " ".join(text.split())[:200]
        self.apply(replace(self.node, message=text))


class RepeatPanel(Panel):
    def build(self) -> None:
        self.hint("graph_hint_repeat")
        self.radios(((MANUAL, "graph_repeat_mode_manual"), (UNTIL, "graph_repeat_mode_until"),
                     (FOR, "graph_repeat_mode_for"), (TIMES, "graph_repeat_mode_times")), self.node.mode, self._mode)
        node = self.node
        if node.mode == UNTIL:
            self.entry("graph_field_until", node.until or "18:00", self._until, width=6)
        elif node.mode == FOR:
            self.entry("graph_field_minutes", fmt.number(self.ui, (node.seconds or 600) / 60, 2), self._minutes)
        elif node.mode == TIMES:
            self.entry("graph_field_times", str(node.times or 3), self._times)
        if node.mode == MANUAL:
            self.hint("graph_hint_repeat_manual")

    def _mode(self, mode: str) -> None:
        node = self.node
        fields = {MANUAL: {}, UNTIL: {"until": node.until or "18:00"}, FOR: {"seconds": node.seconds or 600.0},
                  TIMES: {"times": node.times or 3}}[mode]
        self.apply(RepeatNode(node.id, at=node.at, mode=mode, **fields))

    def _until(self, text: str) -> None:
        text = text.strip()
        if len(text) == 4 and text[1] == ":":
            text = "0" + text
        try:
            self.apply(replace(self.node, until=text))
        except Exception:
            self.refuse("graph_err_time")

    def _minutes(self, text: str) -> None:
        value = _number(text)
        if value is None or not 0 < value * 60 <= 24 * 3600:
            return self.refuse("graph_err_minutes")
        self.apply(replace(self.node, seconds=value * 60))

    def _times(self, text: str) -> None:
        try:
            value = int(text.strip())
        except ValueError:
            return self.refuse("graph_err_times")
        if not 1 <= value <= 1_000_000:
            return self.refuse("graph_err_times")
        self.apply(replace(self.node, times=value))


# ---- click ------------------------------------------------------------------------------------------------------
class ClickPanel(Panel):
    def build(self) -> None:
        node = self.node
        self.hint("graph_hint_click")
        self.heading("graph_click_how")
        self.radios(tuple((how, f"graph_click_kind_{how}") for how in CLICK_KINDS), node.how, self._how)
        if node.how == HOLD:
            self.entry("graph_field_hold", fmt.number(self.ui, node.hold_s, 1), self._hold)
        self.heading("graph_click_where")
        if node.how != DRAG:
            self.radios(((AT_POINT, "graph_click_where_point"), (AT_MATCH, "graph_click_where_match")), node.where,
                        self._where)
        if node.where == AT_POINT:
            text = (self.ui.t("graph_click_point").format(x=node.point[0], y=node.point[1]) if node.point
                    else self.ui.t("graph_click_no_point"))
            ttk.Label(self.frame, text=text, style="TLabel").grid(row=self.row, column=0, columnspan=2,
                                                                       sticky="w", pady=(4, 0))
            self.row += 1
            self.button("graph_btn_pick_point" if node.how != DRAG else "graph_btn_pick_from",
                        lambda: self.pick("point", self._picked_point), style="Primary.TButton")
            if node.how == DRAG:
                text = (self.ui.t("graph_click_point").format(x=node.to[0], y=node.to[1]) if node.to
                        else self.ui.t("graph_click_no_point"))
                ttk.Label(self.frame, text=text, style="TLabel").grid(row=self.row, column=0, columnspan=2,
                                                                           sticky="w", pady=(6, 0))
                self.row += 1
                self.button("graph_btn_pick_to", lambda: self.pick("point", self._picked_to))
        else:
            self.hint("graph_hint_click_match")

    def _how(self, how: str) -> None:
        node = self.node
        where = AT_POINT if how == DRAG else node.where
        self.apply(ClickNode(node.id, at=node.at, how=how, where=where, point=node.point, to=node.to,
                             size=node.size, hold_s=node.hold_s))

    def _where(self, where: str) -> None:
        self.apply(replace(self.node, where=where))

    def _hold(self, text: str) -> None:
        value = _number(text)
        if value is None or not 0.05 <= value <= 60:
            return self.refuse("graph_err_hold")
        self.apply(replace(self.node, hold_s=value))

    def _picked_point(self, screen, answer) -> None:
        self.apply(replace(self.node, point=tuple(answer["point"]), size=(screen.width, screen.height),
                           to=self.node.to if self.node.size in (None, (screen.width, screen.height)) else None))

    def _picked_to(self, screen, answer) -> None:
        if self.node.point is not None and self.node.size not in (None, (screen.width, screen.height)):
            return self.refuse("graph_err_drag_size")
        self.apply(replace(self.node, to=tuple(answer["point"]), size=(screen.width, screen.height)))


# ---- macro ------------------------------------------------------------------------------------------------------
class MacroPanel(Panel):
    def build(self) -> None:
        self.hint("graph_hint_macro")
        self.choices = self.c.recorded_macro_choices()              # [(key, unique label)]
        names = [label for _key, label in self.choices]
        current = next((label for key, label in self.choices if key == self.node.macro), "")
        self.var = tk.StringVar(value=current)
        self.box = box = ttk.Combobox(self.frame, textvariable=self.var, values=names, state="readonly", width=32)
        box.grid(row=self.row, column=0, columnspan=2, sticky="w", pady=4)
        box.bind("<<ComboboxSelected>>", lambda _e: self._chosen())
        self.row += 1
        if not names:
            self.hint("graph_hint_macro_none")
        self.button("graph_btn_record_here", lambda: self.editor.record_macro())

    def _chosen(self) -> None:
        index = self.box.current()                                    # by its place in the list, never its name
        if 0 <= index < len(self.choices):
            self.apply(replace(self.node, macro=self.choices[index][0]))


# ---- the parts of a check -----------------------------------------------------------------------------------------
class PartEditor:
    """One part of a check, laid out in `panel`'s frame from its current row: what it looks for and its buttons.
    `kind` says what a part not made yet will be. `changed(part | None)`: the part as it is now (None: removed)."""

    def __init__(self, panel: Panel, kind: str, part: Part | None, changed, *, removable: bool) -> None:
        self.panel, self.ui, self.kind, self.part, self.changed = panel, panel.ui, kind, part, changed
        f = panel.frame
        t = self.ui.t
        box = ttk.Frame(f, style="Plain.TFrame", padding=(10, 6))
        box.grid(row=panel.row, column=0, columnspan=2, sticky="ew", pady=(6, 2))
        box.columnconfigure(1, weight=1)
        panel.row += 1
        tk.Frame(box, width=4, background=th.ACTIVE.primary).grid(row=0, column=0, rowspan=8, sticky="ns",
                                                                       padx=(0, 10))
        ttk.Label(box, text=t(f"graph_kind_{kind}"), style="FieldLabel.TLabel").grid(row=0, column=1, sticky="w")
        if removable:
            ttk.Button(box, text=t("graph_btn_remove"), style="SmallDanger.TButton",
                       command=lambda: changed(None)).grid(row=0, column=2, sticky="e")
        self.box, self.r = box, 1
        {PIXEL: self._pixel, PICTURE: self._picture, TEXT: self._text}[kind]()

    def _line(self, text: str, style: str = "TLabel") -> ttk.Label:
        label = ttk.Label(self.box, text=text, style=style, wraplength=280, justify="left")
        label.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=1)
        self.r += 1
        return label

    def _button(self, key: str, command, *, primary: bool = False) -> None:
        ttk.Button(self.box, text=self.ui.t(key), style="Primary.TButton" if primary else "TButton",
                   command=command).grid(row=self.r, column=1, columnspan=2, sticky="w", pady=(4, 2))
        self.r += 1

    # ---- a pixel's colour: its point and colour picked at once -------------------------------------------------
    def _pixel(self) -> None:
        q = self.part.condition.query if self.part else None
        if q is None:
            self._line(self.ui.t("graph_pixel_none"), "Muted.TLabel")
        else:
            row = ttk.Frame(self.box, style="Plain.TFrame")
            row.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=2)
            self.r += 1
            tk.Label(row, width=3, background="#%02x%02x%02x" % tuple(q.color), relief="solid",
                     borderwidth=1).pack(side="left", padx=(0, 8))
            ttk.Label(row, text=self.ui.t("graph_pixel_at").format(x=q.point[0], y=q.point[1],
                                                                   color=", ".join(map(str, q.color))),
                      style="TLabel").pack(side="left")
        self._button("graph_btn_pick_pixel", lambda: self.panel.pick("point", self._picked_pixel), primary=True)
        if q is not None:
            self._tolerance(q.tolerance)

    def _tolerance(self, value: int) -> None:
        row = ttk.Frame(self.box, style="Plain.TFrame")
        row.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=2)
        self.r += 1
        ttk.Label(row, text=self.ui.t("graph_field_tolerance"), style="TLabel").pack(side="left", padx=(0, 8))
        var = tk.StringVar(value=str(value))
        box = ttk.Entry(row, textvariable=var, width=5)
        box.pack(side="left")

        def commit(_e=None):
            try:
                tolerance = int(var.get().strip())
            except ValueError:
                return self.panel.refuse("graph_err_tolerance")
            if not 0 <= tolerance <= 255:
                return self.panel.refuse("graph_err_tolerance")
            q = self.part.condition.query
            self.changed(Part(Condition.pixel(q.point, q.color, tolerance=tolerance), self.part.size))
        box.bind("<Return>", lambda e: self.panel.safely(commit, e))
        box.bind("<FocusOut>", lambda e: self.panel.safely(commit, e))

    def _picked_pixel(self, screen, answer) -> None:
        tolerance = self.part.condition.query.tolerance if self.part else PIXEL_TOLERANCE
        self.changed(Part(Condition.pixel(tuple(answer["point"]), tuple(answer["color"]), tolerance=tolerance),
                          (screen.width, screen.height)))

    # ---- a picture ----------------------------------------------------------------------------------------------
    def _picture(self) -> None:
        q = self.part.condition.query if self.part else None
        if q is None:
            self._line(self.ui.t("graph_picture_none"), "Muted.TLabel")
        else:
            image = tk.PhotoImage(master=self.ui.root, data=base64.b64encode(encode_png(q.template)))
            factor = geo.display_factor(image.width(), image.height(), *THUMB)
            self._thumb = image.subsample(factor) if factor > 1 else image
            tk.Label(self.box, image=self._thumb, relief="solid", borderwidth=1).grid(row=self.r, column=1,
                                                                                     columnspan=2, sticky="w")
            self.r += 1
            self._line(self.ui.t("graph_picture_size").format(w=q.template.width, h=q.template.height),
                       "Muted.TLabel")
        self._button("graph_btn_take_picture", lambda: self.panel.pick("template", self._picked_picture),
                     primary=True)
        if q is None:
            return
        self._area(q.region)
        row = ttk.Frame(self.box, style="Plain.TFrame")
        row.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=2)
        self.r += 1
        ttk.Label(row, text=self.ui.t("field_min_confidence"), style="TLabel").pack(side="left", padx=(0, 8))
        var = tk.StringVar(value=fmt.number(self.ui, q.min_confidence, 4))
        box = ttk.Entry(row, textvariable=var, width=7)
        box.pack(side="left")

        def commit(_e=None):
            value = _number(var.get())
            if value is None or not 0 < value <= 1:
                return self.panel.refuse("graph_err_confidence")
            self._picture_changed(min_confidence=value)
        box.bind("<Return>", lambda e: self.panel.safely(commit, e))
        box.bind("<FocusOut>", lambda e: self.panel.safely(commit, e))
        any_size = tk.BooleanVar(value=q.any_size)
        ttk.Checkbutton(self.box, text=self.ui.t("template_any_size"), variable=any_size, style="Choice.TCheckbutton",
                        command=lambda: self._picture_changed(any_size=any_size.get())).grid(
            row=self.r, column=1, columnspan=2, sticky="w", pady=2)
        self.r += 1

    def _picture_changed(self, **changes) -> None:
        q = self.part.condition.query
        self.changed(Part(Condition(replace(q, **changes)), self.part.size))

    def _picked_picture(self, screen, answer) -> None:
        roi = answer["roi"]
        crop = screen.frame.bitmap.crop(roi.x, roi.y, roi.width, roi.height)
        size = (screen.width, screen.height)
        region = self.part.condition.query.region if self.part else None
        old = self.part.size if self.part else None
        if region is not None and old is not None and old != size:     # the area follows into the new size
            region = scaled_part(TemplateQuery(crop, region=region), "region", size[0] / old[0], size[1] / old[1],
                                 size[0], size[1]).region
        confidence = self.part.condition.query.min_confidence if self.part else 0.95
        try:
            self.changed(Part(Condition.template(crop, region=region, min_confidence=confidence), size))
        except Exception as exc:                       # a flat picture: refused, in words
            self.panel.editor.show_message(self.ui.t("err_query_flat_picture") if "flat" in str(exc).lower()
                                           else str(exc), error=True)

    # ---- where to look ------------------------------------------------------------------------------------------
    def _area(self, region) -> None:
        text = self.ui.t("graph_area_all") if region is None else self.ui.t("graph_area_at").format(
            w=region.width, h=region.height, x=region.x, y=region.y)
        self._line(text, "Muted.TLabel")
        row = ttk.Frame(self.box, style="Plain.TFrame")
        row.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=(2, 2))
        self.r += 1
        ttk.Button(row, text=self.ui.t("graph_btn_pick_area"),
                   command=lambda: self.panel.pick("region", self._picked_area)).pack(side="left", padx=(0, 6))
        if region is not None:
            ttk.Button(row, text=self.ui.t("graph_btn_whole_screen"), command=lambda: self._set_area(None, None)
                       ).pack(side="left")

    def _picked_area(self, screen, answer) -> None:
        self._set_area(answer["roi"], (screen.width, screen.height))

    def _set_area(self, region, size) -> None:
        """An area picked on a picture of `size` (None: the whole screen). A part keeps ONE size -- the one its
        picture (or first area) was picked at -- so an area picked on another size is scaled into it."""
        q = self.part.condition.query
        own = self.part.size
        if region is None:
            return self.changed(Part(Condition(replace(q, region=None)), own))
        query = replace(q, region=region)
        if own is not None and size is not None and own != size:
            query = scaled_part(query, "region", own[0] / size[0], own[1] / size[1], own[0], own[1])
        self.changed(Part(Condition(query), own or size))

    # ---- a text in its own area -----------------------------------------------------------------------------------
    def _text(self) -> None:
        q = self.part.condition.query if self.part else None
        value = q.lines[0].text if q else ""
        var = tk.StringVar(value=value)
        box = ttk.Entry(self.box, textvariable=var, width=30)
        box.grid(row=self.r, column=1, columnspan=2, sticky="w", pady=2)
        self.r += 1

        def commit(_e=None):
            text = " ".join(var.get().split())
            if not text:
                if self.part is not None:
                    self.panel.refuse("graph_err_text_empty")
                return
            if q is not None and text == q.lines[0].text:
                return
            region = q.region if q else None
            self.changed(Part(Condition.text_lines([(text, True)], region=region),
                              self.part.size if self.part else None))
        box.bind("<Return>", lambda e: self.panel.safely(commit, e))
        box.bind("<FocusOut>", lambda e: self.panel.safely(commit, e))
        if q is not None:
            self._area(q.region)
        else:
            self._line(self.ui.t("graph_text_type"), "Muted.TLabel")


# ---- a check ----------------------------------------------------------------------------------------------------
class CheckPanel(Panel):
    """ "If ... Then" (one kind: a pixel, a picture, or texts each in its own area) or "Mixed" (parts of any kinds,
    at least one / all)."""

    KINDS = (PIXEL, PICTURE, TEXT)

    def build(self) -> None:
        node = self.node
        if node.mixed:
            self.hint("graph_hint_mixed")
            self.radios(((ANY, "graph_mixed_any"), (ALL, "graph_mixed_all")), node.mode, self._mode)
            for index, part in enumerate(node.parts):
                PartEditor(self, part.kind, part, lambda p, i=index: self._part(i, p), removable=True)
            pending = self.editor.pending_part.get(node.id)
            if pending:
                PartEditor(self, pending, None, self._pending_done, removable=True)
            if len(node.parts) < 10:
                self.heading("graph_add_part")
                row = ttk.Frame(self.frame, style="Plain.TFrame")
                row.grid(row=self.row, column=0, columnspan=2, sticky="w")
                self.row += 1
                for kind in self.KINDS:
                    ttk.Button(row, text=self.ui.t(f"graph_kind_{kind}"),
                               command=lambda k=kind: self._add_kind(k)).pack(side="left", padx=(0, 6))
        else:
            self.hint("graph_hint_check")
            kind = node.parts[0].kind if node.parts else self.editor.pending_kind.get(node.id, PICTURE)
            names = [self.ui.t(f"graph_kind_{k}") for k in self.KINDS]
            self.kind_var = tk.StringVar(value=self.ui.t(f"graph_kind_{kind}"))
            ttk.Label(self.frame, text=self.ui.t("graph_check_what"), style="TLabel").grid(
                row=self.row, column=0, sticky="w", padx=(0, 8))
            box = ttk.Combobox(self.frame, textvariable=self.kind_var, values=names, state="readonly", width=18)
            box.grid(row=self.row, column=1, sticky="w", pady=4)
            box.bind("<<ComboboxSelected>>", lambda _e: self._kind(self.KINDS[names.index(self.kind_var.get())]))
            self.row += 1
            if kind == TEXT:
                for index, part in enumerate(node.parts):
                    PartEditor(self, TEXT, part, lambda p, i=index: self._part(i, p), removable=len(node.parts) > 1)
                if len(node.parts) < 10:
                    PartEditor(self, TEXT, None, lambda p: self._part(len(self.node.parts), p), removable=False)
            else:
                part = node.parts[0] if node.parts else None
                PartEditor(self, kind, part, lambda p: self._part(0, p), removable=False)
        if node.parts:
            self.button("graph_btn_test", self.test)
            self.result = ttk.Label(self.frame, text="", style="TLabel", wraplength=330, justify="left")
            self.result.grid(row=self.row, column=0, columnspan=2, sticky="w", pady=(2, 6))
            self.row += 1

    def _mode(self, mode: str) -> None:
        self.apply(replace(self.node, mode=mode))

    def _pending_done(self, part: Part | None) -> None:
        if part is None:                               # the part being added was given up
            self.editor.pending_part.pop(self.node.id, None)
            self.editor.refresh_panel()
            return
        self._part(len(self.node.parts), part)

    def _kind(self, kind: str) -> None:
        current = self.node.parts[0].kind if self.node.parts else None
        if kind == current:
            return
        self.editor.pending_kind[self.node.id] = kind
        if self.node.parts:
            self.apply(replace(self.node, parts=()))
        else:
            self.editor.refresh_panel()

    def _add_kind(self, kind: str) -> None:
        # a part is made by what is picked or typed: open its editor at the end, empty
        self.editor.pending_part[self.node.id] = kind
        self.editor.refresh_panel()

    def _part(self, index: int, part: Part | None) -> None:
        parts = list(self.node.parts)
        if part is None:
            if index < len(parts):
                del parts[index]
        elif index < len(parts):
            parts[index] = part
        else:
            parts.append(part)
        self.editor.pending_part.pop(self.node.id, None)
        self.apply(replace(self.node, parts=tuple(parts)))

    # ---- test it now --------------------------------------------------------------------------------------------
    def test(self) -> None:
        if not self.c.target_ready:
            self.ui.choose_target()
            if not self.c.target_ready:
                return self.refuse("graph_need_target")
        self.result.configure(text=self.ui.t("probe_running"))
        self._answers = []
        self._ask(0)

    def _ask(self, index: int) -> None:
        if index >= len(self.node.parts):
            return self._say()
        part = self.node.parts[index]
        job = self.c.begin_probe(part.condition, picked_on=part.size)
        if job is None:
            self.result.configure(text=self.ui.status_text())
            return
        self.ui.run_job(job, lambda outcome: self._asked(outcome, job, index))

    def _asked(self, outcome, job, index: int) -> None:
        if not self.frame.winfo_exists():
            return
        result = self.c.finish_probe(outcome, job)
        if result is None:
            self.result.configure(text=self.ui.status_text(self.c.probe_status))
            return
        self._answers.append(result)
        self._ask(index + 1)

    def _say(self) -> None:
        answers = self._answers
        found = all(a.satisfied for a in answers) if self.node.mode == ALL else any(a.satisfied for a in answers)
        lines = [self.ui.t("graph_test_found" if found else "graph_test_missing")]
        for part, answer in zip(self.node.parts, answers):
            detail = answer.observation.detail
            if part.kind == PICTURE:
                best = detail.get("best")
                value = answer.confidence if answer.satisfied else (best or {}).get("confidence", 0.0)
                lines.append(self.ui.t("graph_test_picture").format(
                    state=self.ui.t("graph_out_found" if answer.satisfied else "graph_out_missing"),
                    confidence=fmt.confidence(self.ui, value, part.condition.query.min_confidence)))
            else:
                lines.append(f"{part_label(self.ui, part)}: "
                             + self.ui.t("graph_out_found" if answer.satisfied else "graph_out_missing"))
        self.result.configure(text="\n".join(lines))


def part_label(ui, part: Part) -> str:
    from app.gui.view.graph_editor import part_text
    return part_text(ui, part)


def panel_for(editor, parent, node) -> Panel:
    kind = {StartNode: StartPanel, WaitNode: WaitPanel, StopNode: StopPanel, RepeatNode: RepeatPanel,
            ClickNode: ClickPanel, MacroNode: MacroPanel, CheckNode: CheckPanel}[type(node)]
    return kind(editor, parent, node)
