r"""Visual check: the REAL application window, driven from inside, photographed screen by screen. Sends NO input.

    .venv\Scripts\python.exe tools\visual_check.py <scenario> <folder for the pictures>
    .venv\Scripts\python.exe tools\visual_check.py --list

It is what "check it visually" means in this project (the person's standing permission of 2026-10-25): Olive is
opened with a temporary macro folder (the person's own `data/` is never read or written), its screens are driven
through their own methods and Tk calls -- never SendInput, the pointer does not move, nothing is sent to any other
program -- and each moment worth seeing is saved as a PNG of Olive's own window, taken through the Windows provider
(the same capture a run uses). The scenarios are the functions below whose names start with `scenario_`; each
prints what it checked (ok / FAIL) and which pictures it saved, so the pictures can be looked at afterwards.

Olive's own window is the observed target where a scenario needs one (its pixels are stable), as in
tools/gui_smoke.py: the services are built with `own_pid=-1` so discovery lists it. Modal dialogs are answered by
the scenario (`answer`), as a person would answer them.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.bootstrap.main import _build_ocr, _enable_dpi_awareness  # noqa: E402
from app.bootstrap.windows_services import WindowsServices  # noqa: E402
from app.gui.app import App  # noqa: E402
from app.gui.library import MacroLibrary  # noqa: E402
from app.vision.bitmap import Bitmap, encode_png  # noqa: E402
from app.windows.win32 import Win32Backend, Win32InputBackend, Win32ObservationBackend  # noqa: E402

APP_TITLE = "Olive Automation"
WAKE_S = 2.0             # Tk can stand still in its wait for a Windows message (tools/gui_smoke.py): one WM_NULL wakes it


class Answer:
    """Stands in for a modal dialog: the scenario gives the answer a person would."""

    def __init__(self, value):
        self.value = value
        self._args = ()

    def __call__(self, *args, **kwargs):
        self._args = args
        return self

    def show(self):
        return self.value(*self._args) if callable(self.value) else self.value


class Session:
    """Olive, opened on a temporary library, with the helpers a scenario needs."""

    def __init__(self, out: Path, *, language: str = "ru", size: str = "1280x800+40+40") -> None:
        self.out = out
        self.failures: list[str] = []
        self.saved: list[Path] = []
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self._originals: dict = {}
        self._done = threading.Event()
        self._pumping = {"on": False, "tick": time.monotonic()}
        self.services = WindowsServices(Win32InputBackend(), Win32ObservationBackend, own_pid=-1)
        self.library = MacroLibrary(self.folder / "macros")
        self._meta_path = self.folder / "meta.json"      # what Olive remembers per macro (as data/macro_meta.json)
        self._language, self._size = language, size
        self._open()
        post = ctypes.WinDLL("user32").PostMessageW
        post.argtypes, post.restype = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL

        def watch() -> None:
            while not self._done.wait(0.25):
                if self._pumping["on"] and time.monotonic() - self._pumping["tick"] > WAKE_S:
                    post(self._hwnd, 0, 0, 0)        # WM_NULL: does nothing, no input, no focus, no pointer
                    self._pumping["tick"] = time.monotonic()
        threading.Thread(target=watch, daemon=True, name="visual-wake").start()
        self.pump(0.6)

    def _open(self) -> None:
        from app.gui.meta import MacroMeta
        self.app = App(self.services, self.library, auto_refresh=False, confirm=lambda t, m: True,
                       meta=MacroMeta(self._meta_path), ocr=_build_ocr(self.services.stop))
        self.c = self.app.controller
        self.app.apply_language(self._language)
        self.app.root.geometry(self._size)
        self._hwnd = int(self.app.root.wm_frame(), 16)

    def restart(self) -> None:
        """Olive closed and opened again on the same macro folder and the same remembered metadata: a new window
        (another handle), nothing chosen in the session any more."""
        for (module, name), value in self._originals.items():
            setattr(module, name, value)
        self._originals.clear()
        self.app.close()
        self._open()
        self.pump(0.6)

    # ---- driving -------------------------------------------------------------------------------------------
    def pump(self, seconds: float = 0.3, until=None) -> None:
        """Let the window work for `seconds` (or until `until()`), through mainloop as the application runs."""
        app = self.app
        end = time.time() + seconds

        def poll() -> None:
            self._pumping["tick"] = time.monotonic()
            if time.time() >= end or (until and until()):
                app.root.quit()
            else:
                app.root.after(10, poll)
        self._pumping["tick"], self._pumping["on"] = time.monotonic(), True
        try:
            app.root.after(0, poll)
            app.root.mainloop()
        finally:
            self._pumping["on"] = False
        app.root.update_idletasks()

    def answer(self, module, name, value) -> None:
        """Replace the dialog `module.name` with one that answers `value`."""
        self._originals.setdefault((module, name), getattr(module, name))
        setattr(module, name, Answer(value))

    def check(self, ok: bool, what: str) -> None:
        print(("  ok    " if ok else "  FAIL  ") + what, flush=True)
        if not ok:
            self.failures.append(what)

    # ---- targets and pictures --------------------------------------------------------------------------------
    def own_window(self):
        """THIS Olive's window as a target: found by its own handle, never by title alone."""
        self.c.refresh()
        self.pump(0.2)
        mine = f"w{int(self.app.root.wm_frame(), 16)}"
        found = [e for e in self.c.entries if e.ref == mine]
        return found[0] if len(found) == 1 else None

    def choose_own_window(self) -> None:
        entry = self.own_window()
        if entry is None:
            raise RuntimeError("Olive's own window is not in the list of targets.")
        self.c.select(entry.ref)
        self.pump(0.3)

    def screen(self):
        """A fresh screenshot of the chosen target, through the provider (as `ScreenPicker` gets one)."""
        job = self.c.begin_screen()
        return self.c.finish_screen(job(), job)

    def shot(self, name: str, *, widget=None) -> Path:
        """Olive's whole window (or its toplevel `widget`, a dialog) saved as `<out>/<name>.png`."""
        self.pump(0.25)
        top = widget.winfo_toplevel() if widget is not None else self.app.root
        width, height, rgb = _capture(Win32Backend(), int(top.wm_frame(), 16))
        path = self.out / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_png(Bitmap(width, height, rgb)))
        self.saved.append(path)
        print(f"  shot  {path}", flush=True)
        return path

    def close(self) -> None:
        for (module, name), value in self._originals.items():
            setattr(module, name, value)
        try:
            self.app.close()
        except Exception:
            pass
        self._done.set()
        self._tmp.cleanup()


