"""OCR through the whole application path, deterministically: text condition and language -> run -> observation ->
the real OCR adapter -> its answer -> the decision -> the action on the right target -> the history.

Everything is real except the two outermost edges: the real `AppController` (and, for the probe and the editor, the
real Tk application, built but never shown) over the real `LDPlayerServices` -> `MappingService` ->
`create_ldplayer_playback` -> `InputSession` chain with only the `AdbClient` faked (the wiretap that records every
command with its serial), and the real `WindowsOcrProvider` with only its `CommandRunner` faked. That runner answers
each read according to WHICH target's screen it was handed -- recognised by where that target shows its mark -- so
a tap that lands on the right serial at the right place proves the read, the decision and the action belonged to the
same target. Nothing here starts PowerShell, reads a real screen or sends real input.
"""
import gc
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped
from app.decide import Cancelled, Condition, ConditionalMacro, Decision, RunMacro, TapMatch, WaitUntil
from app.playback.macro import Macro
from app.providers.recording import Recording
from tests.test_playback import down, up
from app.gui.controller import AppController, RunState
from app.gui.flows import FlowJob
from app.gui.history import COMPLETED, FAILED, PARTIAL, STOPPED, RunHistory
from app.gui.library import MacroLibrary
from app.ocr.winrt import WindowsOcrProvider
from app.vision.bitmap import Bitmap, decode_png
from tests.test_gui_v2 import AREA, RED, SCENES, SPOTS, ScreenAdb
from tests.test_ldplayer_services import FakeConsole, port_owners_for, read_config_for
from tests.test_parallel_run import ld_world
from app.core.process import CommandResult

WORD = "Забрать"                      # what each target shows next to its mark, in Russian


def which_target(image: Bitmap) -> int:
    for index, (x, y) in SPOTS.items():
        if x < image.width and y < image.height and image.pixel(x, y) == RED:
            return index
    raise AssertionError("an OCR read was handed a screen that belongs to no target")


def answer(index: int, lang: str = "ru") -> str:
    """The text each target shows, where its mark is -- so a tap at its centre lands on that target's own spot."""
    x, y = SPOTS[index]
    return json.dumps({"lang": lang, "lines": [{"text": f"{WORD} {index}", "x": x, "y": y, "w": 8, "h": 8}]})


class ScreenReadingRunner:
    """The OCR engine's `CommandRunner`, faked: it records each read (target, arguments, cancel) and answers
    `respond(target)` -> (stdout, returncode); with `hold`, it waits like the real runner does around a long read,
    until the read's cancel or the emergency stop is set."""

    def __init__(self, respond, *, hold=False, stop: EmergencyStop | None = None):
        self.respond, self.hold, self.stop = respond, hold, stop
        self.reads: list[tuple[int, tuple[str, ...], threading.Event | None]] = []
        self.images: list[Path] = []
        self._lock = threading.Lock()

    def run(self, args, *, timeout, cwd=None, cancel=None):
        args = tuple(args)
        image = Path(args[args.index("-Path") + 1])
        target = which_target(decode_png(image.read_bytes()))
        with self._lock:
            self.reads.append((target, args, cancel))
            self.images.append(image)
        if self.hold:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if (cancel is not None and cancel.is_set()) or (self.stop is not None and self.stop.is_set()):
                    return CommandResult(args, None, cancelled=True)
                time.sleep(0.005)
            return CommandResult(args, None, timed_out=True)
        stdout, returncode = self.respond(target)
        if stdout is None:
            return CommandResult(args, None, timed_out=True)
        return CommandResult(args, returncode, stdout.encode("utf-8"), b"")

    def languages(self) -> set[str]:
        return {args[args.index("-Language") + 1] if "-Language" in args else "" for _, args, _ in self.reads}


