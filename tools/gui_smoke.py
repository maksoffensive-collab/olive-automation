r"""GUI smoke test: the REAL application window, driven through its own screens, over the real Windows provider.

    .venv\Scripts\python.exe tools\gui_smoke.py [screenshot.png]

What it walks, in the order a person would (every check prints ok / FAIL):

   1. open the application (1120 x 680, the declared minimum)
   2. the library: a recorded macro on the Macros page and a script on the Scripts page, the template folder not
      offered as a folder
   3. the recorded macro: chosen, described, dry-run on an explicitly chosen target
   4. the script: called a script, its steps described
   5. Convert to a script: a new script, the original byte-for-byte unchanged
   6. open the script editor
   7. edit: a WAIT UNTIL added in the properties panel, its template CAPTURED from a real screenshot (taken off the
      UI thread through the provider) and tested on the target before it is applied
   8. save (Ctrl+S): the template stored as <macro>.assets/template-<sha256>.png
   9. reopen: the same steps, read back from disk with the template hash-checked
  10. run: the v2 macro on its target; foreground window and pointer unchanged
  11. history: the run, with its per-target result
  12. target selection: an empty choice disables Run and is refused, never widened to "all"
  13. Emergency Stop ends a v2 run that is waiting
  14. both languages
  15. Read text: this GUI's own window read by the real OCR engine through the Read-text screen (skipped when the
      machine has no engine); nothing is sent, the pointer and the foreground window stay where they were

Why it changed. The previous version drove the single-screen interface of the first GUI slice through its private
widgets and handlers (`app.targets`, `app.macros`, `lbl_status_pill`, `_on_load` / `_on_bind` / `_on_play`,
`toggle_advanced`). The redesign that began at 5f5701a (sidebar pages) and was finished by c193690 / c9264db
(macro list -> run targets -> batch Run, editor, history) removed every one of them, so it stopped at its first
check with AttributeError. The application-wide redesign of 2026-10-18/19 (window title "Olive Automation", the
brand mark in the navigation rail, recorded macros and scripts on two pages, steps edited in a properties panel)
is followed here too: the brand mark's place is measured from the window itself, not assumed. It also relied on real playback into a test window; Windows playback has since become
real hardware input (`SendInput`, app/windows/win32.py), which would move the person's pointer. So this version
sends NO input at all: a run here is observation only (WAIT UNTIL steps look, and complete) or a dry run, and the
click-test window must receive nothing. Real input through the GUI is exercised on device targets by
tools/gui_ldplayer_smoke.py, where it moves nothing on this desktop.

The observed target is this GUI's own window (its pixels are stable and rich enough to hold a template); the
application normally leaves its own windows out of discovery, so this script builds the services with
`own_pid=-1`. Modal questions (a name, a target) are answered by the script instead of a person; everything else
is the real screen, the real controller and the real provider. The temporary macro folder is deleted at the end;
the user's own macro folder is never touched.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import faulthandler
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_click_smoke import TestWindow  # noqa: E402

from app.bootstrap.main import _build_ocr, _enable_dpi_awareness  # noqa: E402
from app.bootstrap.windows_services import WindowsServices  # noqa: E402
from app.decide import Condition, ConditionalMacro, Decision, TapMatch, WaitUntil  # noqa: E402
from app.decide.format import ASSET_NAME, asset_dir_for  # noqa: E402
from app.gui import flow_edit as fe  # noqa: E402
from app.gui.app import App  # noqa: E402
from app.gui.controller import RunState  # noqa: E402
from app.gui.library import MacroLibrary  # noqa: E402
from app.gui.view import dialogs  # noqa: E402
from app.gui.view import flow as flow_view  # noqa: E402
from app.playback.macro import Macro  # noqa: E402
from app.providers.recording import PointerButton, RecordedEvent as E, RecordedEventKind as K, Recording  # noqa
from app.vision.roi import Roi  # noqa: E402
from app.windows.win32 import Win32InputBackend, Win32ObservationBackend  # noqa: E402

TEST_TITLE = "Automation platform click test"
APP_TITLE = "Olive Automation"
STALL_S = 20.0           # no check for this long: a step is stuck, so where every thread waits is printed
WAKE_S = 2.0             # the window pumped but still for this long: woken with WM_NULL, and counted
user32 = ctypes.windll.user32


def desktop_state():
    point = wt.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return user32.GetForegroundWindow(), (point.x, point.y)


class Answer:
    """Stands in for a modal dialog: the script gives the answer a person would."""

    def __init__(self, value):
        self.value = value
        self._args = ()

    def __call__(self, *args, **kwargs):
        self._args = args                    # what the dialog was opened with, for an answer that depends on it
        return self

    def show(self):
        return self.value(*self._args) if callable(self.value) else self.value


def main() -> int:
    _enable_dpi_awareness()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "screenshots" / "gui_smoke.png"
    window = TestWindow()
    window.start()
    failures: list[str] = []
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name)
    app = None
    originals = {}
    done = threading.Event()                 # ends the watcher below
    pumping = {"woken": 0}

    started = time.monotonic()
    # a step that hangs (the person saw one go on only once the mouse was moved over the window): every thread's
    # stack is printed after STALL_S without a check, and again every STALL_S, so the place it waits is seen
    faulthandler.dump_traceback_later(STALL_S, repeat=True)

    def check(ok: bool, what: str) -> None:
        faulthandler.cancel_dump_traceback_later()
        print(f"[{time.monotonic() - started:6.1f} s] " + ("  ok    " if ok else "  FAIL  ") + what, flush=True)
        if not ok:
            failures.append(what)
        faulthandler.dump_traceback_later(STALL_S, repeat=True)

    def answer(module, name, value) -> None:
        originals.setdefault((module, name), getattr(module, name))
        setattr(module, name, Answer(value))

    try:
        services = WindowsServices(Win32InputBackend(), Win32ObservationBackend, own_pid=-1)
        library = MacroLibrary(folder)
        app = App(services, library, auto_refresh=False, confirm=lambda t, m: True, ocr=_build_ocr(services.stop))
        c = app.controller
        macros, scripts, editor = app.screens["macros"], app.screens["scripts"], app.screens["editor"]
        app.root.geometry("1120x680+60+60")

        # Tk on Windows can stand still inside its own wait for a Windows message, timers or not, until ANY message
        # arrives (seen on 2026-10-20: 126 s at step 7, under update() and under mainloop alike, always right after
        # a capture of this window; a mouse moved over the window, or one WM_NULL, ended it). While the script
        # pumps, a pause of WAKE_S without a poll is ended with WM_NULL -- a message that does nothing: no input, no
        # focus, no pointer -- and counted, so the run never depends on the person's mouse and still says it happened.
        hwnd = int(app.root.wm_frame(), 16)          # read here: a Tk call from the watcher would wait for this thread
        post = ctypes.WinDLL("user32").PostMessageW
        post.argtypes, post.restype = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL
        pumping.update(on=False, tick=time.monotonic())

        def watch() -> None:
            while not done.wait(0.25):
                if pumping["on"] and time.monotonic() - pumping["tick"] > WAKE_S:
                    post(hwnd, 0, 0, 0)              # WM_NULL
                    pumping["woken"] += 1
                    pumping["tick"] = time.monotonic()
        threading.Thread(target=watch, daemon=True, name="smoke-wake").start()

        def pump(seconds: float = 0.3, until=None) -> None:
            """Let the window work for `seconds` (or until `until()`) through mainloop, as the application runs."""
            end = time.time() + seconds

            def poll() -> None:
                pumping["tick"] = time.monotonic()
                if time.time() >= end or (until and until()):
                    app.root.quit()
                else:
                    app.root.after(10, poll)
            pumping["tick"], pumping["on"] = time.monotonic(), True
            try:
                app.root.after(0, poll)
                app.root.mainloop()
            finally:
                pumping["on"] = False
            app.root.update_idletasks()

        def entry(title):
            return next((e for e in c.entries if e.title.startswith(title)), None)

        def own_window():
            """THIS GUI's window: found by its own window handle, so another "Olive Automation" the person has open
            is never taken for it (by title only when exactly one window has it)."""
            mine = f"w{int(app.root.wm_frame(), 16)}"
            found = [e for e in c.entries if e.ref == mine] or [e for e in c.entries if e.title == APP_TITLE]
            return found[0] if len(found) == 1 else None

        def run(page, refs, *, dry_run=False, timeout=60.0):
            c.set_run_refs(refs)
            page._run(dry_run=dry_run)
            pump(timeout, until=lambda: not c.run_active)

        def brand_mark() -> Roi:
            """Where the brand mark (the logo and "Olive Automation" at the top of the navigation rail) is in this
            window's client area, which is what a screenshot of it shows: constant while the application runs."""
            brand = app._mark.master
            return Roi(brand.winfo_rootx() - app.root.winfo_rootx(), brand.winfo_rooty() - app.root.winfo_rooty(),
                       brand.winfo_width(), brand.winfo_height())

        # ---- 1. open ---------------------------------------------------------------------------------------
        print("1. open the application")
        pump(0.6)
        check(app.root.winfo_viewable() == 1, f"window visible ({app.root.winfo_geometry()})")
        check(app.root.minsize() == (1120, 680) and app.root.winfo_width() == 1120, "shown at the 1120 x 680 minimum")
        check(app.screen == "macros", "opens on the macro list")
        app._on_refresh()
        pump()
        test, gui = entry(TEST_TITLE), own_window()
        LOGO = brand_mark()
        check(LOGO.width > 40 and LOGO.height > 20, f"the brand mark is at {LOGO}")
        check(test is not None and gui is not None, f"discovery lists the test window and this window "
                                                    f"({len(c.entries)} targets)")
        check(c.target is None and c.run_refs == (), "nothing chosen automatically")

        # ---- 2. the library ---------------------------------------------------------------------------------
        print("2. library: a recorded macro and a script, each on its own page")
        c.select(test.ref)
        test_id = c.target.target_id
        down = E(K.BUTTON_DOWN, 0, 40, 30, PointerButton.PRIMARY, 1)
        up = E(K.BUTTON_UP, 30, 40, 30, PointerButton.PRIMARY, 1)
        v1_path = library.save(Macro(Recording("windows", test_id, (down, up)), "Smoke recorded"))
        c.select(gui.ref)
        job = c.begin_screen()
        shot = job()
        screen = c.finish_screen(shot, job)
        check(screen is not None, f"screenshot of this window through the provider ({screen and screen.width}x"
                                  f"{screen and screen.height})")
        logo = screen.frame.bitmap.crop(LOGO.x, LOGO.y, LOGO.width, LOGO.height)
        near = Roi(0, 0, LOGO.right + 40, LOGO.bottom + 20)
        seen = Condition.template(logo, region=near, min_confidence=0.9, label="the brand mark")
        v2_path = library.save(ConditionalMacro("Smoke flow", (WaitUntil(seen, 20, 0.2),)))
        c.refresh_macros()
        app.render()
        pump()
        infos = {i.name: i for i in c.macros}
        check(list(macros._row_widgets) == [infos["Smoke recorded"].key], "the Macros page lists the recording only")
        app.show_screen("scripts")
        pump()
        check(list(scripts._row_widgets) == [infos["Smoke flow"].key], "the Scripts page lists the script only")
        app.show_screen("macros")
        pump()
        check(infos["Smoke recorded"].version == 1 and infos["Smoke flow"].version == 2, "v1 and v2 told apart")
        check(asset_dir_for(v2_path).is_dir() and c.folders() == [], "the template folder is not offered as a folder")

        # ---- 3. v1 --------------------------------------------------------------------------------------------
        print("3. recorded macro: chosen, described, dry run")
        macros.select_row(infos["Smoke recorded"].key)
        pump()
        check("1 action" in macros.lbl_meta.cget("text"), f"details: {macros.lbl_meta.cget('text')!r}")
        answer(dialogs, "ChooseTargets", (test.ref,))
        macros._choose_targets()
        check(c.run_refs == (test.ref,), "run target chosen explicitly")
        before = len(window.messages)
        run(macros, (test.ref,), dry_run=True)
        check(c.run_results and c.run_results[0].state is RunState.COMPLETED, f"v1 dry run: {c.run_results}")
        check(len(window.messages) == before, "the test window received nothing")

        # ---- 4. v2 --------------------------------------------------------------------------------------------
        print("4. script")
        app.show_screen("scripts")
        scripts.select_row(infos["Smoke flow"].key)
        pump()
        check(scripts.lbl_kind.cget("text") == app.t("kind_script"), f"details: {scripts.lbl_kind.cget('text')!r}")
        check("brand mark" in scripts.lbl_steps.cget("text"), "its steps are described")
        app.show_screen("macros")
        pump()

        # ---- 5. convert ---------------------------------------------------------------------------------------
        print("5. convert a recorded macro to a script")
        v1_bytes = v1_path.read_bytes()
        answer(dialogs, "TextPrompt", "Smoke recorded v2")
        macros._convert(infos["Smoke recorded"].key)
        pump()
        converted = c.selected_macro()
        check(converted is not None and converted.version == 2 and converted.name == "Smoke recorded v2",
              f"converted: {converted and (converted.name, converted.version)}")
        check(v1_path.read_bytes() == v1_bytes, "the v1 file is unchanged")

        # ---- 6. open the v2 editor ----------------------------------------------------------------------------
        print("6. open the script editor")
        app.show_screen("scripts")
        scripts.select_row(infos["Smoke flow"].key)
        scripts._edit_selected()
        pump()
        check(app.screen == "editor" and c.editing_v2, "the editor opened in v2 mode")
        check([r for r in editor.tree.get_children() if r != "start"] == ["s:0"],
              "one step listed (after the Start row)")

        # ---- 7. edit in the properties panel ----------------------------------------------------------------------
        print("7. edit: a step with a template captured from the screen")
        answer(flow_view, "ScreenPicker", {"roi": LOGO, "limit": True})
        probe = {}
        editor.tree.selection_set("s:0")                         # a script's first step (rows: "start", "s:0", ...)
        pump()                                                   # the choice arrives first, as a person's would
        editor._add_step(fe.WAIT)                                # shown where it will go; added on Apply
        pump()
        form = editor.form
        cond = form.condition
        cond.kind_var.set(app.t("cond_template"))
        cond._on_kind()
        cond._pick("template", cond._set_template)               # screenshot on a worker thread
        pump(10, until=lambda: cond.template is not None)
        cond.label_var.set("logo again")
        cond._test()                                             # "test on target": only looks
        pump(10, until=lambda: cond.lbl_result.cget("text") not in ("", app.t("probe_running")))
        probe["text"] = cond.lbl_result.cget("text")
        form.timeout.set("15")
        check(editor.apply_form(), f"applied in the properties panel: {c.status.text}")
        pump()
        steps = c.editor.document.steps
        check(len(steps) == 2 and steps[1].condition.label == "logo again", f"step added ({len(steps)} steps)")
        check(steps[1].condition.query.template.width == LOGO.width, "its template was cut from the screenshot")
        check(probe.get("text", "").startswith("True"), f"tested on the target: {probe.get('text')!r}")
        check(c.editor_dirty and "disabled" not in editor.btn_save.state(), "unsaved changes are shown")

        # ---- 8. save --------------------------------------------------------------------------------------------
        print("8. save")
        app._key_save()
        pump()
        assets = sorted(p.name for p in asset_dir_for(v2_path).iterdir())
        check(not c.editor_dirty, f"saved: {c.status.text}")
        check(len(assets) == 1 and ASSET_NAME.match(assets[0]), f"template stored as {assets}")

        # ---- 9. reopen ------------------------------------------------------------------------------------------
        print("9. reopen")
        saved = c.editor.document
        editor.go_back()
        pump()
        scripts.select_row(c.library.key_of(v2_path))
        scripts._edit_selected()
        pump()
        check(c.editor is not None and c.editor.document == saved, "reopened with the same steps and template")
        editor.go_back()
        pump()

        # ---- 10. run ---------------------------------------------------------------------------------------------
        print("10. run the script")
        scripts.select_row(c.library.key_of(v2_path))
        state_before, msgs = desktop_state(), len(window.messages)
        run(scripts, (gui.ref,))
        state_after = desktop_state()
        result = c.run_results[0] if c.run_results else None
        check(result is not None and result.state is RunState.COMPLETED and result.completed == 2,
              f"run: {result}")
        # (foreground window, pointer) before -> after: a person using the desktop meanwhile changes them too
        check(state_before == state_after, f"foreground window and pointer unchanged {state_before} -> {state_after}")
        check(len(window.messages) == msgs, "nothing was sent anywhere")
        tap_flow = library.save(ConditionalMacro("Smoke tap", (Decision(seen, then=TapMatch()),)))
        c.refresh_macros()
        scripts.select_row(c.library.key_of(tap_flow))
        run(scripts, (gui.ref,), dry_run=True)
        check(c.run_results[0].state is RunState.COMPLETED and len(window.messages) == msgs,
              "IF -> tap as a dry run: decided, nothing sent")

        # ---- 11. history ----------------------------------------------------------------------------------------
        print("11. history")
        record = c.history.list()[0] if c.history.list() else None
        check(record is not None and record.macro == "Smoke flow" and record.outcome == "completed",
              f"recorded: {record and (record.macro, record.outcome)}")
        check(record is not None and [r.outcome for r in record.results] == ["completed"], "per-target result kept")
        app.show_screen("history")
        pump()
        check(len(app.screens["history"].tree.get_children()) >= 1, "the History screen lists it")
        app.show_screen("scripts")

        # ---- 12. target selection ---------------------------------------------------------------------------------
        print("12. target selection")
        answer(dialogs, "ChooseTargets", ())
        scripts._choose_targets()
        pump()
        check(c.run_refs == () and "disabled" in scripts.btn_run.state(), "no targets: Run disabled")
        check(c.why_cannot_run() == "msg_no_run_targets" and not c.start_run(), "an empty choice is refused")
        c.set_run_refs(["not-a-target", gui.ref])
        check(c.run_refs == (gui.ref,), "an unknown target is dropped, never widened")

        # ---- 13. emergency stop -------------------------------------------------------------------------------------
        print("13. Emergency Stop")
        gone = Condition.template(logo, region=near, min_confidence=0.9, expected=False)
        never = library.save(ConditionalMacro("Smoke never", (WaitUntil(gone, 60, 0.2),)))
        c.refresh_macros()
        scripts.select_row(c.library.key_of(never))
        c.set_run_refs((gui.ref,))
        scripts._run()
        pump(1.0)
        check(c.run_active, "the run is waiting")
        pressed = time.time()
        app._on_estop()
        pump(10, until=lambda: not c.run_active)
        took = time.time() - pressed
        check(not c.run_active and c.run_results[0].detail == "emergency_stop" and took < 5,
              f"stopped in {took:.2f}s: {c.run_results[0]}")
        app._on_estop()
        pump()
        check(c.emergency_active, "pressing the stop again keeps the stop")
        app._on_reset_stop()
        pump()
        check(not c.emergency_active, "stop reset explicitly, with its own button")

        # ---- 14. languages --------------------------------------------------------------------------------------
        print("14. RU / EN")
        app.apply_language("ru")
        pump()
        check(app.nav_buttons["macros"][0].cget("text") == "Макросы", "Russian applied")
        check(scripts.lbl_kind.cget("text") == "СЦЕНАРИЙ", "a script is called a script in Russian too")
        app.apply_language("en")
        pump()
        check(app.nav_buttons["macros"][0].cget("text") == "Macros", "back to English")

        # ---- 15. read text ------------------------------------------------------------------------------------
        print("15. Read text")
        if c.ocr is None:
            print("  skip  this machine has no text reader")
        else:
            app.show_screen("text")
            pump(30, until=lambda: c.text_languages_state not in ("idle", "loading"))
            check(c.text_languages_state == "ready" and bool(c.text_languages), f"languages: {c.text_languages}")
            c.select(own_window().ref)
            reader = app.screens["text"]
            reader.language_var.set("")                     # automatic: the reader's own choice, then shown
            before, msgs = desktop_state(), len(window.messages)
            reader.btn_read.invoke()
            pump(30, until=lambda: not c.reading_text)
            reading = c.reading
            check(reading is not None and reading.state == "done"
                  and any(word in reading.text for word in ("Automation", "Read text")),
                  f"its own window read: {reading and (reading.state, reading.read_in, reading.text[:60])}")
            check(desktop_state() == before and len(window.messages) == msgs,
                  "nothing sent: pointer and foreground window unchanged")
            app.show_screen("macros")
            pump()                                          # repainted before the screenshot below

        try:
            c.refresh()
            c.select(own_window().ref)
            job = c.begin_screen()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(job().png)
            print(f"GUI screenshot saved: {out}")
        except Exception as exc:
            print(f"GUI screenshot not saved: {exc}")
    finally:
        for (module, name), value in originals.items():
            setattr(module, name, value)
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        window.close()
        tmp.cleanup()
        done.set()
    faulthandler.cancel_dump_traceback_later()
    print(f"note: the window stood still {pumping['woken']} time(s) while pumped, each woken after {WAKE_S:g} s "
          "with WM_NULL")
    print("PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