def _capture(backend, hwnd: int) -> tuple[int, int, bytes]:
    """The window's client area, as the provider captures a target (PrintWindow), as RGB."""
    info = backend.query(hwnd)
    if info is None:
        raise RuntimeError("The window to photograph is gone.")
    raw = backend.capture(hwnd, info.client_width, info.client_height)
    bgra = raw.bgra
    rgb = bytearray(raw.width * raw.height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return raw.width, raw.height, bytes(rgb)


SCENARIOS: dict = {}


def scenario(fn):
    SCENARIOS[fn.__name__.removeprefix("scenario_")] = fn
    return fn


@scenario
def scenario_start(s: Session) -> None:
    """Olive as it opens: the Macros page and the Scripts page."""
    s.shot("01-macros")
    s.app.show_screen("scripts")
    s.pump()
    s.shot("02-scripts")


def _brand_mark(s: Session):
    """Where the brand mark (logo and name, top of the navigation rail) is in Olive's client area."""
    from app.vision.roi import Roi
    brand = s.app._mark.master
    root = s.app.root
    return Roi(brand.winfo_rootx() - root.winfo_rootx(), brand.winfo_rooty() - root.winfo_rooty(),
               brand.winfo_width(), brand.winfo_height())


def _open_script(s: Session, macro) -> object:
    """`macro` saved in the temporary library and opened in the editor; the editor screen."""
    path = s.library.save(macro)
    s.c.refresh_macros()
    s.app.show_screen("scripts")
    s.pump()
    scripts = s.app.screens["scripts"]
    scripts.select_row(s.c.library.key_of(path))
    scripts._edit_selected()
    s.pump(0.5)
    return s.app.screens["editor"]


@scenario
def scenario_confidence(s: Session) -> None:
    """A picture condition tested on the target at confidence 1, 0.9555, 0,9555 and 0.95."""
    from app.decide import Condition, ConditionalMacro, WaitUntil
    from app.gui import flow_edit as fe
    from app.vision.roi import Roi
    s.choose_own_window()
    mark = _brand_mark(s)
    screen = s.screen()
    logo = screen.frame.bitmap.crop(mark.x, mark.y, mark.width, mark.height)
    near = Roi(0, 0, mark.right + 40, mark.bottom + 20)
    seen = Condition.template(logo, region=near, min_confidence=0.95, label="логотип")
    editor = _open_script(s, ConditionalMacro("Проверка уверенности", (WaitUntil(seen, 20, 0.2),)))
    editor.tree.selection_set("s:0")
    s.pump(0.5)
    cond = editor.form.condition
    for value in ("1", "0.9555", "0,9555", "0.95", "0.99999"):
        cond.vars[fe.TEMPLATE]["min_confidence"].set(value)
        cond.lbl_result.configure(text="")
        cond._test()
        s.pump(15, until=lambda: cond.lbl_result.cget("text") not in ("", s.app.t("probe_running")))
        text = cond.lbl_result.cget("text")
        error = editor.lbl_form_error.cget("text") if hasattr(editor, "lbl_form_error") else ""
        s.check(text.startswith("Сейчас выполняется"), f"confidence {value}: {text!r} {error!r}")
        s.shot("confidence-" + value.replace(",", "_").replace(".", "_"))
    # a live screen is rarely the picture to the last pixel: the same logo with a few pixels a little different
    from app.vision.bitmap import Bitmap
    rgb = bytearray(logo.rgb)
    for i in range(0, len(rgb), 97):
        rgb[i] = min(255, rgb[i] + 6)
    nearly = Condition.template(Bitmap(logo.width, logo.height, bytes(rgb)), region=near, min_confidence=0.95)
    editor.go_back()
    s.pump()
    editor = _open_script(s, ConditionalMacro("Почти та же картинка", (WaitUntil(nearly, 20, 0.2),)))
    editor.tree.selection_set("s:0")
    s.pump(0.5)
    cond = editor.form.condition
    for value, any_size in (("1", "0"), ("0.9999", "0"), ("0.9995", "0"), ("1", "1"), ("0.95", "1")):
        cond.vars[fe.TEMPLATE]["min_confidence"].set(value)
        cond.vars[fe.TEMPLATE]["any_size"].set(any_size)
        cond.lbl_result.configure(text="")
        cond._test()
        s.pump(15, until=lambda: cond.lbl_result.cget("text") not in ("", s.app.t("probe_running")))
        text = cond.lbl_result.cget("text")
        print(f"  info  nearly the same picture, confidence {value}, any size {any_size}: {text!r}")
        s.shot(f"nearly-{value.replace('.', '_')}-any{any_size}")


@scenario
def scenario_big_picture(s: Session) -> None:
    """Pictures as large as the screen, or nearly, with and without "any size": what "test it now" says."""
    from app.decide import Condition, ConditionalMacro, WaitUntil
    from app.gui import flow_edit as fe
    from app.vision.roi import Roi
    s.choose_own_window()
    screen = s.screen()
    W, H = screen.width, screen.height
    cases = (("whole", Roi(0, 0, W, H), None), ("nearly", Roi(5, 5, W - 10, H - 10), None),
             ("half", Roi(0, 0, W // 2, H // 2), None), ("in-area", Roi(10, 10, 300, 200), Roi(0, 0, 320, 220)),
             ("area-too-small", Roi(10, 10, 300, 200), Roi(0, 0, 200, 150)))
    editor = None
    for name, roi, region in cases:
        picture = screen.frame.bitmap.crop(roi.x, roi.y, roi.width, roi.height)
        if editor is not None:
            editor.go_back()
            s.pump()
        cond = Condition.template(picture, region=region, min_confidence=0.95)
        editor = _open_script(s, ConditionalMacro(f"Большая картинка {name}", (WaitUntil(cond, 20, 0.2),)))
        editor.tree.selection_set("s:0")
        s.pump(0.5)
        form = editor.form.condition
        for any_size in ("0", "1"):
            form.vars[fe.TEMPLATE]["any_size"].set(any_size)
            form.lbl_result.configure(text="")
            form._test()
            s.pump(20, until=lambda: form.lbl_result.cget("text") not in ("", s.app.t("probe_running")))
            note = form.dialog.lbl_note.cget("text") if hasattr(form.dialog, "lbl_note") else ""
            print(f"  info  {name} {roi.width}x{roi.height} in {W}x{H}, any size {any_size}: "
                  f"{form.lbl_result.cget('text')!r} | status {s.c.status.text!r} | {note!r}")
            s.shot(f"big-{name}-any{any_size}")


@scenario
def scenario_resized_area(s: Session) -> None:
    """A picture taken at one window size, then the window resized and an area picked: what "test it now" says."""
    from app.decide import Condition, ConditionalMacro, WaitUntil
    from app.gui.view import flow as flow_view
    from app.vision.roi import Roi
    s.choose_own_window()
    mark = _brand_mark(s)
    screen = s.screen()
    W0, H0 = screen.width, screen.height
    logo = screen.frame.bitmap.crop(mark.x, mark.y, mark.width, mark.height)
    seen = Condition.template(logo, min_confidence=0.9, label="логотип")
    editor = _open_script(s, ConditionalMacro("Область после изменения размера", (WaitUntil(seen, 20, 0.2),),
                                              size=(W0, H0), adapt=True))
    print(f"  info  picked at {W0}x{H0}")
    for size in ("1180x740", "1600x1000", "900x560"):
        s.app.root.geometry(size)
        s.pump(0.6)
        editor.tree.selection_set("s:0")
        s.pump(0.4)
        cond = editor.form.condition
        now = s.screen()
        for name, roi in (("whole", Roi(0, 0, now.width, now.height)), ("corner", Roi(0, 0, 320, 160))):
            s.answer(flow_view, "ScreenPicker", {"roi": roi})
            cond._pick("region", cond._set_region)
            s.pump(10, until=lambda: cond.region is not None and cond.region.width == roi.width)
            cond.lbl_result.configure(text="")
            cond._test()
            s.pump(20, until=lambda: cond.lbl_result.cget("text") not in ("", s.app.t("probe_running")))
            print(f"  info  window {now.width}x{now.height}, area {name} {roi}: {cond.lbl_result.cget('text')!r} "
                  f"| picked {cond.picked}")
            s.shot(f"resized-{size}-{name}")


@scenario
def scenario_wheel(s: Session) -> None:
    """The wheel turned over the "what to look for" list of the properties panel: the panel scrolls, the choice stays."""
    from app.decide import Condition, ConditionalMacro, WaitUntil
    from app.vision.bitmap import Bitmap
    picture = Bitmap(20, 10, bytes((x * 9 + y * 31 + c * 70) % 256 for y in range(10) for x in range(20)
                                   for c in range(3)))
    editor = _open_script(s, ConditionalMacro("Колесо мыши", (WaitUntil(Condition.template(picture), 20, 0.2),)))
    editor.tree.selection_set("s:0")
    s.pump(0.5)
    cond = editor.form.condition
    box = next(w for w in _descendants(editor.form.frame) if w.winfo_class() == "TCombobox"
               and w.cget("textvariable") == str(cond.kind_var))
    before = cond.kind_var.get()
    s.shot("wheel-before")
    canvas = next(w for w in _ancestors(box) if getattr(w, "olive_scrolls", False))
    top_before = canvas.yview()[0]
    for _ in range(6):
        box.event_generate("<MouseWheel>", delta=-120, when="now")
        s.pump(0.05)
    s.check(cond.kind_var.get() == before, f"the choice stayed {before!r} (now {cond.kind_var.get()!r})")
    s.check(canvas.yview()[0] > top_before, f"the panel scrolled ({top_before:.2f} -> {canvas.yview()[0]:.2f})")
    s.shot("wheel-after")


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _ancestors(widget):
    widget = widget.master
    while widget is not None:
        yield widget
        widget = widget.master


# ---- a run's window in front (task 7 of 2026-10-25): two windows of this tool's own, in processes of their own -----
def _child_target() -> None:
    """A window of this tool's own (red, a green rectangle in its middle), shown without being activated."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from windows_click_smoke import TestWindow
    window = TestWindow(marker=(0.25, 0.25, 0.5, 0.5))
    window.start()
    ctypes.windll.user32.MoveWindow(window.hwnd, 300, 260, 520, 420, True)
    print(window.hwnd, flush=True)
    time.sleep(60)


def _child_cover() -> None:
    """Another window of this tool's own, put over the target and given the focus, as a browser would be."""
    import tkinter as tk
    cover = tk.Tk()
    cover.title("Visual check: a window over the target")
    cover.geometry("640x520+260+220")
    tk.Label(cover, text="Окно поверх цели", font=("Segoe UI", 20)).pack(expand=True)
    cover.update()
    cover.lift()
    cover.focus_force()
    cover.update()
    print(int(cover.wm_frame(), 16), flush=True)
    end = time.time() + 60
    while time.time() < end:
        cover.update()
        time.sleep(0.02)


@scenario
def scenario_graph(s: Session) -> None:
    """The new script editor: a new script, blocks of every kind joined by arrows, each block's panel."""
    from app.gui import graph_edit as ge
    s.app.show_screen("scripts")
    s.pump()
    s.check(s.c.new_script("Новый сценарий"), f"a new script: {s.c.status.text}")
    s.app.show_screen("editor")
    s.pump(0.8)
    s.check(s.app.screen == "graph", f"it opens in the new editor ({s.app.screen})")
    s.check(not s.app.nav.winfo_ismapped(), "the navigation steps aside")
    s.shot("graph-01-new")
    editor = s.app.screens["graph"]
    apply = s.c.editor_apply_graph
    check = apply(ge.add_node, "check", (0, 150))
    click = apply(ge.add_node, "click", (-200, 330), where="match")
    repeat = apply(ge.add_node, "repeat", (240, 330), mode="times", times=5)
    mixed = apply(ge.add_node, ge.MIXED, (-200, 510), mode="all")
    wait = apply(ge.add_node, "wait", (-420, 700), seconds=2)
    macro = apply(ge.add_node, "macro", (20, 700))
    stop = apply(ge.add_node, "stop", (240, 520), success=False, message="Кнопки нет")
    for edge in (("start", "next", check), (check, "found", click), (check, "missing", repeat),
                 (repeat, "again", check), (repeat, "done", stop), (click, "next", mixed), (mixed, "found", wait),
                 (mixed, "missing", macro)):
        s.check(apply(ge.connect, *edge) is not None, f"arrow {edge}")
    s.app.render()
    s.pump(0.3)
    editor._fit()
    s.pump(0.5)
    s.shot("graph-02-blocks")
    for node in (check, click, repeat, mixed, wait, macro, stop, "start"):
        editor._select(node)
        s.pump(0.4)
        s.shot(f"graph-03-panel-{node}")
    editor._select(None)
    editor.toggle_palette()
    s.pump(0.3)
    s.shot("graph-04-palette-closed")
    editor.toggle_palette()
    s.pump(0.3)


@scenario
def scenario_graph_run(s: Session) -> None:
    """The new editor end to end on Olive's own window: a picture taken by picking on the window, tested, and the
    script run from the editor with its blocks lit (it only looks and waits: nothing is clicked)."""
    from app.gui import graph_edit as ge
    from app.gui.view import graph_panels
    s.choose_own_window()
    mark = _brand_mark(s)
    s.app.show_screen("scripts")
    s.pump()
    s.check(s.c.new_script("Логотип на месте"), s.c.status.text)
    s.app.show_screen("editor")
    s.pump(0.6)
    editor = s.app.screens["graph"]
    editor._select("start")
    editor.add_block("check", (0, 160))
    s.pump(0.3)
    panel = editor.panel
    s.answer(graph_panels, "OverlayPicker", {"roi": mark})        # the person drags round the logo on the window
    take = next(w for w in _descendants(panel.frame) if w.winfo_class() == "TButton"
                and w.cget("text") == s.app.t("graph_btn_take_picture"))
    take.invoke()
    s.pump(10, until=lambda: bool(s.c.editor.document.node("check1").parts))
    s.check(bool(s.c.editor.document.node("check1").parts), "the picture was taken from the window")
    s.pump(0.4)
    s.shot("run-01-picture-taken")
    test = next(w for w in _descendants(editor.panel.frame) if w.winfo_class() == "TButton"
                and w.cget("text") == s.app.t("graph_btn_test"))
    test.invoke()
    s.pump(10, until=lambda: editor.panel.result.cget("text") not in ("", s.app.t("probe_running")))
    s.check(editor.panel.result.cget("text").startswith(s.app.t("graph_test_found")),
            f"tested on the target: {editor.panel.result.cget('text')!r}")
    s.shot("run-02-tested")
    wait = s.c.editor_apply_graph(ge.add_node, "wait", (-200, 330), seconds=2)
    repeat = s.c.editor_apply_graph(ge.add_node, "repeat", (240, 330), mode="times", times=2)
    stop = s.c.editor_apply_graph(ge.add_node, "stop", (-200, 500))
    for edge in (("check1", "found", wait), ("check1", "missing", repeat), (repeat, "again", "check1"),
                 (wait, "next", stop)):
        s.c.editor_apply_graph(ge.connect, *edge)
    editor._select(None)
    s.app._confirm = lambda title, text: True                     # "save and run?": yes
    editor.run()
    s.check(s.c.run_active, f"the run started from the editor: {s.c.status.text}")
    s.pump(1.0)
    s.check(s.c.graph_lit.get(wait) == "running", f"the wait block is lit while it runs: {s.c.graph_lit}")
    s.shot("run-03-running")
    s.pump(15, until=lambda: not s.c.run_active)
    result = s.c.run_results[0] if s.c.run_results else None
    s.check(result is not None and result.state.value == "completed", f"the run: {result}")
    s.app.render()
    s.pump(0.4)
    s.shot("run-04-done")


@scenario
def scenario_graph_fail(s: Session) -> None:
    """A run from the new editor that stops: the block it stopped in bright red, and why, where and what to do --
    in the editor and in the history (it only looks: nothing is clicked)."""
    from app.decide import Condition, Part
    from app.gui import graph_edit as ge
    from app.vision.bitmap import Bitmap
    s.choose_own_window()
    s.check(s.c.new_script("Кнопки нет"), s.c.status.text)
    s.app.show_screen("editor")
    s.pump(0.6)
    editor = s.app.screens["graph"]
    absent = Bitmap(24, 16, bytes((x * 37 + y * 91 + c * 113) % 256 for y in range(16) for x in range(24)
                                  for c in range(3)))
    check = s.c.editor_apply_graph(ge.add_node, "check", (0, 160),
                                   parts=(Part(Condition.template(absent, min_confidence=0.95)),))
    stop = s.c.editor_apply_graph(ge.add_node, "stop", (220, 330), success=False, message="Кнопки «Забрать» нет")
    s.c.editor_apply_graph(ge.connect, "start", "next", check)
    s.c.editor_apply_graph(ge.connect, check, "missing", stop)
    s.app._confirm = lambda title, text: True
    editor.run()
    s.pump(15, until=lambda: not s.c.run_active)
    s.app.render()
    s.pump(0.5)
    result = s.c.run_results[0]
    s.check(result.state.value == "failed", f"the run failed: {result.detail}")
    s.check(s.c.graph_lit.get(stop) == "failed", f"the stop block is red: {s.c.graph_lit}")
    s.check(editor.banner.winfo_ismapped(), "the editor says why")
    print("  info  " + editor.lbl_banner.cget("text").replace("\n", " | "))
    s.shot("fail-01-editor")
    editor.go_back()
    s.app.show_screen("history")
    s.pump(0.4)
    history = s.app.screens["history"]
    rows = [child for row in history.tree.get_children() for child in (history.tree.get_children(row) or (row,))]
    if rows:
        history.tree.selection_set(rows[0])                       # the run itself, not its day's heading
        s.pump(0.4)
    s.check("Почему" in history.lbl_results.cget("text"), f"the history says why: "
                                                         f"{history.lbl_results.cget('text')!r}")
    s.shot("fail-02-history")


@scenario
def scenario_memory(s: Session) -> None:
    """Item 12 of 2026-10-25: a script made on a target remembers it. Olive is opened again (a new window: another
    handle, nothing chosen) and the script shows that target with Run ready; its editor opens on it."""
    from app.gui import graph_edit as ge
    title = "Olive Automation — проверка памяти"   # another Olive may be open: alike windows are told by the title
    s.app.root.title(title)
    s.choose_own_window()
    before = s.c.selected.ref
    s.app.show_screen("scripts")
    s.pump()
    s.check(s.c.new_script("Запомненная цель"), f"a new script: {s.c.status.text}")
    s.app.show_screen("editor")
    s.pump(0.8)
    wait = s.c.editor_apply_graph(ge.add_node, "wait", (0, 150), seconds=1)
    s.check(s.c.editor_apply_graph(ge.connect, "start", "next", wait) is not None, "start -> wait")
    s.check(s.c.save_editor(), f"saved: {s.c.status.text}")
    key = s.c.editor_key
    s.restart()
    s.check(s.c.target is None and not s.c.entries, "opened again: nothing chosen, nothing listed yet")
    s.app.root.title(title)
    s.pump(0.2)
    s.c.refresh()
    alike = [e.ref for e in s.c.entries if e.process == "python.exe" and e.kind == "TkTopLevel"]
    print(f"  info  Olive windows listed now: {len(alike)}")
    s.app.show_screen("scripts")
    s.pump()
    scripts = s.app.screens["scripts"]
    scripts.select_row(key)
    s.pump(0.4)
    mine = f"w{int(s.app.root.wm_frame(), 16)}"           # its handle as listed (taken once the window is shown)
    s.check(mine != before, f"Olive's window has another handle now ({before} -> {mine})")
    s.check(s.c.run_refs == (mine,), f"the script's run target is the new window: {s.c.run_refs}")
    s.check(s.c.can_run(), f"Run is ready ({s.c.why_cannot_run()!r})")
    s.shot("memory-01-scripts")
    scripts._edit_selected()
    s.pump(0.8)
    s.check(s.app.screen == "graph", f"the editor is open ({s.app.screen})")
    s.check(s.c.selected is not None and s.c.selected.ref == mine and s.c.target_ready,
            "the editor opens on the remembered target, verified")
    s.check(s.c.status.key == "msg_editing_on_target", f"and says so: {s.c.status.text}")
    s.shot("memory-02-editor")
    # opened again under another title: only offered (pre-ticked in the picker), never chosen by itself
    s.restart()
    s.app.root.title(title + " (2)")
    s.pump(0.2)
    s.c.refresh()
    s.app.show_screen("scripts")
    s.pump()
    scripts = s.app.screens["scripts"]
    scripts.select_row(key)
    s.pump(0.4)
    mine = f"w{int(s.app.root.wm_frame(), 16)}"
    s.check(s.c.run_refs == () and not s.c.can_run(), f"another title: nothing chosen ({s.c.run_refs})")
    alike = [e.ref for e in s.c.entries if e.process == "python.exe" and e.kind == "TkTopLevel"]
    offered = (mine,) if alike == [mine] else ()               # with another Olive open, two look alike: no offer
    s.check(s.c.suggested_run_refs() == offered,
            f"{len(alike)} alike window(s): offered for a confirmation {s.c.suggested_run_refs()}")
    s.shot("memory-03-offered")


@scenario
def scenario_places(s: Session) -> None:
    """Item 12, part B: a Full Windows recording knows the window of each click. Its panel names the windows; a
    DRY run while that window is not open is refused before anything, and the history says which window, in
    Russian. Nothing is sent: a dry run, and a window no program has."""
    from app.playback.macro import Macro
    from app.providers.recording import (Place, PointerButton as B, RecordedEvent as E, RecordedEventKind as K,
                                         Recording)
    s.c.refresh()
    s.pump(0.2)
    full = next(e for e in s.c.entries if e.whole_screen)
    s.check(s.c.select(full.ref), f"Full Windows chosen: {s.c.status.text}")
    place = Place("olive-visual-check.exe", "CheckWnd", "Окно проверки", 800, 600, True, 0)
    events = (E(K.BUTTON_DOWN, 0, 100, 100, B.PRIMARY, 1, place=(0, 40, 50)),
              E(K.BUTTON_UP, 40, 100, 100, B.PRIMARY, 1, place=(0, 40, 50)))
    macro = Macro(Recording(full.provider_id, s.c.target.target_id, events, places=(place,)), "Клик в окне")
    key = s.library.key_of(s.library.save(macro))
    s.c.refresh_macros()
    s.app.show_screen("macros")
    s.pump()
    macros = s.app.screens["macros"]
    macros.select_row(key)
    s.pump(0.4)
    s.check(s.c.run_refs == (full.ref,) or s.c.set_run_refs([full.ref]), "its run target: Full Windows")
    s.app.render()
    s.pump(0.3)
    s.shot("places-01-panel")
    s.check(s.c.start_run(dry_run=True), f"a dry run: {s.c.status.text}")
    s.pump(15, until=lambda: not s.c.run_active)
    result = s.c.run_results[0]
    s.check(result.state.value == "refused" and "place_missing" in result.detail, f"refused: {result.detail!r}")
    s.app.render()
    s.pump(0.5)
    text = s.app.status_text()                       # a dry run is in no history: its notice says why
    s.check("Окно «Окно проверки» программы olive-visual-check.exe" in text, f"the notice names it: {text!r}")
    s.shot("places-02-notice")


def _line_within(child, seconds: float) -> str:
    """The first line a child process prints, or an error after `seconds` (a child that hangs ends the check)."""
    got: list[str] = []
    reader = threading.Thread(target=lambda: got.append(child.stdout.readline()), daemon=True)
    reader.start()
    reader.join(seconds)
    if not got or not got[0].strip():
        child.kill()
        raise RuntimeError("a window of this check did not start in time")
    return got[0]


@scenario
def scenario_front(s: Session) -> None:
    """A covered window: a script run brings it to the front (conditions only, nothing is clicked). Covered by Olive
    itself (Olive holds the focus, as when Run was just pressed: the target becomes the main window), then by another
    program's window that holds the focus (the target is raised over it)."""
    import subprocess
    from app.decide import Condition, ConditionalMacro, WaitUntil
    u = ctypes.WinDLL("user32")
    u.GetForegroundWindow.restype = wt.HWND
    u.WindowFromPoint.restype, u.WindowFromPoint.argtypes = wt.HWND, [wt.POINT]
    u.GetAncestor.restype, u.GetAncestor.argtypes = wt.HWND, [wt.HWND, ctypes.c_uint]
    tool = [sys.executable, str(Path(__file__).resolve())]
    target = subprocess.Popen(tool + ["--child-target"], stdout=subprocess.PIPE, text=True)
    cover = None
    olive = int(s.app.root.wm_frame(), 16)
    try:
        hwnd = int(_line_within(target, 10))
        time.sleep(0.5)

        def on_top() -> int:
            hit = u.WindowFromPoint(wt.POINT(300 + 260, 260 + 210))
            return int(u.GetAncestor(hit, 2) or 0) if hit else 0

        def foreground() -> int:
            return int(u.GetForegroundWindow() or 0)

        s.c.refresh()
        s.pump(0.3)
        entry = next(e for e in s.c.entries if e.ref == f"w{hwnd}")
        green = WaitUntil(Condition.pixel((250, 190), (0, 255, 0), label="зелёный прямоугольник"), 10, 0.1)
        path = s.library.save(ConditionalMacro("Окно вперёд", (green, green)))
        s.c.refresh_macros()
        s.app.show_screen("scripts")
        s.pump()
        scripts = s.app.screens["scripts"]
        scripts.select_row(s.c.library.key_of(path))

        def run(label: str) -> None:
            s.c.set_run_refs([entry.ref])
            s.pump()
            scripts._run()
            s.check(s.c._escape is not None and s.c._escape.active, f"{label}: Esc anywhere is watched during the run")
            s.pump(15, until=lambda: not s.c.run_active)
            result = s.c.run_results[0]
            s.check(result.state.value == "completed", f"{label}: the run {result.state.value} {result.detail}")
            s.check(on_top() == hwnd, f"{label}: the target is on top now ({on_top()}, target {hwnd})")
            s.check(s.c._escape is None, f"{label}: the Esc watch ended with the run")

        # 1. Olive itself over the target, holding the focus (the person has just pressed Run)
        s.app.root.geometry("900x700+200+180")
        s.app.root.lift()
        s.app.root.focus_force()
        s.pump(0.6)
        s.check(on_top() == olive, f"1. before: Olive covers the target ({on_top()}, Olive {olive})")
        print(f"  info  1. the focus before the run: {'Olive' if foreground() == olive else foreground()}")
        run("1. covered by Olive")
        s.check(foreground() == hwnd, f"1. the target is now the main window (focus: {foreground()})")

        # 2. another program's window over it, holding the focus
        cover = subprocess.Popen(tool + ["--child-cover"], stdout=subprocess.PIPE, text=True)
        cover_hwnd = int(_line_within(cover, 10))
        time.sleep(0.8)
        s.check(on_top() == cover_hwnd, f"2. before: another program covers the target ({on_top()})")
        run("2. covered by another program")
        print(f"  info  2. the focus after the run: {'the target' if foreground() == hwnd else foreground()} "
              "(Windows lets only the program that holds the focus pass it on: the target was raised over it)")
        s.app.root.lift()
        s.pump(0.3)
        s.shot("front-after-run")
    finally:
        for child in (cover, target):
            if child is not None:
                child.kill()


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--child-target":
        _child_target()
        return 0
    if argv and argv[0] == "--child-cover":
        _child_cover()
        return 0
    if not argv or argv[0] == "--list":
        for name, fn in SCENARIOS.items():
            print(f"{name:24} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    name, out = argv[0], Path(argv[1])
    if name not in SCENARIOS:
        print(f"Unknown scenario {name!r}; --list shows them.")
        return 2
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _enable_dpi_awareness()
    s = Session(out)
    try:
        SCENARIOS[name](s)
    except Exception:
        traceback.print_exc()
        s.failures.append("the scenario raised")
    finally:
        s.close()
    print("PASS" if not s.failures else "FAIL: " + "; ".join(s.failures))
    return 0 if not s.failures else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
