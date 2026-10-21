"""The connected OCR engine (`app.ocr.winrt.WindowsOcrProvider`) and the layers that reach it.

The engine's outside world is ONE object: the platform's `CommandRunner`. Most tests here replace it with a fake that
records what it was asked to run (and the image it was handed) and answers with a scripted `CommandResult` -- or, for
cancellation, waits exactly as the real runner does until the caller's cancel or the emergency stop is set. So every
path of the adapter is checked without PowerShell, without the Windows OCR engine, without a network and without
any target: text and positions, regions and clipping, strict validation, honest confidence, language, cancellation,
timeouts, temporary files, and the composition root.

Where a claim is about the REAL boundary -- what Windows PowerShell writes, how a box is rounded, whether a read can
really be cancelled, whether Russian and Swedish are really recognized -- the test runs the real thing (the module's
own PowerShell functions, the real `CommandRunner`, the real engine) and skips, never fails, where it is missing.
Nothing here reads a screen or sends input.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped
from app.core.process import CommandResult, CommandRunner
from app.decide import Condition, Decision, FlowState, RunMacro, TapMatch, WaitUntil, evaluate, run_steps
from app.observe import (ObservationCancelled, ObservationTimeout, OcrLanguageUnavailable as ObserveLanguageError,
                         OcrUnavailable, TextQuery)
from app.ocr import winrt
from app.ocr.provider import (LanguageNotSupported, OcrProvider, OcrResult, OcrTextBlock, ReadCancelled,
                              reports_confidence)
from app.ocr.winrt import OcrEngineError, OcrEngineTimeout, OcrLanguageUnavailable, WindowsOcrProvider
from app.vision.bitmap import Bitmap, VisionError, decode_png
from app.vision.roi import Roi
from app.playback.macro import Macro
from app.providers.recording import Recording
from tests.test_decide import Rig, ctx
from tests.test_playback import PROV, TID, down, up
from tests.test_observe import RED, WHITE, FakeOcr, frame_of

ROOT = Path(__file__).resolve().parents[1] / "app"
SCREEN = Bitmap.solid(200, 100, WHITE).with_rect(30, 20, 4, 4, RED)
POWERSHELL = shutil.which("powershell.exe")


def payload(*items, lang: str = "en-US") -> str:
    """What the script writes on success: the recognizer's language and the lines (escaped JSON, as written)."""
    return json.dumps({"lang": lang, "lines": [dict(text=t, x=x, y=y, w=w, h=h) for t, x, y, w, h in items]})


class FakeRunner:
    """Stands in for `CommandRunner`: records each call, the image it was handed and whether that image still
    exists afterwards; answers as scripted. With `wait_for_cancel`, it behaves like the real runner around a long
    read: it returns `cancelled` as soon as the caller's cancel (or `stop`) is set, or `timed_out` at the deadline."""

    def __init__(self, stdout: str = "", *, timed_out=False, cancelled=False, returncode=0, error=None, stderr="",
                 on_run=None, wait_for_cancel=False, stop: EmergencyStop | None = None):
        self.stdout, self.stderr, self.error = stdout, stderr, error
        self.timed_out, self.cancelled, self.returncode = timed_out, cancelled, returncode
        self.on_run, self.wait_for_cancel, self.stop = on_run, wait_for_cancel, stop
        self.calls: list[tuple[tuple[str, ...], float, object]] = []
        self.images: list[Bitmap] = []
        self.paths: list[Path] = []
        self.started = threading.Event()

    def run(self, args, *, timeout, cwd=None, cancel=None):
        args = tuple(args)
        self.calls.append((args, timeout, cancel))
        if "-Path" in args:
            image = Path(args[args.index("-Path") + 1])
            self.paths.append(image)
            if image.exists():
                self.images.append(decode_png(image.read_bytes()))
        self.started.set()
        if self.on_run is not None:
            self.on_run()
        cancelled, timed_out = self.cancelled, self.timed_out
        if self.wait_for_cancel:
            deadline = time.monotonic() + timeout
            while not cancelled and not timed_out:
                if (cancel is not None and cancel.is_set()) or (self.stop is not None and self.stop.is_set()):
                    cancelled = True
                elif time.monotonic() >= deadline:
                    timed_out = True
                else:
                    time.sleep(0.005)
        if cancelled or timed_out:
            return CommandResult(args, None, b"", b"", duration_ms=3, timed_out=timed_out, cancelled=cancelled)
        return CommandResult(args, self.returncode, self.stdout.encode("utf-8"), self.stderr.encode("utf-8"),
                             duration_ms=3, error=self.error)

    def args(self) -> tuple[str, ...]:
        return self.calls[-1][0]


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def engine(self, runner, **kw) -> WindowsOcrProvider:
        return WindowsOcrProvider(runner, self.dir, **kw)

    def read(self, runner, **kw) -> OcrResult:
        return self.engine(runner).read_text(frame_of(SCREEN), **kw)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.suffix in (".png", ".tmp"))