class Base(unittest.TestCase):
    instances = 2

    def setUp(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ocr_dir = root / "ocr"
        self.world = ld_world(self.instances)
        self.adb = ScreenAdb(self.world, {self.serial(i): SCENES[i] for i in range(1, self.instances + 1)})
        self.stop = EmergencyStop()
        # structural fakes of the console and the client, as in tests.test_parallel_run
        self.services = LDPlayerServices(FakeConsole(self.world), self.adb,  # pyright: ignore[reportArgumentType]
                                         read_config_for(self.world), port_owners_for(self.world), self.stop)
        self.library = MacroLibrary(root / "macros")
        self.history = RunHistory(root / "history.json")

    def controller(self, runner=None) -> AppController:
        engine = WindowsOcrProvider(runner, self.ocr_dir, timeout_s=30) if runner is not None else None
        c = AppController(self.services, self.library, history=self.history, ocr=engine)
        self.addCleanup(c.shutdown)
        c.refresh()
        return c

    @staticmethod
    def serial(index):
        return f"emulator-{5552 + 2 * index}"

    def entry(self, c, index):
        return next(e for e in c.entries if e.title.endswith(f"-{index}"))

    def put(self, c, macro) -> None:
        path = self.library.save(macro)
        c.refresh_macros()
        c.select_macro(self.library.key_of(path))

    def run_both(self, c, macro, *, timeout=30.0) -> None:
        self.put(c, macro)
        c.set_run_refs([self.entry(c, 1).ref, self.entry(c, 2).ref])
        self.assertTrue(c.start_run(), c.status)
        self.finish(c, timeout)

    def finish(self, c, timeout=30.0):
        deadline = time.monotonic() + timeout
        while c.run_active and time.monotonic() < deadline:
            c.poll()
            time.sleep(0.005)
        c.poll()
        self.assertFalse(c.run_active, c.run_results)

    def taps(self):
        return sorted((serial, args) for serial, op, args in self.adb.log if op == "tap")

    def results(self, c):
        return {r.title.rsplit("-", 1)[1]: (r.state, r.detail) for r in c.run_results}


def text_flow(language="ru", **condition) -> ConditionalMacro:
    seen = Condition.text(WORD, region=AREA, language=language, **condition)
    return ConditionalMacro("Read and tap", (WaitUntil(seen, 5, 0.05), Decision(seen, then=TapMatch())))


class TextFlowsOnRealTargets(Base):
    def test_text_in_the_chosen_language_is_read_decided_and_acted_on_per_target(self):
        runner = ScreenReadingRunner(lambda target: (answer(target), 0))
        c = self.controller(runner)
        self.run_both(c, text_flow())
        self.assertEqual({r.state for r in c.run_results}, {RunState.COMPLETED})
        self.assertEqual(self.taps(), sorted((self.serial(i), (SPOTS[i][0] + 4, SPOTS[i][1] + 4)) for i in (1, 2)))
        self.assertEqual(runner.languages(), {"ru"})                            # every read, both steps
        self.assertEqual({target for target, _, _ in runner.reads}, {1, 2})
        record = self.history.list()[0]
        self.assertEqual((record.outcome, [r.outcome for r in record.results]), (COMPLETED, ["completed"] * 2))
        self.assertEqual([p for p in runner.images if p.exists()], [])          # no temporary image left

    def test_a_bad_answer_for_one_target_fails_only_that_target(self):
        def respond(target):
            if target == 1:
                return '{"lang": "ru", "lines": [{"text": "x", "x": NaN, "y": 1, "w": 2, "h": 2}]}', 0
            return answer(target), 0
        c = self.controller(ScreenReadingRunner(respond))
        self.run_both(c, text_flow())
        results = self.results(c)
        self.assertEqual(results["1"][0], RunState.FAILED)
        self.assertTrue(results["1"][1].startswith("ocr_unavailable"), results["1"])
        self.assertEqual(results["2"], (RunState.COMPLETED, ""))
        self.assertEqual(self.taps(), [(self.serial(2), (SPOTS[2][0] + 4, SPOTS[2][1] + 4))])
        self.assertEqual(self.history.list()[0].outcome, PARTIAL)

    def test_a_language_that_cannot_be_read_is_reported_as_such_and_nothing_is_sent(self):
        refusal = json.dumps({"error": "no OCR language pack for 'ja' (available: en-US,ru,sv-SE)"})
        c = self.controller(ScreenReadingRunner(lambda target: (refusal, 3)))
        self.run_both(c, text_flow(language="ja"))
        for state, detail in self.results(c).values():
            self.assertEqual(state, RunState.FAILED)
            self.assertTrue(detail.startswith("ocr_language_unavailable"), detail)
        self.assertEqual(self.taps(), [])
        self.assertEqual(self.history.list()[0].outcome, FAILED)

    def test_an_engine_that_answers_in_another_language_is_refused(self):
        c = self.controller(ScreenReadingRunner(lambda target: (answer(target, lang="en-US"), 0)))
        self.run_both(c, text_flow(language="ru"))
        self.assertTrue(all(d.startswith("ocr_language_unavailable") for _, d in self.results(c).values()))
        self.assertEqual(self.taps(), [])

    def test_without_an_ocr_engine_a_text_flow_fails_as_such_and_nothing_is_sent(self):
        c = self.controller(None)
        first = self.services.verify(self.entry(c, 1))
        tap_first = Macro(Recording(first.provider_id, first.target_id, (down(0, 5, 6), up(10, 5, 6))), "tap")
        seen = Condition.text(WORD, region=AREA, language="ru")
        # the recorded tap comes FIRST: it must not be sent, because the text step could never be answered
        self.run_both(c, ConditionalMacro("Tap then read", (RunMacro(tap_first), WaitUntil(seen, 5, 0.05))))
        for state, detail in self.results(c).values():
            self.assertEqual(state, RunState.FAILED)
            self.assertIn("No OCR provider is configured", detail)
        self.assertEqual(self.taps(), [])

    def test_an_engine_timeout_is_an_ocr_failure_and_absent_text_is_a_wait_timeout(self):
        c = self.controller(ScreenReadingRunner(lambda target: (None, None)))
        self.run_both(c, text_flow())
        self.assertTrue(all(d.startswith("ocr_unavailable") for _, d in self.results(c).values()))
        empty = json.dumps({"lang": "ru", "lines": []})
        c2 = self.controller(ScreenReadingRunner(lambda target: (empty, 0)))
        seen = Condition.text(WORD, region=AREA, language="ru")
        self.run_both(c2, ConditionalMacro("Too early", (WaitUntil(seen, 0.2, 0.05),)))
        self.assertTrue(all(d.startswith("timeout") for _, d in self.results(c2).values()), self.results(c2))
        self.assertEqual(self.taps(), [])

    def test_a_saved_minimum_confidence_does_not_break_a_flow_and_is_not_applied(self):
        c = self.controller(ScreenReadingRunner(lambda target: (answer(target), 0)))
        self.run_both(c, text_flow(min_confidence=0.9))
        self.assertEqual({r.state for r in c.run_results}, {RunState.COMPLETED})


class ReadsUnderWayAreEnded(Base):
    """A read that takes long (here: one that never answers on its own) ends at once on Stop, on the emergency stop
    and when the application closes -- the engine is killed, not waited for -- and nothing is sent afterwards."""

    def start(self, runner):
        c = self.controller(runner)
        self.put(c, text_flow())
        c.set_run_refs([self.entry(c, 1).ref, self.entry(c, 2).ref])
        self.assertTrue(c.start_run(), c.status)
        deadline = time.monotonic() + 5
        while len(runner.reads) < 2 and time.monotonic() < deadline:
            c.poll()
            time.sleep(0.01)
        self.assertEqual(len(runner.reads), 2, "both reads should be under way")
        return c

    def test_stop_ends_the_reads_under_way(self):
        runner = ScreenReadingRunner(lambda target: ("", 0), hold=True)
        c = self.start(runner)
        started = time.monotonic()
        c.cancel()
        self.finish(c, 5)
        self.assertLess(time.monotonic() - started, 2.0)                       # not the 30 s read timeout
        self.assertEqual(set(self.results(c).values()), {(RunState.STOPPED, "cancelled")})
        self.assertTrue(all(cancel is not None and cancel.is_set() for _, _, cancel in runner.reads))
        self.assertEqual(self.taps(), [])
        self.assertEqual([p for p in runner.images if p.exists()], [])
        self.assertEqual(self.history.list()[0].outcome, STOPPED)

    def test_the_emergency_stop_ends_the_reads_and_is_reported_as_the_stop(self):
        runner = ScreenReadingRunner(lambda target: ("", 0), hold=True, stop=self.stop)
        c = self.start(runner)
        c.emergency_stop()
        self.finish(c, 5)
        self.assertEqual(set(self.results(c).values()), {(RunState.STOPPED, "emergency_stop")})
        self.assertEqual(self.taps(), [])
        self.assertTrue(c.clear_emergency_stop())

    def test_closing_the_application_leaves_no_read_running(self):
        runner = ScreenReadingRunner(lambda target: ("", 0), hold=True)
        c = self.start(runner)
        jobs = [job for job in c._running.values() if isinstance(job, FlowJob)]
        self.assertEqual(len(jobs), 2)
        started = time.monotonic()
        c.shutdown()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(any(job.running for job in jobs))                       # every worker has ended
        self.assertTrue(all(cancel is not None and cancel.is_set() for _, _, cancel in runner.reads))
        self.assertEqual(self.history.list()[0].outcome, STOPPED)
        self.assertEqual(self.taps(), [])


class ProbeStoppedBeforeItStarts(Base):
    """The emergency stop sets a probe's own cancel as well. A probe stopped before its thread reaches the condition
    is reported as the stop -- never as "the run was cancelled", which would overwrite the EMERGENCY STOP status --
    while a probe ended by closing the window, with no stop, stays a cancel."""

    def begun(self, condition):
        runner = ScreenReadingRunner(lambda target: (answer(target), 0))
        c = self.controller(runner)
        self.assertTrue(c.select(self.entry(c, 1).ref), c.status)
        self.adb.screencaps.clear()
        job = c.begin_probe(condition)
        assert job is not None, c.status
        return c, job, runner

    @staticmethod
    def outcome(job):
        try:
            return job()
        except BaseException as exc:
            return exc

    def test_the_emergency_stop_before_the_probe_runs_is_reported_as_the_stop(self):
        x, y = SPOTS[1]
        for condition in (Condition.text(WORD, language="ru"), Condition.pixel((x, y), RED)):
            with self.subTest(query=type(condition.query).__name__):
                self.stop.reset()                                                # each case starts unstopped
                c, job, runner = self.begun(condition)
                c.emergency_stop()
                outcome = self.outcome(job)
                self.assertIsInstance(outcome, EmergencyStopped)
                self.assertIsNone(c.finish_probe(outcome, job))
                self.assertEqual((c.status.category, c.status.key), ("emergency_stop", "msg_emergency_active"))
                self.assertEqual((runner.reads, self.adb.screencaps, self.taps()), ([], [], []))
                self.assertTrue(c.clear_emergency_stop())

    def test_an_answer_that_arrives_after_the_stop_is_given_but_the_stop_stays_said(self):
        c, job, runner = self.begun(Condition.text(WORD, language="ru"))
        outcome = self.outcome(job)                                              # answered just before the stop
        c.emergency_stop()
        result = c.finish_probe(outcome, job)
        self.assertTrue(result is not None and result.satisfied)
        self.assertEqual((c.status.category, c.status.key), ("emergency_stop", "msg_emergency_stop"))
        self.assertEqual(self.taps(), [])

    def test_a_probe_ended_by_closing_without_a_stop_stays_a_cancel(self):
        c, job, runner = self.begun(Condition.text(WORD, language="ru"))
        c.shutdown()
        outcome = self.outcome(job)
        self.assertIsInstance(outcome, Cancelled)
        self.assertNotIsInstance(outcome, EmergencyStopped)
        self.assertEqual((runner.reads, self.adb.screencaps), ([], []))


class SavedValuesSurviveTheEditor(unittest.TestCase):
    """Opening a saved condition in the editor and applying it unchanged must not change the file. The form used
    to format numbers with 6 significant digits, so a saved 0.12345678 came back as 0.123457 -- invisibly, for a
    text minimum the editor no longer shows."""

    def test_every_saved_number_comes_back_exactly(self):
        from app.gui import flow_edit as fe
        from tests.test_gui_v2 import TEMPLATE
        for condition in (Condition.text(WORD, language="ru", min_confidence=0.12345678),
                          Condition.template(TEMPLATE, min_confidence=0.987654321),
                          Condition.color((1, 2, 3), min_ratio=0.333333333333)):
            self.assertEqual(fe.build_condition(fe.condition_form(condition)), condition)


# ---- the probe and the editor, in the real (never shown) application ------------------------------------------
try:
    import tkinter as tk
except ImportError:                                   # pragma: no cover
    tk = None


@unittest.skipIf(tk is None, "tkinter is not available")
class ProbeAndEditor(Base):
    def setUp(self):
        super().setUp()
        assert tk is not None
        # Registered first, so it runs last: the dialogs' Tk variables are collected HERE, on the main thread. Left
        # to chance, a later test's worker thread could collect them, and tkinter then waits for a main loop that a
        # test never runs (about a second per variable), stalling that worker.
        self.addCleanup(gc.collect)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()

    def app(self, runner):
        from app.gui.app import App
        engine = WindowsOcrProvider(runner, self.ocr_dir) if runner is not None else None
        app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                  preview_runner=lambda fn: fn(), ocr=engine)
        self.addCleanup(self.close_quietly, app)
        app.controller.refresh()
        self.assertIs(app.controller.ocr, engine)                               # handed through unchanged
        app.controller.select(self.entry(app.controller, 1).ref)
        return app

    @staticmethod
    def close_quietly(app):
        """Close the application unless the test already did (a destroyed root cannot be asked anything)."""
        assert tk is not None
        try:
            app.close()
        except tk.TclError:
            pass

    def dialog(self, app, step=None):
        from app.gui.view.flow import StepDialog
        made = StepDialog(app, "wait_until", step)
        made.top.withdraw()
        self.addCleanup(lambda: made.top.winfo_exists() and made.top.destroy())
        return made

    def test_testing_a_text_condition_reads_in_its_language_and_says_confidence_is_not_measured(self):
        runner = ScreenReadingRunner(lambda target: (answer(target), 0))
        app = self.app(runner)
        dialog = self.dialog(app)
        editor = dialog.condition
        editor.lines[0]["text"].set(WORD)
        editor.vars["text"]["language"].set("ru")
        editor._test()
        app._drain_jobs()
        x, y = SPOTS[1]
        self.assertEqual(editor.lbl_result.cget("text"),
                         app.t("probe_true_unmeasured").format(at=f"({x + 4}, {y + 4})"))
        self.assertEqual(runner.languages(), {"ru"})
        # the probe's own cancel reaches the read; closing the app (below) and the emergency stop
        # (tests.test_text_screen) set it
        self.assertIsInstance(runner.reads[0][2], threading.Event)

    def test_a_probe_that_failed_for_its_own_reason_says_that_reason_even_under_the_stop(self):
        """The answer arrives after the stop: the status bar keeps saying the stop, but the dialog that asked shows
        why THIS probe failed -- it did not fail because of the stop."""
        app = self.app(ScreenReadingRunner(lambda target: ("not json", 0)))     # the reader fails on its own
        editor = self.dialog(app).condition
        editor.lines[0]["text"].set(WORD)
        editor.vars["text"]["language"].set("ru")
        editor._test()                                                           # read now, answer still queued
        app.controller.emergency_stop()
        app._drain_jobs()
        c = app.controller
        self.assertEqual(c.status.key, "msg_emergency_stop")
        probe = c.probe_status
        assert probe is not None
        self.assertEqual(probe.category, "probe")
        self.assertEqual(editor.lbl_result.cget("text"), app.status_text(probe))
        self.assertNotEqual(editor.lbl_result.cget("text"), app.status_text())

    def test_the_editor_does_not_offer_a_confidence_the_reader_cannot_apply_and_keeps_a_saved_one(self):
        app = self.app(ScreenReadingRunner(lambda target: (answer(target), 0)))
        self.assertFalse(app.controller.text_confidence_reported)
        saved = WaitUntil(Condition.text(WORD, language="ru", min_confidence=0.8), 20, 0.5)
        dialog = self.dialog(app, saved)
        editor = dialog.condition
        self.assertNotIn("min_confidence", [name for name, _, _ in editor._fields()])
        note = editor._confidence_note()
        self.assertIn(app.t("text_no_confidence"), note)
        self.assertIn("0.8", note)                                               # the saved value, said to be ignored
        dialog.confirm()
        self.assertEqual(dialog.result, saved)                                   # the saved flow is unchanged

    def test_a_reader_with_real_confidence_still_gets_the_field(self):
        from tests.test_observe import FakeOcr
        from app.gui.app import App
        app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                  preview_runner=lambda fn: fn(), ocr=FakeOcr())
        self.addCleanup(self.close_quietly, app)
        self.assertTrue(app.controller.text_confidence_reported)
        dialog = self.dialog(app)
        self.assertIn("min_confidence", [name for name, _, _ in dialog.condition._fields()])
        self.assertEqual(dialog.condition._confidence_note(), "")

    def test_without_a_reader_the_editor_says_text_cannot_be_checked(self):
        app = self.app(None)
        self.assertEqual(self.dialog(app).condition._confidence_note(), app.t("text_no_reader"))

    def test_closing_the_application_ends_a_probe_that_is_reading(self):
        runner = ScreenReadingRunner(lambda target: ("", 0), hold=True)
        app = self.app(runner)
        job = app.controller.begin_probe(Condition.text(WORD, language="ru"))
        assert job is not None
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("result", self.call(job)))
        worker.start()
        deadline = time.monotonic() + 5
        while not runner.reads and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        app.close()
        worker.join(5)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(out["result"], Exception)                          # cancelled, not answered
        cancel = runner.reads[0][2]
        self.assertTrue(cancel is not None and cancel.is_set())

    @staticmethod
    def call(job):
        try:
            return job()
        except Exception as exc:
            return exc


if __name__ == "__main__":
    unittest.main()
