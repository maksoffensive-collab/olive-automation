"""Picking on the target itself (the person's list of 2026-10-25: "press the button, click the window, the point is
taken"): a picture of the target, at its own size, laid exactly over it -- so a click lands on what it shows.

A fresh screenshot of the chosen target is drawn 1:1 in a borderless window placed over the target's drawing area
(`screen_rect`, from the target's services). The person clicks a point, or drags a rectangle (an area, a picture);
Esc, a right click or the "Cancel" chip gives up. Nothing reaches the target: the click lands on Olive's picture of
it, and the target is never sent anything. What it returns is in the screenshot's own pixels -- the same space a
search and a click use -- exactly like the dialog picker (`flow.ScreenPicker`), which is used when the target has no
place on this computer's screen (a device over its own connection).
"""
from __future__ import annotations

import base64
import tkinter as tk

from app.gui import theme as th
from app.vision.roi import Roi

HINT_H = 34


class OverlayPicker:
    """`show()` -> {"point": (x, y), "color": (r, g, b)} | {"roi": Roi} | None (cancelled)."""

    def __init__(self, ui, screen, rect: tuple[int, int, int, int], mode: str, *, hint: str = "") -> None:
        self.ui, self.screen, self.mode = ui, screen, mode
        self.result = None
        left, top, width, height = rect
        self.image = tk.PhotoImage(master=ui.root, data=base64.b64encode(screen.png))    # before any window
        self.top = top_ = tk.Toplevel(ui.root)
        top_.withdraw()                                   # shown only once it is whole (`show`)
        try:
            top_.overrideredirect(True)
            top_.attributes("-topmost", True)
            top_.geometry(f"{width}x{height}+{left}+{top}")
            top_.configure(background="#000000")
            self.canvas = c = tk.Canvas(top_, width=width, height=height, highlightthickness=0, cursor="crosshair",
                                        background="#000000")
            c.pack(fill="both", expand=True)
        except Exception:
            top_.destroy()
            raise
        c.create_image(0, 0, image=self.image, anchor="nw")
        c.create_rectangle(1, 1, width - 2, height - 2, outline=th.ACTIVE.error, width=3)
        band = min(HINT_H, max(20, height // 6))
        c.create_rectangle(0, 0, width, band, fill="#000000", stipple="gray50", outline="")
        c.create_text(12, band // 2, anchor="w", fill="#FFFFFF", font=(th.FONT_SEMIBOLD, 10),
                      text=hint or ui.t(f"overlay_hint_{mode}"))
        self._start: tuple[int, int] | None = None
        self._mark = None
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<ButtonPress-3>", lambda _e: self._close(None))
        top_.bind("<Escape>", lambda _e: self._close(None))

    def show(self):
        self.top.update_idletasks()
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
        try:
            self.top.grab_set()
        except tk.TclError:
            pass
        self.top.wait_window()
        return self.result

    def _inside(self, x: int, y: int) -> tuple[int, int]:
        return min(max(int(x), 0), self.screen.width - 1), min(max(int(y), 0), self.screen.height - 1)

    def _press(self, event) -> None:
        self._start = self._inside(event.x, event.y)
        if self.mode in ("point", "color"):
            point = self._start
            self._close({"point": point, "color": self.screen.frame.bitmap.pixel(*point)})

    def _drag(self, event) -> None:
        if self._start is None or self.mode in ("point", "color"):
            return
        if self._mark is not None:
            self.canvas.delete(self._mark)
        x, y = self._inside(event.x, event.y)
        self._mark = self.canvas.create_rectangle(self._start[0], self._start[1], x, y, outline=th.ACTIVE.error,
                                                  width=2)

    def _release(self, event) -> None:
        if self._start is None or self.mode in ("point", "color"):
            return
        (x0, y0), (x1, y1) = self._start, self._inside(event.x, event.y)
        self._start = None
        left, right, top, bottom = min(x0, x1), max(x0, x1) + 1, min(y0, y1), max(y0, y1) + 1
        if right - left < 2 or bottom - top < 2:
            return                                     # a click, not a rectangle: keep waiting
        self._close({"roi": Roi(left, top, right - left, bottom - top)})

    def _close(self, result) -> None:
        self.result = result
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()