# ---- recognition ----------------------------------------------------------------------------------------------
class Recognition(EngineCase):
    def test_it_satisfies_the_existing_contract(self):
        self.assertIsInstance(self.engine(FakeRunner(payload())), OcrProvider)

    def test_text_comes_back_with_its_place_in_the_frame(self):
        runner = FakeRunner(payload(("Collect", 10, 20, 60, 14), ("Settings", 10, 50, 70, 14)))
        result = self.read(runner)
        self.assertEqual([(b.text, b.bounds) for b in result.blocks],
                         [("Collect", Roi(10, 20, 60, 14)), ("Settings", Roi(10, 50, 70, 14))])
        self.assertEqual(result.text, "Collect\nSettings")
        self.assertEqual((runner.images[0].width, runner.images[0].height), (200, 100))

    def test_no_text_is_an_empty_result_not_an_error(self):
        for stdout in (payload(), json.dumps({"lang": "en-US", "lines": None})):
            result = self.read(FakeRunner(stdout))
            self.assertEqual((result.blocks, result.text), ((), ""), stdout)

    def test_a_single_line_that_json_collapsed_into_an_object_is_still_read(self):
        stdout = json.dumps({"lang": "en-US", "lines": {"text": "Only", "x": 1, "y": 2, "w": 3, "h": 4}})
        self.assertEqual(self.read(FakeRunner(stdout)).text, "Only")

    def test_the_command_is_fixed_and_carries_no_caller_text(self):
        runner = FakeRunner(payload(lang="ru"))
        self.engine(runner, language="ru", timeout_s=7).read_text(frame_of(SCREEN))
        args, timeout, _ = runner.calls[0]
        self.assertEqual(args[:7], ("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                    "Bypass", "-File", str(self.dir / winrt._SCRIPT_NAME)))
        self.assertEqual(args[-2:], ("-Language", "ru"))
        self.assertEqual(timeout, 7.0)                                        # every read is time-bounded

    def test_only_a_frame_from_the_verified_pipeline_is_read(self):
        runner = FakeRunner(payload())
        with self.assertRaises(OcrEngineError):
            self.engine(runner).read_text(SCREEN)  # pyright: ignore[reportArgumentType]  (deliberately not a Frame)
        self.assertEqual(runner.calls, [])

    def test_asking_whether_the_engine_is_available_never_swallows_the_emergency_stop(self):
        self.assertTrue(self.engine(FakeRunner(payload())).available())
        self.assertFalse(self.engine(FakeRunner('{"error": "no WinRT"}', returncode=4)).available())
        with self.assertRaises(EmergencyStopped):
            self.engine(FakeRunner(cancelled=True)).available()                 # killed by the stop

    def test_a_missing_runner_is_refused_at_construction(self):
        with self.assertRaises(OcrEngineError):
            WindowsOcrProvider(None, self.dir)


# ---- boxes and regions ------------------------------------------------------------------------------------------
class BoxesAndRegions(EngineCase):
    def test_a_region_is_read_alone_and_positions_come_back_in_frame_pixels(self):
        runner = FakeRunner(payload(("Collect", 2, 3, 10, 5)))
        result = self.read(runner, roi=Roi(30, 10, 40, 30))
        self.assertEqual(runner.images[0], SCREEN.crop(30, 10, 40, 30))        # only the region was sent
        self.assertEqual(result.blocks[0].bounds, Roi(32, 13, 10, 5))

    def test_a_box_that_crosses_an_edge_is_clipped_to_the_image_on_every_side(self):
        runner = FakeRunner(payload(("left", -3, 5, 10, 4), ("top", 5, -2, 4, 6), ("right", 35, 1, 50, 2),
                                    ("bottom", 1, 28, 2, 9), ("all", -5, -5, 100, 100)))
        result = self.read(runner, roi=Roi(30, 10, 40, 30))
        self.assertEqual([b.bounds for b in result.blocks],
                         [Roi(30, 15, 7, 4), Roi(35, 10, 4, 4), Roi(65, 11, 5, 2), Roi(31, 38, 2, 2),
                          Roi(30, 10, 40, 30)])

    def test_a_box_exactly_on_the_edges_is_kept_whole(self):
        result = self.read(FakeRunner(payload(("corner", 0, 0, 1, 1), ("edge", 199, 99, 1, 1), ("all", 0, 0, 200, 100))))
        self.assertEqual([b.bounds for b in result.blocks], [Roi(0, 0, 1, 1), Roi(199, 99, 1, 1), Roi(0, 0, 200, 100)])

    def test_a_box_entirely_outside_the_image_is_a_malformed_answer_not_a_made_up_one(self):
        for box in ((200, 0, 5, 5), (0, 100, 5, 5), (-10, 0, 10, 5), (0, -9, 5, 9)):
            with self.assertRaises(OcrEngineError, msg=box):
                self.read(FakeRunner(payload(("ghost", *box))))

    def test_a_region_outside_the_frame_is_refused_before_the_engine_runs(self):
        runner = FakeRunner(payload())
        with self.assertRaises(VisionError):
            self.read(runner, roi=Roi(190, 90, 50, 50))
        self.assertEqual(runner.calls, [])

    def test_a_region_the_size_of_the_frame_reads_the_whole_frame(self):
        runner = FakeRunner(payload(("x", 0, 0, 200, 100)))
        self.assertEqual(self.read(runner, roi=Roi(0, 0, 200, 100)).blocks[0].bounds, Roi(0, 0, 200, 100))
        self.assertEqual(runner.images[0], SCREEN)


@unittest.skipIf(POWERSHELL is None, "Windows PowerShell is not available")
class LineBoxInTheRealScript(unittest.TestCase):
    """The script's own `Get-LineBox`, run in a real PowerShell: a box is the smallest whole-pixel box that covers
    every word. The previous rounding, ceil(right - left), fell a pixel short for a word spanning 10.6..20.2."""

    def box(self, *rects):
        words = ", ".join(f"[pscustomobject]@{{ BoundingRect = [pscustomobject]@{{ X = {x}; Y = {y}; "
                          f"Width = {w}; Height = {h} }} }}" for x, y, w, h in rects)
        script = (winrt._JSON_WRITER + winrt._LINE_BOX
                  + f"$box = Get-LineBox @({words}); if ($null -eq $box) {{ 'null' }} else {{ Write-Json $box }}")
        assert POWERSHELL is not None
        result = CommandRunner(EmergencyStop()).run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
        self.assertFalse(result.timed_out or result.error, result)
        return json.loads(result.stdout_text.strip())

    def test_fractional_edges_round_outwards(self):
        self.assertEqual(self.box((10.6, 5.2, 9.6, 3.1)), {"x": 10, "y": 5, "w": 11, "h": 4})

    def test_whole_pixel_words_are_kept_exactly(self):
        self.assertEqual(self.box((10, 5, 20, 8)), {"x": 10, "y": 5, "w": 20, "h": 8})

    def test_a_line_covers_all_of_its_words(self):
        self.assertEqual(self.box((10, 5, 5, 5), (30, 4, 10, 8)), {"x": 10, "y": 4, "w": 30, "h": 8})

    def test_a_word_of_no_width_still_covers_the_pixel_it_stands_on(self):
        self.assertEqual(self.box((7, 3, 0, 2)), {"x": 7, "y": 3, "w": 1, "h": 2})

    def test_a_line_without_words_has_no_box(self):
        self.assertIsNone(self.box())


# ---- strict validation -------------------------------------------------------------------------------------------
class StrictValidation(EngineCase):
    LINE = '{"text": "a", "x": 1, "y": 1, "w": 2, "h": 2}'
    MALFORMED = {
        "not json": "not json at all",
        "a list": "[1, 2]",
        "NaN": '{"lang": "en-US", "lines": [{"text": "a", "x": NaN, "y": 1, "w": 2, "h": 2}]}',
        "Infinity": '{"lang": "en-US", "lines": [{"text": "a", "x": Infinity, "y": 1, "w": 2, "h": 2}]}',
        "-Infinity": '{"lang": "en-US", "lines": [{"text": "a", "x": -Infinity, "y": 1, "w": 2, "h": 2}]}',
        "overflowing float": '{"lang": "en-US", "lines": [{"text": "a", "x": 1e999, "y": 1, "w": 2, "h": 2}]}',
        "duplicate key": '{"lang": "en-US", "lang": "ru", "lines": []}',
        "missing lang": '{"lines": []}',
        "extra key": '{"lang": "en-US", "lines": [], "extra": 1}',
        "lang not text": '{"lang": 5, "lines": []}',
        "lang not a tag": '{"lang": "not a tag!", "lines": []}',
        "lines not a list": '{"lang": "en-US", "lines": "abc"}',
        "line not an object": '{"lang": "en-US", "lines": ["a"]}',
        "line missing a key": '{"lang": "en-US", "lines": [{"text": "a", "x": 1, "y": 1, "w": 2}]}',
        "line with an extra key": '{"lang": "en-US", "lines": [{"text": "a", "x": 1, "y": 1, "w": 2, "h": 2, "z": 0}]}',
        "text not text": '{"lang": "en-US", "lines": [{"text": 5, "x": 1, "y": 1, "w": 2, "h": 2}]}',
        "fractional coordinate": '{"lang": "en-US", "lines": [{"text": "a", "x": 1.5, "y": 1, "w": 2, "h": 2}]}',
        "boolean coordinate": '{"lang": "en-US", "lines": [{"text": "a", "x": true, "y": 1, "w": 2, "h": 2}]}',
        "huge coordinate": '{"lang": "en-US", "lines": [{"text": "a", "x": 99999999999, "y": 1, "w": 2, "h": 2}]}',
        "zero width": '{"lang": "en-US", "lines": [{"text": "a", "x": 1, "y": 1, "w": 0, "h": 2}]}',
        "negative height": '{"lang": "en-US", "lines": [{"text": "a", "x": 1, "y": 1, "w": 2, "h": -2}]}',
        "malformed error": '{"error": ""}',
        "error with more": '{"error": "x", "lines": []}',
    }

    def test_every_malformed_answer_is_an_engine_error_and_nothing_is_believed(self):
        for name, stdout in self.MALFORMED.items():
            with self.assertRaises(OcrEngineError, msg=name):
                self.read(FakeRunner(stdout))

    def test_one_bad_line_fails_the_read_instead_of_hiding_the_text_it_held(self):
        """Dropping a line silently could hide exactly the text a condition looks for -- and a decision taken on
        "it is not there" would be wrong. A malformed line therefore fails the whole read."""
        stdout = '{"lang": "en-US", "lines": [' + self.LINE + ', {"text": "Collect", "x": "1", "y": 1, "w": 2, "h": 2}]}'
        with self.assertRaises(OcrEngineError):
            self.read(FakeRunner(stdout))

    def test_too_many_lines_is_not_an_answer(self):
        stdout = '{"lang": "en-US", "lines": [' + ", ".join([self.LINE] * (winrt.MAX_LINES + 1)) + "]}"
        with self.assertRaises(OcrEngineError):
            self.read(FakeRunner(stdout))

    def test_only_the_last_line_of_output_is_the_answer(self):
        stdout = "WARNING: something PowerShell printed first\n" + payload(("ok", 1, 1, 2, 2))
        self.assertEqual(self.read(FakeRunner(stdout)).text, "ok")

    def test_a_malformed_answer_fails_only_the_run_that_asked_and_sends_nothing(self):
        rig = Rig([SCREEN], ocr=self.engine(FakeRunner(self.MALFORMED["NaN"])))
        report = rig.run(Decision(Condition.text("a"), then=TapMatch()))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_unavailable"))
        self.assertEqual((rig.built, rig.input.calls), ([], []))


# ---- failures and timeouts ---------------------------------------------------------------------------------------
class EngineFailures(EngineCase):
    def test_an_engine_error_is_an_engine_error_and_not_a_language_problem(self):
        for returncode in (4, 5):
            with self.assertRaises(OcrEngineError) as caught:
                self.read(FakeRunner('{"error": "boom"}', returncode=returncode))
            self.assertNotIsInstance(caught.exception, (OcrEngineTimeout, OcrLanguageUnavailable))

    def test_an_engine_that_could_not_start_says_why(self):
        with self.assertRaises(OcrEngineError) as caught:
            self.read(FakeRunner(error="could not start: [WinError 2] not found"))
        self.assertIn("WinError 2", str(caught.exception))

    def test_an_engine_that_said_nothing_says_how_it_ended(self):
        with self.assertRaises(OcrEngineError) as caught:
            self.read(FakeRunner("", returncode=1, stderr="Add-Type : cannot load"))
        self.assertIn("exit code 1", str(caught.exception))
        self.assertIn("Add-Type", str(caught.exception))

    def test_an_engine_that_does_not_answer_in_time_is_its_own_kind_of_engine_failure(self):
        with self.assertRaises(OcrEngineTimeout) as caught:
            self.engine(FakeRunner(timed_out=True), timeout_s=2).read_text(frame_of(SCREEN))
        self.assertIsInstance(caught.exception, OcrEngineError)
        self.assertIn("2 s", str(caught.exception))
        self.assertEqual(self.leftovers(), [])

    def test_an_engine_timeout_is_ocr_unavailable_never_an_observation_timeout(self):
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(timed_out=True)))
        with self.assertRaises(OcrUnavailable) as caught:
            context.observe(TextQuery("Collect"))
        self.assertNotIsInstance(caught.exception, ObservationTimeout)
        self.assertIsInstance(caught.exception.__cause__, OcrEngineTimeout)

    def test_a_wait_whose_engine_fails_ends_failed_not_timed_out_and_sends_nothing(self):
        for runner in (FakeRunner(timed_out=True), FakeRunner('{"error": "broken"}', returncode=4)):
            rig = Rig([SCREEN], ocr=self.engine(runner))
            report = rig.run(WaitUntil(Condition.text("Collect"), timeout_s=5, interval_s=0.05),
                             Decision(Condition.text("Collect"), then=TapMatch()))
            self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_unavailable"))
            self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_text_that_never_appears_is_a_wait_timeout_not_an_ocr_failure(self):
        rig = Rig([SCREEN], ocr=self.engine(FakeRunner(payload(("Settings", 1, 1, 9, 9)))))
        report = rig.run(WaitUntil(Condition.text("Collect"), timeout_s=0.2, interval_s=0.05))
        self.assertEqual((report.state, report.reason), (FlowState.TIMED_OUT, "timeout"))

    def test_the_temporary_image_never_outlives_a_read(self):
        for runner in (FakeRunner(payload(("x", 0, 0, 1, 1))), FakeRunner('{"error": "boom"}', returncode=4),
                       FakeRunner(timed_out=True), FakeRunner("garbage")):
            with contextlib.suppress(OcrEngineError):
                self.read(runner)
            self.assertTrue(runner.paths and not runner.paths[0].exists())
        self.assertEqual(self.leftovers(), [])


# ---- cancellation and the emergency stop -------------------------------------------------------------------------
class Cancellation(EngineCase):
    def start_read(self, engine, **kw):
        out: dict = {}

        def work():
            started = time.monotonic()
            try:
                out["result"] = engine.read_text(frame_of(SCREEN), **kw)
            except BaseException as exc:
                out["error"] = exc
            out["took"] = time.monotonic() - started
        thread = threading.Thread(target=work)
        thread.start()
        return thread, out

    def test_a_cancel_ends_a_read_under_way_at_once_and_cleans_up(self):
        runner = FakeRunner(wait_for_cancel=True)
        cancel = threading.Event()
        thread, out = self.start_read(self.engine(runner, timeout_s=30), cancel=cancel)
        self.assertTrue(runner.started.wait(5))
        cancel.set()
        thread.join(5)
        self.assertIsInstance(out.get("error"), ReadCancelled)
        self.assertLess(out["took"], 2.0)                                       # not the 30 s read timeout
        self.assertIs(runner.calls[0][2], cancel)                              # handed to the runner itself
        self.assertFalse(runner.paths[0].exists())
        self.assertEqual(self.leftovers(), [])

    def test_the_emergency_stop_is_a_stop_not_a_cancel(self):
        stop = EmergencyStop()
        runner = FakeRunner(wait_for_cancel=True, stop=stop)
        thread, out = self.start_read(self.engine(runner), cancel=threading.Event())
        self.assertTrue(runner.started.wait(5))
        stop.trigger("test")
        thread.join(5)
        self.assertIsInstance(out.get("error"), EmergencyStopped)
        self.assertNotIsInstance(out.get("error"), ReadCancelled)

    def test_through_observation_a_cancel_is_observation_cancelled_and_the_stop_wins_a_tie(self):
        cancel = threading.Event()
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(wait_for_cancel=True, on_run=cancel.set)))
        with self.assertRaises(ObservationCancelled):
            context.observe(TextQuery("x"), cancel=cancel)
        stop, cancel = EmergencyStop(), threading.Event()

        def both():                                                        # both arrive during the same read
            cancel.set()
            stop.trigger("t")
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(wait_for_cancel=True, on_run=both)), stop=stop)
        with self.assertRaises(EmergencyStopped):
            context.observe(TextQuery("x"), cancel=cancel)

    def test_a_cancel_that_is_already_set_starts_nothing(self):
        runner = FakeRunner(payload())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ReadCancelled):
            self.engine(runner).read_text(frame_of(SCREEN), cancel=cancel)
        self.assertEqual((runner.calls, self.leftovers()), ([], []))           # no image written, no process

        legacy = ProvidersWithoutTheOptionalKeywords.Legacy()                 # one that cannot be told: not asked
        context, _ = ctx([SCREEN], ocr=legacy)  # pyright: ignore[reportArgumentType]
        with self.assertRaises(ObservationCancelled):
            context.observe(TextQuery("Collect"), cancel=cancel)
        self.assertEqual(legacy.calls, 0)

    def test_a_run_reports_the_stop_even_when_its_context_does_not_know_the_stop(self):
        stop, cancel = EmergencyStop(), threading.Event()

        def both():
            cancel.set()
            stop.trigger("t")
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(wait_for_cancel=True, on_run=both)))   # no stop
        rig = Rig([SCREEN], stop=stop)
        report = run_steps([Decision(Condition.text("x"), then=TapMatch())], target=rig.target, context=context,
                           create_playback=rig.create_playback, stop=stop, cancel=cancel)
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))
        self.assertEqual(rig.input.calls, [])

    def test_a_runs_own_cancel_ends_a_wait_whose_text_is_being_read(self):
        runner = FakeRunner(wait_for_cancel=True)
        rig = Rig([SCREEN], ocr=self.engine(runner, timeout_s=30))
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        report = rig.run(WaitUntil(Condition.text("Collect"), timeout_s=60, interval_s=0.05),
                         Decision(Condition.text("Collect"), then=TapMatch()), cancel=cancel)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "cancelled"))
        self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_the_emergency_stop_ends_a_run_whose_text_is_being_read(self):
        stop = EmergencyStop()
        rig = Rig([SCREEN], ocr=self.engine(FakeRunner(wait_for_cancel=True, stop=stop), timeout_s=30), stop=stop)
        threading.Timer(0.2, lambda: stop.trigger("test")).start()
        report = rig.run(Decision(Condition.text("Collect"), then=TapMatch()), cancel=threading.Event())
        self.assertEqual((report.state, report.reason), (FlowState.CANCELLED, "emergency_stop"))
        self.assertEqual(rig.input.calls, [])

    def test_a_probe_style_evaluation_hands_its_cancel_to_the_read(self):
        runner = FakeRunner(payload(("x", 0, 0, 1, 1)))
        context, _ = ctx([SCREEN], ocr=self.engine(runner))
        cancel = threading.Event()
        evaluate(Condition.text("x"), context, cancel=cancel)
        self.assertIs(runner.calls[0][2], cancel)


@unittest.skipIf(POWERSHELL is None, "Windows PowerShell is not available")
class RealProcessCancellation(unittest.TestCase):
    """The real `CommandRunner` and the real engine script: a cancel kills the PowerShell process mid-read."""

    def test_a_real_read_is_killed_by_a_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = WindowsOcrProvider(CommandRunner(EmergencyStop()), Path(tmp))
            if not engine.available():
                self.skipTest("Windows OCR engine is not available on this machine")
            cancel = threading.Event()
            threading.Timer(0.1, cancel.set).start()                       # well before PowerShell can answer
            started = time.monotonic()
            try:
                engine.read_text(frame_of(Bitmap.solid(400, 200, WHITE)), cancel=cancel)
            except ReadCancelled:
                took = time.monotonic() - started
            else:
                self.skipTest("the engine answered before the cancel could land")
            self.assertLess(took, 2.0)
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("*.png")), [])


# ---- temporary files and the script --------------------------------------------------------------------------------
class Lifetime(EngineCase):
    def test_building_an_engine_touches_nothing_on_disk(self):
        folder = self.dir / "not-yet"
        WindowsOcrProvider(FakeRunner(), folder)
        self.assertFalse(folder.exists())

    def test_the_script_is_written_on_first_use_as_ascii_and_atomically(self):
        self.read(FakeRunner(payload()))
        script = self.dir / winrt._SCRIPT_NAME
        self.assertEqual(script.read_bytes(), winrt._SCRIPT.encode("ascii"))
        self.assertTrue(winrt._SCRIPT.isascii())                                # PowerShell 5.1 reads it as ANSI
        self.assertEqual(self.leftovers(), [])

    def test_a_corrupted_or_deleted_script_is_restored_by_the_next_read(self):
        engine = self.engine(FakeRunner(payload()))
        script = self.dir / winrt._SCRIPT_NAME
        engine.read_text(frame_of(SCREEN))
        script.write_bytes(b"\xff\xfe not the script \x80")                   # not even UTF-8
        engine.read_text(frame_of(SCREEN))
        self.assertEqual(script.read_bytes(), winrt._SCRIPT.encode("ascii"))
        script.unlink()
        engine.read_text(frame_of(SCREEN))
        self.assertTrue(script.is_file())

    def test_an_unusable_folder_fails_the_read_explicitly_and_heals_when_it_is_usable_again(self):
        blocker = self.dir / "folder"
        blocker.write_text("a file where the folder should be", encoding="utf-8")
        engine = WindowsOcrProvider(FakeRunner(payload(("ok", 0, 0, 1, 1))), blocker)
        with self.assertRaises(OcrEngineError) as caught:
            engine.read_text(frame_of(SCREEN))
        self.assertIn("tried again", str(caught.exception))
        blocker.unlink()
        self.assertEqual(engine.read_text(frame_of(SCREEN)).text, "ok")

    def test_stale_temporary_files_of_a_killed_process_are_swept_and_fresh_ones_kept(self):
        import os
        old, fresh, probe, tmp = (self.dir / "ocr-old.png", self.dir / "ocr-fresh.png", self.dir / "probe-old.png",
                                  self.dir / f"{winrt._SCRIPT_NAME}.dead.tmp")
        for path in (old, fresh, probe, tmp):
            path.write_bytes(b"x")
        long_ago = time.time() - winrt.STALE_TEMP_S - 60
        for path in (old, probe, tmp):
            os.utime(path, (long_ago, long_ago))
        self.read(FakeRunner(payload()))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted(["ocr-fresh.png", winrt._SCRIPT_NAME]))

    def test_the_script_is_named_by_its_content_so_versions_never_share_one(self):
        import hashlib
        digest = hashlib.sha256(winrt._SCRIPT.encode("ascii")).hexdigest()[:16]
        self.assertEqual(winrt._SCRIPT_NAME, f"winrt_ocr-{digest}.ps1")
        older = self.dir / "winrt_ocr.ps1"                                      # what an older version uses
        older.write_bytes(b"# an older version's script, possibly running right now")
        runner = FakeRunner(payload())
        self.read(runner)
        self.assertEqual(older.read_bytes(), b"# an older version's script, possibly running right now")
        self.assertEqual(runner.args()[6], str(self.dir / winrt._SCRIPT_NAME))  # this version runs its own

    def test_several_targets_reading_at_once_share_one_script_safely(self):
        engine = self.engine(FakeRunner(payload(("ok", 0, 0, 1, 1))))
        results, errors = [], []

        def work():
            try:
                results.append(engine.read_text(frame_of(SCREEN)).text)
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual((errors, results), ([], ["ok"] * 8))
        self.assertEqual(self.leftovers(), [])


# ---- confidence --------------------------------------------------------------------------------------------------
class Confidence(EngineCase):
    """Windows.Media.Ocr has no confidence (OcrWord: BoundingRect, Text). Nothing may present its placeholder as a
    measurement, and a minimum that cannot be applied must not pretend to be applied."""

    def test_the_engine_says_it_has_no_confidence(self):
        result = self.read(FakeRunner(payload(("Collect", 1, 1, 9, 9))))
        self.assertFalse(result.confidence_measured)
        self.assertFalse(WindowsOcrProvider.reports_confidence)
        self.assertFalse(reports_confidence(self.engine(FakeRunner())))
        self.assertTrue(reports_confidence(FakeOcr()))                          # one that does not say: assumed

    def test_a_saved_minimum_is_not_applied_and_the_answer_says_so(self):
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(payload(("Collect", 1, 1, 9, 9)))))
        result = context.observe(TextQuery("Collect", min_confidence=0.9))
        self.assertTrue(result.found)                                           # a saved flow keeps working
        self.assertEqual((result.detail["confidence_measured"], result.detail["min_confidence_applied"]),
                         (False, False))

    def test_an_unmeasured_confidence_is_never_filtered_on_whatever_placeholder_it_carries(self):
        """The rule is "not measured -> not compared", not "1.0 happens to pass": a reader that says it measured
        nothing must not have its blocks thrown away by a minimum, whatever number it put in them."""
        class Unmeasured:
            reports_confidence = False

            def read_text(self, frame, *, roi=None, language="", cancel=None):
                return OcrResult((OcrTextBlock("Collect", Roi(1, 1, 9, 9), 0.2),), confidence_measured=False)

        context, _ = ctx([SCREEN], ocr=Unmeasured())
        result = context.observe(TextQuery("Collect", min_confidence=0.9))
        self.assertTrue(result.found)
        self.assertFalse(result.detail["min_confidence_applied"])

    def test_a_reader_with_real_confidence_still_has_its_minimum_applied(self):
        context, _ = ctx([SCREEN], ocr=FakeOcr([OcrTextBlock("Collect", Roi(1, 1, 9, 9), 0.5)]))
        result = context.observe(TextQuery("Collect", min_confidence=0.9))
        self.assertFalse(result.found)
        self.assertEqual((result.detail["confidence_measured"], result.detail["min_confidence_applied"]),
                         (True, True))

    def test_diagnostics_never_show_the_placeholder_as_a_number(self):
        unmeasured, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(payload(("Collect", 1, 1, 9, 9)))))
        self.assertIsNone(evaluate(Condition.text("Collect"), unmeasured).diagnostics()["confidence"])
        measured, _ = ctx([SCREEN], ocr=FakeOcr([OcrTextBlock("Collect", Roi(1, 1, 9, 9), 0.75)]))
        self.assertEqual(evaluate(Condition.text("Collect"), measured).diagnostics()["confidence"], 0.75)

    def test_the_result_type_refuses_a_flag_that_is_not_a_flag(self):
        with self.assertRaises(ValueError):
            OcrResult((), confidence_measured="no")  # pyright: ignore[reportArgumentType]  (deliberately invalid)


# ---- Unicode across the PowerShell -> Python boundary ---------------------------------------------------------
# Cyrillic, Swedish, a dash and a character outside the Basic Multilingual Plane (a UTF-16 surrogate pair).
SAMPLE = "Привет, Hämta Åäö — \U0001F600"


def ps_string(text: str) -> str:
    """A PowerShell expression that BUILDS `text` from UTF-16 code units, so a test checks the output side
    alone and never depends on how a non-ASCII command line reaches PowerShell."""
    data = text.encode("utf-16-le")
    units = ",".join(f"0x{int.from_bytes(data[i:i + 2], 'little'):04X}" for i in range(0, len(data), 2))
    return f"(-join ([char[]]@({units})))"


@unittest.skipIf(POWERSHELL is None, "Windows PowerShell is not available")
class UnicodeThroughTheRealRunner(unittest.TestCase):
    """The defect: Windows PowerShell 5.1 wrote the script's JSON in the console's code page and ConvertTo-Json
    does not escape non-ASCII, so b"H\\x84mta ???" reached Python and no decoder could repair it. These run the
    engine script's OWN writer in a real powershell.exe through the platform's real `CommandRunner` and its own
    UTF-8 decoding (`CommandResult.stdout_text`)."""

    def powershell(self, command: str) -> CommandResult:
        assert POWERSHELL is not None
        result = CommandRunner(EmergencyStop()).run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command], timeout=60)
        self.assertFalse(result.timed_out or result.error, result)
        return result

    def test_the_scripts_writer_delivers_cyrillic_swedish_and_beyond_intact(self):
        result = self.powershell(winrt._JSON_WRITER + f"Write-Json ([pscustomobject]@{{ text = {ps_string(SAMPLE)} }})")
        self.assertTrue(result.stdout.strip().isascii(), result.stdout)          # no code page can touch it
        self.assertEqual(json.loads(result.stdout_text.strip()), {"text": SAMPLE})

    def test_without_the_writer_this_console_corrupts_the_text(self):
        """The previous output path, kept as evidence that the test above can fail on this machine."""
        result = self.powershell(f"[pscustomobject]@{{ text = {ps_string(SAMPLE)} }} | ConvertTo-Json -Compress")
        decoded = json.loads(result.stdout_text.strip())["text"]
        if decoded == SAMPLE:
            self.skipTest("this console writes UTF-8, so the old output path happens to survive here")
        self.assertNotEqual(decoded, SAMPLE)

    def test_every_output_of_the_engine_script_goes_through_the_writer(self):
        script = winrt._SCRIPT
        self.assertIn(winrt._JSON_WRITER, script)
        self.assertEqual(script.count("ConvertTo-Json"), 1)                     # the one inside Write-Json
        self.assertEqual(script.count("Write-Output"), 1)                       # ... and its one way out
        self.assertNotIn("Write-Host", script)

    def test_the_whole_script_reports_a_windows_error_readably(self):
        """End to end through the real script: a missing image makes Windows raise an error in the user's own
        language (on this machine Swedish, with å/ä/ö); it must arrive as a readable engine error."""
        with tempfile.TemporaryDirectory() as tmp:
            engine = WindowsOcrProvider(CommandRunner(EmergencyStop()), Path(tmp))
            with self.assertRaises(OcrEngineError) as caught:
                engine._run(engine._prepare(), Path(tmp) / "missing.png")
        self.assertNotIn("�", str(caught.exception))
        self.assertNotIn("???", str(caught.exception))


class UnicodeInTheAdapter(EngineCase):
    def test_escaped_text_from_the_script_becomes_the_original_text(self):
        stdout = payload((SAMPLE, 1, 2, 30, 9))
        self.assertTrue(stdout.isascii())                                       # escaped, as the writer emits
        self.assertEqual(self.read(FakeRunner(stdout)).text, SAMPLE)

    def test_cyrillic_text_is_found_by_a_cyrillic_condition(self):
        context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(payload(("Забрать награду", 10, 20, 60, 14)))))
        self.assertTrue(context.observe(TextQuery("награду")).found)
        self.assertTrue(context.observe(TextQuery("ЗАБРАТЬ")).found)                # case folding works too


# ---- the language of a text condition --------------------------------------------------------------------------
class LanguageReachesTheEngine(EngineCase):
    def test_the_language_of_a_text_condition_reaches_the_engine(self):
        runner = FakeRunner(payload(("Привет", 10, 20, 60, 14), lang="ru"))
        context, _ = ctx([SCREEN], ocr=self.engine(runner))
        result = context.observe(TextQuery("привет", language="ru"))
        self.assertTrue(result.found)
        self.assertEqual(runner.args()[-2:], ("-Language", "ru"))
        self.assertEqual(result.detail["language"], "ru")                      # said, and now also true

    def test_it_reaches_the_engine_from_a_wait_and_from_an_if(self):
        runner = FakeRunner(payload(("Hämta", 10, 20, 60, 14), lang="sv-SE"))
        rig = Rig([SCREEN], ocr=self.engine(runner))
        report = rig.run(WaitUntil(Condition.text("Hämta", language="sv"), timeout_s=5, interval_s=0.05),
                         Decision(Condition.text("Hämta", language="sv-SE"), then=TapMatch()))
        self.assertEqual(report.state, FlowState.COMPLETED)
        self.assertEqual([c[0][-2:] for c in runner.calls], [("-Language", "sv"), ("-Language", "sv-SE")])
        self.assertEqual([c[:2] for c in rig.input.calls], [("tap", (40, 27))])

    def test_no_language_asks_for_none_and_the_query_overrides_the_engines_default(self):
        runner = FakeRunner(payload())
        ctx([SCREEN], ocr=self.engine(runner))[0].observe(TextQuery("x"))
        self.assertNotIn("-Language", runner.args())                           # nothing invented
        configured = self.engine(FakeRunner(payload(lang="en-US")), language="en")
        configured.read_text(frame_of(SCREEN))
        self.assertEqual(configured._runner.args()[-2:], ("-Language", "en"))
        other = self.engine(FakeRunner(payload(lang="ru")), language="en")
        other.read_text(frame_of(SCREEN), language="ru")
        self.assertEqual(other._runner.args()[-2:], ("-Language", "ru"))

    def test_a_malformed_language_never_reaches_the_command(self):
        for bad in ("-Path", "ru -Path x", "r", "ru;x", "ru\n", "ру", 5):
            runner = FakeRunner(payload())
            with self.assertRaises(OcrLanguageUnavailable, msg=repr(bad)):
                self.engine(runner).read_text(frame_of(SCREEN), language=bad)  # pyright: ignore[reportArgumentType]
            self.assertEqual(runner.calls, [], repr(bad))
        with self.assertRaises(OcrLanguageUnavailable):
            WindowsOcrProvider(FakeRunner(), self.dir, language="-Path")

    def test_a_language_without_an_installed_pack_is_refused_with_its_own_error_and_reason(self):
        for message in ("no OCR language pack for 'ja' (available: en-US,ru,sv-SE)",
                        "'qq' is not a language Windows recognizes"):
            refusal = json.dumps({"error": message})
            with self.assertRaises(OcrLanguageUnavailable) as caught:
                self.read(FakeRunner(refusal, returncode=3), language="ja")
            self.assertIsInstance(caught.exception, LanguageNotSupported)       # the contract's own error
            context, _ = ctx([SCREEN], ocr=self.engine(FakeRunner(refusal, returncode=3)))
            with self.assertRaises(ObserveLanguageError):
                context.observe(TextQuery("x", language="ja"))
            rig = Rig([SCREEN], ocr=self.engine(FakeRunner(refusal, returncode=3)))
            report = rig.run(Decision(Condition.text("x", language="ja"), then=TapMatch()))
            self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_language_unavailable"))
            self.assertIn(message.split("(")[0].strip(), report.message)
            self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_a_requested_script_must_be_the_script_that_was_read(self):
        with self.assertRaises(OcrLanguageUnavailable):
            self.read(FakeRunner(payload(lang="sr-Latn-RS")), language="sr-Cyrl")
        self.assertEqual(self.read(FakeRunner(payload(lang="sr-Cyrl-RS")), language="sr-Cyrl").blocks, ())
        self.assertEqual(self.read(FakeRunner(payload(lang="zh-Hans-CN")), language="zh").blocks, ())   # none asked

    def test_an_engine_that_answers_in_another_language_is_refused(self):
        with self.assertRaises(OcrLanguageUnavailable) as caught:
            self.read(FakeRunner(payload(("Hello", 1, 1, 9, 9), lang="en-US")), language="ru")
        self.assertIn("'ru'", str(caught.exception))
        self.assertEqual(self.read(FakeRunner(payload(lang="sv-SE")), language="sv").blocks, ())   # same language


class InstalledLanguages(EngineCase):
    """`languages()`: the same fixed script, in its list mode, through the same runner -- as strict, stop-aware and
    cancellable as a read -- and the language each read reports it was read in."""

    def langs(self, stdout: str, **kw):
        runner = FakeRunner(stdout, **kw)
        return self.engine(runner).languages(), runner

    def test_the_installed_languages_are_listed_in_order_without_repeats(self):
        tags, runner = self.langs(json.dumps({"languages": ["en-US", "ru", "sv-SE", "ru"]}))
        self.assertEqual(tags, ("en-US", "ru", "sv-SE"))
        args = runner.args()
        self.assertEqual(args[args.index("-File") + 2:], ("-ListLanguages",))  # nothing else is passed
        self.assertEqual(self.leftovers(), [])                                 # no image is written for a list

    def test_a_single_language_that_json_collapsed_is_still_a_list(self):
        self.assertEqual(self.langs(json.dumps({"languages": "ru"}))[0], ("ru",))

    def test_no_language_installed_is_an_empty_list_not_an_error(self):
        for stdout in (json.dumps({"languages": []}), json.dumps({"languages": None})):
            with self.subTest(stdout=stdout):
                self.assertEqual(self.langs(stdout)[0], ())

    def test_a_malformed_list_is_an_engine_error_and_nothing_is_believed(self):
        for bad in ({"languages": ["ru", 5]}, {"languages": ["not a tag!"]}, {"languages": {"ru": 1}},
                    {"languages": ["ru"], "extra": 1}, {"lang": "ru", "lines": []}, [], "ru", {"error": ""},
                    {"languages": ["ru"] * (winrt.MAX_LANGUAGES + 1)}):
            with self.subTest(bad=bad), self.assertRaises(OcrEngineError):
                self.langs(json.dumps(bad))
        with self.assertRaises(OcrEngineError):
            self.langs('{"languages": ["ru"], "languages": ["en"]}')            # duplicate keys

    def test_the_engines_own_error_is_reported_as_the_engines_error(self):
        with self.assertRaises(OcrEngineError) as caught:
            self.langs(json.dumps({"error": "WinRT OCR is not available here"}), returncode=4)
        self.assertIn("WinRT OCR is not available here", str(caught.exception))

    def test_an_engine_that_does_not_answer_in_time_is_a_timeout(self):
        with self.assertRaises(OcrEngineTimeout):
            self.langs("", timed_out=True)

    def test_a_cancel_already_set_starts_nothing_and_one_set_later_ends_the_listing(self):
        runner = FakeRunner("", wait_for_cancel=True)
        engine = self.engine(runner)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ReadCancelled):
            engine.languages(cancel=cancel)
        self.assertEqual(runner.calls, [])
        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(ReadCancelled):
            engine.languages(cancel=cancel)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIs(runner.calls[-1][2], cancel)

    def test_the_emergency_stop_is_a_stop_not_a_cancel(self):
        stop = EmergencyStop()
        runner = FakeRunner("", wait_for_cancel=True, stop=stop)
        timer = threading.Timer(0.05, stop.trigger, args=("test",))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(EmergencyStopped):
            self.engine(runner).languages()

    def test_a_read_says_which_language_it_was_read_in(self):
        self.assertEqual(self.read(FakeRunner(payload(("Hej", 1, 1, 5, 5), lang="sv-SE"))).language, "sv-SE")
        result = self.read(FakeRunner(payload(("Привет", 1, 1, 5, 5), lang="ru")), language="ru")
        self.assertEqual((result.language, result.text), ("ru", "Привет"))

    def test_a_result_holds_a_language_tag_or_nothing(self):
        self.assertEqual(OcrResult(()).language, "")
        self.assertEqual(OcrResult((), language="sv-SE").language, "sv-SE")
        for bad in ("not a tag!", "r", 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OcrResult((), language=bad)                                     # pyright: ignore[reportArgumentType]


class RefusedBeforeTheFirstStep(EngineCase):
    """A text question the run could never ask is refused before ANY step runs -- in particular before an earlier
    step has acted. (Found by review: with no OCR reader, a tap in step 1 was sent and the run failed in step 2.)"""

    def flow(self, condition):
        return [RunMacro(Macro(Recording(PROV, TID, (down(0, 5, 6), up(10, 5, 6))), "tap first")),
                WaitUntil(condition, timeout_s=5, interval_s=0.05)]

    def test_no_reader_refuses_the_run_before_its_first_action(self):
        rig = Rig([SCREEN])
        report = rig.run(*self.flow(Condition.text("Collect")))
        self.assertEqual((report.state, report.reason, report.steps), (FlowState.FAILED, "ocr_unavailable", ()))
        self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_a_language_the_reader_cannot_be_asked_for_refuses_the_run_before_its_first_action(self):
        rig = Rig([SCREEN], ocr=ProvidersWithoutTheOptionalKeywords.Legacy())
        report = rig.run(*self.flow(Condition.text("Collect", language="ru")))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_language_unavailable"))
        self.assertEqual((rig.built, rig.input.calls), ([], []))

    def test_a_malformed_language_from_a_file_refuses_the_run_before_its_first_action(self):
        for tag in ("-Path", "1234", "en-", "e n"):
            runner = FakeRunner(payload())
            rig = Rig([SCREEN], ocr=self.engine(runner))
            report = rig.run(*self.flow(Condition.text("Collect", language=tag)))
            self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_language_unavailable"), tag)
            self.assertEqual((rig.built, rig.input.calls, runner.calls), ([], [], []), tag)

    def test_a_flow_that_can_be_asked_still_runs_in_order(self):
        rig = Rig([SCREEN], ocr=self.engine(FakeRunner(payload(("Collect", 1, 1, 9, 9)))))
        report = rig.run(*self.flow(Condition.text("Collect", language="en")))
        self.assertEqual(report.state, FlowState.COMPLETED)
        self.assertEqual([c[:2] for c in rig.input.calls], [("tap", (5, 6))])


class ProvidersWithoutTheOptionalKeywords(unittest.TestCase):
    """Providers written before `language` and `cancel` existed keep working, and are never told a language they
    would ignore."""

    class Legacy:
        def __init__(self):
            self.calls = 0

        def read_text(self, frame, *, roi=None):
            self.calls += 1
            return OcrResult((OcrTextBlock("Collect", Roi(0, 0, 20, 10), 1.0),))

    class AnyKeyword:
        def __init__(self):
            self.options = None

        def read_text(self, frame, **options):
            self.options = options
            return OcrResult(())

    def test_they_still_answer_queries_without_a_language(self):
        legacy = self.Legacy()
        context, _ = ctx([SCREEN], ocr=legacy)  # pyright: ignore[reportArgumentType]  (an older, narrower shape)
        self.assertTrue(context.observe(TextQuery("Collect"), cancel=threading.Event()).found)
        self.assertEqual(legacy.calls, 1)                                       # cancel not forced on it

    def test_a_query_with_a_language_is_refused_with_its_own_reason_and_the_provider_is_never_called(self):
        legacy = self.Legacy()
        context, _ = ctx([SCREEN], ocr=legacy)  # pyright: ignore[reportArgumentType]
        with self.assertRaises(ObserveLanguageError) as caught:
            context.observe(TextQuery("Collect", language="ru"))
        self.assertIn("'ru'", str(caught.exception))
        self.assertEqual(legacy.calls, 0)
        rig = Rig([SCREEN], ocr=legacy)
        report = rig.run(WaitUntil(Condition.text("Collect", language="ru"), timeout_s=5, interval_s=0.05))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_language_unavailable"))

    def test_a_provider_that_takes_any_keyword_is_told_the_language_and_the_cancel(self):
        anything = self.AnyKeyword()
        cancel = threading.Event()
        context, _ = ctx([SCREEN], ocr=anything)  # pyright: ignore[reportArgumentType]
        context.observe(TextQuery("x", language="ru"), cancel=cancel)
        self.assertEqual(anything.options, {"roi": None, "language": "ru", "cancel": cancel})

    def test_the_shared_test_fake_is_told_the_language_as_well(self):
        fake = FakeOcr([])
        ctx([SCREEN], ocr=fake)[0].observe(TextQuery("x", language="sv-SE"))
        ctx([SCREEN], ocr=fake)[0].observe(TextQuery("x"))
        self.assertEqual(fake.languages, ["sv-SE", ""])


class RealEngineReadsTheChosenLanguage(unittest.TestCase):
    """Live, like `tests.test_observe.RealOcrEngine`: Windows' own OCR on rendered Cyrillic and Swedish words,
    each read in its own language. Skipped -- never failed -- where the engine, the renderer or a language pack
    is missing. No screen is read and nothing is sent anywhere."""

    tmp: tempfile.TemporaryDirectory
    runner: CommandRunner
    engine: WindowsOcrProvider

    @classmethod
    def setUpClass(cls):
        if POWERSHELL is None:
            raise unittest.SkipTest("Windows PowerShell is not available")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runner = CommandRunner(EmergencyStop())
        cls.engine = WindowsOcrProvider(cls.runner, Path(cls.tmp.name))
        if not cls.engine.available():
            cls.tmp.cleanup()
            raise unittest.SkipTest("Windows OCR engine is not available on this machine")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, text: str) -> Bitmap:
        assert POWERSHELL is not None
        path = Path(self.tmp.name) / f"render-{len(text)}.png"
        script = ("Add-Type -AssemblyName System.Drawing;"
                  "$b = New-Object System.Drawing.Bitmap(520,120);"
                  "$g = [System.Drawing.Graphics]::FromImage($b);"
                  "$g.Clear([System.Drawing.Color]::White);"
                  "$g.TextRenderingHint = 'AntiAliasGridFit';"
                  "$f = New-Object System.Drawing.Font('Segoe UI', 30);"
                  f"$g.DrawString({ps_string(text)}, $f, [System.Drawing.Brushes]::Black, 25, 30);"
                  f"$g.Dispose(); $b.Save('{path}'); $b.Dispose()")
        self.runner.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)
        if not path.is_file():
            self.skipTest("could not render a text image to read")
        return decode_png(path.read_bytes())

    def read(self, word: str, language: str) -> OcrResult:
        try:
            return self.engine.read_text(frame_of(self.render(word), "real-1"), language=language)
        except OcrLanguageUnavailable as exc:
            self.skipTest(f"no Windows OCR language pack for {language}: {exc}")

    def test_cyrillic_is_read_in_russian_and_arrives_intact(self):
        result = self.read("Привет", "ru")
        self.assertIn("привет", result.text.casefold())
        self.assertFalse(result.confidence_measured)

    def test_swedish_is_read_in_swedish_and_arrives_intact(self):
        self.assertIn("hämta", self.read("Hämta", "sv-SE").text.casefold())

    def test_the_real_engine_lists_the_installed_languages_and_reads_in_one_of_them(self):
        tags = self.engine.languages()
        self.assertTrue(tags, "an engine that is available has at least one language")
        self.assertTrue(all(isinstance(t, str) and t for t in tags), tags)
        result = self.engine.read_text(frame_of(self.render("Hello")), language="")
        self.assertTrue(result.language, "an automatic read says which language it used")
        self.assertIn(result.language.casefold(), {t.casefold() for t in tags})

    def test_a_language_no_machine_has_a_pack_for_is_refused_by_the_real_engine(self):
        """'qaa' is reserved for private use (ISO 639): no Windows OCR language pack can ever exist for it."""
        with self.assertRaises(OcrLanguageUnavailable) as caught:
            self.engine.read_text(frame_of(self.render("x")), language="qaa")
        self.assertIn("qaa", str(caught.exception))


# ---- without OCR, and the one place that chooses it -------------------------------------------------------
class WithoutOcr(unittest.TestCase):
    def test_a_run_that_needs_text_without_an_engine_fails_as_such_and_others_run(self):
        rig = Rig([SCREEN])                                                   # no OCR configured
        report = rig.run(WaitUntil(Condition.text("Collect"), timeout_s=5, interval_s=0.05))
        self.assertEqual((report.state, report.reason), (FlowState.FAILED, "ocr_unavailable"))
        self.assertIn("No OCR provider is configured", report.message)
        report = rig.run(Decision(Condition.pixel((30, 20), RED), then=TapMatch()))
        self.assertEqual(report.state, FlowState.COMPLETED)

    def test_the_composition_root_starts_without_ocr_and_says_why_when_the_engine_cannot_load(self):
        from app.bootstrap import main
        for patch in (mock.patch.dict(sys.modules, {"app.ocr.winrt": None}),              # import fails
                      mock.patch("app.ocr.winrt.WindowsOcrProvider", side_effect=OSError("no temp folder")),
                      mock.patch("app.ocr.winrt.WindowsOcrProvider", side_effect=OcrEngineError("no runner"))):
            err = io.StringIO()
            with patch, contextlib.redirect_stderr(err):
                self.assertIsNone(main._build_ocr(EmergencyStop()))
            self.assertIn("OCR", err.getvalue())                               # reported, never silent

    def test_the_composition_root_connects_the_windows_engine_over_the_platforms_runner_touching_nothing(self):
        from app.bootstrap import main
        stop = EmergencyStop()
        with mock.patch("app.ocr.winrt._ensure_script", side_effect=AssertionError("touched the disk")):
            engine = main._build_ocr(stop)
        self.assertIsInstance(engine, WindowsOcrProvider)
        assert isinstance(engine, WindowsOcrProvider)
        stop.trigger("test")                                                  # the SAME stop reaches its runner
        with self.assertRaises(EmergencyStopped):
            engine._runner.run(["powershell.exe"], timeout=1)

    def test_the_application_starts_with_no_ai_package_and_no_ocr_engine_at_all(self):
        """In a fresh interpreter in which `app.ai` and the OCR engine module cannot be imported at all: the
        composition root, the controller and the window module still import, and the composition root reports
        "no OCR" instead of failing."""
        code = (
            "import sys, importlib.abc\n"
            "class Block(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path, target=None):\n"
            "        if name == 'app.ai' or name.startswith('app.ai.') or name == 'app.ocr.winrt':\n"
            "            raise ImportError('not installed: ' + name)\n"
            "sys.meta_path.insert(0, Block())\n"
            "from app.bootstrap import main\n"
            "from app.core.cancel import EmergencyStop\n"
            "import app.gui.controller, app.gui.app\n"
            "assert main._build_ocr(EmergencyStop()) is None\n"
            "assert not any(m == 'app.ai' or m.startswith('app.ai.') or m == 'app.ocr.winrt' for m in sys.modules)\n"
            "print('started without ai and without an ocr engine')\n")
        import subprocess
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("started without ai and without an ocr engine", done.stdout)
        self.assertIn("OCR", done.stderr)                                       # and it said so

    def test_main_hands_the_engine_to_the_application(self):
        from app.bootstrap import main
        created = {}

        class FakeApp:
            def __init__(self, *args, **kwargs):
                created.update(kwargs)

            def run(self):
                created["ran"] = True

        services = mock.Mock()
        services.stop = EmergencyStop()
        with mock.patch.object(main, "_enable_dpi_awareness"), \
                mock.patch.object(main, "_build_services", return_value=services), \
                mock.patch("app.gui.app.App", FakeApp):
            self.assertEqual(main.main([]), 0)
        self.assertTrue(created.get("ran"))
        self.assertIsInstance(created["ocr"], WindowsOcrProvider)
        services.stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            created["ocr"]._runner.run(["powershell.exe"], timeout=1)


if __name__ == "__main__":
    unittest.main()
