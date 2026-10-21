"""The Read-text screen: what the chosen target's screen says, read on request -- and nothing more.

Three layers, each with the real code above its outermost edge:

  * the observation API (`read_text`, `reader_languages`, `ObservationContext.read_text`) over the shared fakes;
  * the real `AppController` over the real `LDPlayerServices` -> `MappingService` chain with only the `AdbClient`
    faked (it shows each device's own screen and records every command), and the real `WindowsOcrProvider` with only
    its `CommandRunner` faked -- the runner recognises WHICH target's screen it was handed, so a reading is proven to
    come from the target that was chosen;
  * the real Tk application, built but never shown, driving the screen's own controls.

One class runs the whole chain for real instead -- a device screenshot of rendered Cyrillic text, the real engine,
the real runner -- and skips, never fails, where the engine is missing. Nothing here starts an emulator, reads the
real screen or sends input; every test checks that no tap, key or text reached a device.
"""
import gc
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError
from app.core.process import CommandResult, CommandRunner
from app.gui import theme as th
from app.gui.controller import (LANGS_FAILED, LANGS_IDLE, LANGS_NONE, LANGS_READY, LANGS_UNKNOWN, READ_CANCELLED,
                                READ_DONE, READ_FAILED, AppController, ReadTextJob, TextReading)
from app.observe import (ObservationCancelled, OcrLanguageUnavailable, OcrUnavailable, read_text,
                         reader_languages)
from app.ocr.provider import OcrResult, OcrTextBlock, ReadCancelled
from app.ocr.winrt import WindowsOcrProvider
from app.vision.bitmap import decode_png, encode_png
from app.vision.roi import Roi
from tests.test_observe import FakeOcr, block, context, frame_of, scene
from tests.test_ocr_engine import POWERSHELL, ps_string
from tests.test_ocr_integration import WORD, Base as RealChain, which_target

LANGS = ("en-US", "ru", "sv-SE")


def lines_of(target: int, lang: str) -> str:
    """The text each target shows next to its mark, as the engine script writes it."""
    from tests.test_gui_v2 import SPOTS
    x, y = SPOTS[target]
    return json.dumps({"lang": lang, "lines": [{"text": f"{WORD} {target}", "x": x, "y": y, "w": 8, "h": 8}]})


class TextRunner:
    """The OCR engine's `CommandRunner`, faked for this screen. A language list answers `languages` (or `listing`,
    a (stdout, returncode) pair); a read records which target's screen it was handed, its arguments and its cancel,
    and answers `respond(target, language)`. With `hold`, a read waits like the real runner does around a long read,
    until its cancel or the emergency stop is set."""

    def __init__(self, respond=None, *, languages=LANGS, listing=None, hold=False, stop=None):
        self.respond = respond or (lambda target, language: (lines_of(target, language or "en-US"), 0))
        self.languages, self.listing, self.hold, self.stop = languages, listing, hold, stop
        self.reads: list[tuple[int, tuple[str, ...], threading.Event | None]] = []
        self.lists: list[tuple[str, ...]] = []
        self.started = threading.Event()

    def run(self, args, *, timeout, cwd=None, cancel=None):
        args = tuple(args)
        if "-ListLanguages" in args:
            self.lists.append(args)
            stdout, code = self.listing or (json.dumps({"languages": list(self.languages)}), 0)
            return CommandResult(args, code, stdout.encode("utf-8"), b"")
        image = Path(args[args.index("-Path") + 1])
        self.reads.append((which_target(decode_png(image.read_bytes())), args, cancel))
        self.started.set()
        if self.hold:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if (cancel is not None and cancel.is_set()) or (self.stop is not None and self.stop.is_set()):
                    return CommandResult(args, None, cancelled=True)
                time.sleep(0.005)
            return CommandResult(args, None, timed_out=True)
        stdout, code = self.respond(self.reads[-1][0], self.language(-1))
        return CommandResult(args, code, stdout.encode("utf-8"), b"")

    def language(self, index: int = -1) -> str:
        args = self.reads[index][1]
        return args[args.index("-Language") + 1] if "-Language" in args else ""


def reading_of(c: AppController) -> TextReading:
    reading = c.reading
    assert reading is not None, c.status
    return reading


def begun(c: AppController, language: str = "") -> ReadTextJob:
    job = c.begin_read_text(language)
    assert job is not None, c.status
    return job


def was_set(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def outcome_of(job):
    try:
        return job()
    except BaseException as exc:          # what the view hands back: the result, or what was raised
        return exc


# ---- the observation API ---------------------------------------------------------------------------------------
class ReadingThroughObservation(unittest.TestCase):
    def test_all_the_text_comes_back_in_the_requested_language(self):
        ocr = FakeOcr([block("Hello"), block("World", y=20)])
        result = read_text(frame_of(scene()), ocr=ocr, language="sv-SE")
        self.assertEqual([b.text for b in result.blocks], ["Hello", "World"])
        self.assertEqual(ocr.languages, ["sv-SE"])

    def test_the_same_refusals_as_a_text_condition(self):
        with self.assertRaises(OcrUnavailable):
            read_text(frame_of(scene()), ocr=None)
        with self.assertRaises(OcrLanguageUnavailable):
            read_text(frame_of(scene()), ocr=FakeOcr(), language="not a tag!")
        with self.assertRaises(OcrUnavailable):
            read_text(frame_of(scene()), ocr=FakeOcr(fail=RuntimeError("engine broke")))

    def test_a_context_reads_a_fresh_frame_and_checks_before_it_captures(self):
        ocr = FakeOcr([block("Ready")])
        ctx, provider = context(ocr=ocr)
        self.assertEqual(ctx.read_text(language="ru").text, "Ready")
        self.assertEqual(ctx.read_text().text, "Ready")
        self.assertEqual(provider.captures, 2)                                  # never a cached frame
        with self.assertRaises(OcrLanguageUnavailable):
            ctx.read_text(language="русский")
        no_reader, provider = context(ocr=None)
        with self.assertRaises(OcrUnavailable):
            no_reader.read_text()
        self.assertEqual(provider.captures, 0)                                  # refused before capturing

    def test_a_cancel_set_before_the_read_captures_nothing_and_the_stop_outranks_it(self):
        cancel, stop = threading.Event(), EmergencyStop()
        cancel.set()
        ctx, provider = context(ocr=FakeOcr(), stop=stop)
        with self.assertRaises(ObservationCancelled):
            ctx.read_text(cancel=cancel)
        self.assertEqual(provider.captures, 0)
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            ctx.read_text(cancel=cancel)

    def test_a_cancel_during_the_read_is_a_cancel_unless_the_stop_came_too(self):
        stop = EmergencyStop()
        ctx, _ = context(ocr=FakeOcr(fail=ReadCancelled("ended")), stop=stop)
        with self.assertRaises(ObservationCancelled):
            ctx.read_text(cancel=threading.Event())

        class StopsWhileReading(FakeOcr):
            def read_text(self, frame, *, roi=None, language="", cancel=None):
                stop.trigger("pressed during the read")             # the stop ends the read as a cancel does
                raise ReadCancelled("ended")
        ctx, _ = context(ocr=StopsWhileReading(), stop=stop)
        with self.assertRaises(EmergencyStopped):
            ctx.read_text(cancel=threading.Event())

    def test_the_languages_of_a_reader_that_can_say_and_of_one_that_cannot(self):
        class Lists:
            def __init__(self, answer):
                self.answer, self.cancels = answer, []

            def read_text(self, frame, *, roi=None, language="", cancel=None):
                return OcrResult()

            def languages(self, *, cancel=None):
                self.cancels.append(cancel)
                if isinstance(self.answer, BaseException):
                    raise self.answer
                return self.answer

        cancel = threading.Event()
        lister = Lists(("en-US", "ru"))
        self.assertEqual(reader_languages(lister, cancel=cancel), ("en-US", "ru"))
        self.assertEqual(lister.cancels, [cancel])                              # its cancel reaches the reader
        self.assertIsNone(reader_languages(FakeOcr()))                           # cannot say: None, not ()
        with self.assertRaises(OcrUnavailable):
            reader_languages(None)
        with self.assertRaises(OcrUnavailable):
            reader_languages(Lists(["ru"]))                                      # not a tuple
        with self.assertRaises(OcrUnavailable):
            reader_languages(Lists(("ru", "not a tag!")))
        with self.assertRaises(OcrUnavailable):
            reader_languages(Lists(RuntimeError("broken")))
        with self.assertRaises(ObservationCancelled):
            reader_languages(Lists(ReadCancelled("ended")))
        with self.assertRaises(EmergencyStopped):
            reader_languages(Lists(EmergencyStopped("stop")))
        cancel.set()
        quiet = Lists(("ru",))
        with self.assertRaises(ObservationCancelled):
            reader_languages(quiet, cancel=cancel)
        self.assertEqual(quiet.cancels, [])                                      # asked nothing


# ---- the controller over the real device chain ----------------------------------------------------------------
class ReadTextOnATarget(RealChain):
    def read(self, c, language=""):
        job = begun(c, language)
        c.finish_read_text(outcome_of(job), job)
        return reading_of(c)

    def chosen(self, runner=None, index=1):
        c = self.controller(runner if runner is not None else TextRunner())
        self.assertTrue(c.select(self.entry(c, index).ref), c.status)
        self.adb.screencaps.clear()
        return c

    def assert_nothing_sent(self):
        self.assertEqual((self.adb.log, self.adb.taps, self.adb.keyevents, self.adb.texts), ([], [], [], []))

    def test_the_chosen_targets_screen_is_read_in_the_chosen_language_and_nothing_is_sent(self):
        runner = TextRunner()
        c = self.chosen(runner, index=2)
        reading = self.read(c, "ru")
        self.assertEqual(reading.state, READ_DONE)
        self.assertEqual((reading.lines, reading.text), ((f"{WORD} 2",), f"{WORD} 2"))
        self.assertEqual((reading.language, reading.read_in, reading.confidence_measured), ("ru", "ru", False))
        target = c.target
        assert target is not None
        self.assertEqual(reading.target_id, target.target_id)
        self.assertEqual([target for target, _, _ in runner.reads], [2])      # the frame of the CHOSEN target
        self.assertEqual(runner.language(), "ru")
        self.assertEqual(self.adb.screencaps, [self.serial(2)])                # one fresh capture of that target
        self.assertEqual((c.status.key, c.status.values["count"]), ("msg_text_read", 1))
        self.assertEqual(list(self.ocr_dir.glob("*.png")), [])                 # no temporary image left
        self.assert_nothing_sent()

    def test_automatic_asks_for_no_language_and_says_which_one_was_read(self):
        runner = TextRunner()
        reading = self.read(self.chosen(runner), "")
        self.assertNotIn("-Language", runner.reads[0][1])
        self.assertEqual((reading.language, reading.read_in), ("", "en-US"))

    def test_a_reader_that_measures_confidence_is_never_said_not_to(self):
        c = AppController(self.services, self.library, ocr=FakeOcr([block("Ready")]))   # measures; no language list
        self.addCleanup(c.shutdown)
        c.refresh()
        self.assertTrue(c.select(self.entry(c, 1).ref))
        reading = self.read(c, "")
        self.assertEqual((reading.lines, reading.confidence_measured, reading.read_in), (("Ready",), True, ""))

    def test_the_language_is_passed_as_chosen_and_never_replaced(self):
        runner = TextRunner()
        c = self.chosen(runner)
        for language in ("sv-SE", "  ru  "):
            self.read(c, language)
        self.assertEqual([runner.language(i) for i in range(2)], ["sv-SE", "ru"])

    def test_a_screen_without_text_is_an_empty_answer_not_an_error(self):
        runner = TextRunner(lambda target, language: (json.dumps({"lang": "ru", "lines": []}), 0))
        c = self.chosen(runner)
        reading = self.read(c, "ru")
        self.assertEqual((reading.state, reading.lines, reading.text), (READ_DONE, (), ""))
        self.assertEqual((c.status.level, c.status.key), ("info", "msg_text_none"))

    def test_a_language_without_a_pack_says_so(self):
        refusal = json.dumps({"error": "no OCR language pack for 'ja' (available: en-US,ru,sv-SE)"})
        c = self.chosen(TextRunner(lambda target, language: (refusal, 3)))
        reading = self.read(c, "ja")
        self.assertEqual((reading.state, reading.reason, reading.lines), (READ_FAILED, "language", ()))
        self.assertEqual((c.status.key, c.status.values["language"]), ("msg_text_language_missing", "ja"))
        self.assertIn("no OCR language pack", reading.detail)

    def test_an_answer_in_another_language_is_refused_not_shown(self):
        c = self.chosen(TextRunner(lambda target, language: (lines_of(target, "en-US"), 0)))
        reading = self.read(c, "ru")
        self.assertEqual((reading.state, reading.reason, reading.lines), (READ_FAILED, "language", ()))

    def test_an_engine_failure_is_reported_and_its_detail_kept(self):
        c = self.chosen(TextRunner(lambda target, language: ("not json at all", 0)))
        reading = self.read(c, "ru")
        self.assertEqual((reading.state, reading.reason), (READ_FAILED, "reader"))
        self.assertEqual((c.status.level, c.status.key), ("error", "msg_text_reader_failed"))
        self.assertIn("JSON", reading.detail)

    def test_a_capture_that_fails_is_reported_as_a_capture_failure(self):
        c = self.chosen()

        def broken(serial, *, grant):
            raise PlatformError("the device stopped answering")
        self.adb.screencap_png = broken
        reading = self.read(c, "ru")
        self.assertEqual((reading.state, reading.reason), (READ_FAILED, "capture"))
        self.assertEqual(c.status.key, "msg_text_capture_failed")

    def test_a_malformed_language_is_refused_before_anything_is_captured(self):
        runner = TextRunner()
        c = self.chosen(runner)
        for language in ("ru ssian", "русский", "r", "en_US"):
            with self.subTest(language=language):
                self.assertIsNone(c.begin_read_text(language))
                self.assertEqual(c.status.key, "msg_text_language_bad")
        self.assertIsNone(c.reading)
        self.assertEqual((runner.reads, self.adb.screencaps), ([], []))

    def test_without_a_reader_nothing_is_read_and_it_says_why(self):
        c = self.controller(None)
        self.assertTrue(c.select(self.entry(c, 1).ref))
        self.adb.screencaps.clear()
        self.assertIsNone(c.begin_read_text("ru"))
        self.assertEqual(c.status.key, "msg_text_no_reader")
        self.assertEqual(c.text_languages_state, LANGS_NONE)
        self.assertIsNone(c.begin_text_languages())
        self.assertEqual(self.adb.screencaps, [])

    def test_no_chosen_target_means_nothing_is_read(self):
        runner = TextRunner()
        c = self.controller(runner)
        self.assertIsNone(c.begin_read_text("ru"))                             # never a guess, never "all"
        self.assertEqual(c.status.key, "msg_verify_first")
        self.assertFalse(c.select(None))
        self.assertIsNone(c.begin_read_text("ru"))
        self.assertEqual((runner.reads, self.adb.screencaps), ([], []))

    def test_the_emergency_stop_refuses_a_reading_until_it_is_cleared(self):
        c = self.chosen()
        self.stop.trigger("test")
        self.assertIsNone(c.begin_read_text("ru"))
        self.assertEqual(c.status.category, "emergency_stop")
        self.assertEqual(self.adb.screencaps, [])
        self.assertTrue(c.clear_emergency_stop())
        self.assertEqual(self.read(c, "ru").state, READ_DONE)

    def test_the_stop_refuses_a_reading_even_where_re_verifying_would_not_notice_it(self):
        """The refusal is the controller's own, not borrowed from a provider whose re-check happens to look at the
        stop: a provider may well re-verify a target without it."""
        c = self.chosen()
        selected = c.selected
        assert selected is not None
        setattr(self.services, "recheck", lambda target: selected)            # a re-check blind to the stop
        self.stop.trigger("test")
        self.assertIsNone(c.begin_read_text("ru"))
        self.assertEqual(c.status.category, "emergency_stop")
        self.assertIsNone(c.reading)
        self.assertEqual(self.adb.screencaps, [])

    def test_one_reading_at_a_time(self):
        c = self.chosen()
        first = begun(c, "ru")
        self.assertIsNotNone(first)
        self.assertTrue(c.reading_text)
        self.assertIsNone(c.begin_read_text("ru"))
        self.assertEqual(c.status.key, "msg_text_busy")
        c.finish_read_text(outcome_of(first), first)
        self.assertFalse(c.reading_text)
        self.assertEqual(self.read(c, "ru").request, 2)

    def test_cancel_ends_the_read_under_way_and_its_answer_is_never_shown(self):
        runner = TextRunner(hold=True)
        c = self.chosen(runner)
        job = begun(c, "ru")
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("outcome", outcome_of(job)))
        worker.start()
        self.assertTrue(runner.started.wait(5))
        started = time.monotonic()
        self.assertTrue(c.cancel_read_text())
        self.assertEqual((reading_of(c).state, c.status.key), (READ_CANCELLED, "msg_text_cancelled"))
        self.assertFalse(c.reading_text)                                       # a new reading may start
        worker.join(5)
        self.assertLess(time.monotonic() - started, 2.0)                       # killed, not the 30 s timeout
        self.assertIsInstance(out["outcome"], ObservationCancelled)
        self.assertFalse(c.finish_read_text(out["outcome"], job))
        self.assertEqual((reading_of(c).state, reading_of(c).lines), (READ_CANCELLED, ()))
        self.assertTrue(was_set(runner.reads[0][2]))
        self.assertEqual(list(self.ocr_dir.glob("*.png")), [])
        self.assertEqual(c.cancel_read_text(), True)                           # nothing left: says so, no error
        self.assertEqual(c.status.key, "msg_nothing_to_cancel")

    def test_a_late_answer_never_replaces_a_newer_one(self):
        """Same target, two requests: the first was cancelled, the second answered -- then the first one's answer
        arrives after all (its reader finished just as it was cancelled). The screen keeps the newer answer."""
        c = self.chosen()
        old = begun(c, "ru")
        c.cancel_read_text()
        newer = self.read(c, "ru")
        self.assertEqual((newer.request, newer.state, newer.lines), (2, READ_DONE, (f"{WORD} 1",)))
        stale = OcrResult((OcrTextBlock("stale", Roi(0, 0, 4, 4), 1.0),), confidence_measured=False, language="ru")
        status = c.status
        self.assertFalse(c.finish_read_text(stale, old))
        self.assertEqual(c.reading, newer)                                     # untouched, field for field
        self.assertEqual(c.status, status)                                     # and nothing new was said

    def test_a_target_change_during_a_read_discards_its_answer(self):
        runner = TextRunner()
        c = self.chosen(runner, index=1)
        job = begun(c, "ru")
        self.assertTrue(c.select(self.entry(c, 2).ref))
        outcome = outcome_of(job)
        self.assertEqual([target for target, _, _ in runner.reads], [1])      # the job reads ITS target only
        self.assertFalse(c.finish_read_text(outcome, job))
        self.assertEqual((reading_of(c).state, reading_of(c).reason, reading_of(c).lines), (READ_FAILED, "target", ()))
        self.assertEqual(c.status.key, "msg_text_target_changed")

    def test_the_emergency_stop_ends_a_reading_under_way(self):
        runner = TextRunner(hold=True, stop=self.stop)
        c = self.chosen(runner)
        job = begun(c, "ru")
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("outcome", outcome_of(job)))
        worker.start()
        self.assertTrue(runner.started.wait(5))
        c.emergency_stop()
        worker.join(5)
        self.assertIsInstance(out["outcome"], EmergencyStopped)
        self.assertFalse(c.finish_read_text(out["outcome"], job))
        self.assertEqual((reading_of(c).state, reading_of(c).reason), (READ_FAILED, "stop"))
        self.assertEqual(c.status.category, "emergency_stop")
        self.assert_nothing_sent()

    def stopped_while_reading(self, c, job, runner):
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("outcome", outcome_of(job)))
        worker.start()
        self.assertTrue(runner.started.wait(5))
        started = time.monotonic()
        c.emergency_stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 2.0)                       # ended, not the 30 s timeout
        return out["outcome"]

    def test_the_emergency_stop_ends_a_reading_whose_reader_does_not_watch_the_stop(self):
        """This runner ends a read only when the read's OWN cancel is set (it knows nothing of the stop): the
        emergency stop must reach it all the same, and is reported as the stop."""
        runner = TextRunner(hold=True)
        c = self.chosen(runner)
        job = begun(c, "ru")
        outcome = self.stopped_while_reading(c, job, runner)
        self.assertIsInstance(outcome, EmergencyStopped)
        c.finish_read_text(outcome, job)
        self.assertEqual((reading_of(c).state, reading_of(c).reason), (READ_FAILED, "stop"))
        self.assert_nothing_sent()

    def test_the_emergency_stop_ends_a_probe_whose_reader_does_not_watch_the_stop(self):
        from app.decide import Condition
        runner = TextRunner(hold=True)
        c = self.chosen(runner)
        job = c.begin_probe(Condition.text(WORD, language="ru"))
        assert job is not None
        self.assertIsInstance(self.stopped_while_reading(c, job, runner), EmergencyStopped)
        self.assert_nothing_sent()

    def test_closing_the_application_ends_a_reading_under_way(self):
        runner = TextRunner(hold=True)
        c = self.chosen(runner)
        job = begun(c, "ru")
        worker = threading.Thread(target=lambda: outcome_of(job))
        worker.start()
        self.assertTrue(runner.started.wait(5))
        started = time.monotonic()
        c.shutdown()
        self.assertLess(time.monotonic() - started, 3.0)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(was_set(runner.reads[0][2]))

    def test_closing_waits_for_a_reading_whose_thread_starts_late_and_it_captures_nothing(self):
        """The worker is registered when the reading is begun, on the UI thread -- not when its thread first runs
        -- so closing the window cannot miss one that has not started yet; and starting after the close, it ends
        before taking a screenshot."""
        runner = TextRunner()
        c = self.chosen(runner)
        job = begun(c, "ru")
        out = {}
        worker = threading.Thread(target=lambda: (time.sleep(0.2), out.setdefault("outcome", outcome_of(job))))
        worker.start()
        started = time.monotonic()
        c.shutdown()
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.15)                                  # it waited for the late worker,
        self.assertLess(waited, 1.5)                                           # which then ended at once
        worker.join(1)
        self.assertIsInstance(out["outcome"], ObservationCancelled)
        self.assertEqual((runner.reads, self.adb.screencaps), ([], []))

    def test_the_reader_lists_its_languages_once_asked(self):
        runner = TextRunner()
        c = self.controller(runner)
        self.assertEqual((c.text_languages_state, c.text_languages), (LANGS_IDLE, ()))
        self.assertEqual(runner.lists, [])                                     # not asked at start-up
        job = c.begin_text_languages()
        self.assertIsNone(c.begin_text_languages())                            # one request at a time
        self.assertTrue(c.finish_text_languages(outcome_of(job)))
        self.assertEqual((c.text_languages_state, c.text_languages), (LANGS_READY, LANGS))
        self.assertEqual(len(runner.lists), 1)
        self.assertEqual(self.adb.screencaps, [])                              # listing reads no screen

    def test_a_listing_that_fails_or_that_the_reader_cannot_give_is_said_as_such(self):
        broken = TextRunner(listing=(json.dumps({"error": "WinRT OCR is not available"}), 4))
        c = self.controller(broken)
        job = c.begin_text_languages()
        self.assertFalse(c.finish_text_languages(outcome_of(job)))
        self.assertEqual((c.text_languages_state, c.text_languages), (LANGS_FAILED, ()))
        self.assertIn("WinRT OCR is not available", c.text_languages_detail)
        none_installed = self.controller(TextRunner(languages=()))
        job = none_installed.begin_text_languages()
        none_installed.finish_text_languages(outcome_of(job))
        self.assertEqual((none_installed.text_languages_state, none_installed.text_languages), (LANGS_READY, ()))
        legacy = AppController(self.services, self.library, ocr=FakeOcr())        # no `languages` method
        self.addCleanup(legacy.shutdown)
        legacy.finish_text_languages(outcome_of(legacy.begin_text_languages()))
        self.assertEqual(legacy.text_languages_state, LANGS_UNKNOWN)

    def test_closing_ends_a_language_listing_under_way(self):
        hold = TextRunner(hold=True)

        def slow_listing(args, *, timeout, cwd=None, cancel=None):
            hold.started.set()
            while not was_set(cancel):
                time.sleep(0.005)
            return CommandResult(tuple(args), None, cancelled=True)
        setattr(hold, "run", slow_listing)
        c = self.controller(hold)
        job = c.begin_text_languages()
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("outcome", outcome_of(job)))
        worker.start()
        self.assertTrue(hold.started.wait(5))
        started = time.monotonic()
        c.shutdown()
        self.assertLess(time.monotonic() - started, 3.0)
        worker.join(1)
        self.assertIsInstance(out["outcome"], ObservationCancelled)
        c.finish_text_languages(out["outcome"])
        self.assertEqual(c.text_languages_state, LANGS_IDLE)                   # ended, not "could not be loaded"
        self.assertIsNone(c.begin_text_languages())                            # and never asked again once closed

    @staticmethod
    def blind_listing():
        """A runner whose language list ends only when ITS OWN cancel is set -- it knows nothing of the stop --
        and answers at once when `quick` is set."""
        runner, quick = TextRunner(), threading.Event()
        answer = runner.run

        def run(args, *, timeout, cwd=None, cancel=None):
            if quick.is_set():
                return answer(args, timeout=timeout, cwd=cwd, cancel=cancel)
            runner.started.set()
            while not was_set(cancel):
                time.sleep(0.005)
            return CommandResult(tuple(args), None, cancelled=True)
        setattr(runner, "run", run)
        return runner, quick

    def test_the_emergency_stop_during_the_language_list_is_the_stop_and_the_list_is_asked_again_after(self):
        runner, quick = self.blind_listing()
        c = self.controller(runner)
        job = c.begin_text_languages()
        assert job is not None
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault("outcome", outcome_of(job)))
        worker.start()
        self.assertTrue(runner.started.wait(5))
        c.emergency_stop()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(out["outcome"], EmergencyStopped)                # the stop, not "cancelled"
        c.finish_text_languages(out["outcome"])
        self.assertEqual((c.text_languages_state, c.text_languages_detail), (LANGS_IDLE, ""))   # not FAILED
        self.assertEqual(c.status.key, "msg_emergency_stop")                   # the stop's status is kept
        self.assertIsNone(c.begin_text_languages())                            # not asked again under the stop
        self.assertTrue(c.clear_emergency_stop())
        quick.set()
        job = c.begin_text_languages()                                         # asked again once it is cleared
        assert job is not None
        self.assertTrue(c.finish_text_languages(outcome_of(job)))
        self.assertEqual((c.text_languages_state, c.text_languages), (LANGS_READY, LANGS))

    def test_a_language_list_stopped_before_its_thread_runs_asks_nothing_and_is_the_stop(self):
        runner = TextRunner()
        c = self.controller(runner)
        job = c.begin_text_languages()
        assert job is not None
        c.emergency_stop()
        outcome = outcome_of(job)
        self.assertIsInstance(outcome, EmergencyStopped)
        self.assertEqual(runner.lists, [])
        c.finish_text_languages(outcome)
        self.assertEqual(c.text_languages_state, LANGS_IDLE)
        self.assertEqual(c.status.key, "msg_emergency_stop")

    def test_a_finished_reading_is_dropped_when_another_target_is_chosen(self):
        """What was read on one target must never stay on screen -- or be copied -- as if it were another's."""
        c = self.chosen(TextRunner(), index=1)
        self.assertEqual(self.read(c, "ru").lines, (f"{WORD} 1",))
        self.assertTrue(c.select(self.entry(c, 1).ref))                        # the same target again: kept
        self.assertEqual(reading_of(c).lines, (f"{WORD} 1",))
        self.assertTrue(c.select(self.entry(c, 2).ref))                        # another target: gone
        self.assertIsNone(c.reading)
        self.assertEqual(self.read(c, "ru").lines, (f"{WORD} 2",))
        self.assertFalse(c.select("no such target"))                           # no target at all: gone as well
        self.assertIsNone(c.reading)
        for finished in (READ_FAILED, READ_CANCELLED):                         # any finished reading, not only text
            with self.subTest(state=finished):
                self.assertTrue(c.select(self.entry(c, 1).ref))
                self.read(c, "ru")
                c.reading = replace(reading_of(c), state=finished)
                self.assertTrue(c.select(self.entry(c, 2).ref))
                self.assertIsNone(c.reading)

    def test_a_finished_reading_is_dropped_for_another_provider_with_the_same_target_id(self):
        c = self.chosen(TextRunner(), index=1)
        reading = self.read(c, "ru")
        target = c.target
        assert target is not None
        self.assertEqual((reading.provider_id, reading.target_id), (target.provider_id, target.target_id))
        c.reading = replace(reading, provider_id="another provider")
        self.assertTrue(c.select(self.entry(c, 1).ref))
        self.assertIsNone(c.reading)

    AUTOMATIC_REFUSED = json.dumps({"error": "none of the user's languages has an OCR language pack (available: )"})

    def automatic_refused(self, languages, *, listed=True):
        c = self.chosen(TextRunner(lambda target, language: (self.AUTOMATIC_REFUSED, 3), languages=languages))
        if listed:
            c.finish_text_languages(outcome_of(c.begin_text_languages()))
        return c, self.read(c, "")

    def test_an_automatic_read_with_no_language_installed_says_so_in_words(self):
        c, reading = self.automatic_refused(())                                # the reader lists none at all
        self.assertEqual((reading.state, reading.reason, reading.language), (READ_FAILED, "no_language", ""))
        self.assertEqual((c.status.level, c.status.key), ("error", "msg_text_no_language"))
        self.assertNotIn("language", c.status.values)                          # no empty “” to show
        self.assertIn("none of the user's languages", reading.detail)

    def test_an_automatic_read_that_finds_no_language_never_claims_none_is_installed(self):
        """Automatic may pick only among some languages (Windows: the user's own), so it can fail while others
        are installed -- or while it is not known which are. Only a list that is known to be empty says "none"."""
        for languages, listed in ((("en-US",), True), (LANGS, False)):
            with self.subTest(languages=languages, listed=listed):
                c, reading = self.automatic_refused(languages, listed=listed)
                self.assertEqual((reading.state, reading.reason), (READ_FAILED, "auto_language"))
                self.assertEqual((c.status.level, c.status.key), ("error", "msg_text_auto_language"))

    def test_an_answer_that_arrives_after_the_stop_is_kept_but_the_stop_stays_said(self):
        """A reading that finished just before the stop: its answer is still applied (it was read, nothing acts on
        it), but the status keeps saying EMERGENCY STOP -- never "Read 1 line(s)" in green over it."""
        for respond, state in ((None, READ_DONE), (lambda target, language: ("not json", 0), READ_FAILED)):
            with self.subTest(state=state):
                self.stop.reset()                                              # each case starts unstopped
                c = self.chosen(TextRunner(respond))
                job = begun(c, "ru")
                outcome = outcome_of(job)
                c.emergency_stop()
                c.finish_read_text(outcome, job)
                self.assertEqual(reading_of(c).state, state)
                self.assertEqual((c.status.category, c.status.key), ("emergency_stop", "msg_emergency_stop"))
                self.assertTrue(c.clear_emergency_stop())


# ---- the screen, in the real (never shown) application ----------------------------------------------------------
try:
    import tkinter as tk
except ImportError:                                   # pragma: no cover
    tk = None


class Later:
    """A `preview_runner` that holds each job until the test lets it run: a reading that is still under way."""

    def __init__(self):
        self.pending = []

    def __call__(self, fn):
        self.pending.append(fn)

    def run_all(self):
        while self.pending:
            self.pending.pop(0)()


@unittest.skipIf(tk is None, "tkinter is not available")
class TextScreenView(RealChain):
    def setUp(self):
        super().setUp()
        assert tk is not None
        self.addCleanup(gc.collect)                   # Tk variables collected here, on the main thread
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        self.addCleanup(self.destroy_root)            # even in a test that never builds the application

    def destroy_root(self):
        assert tk is not None
        try:
            self.root.destroy()
        except tk.TclError:
            pass                                      # the application already closed it

    def app(self, runner, *, preview_runner=None, select=1):
        from app.gui.app import App
        engine = WindowsOcrProvider(runner, self.ocr_dir) if runner is not None else None
        app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                  preview_runner=preview_runner or (lambda fn: fn()), ocr=engine)
        self.addCleanup(self.close_quietly, app)
        app.controller.refresh()
        if select:
            self.assertTrue(app.controller.select(self.entry(app.controller, select).ref))
        self.copied: list[str] = []
        setattr(app, "copy_text", self.copied.append)  # never the real clipboard
        return app

    @staticmethod
    def close_quietly(app):
        assert tk is not None
        try:
            app.close()
        except tk.TclError:
            pass

    def show(self, app):
        app.show_screen("text")
        app._drain_jobs()                             # the language list, asked when the screen is first shown
        app.render()
        return app.screens["text"]

    def shown_text(self, screen) -> str:
        return screen.text.get("1.0", "end-1c")

    def stub_targets(self, value):
        from app.gui.view import dialogs

        class Stub:
            calls = 0

            def __init__(self, *args, **kwargs):
                Stub.calls += 1
                Stub.kwargs = kwargs

            def show(self):
                return value
        original = dialogs.ChooseTargets
        dialogs.ChooseTargets = Stub
        self.addCleanup(setattr, dialogs, "ChooseTargets", original)
        return Stub

    # ---- navigation and layout -------------------------------------------------------------------------------
    def test_it_is_a_place_in_the_navigation_with_its_own_breadcrumb(self):
        app = self.app(TextRunner())
        self.assertIn("text", app.nav_buttons)
        self.assertEqual(app.nav_buttons["text"][0].cget("text"), "Read text")
        app.nav_buttons["text"][0].invoke()
        self.assertEqual(app.screen, "text")
        self.assertEqual(app._breadcrumb(), "Read text")
        self.assertEqual(str(app.nav_buttons["text"][0].cget("style")), "NavActive.TButton")
        app.apply_language("ru")
        self.assertEqual(app.nav_buttons["text"][0].cget("text"), "Текст на экране")
        self.assertEqual(app._breadcrumb(), "Текст на экране")

    def test_it_fits_the_smallest_window_in_both_languages(self):
        app = self.app(TextRunner())
        screen = self.show(app)
        for language in ("en", "ru"):
            app.apply_language(language)
            self.root.geometry("1120x680")
            self.root.update_idletasks()
            self.assertLessEqual(screen.frame.winfo_reqwidth(), 1120, language)
            self.assertLessEqual(self.root.winfo_reqwidth(), 1120, language)                 # the topbar too
            chrome = app.header.winfo_reqheight()                                           # the page header
            self.assertLessEqual(screen.frame.winfo_reqheight() + chrome, 680, language)

    # ---- languages ------------------------------------------------------------------------------------------
    def test_the_installed_languages_are_offered_after_automatic_and_only_those(self):
        runner = TextRunner()
        app = self.app(runner)
        self.assertEqual(runner.lists, [])                                     # not before the screen is shown
        screen = self.show(app)
        self.assertEqual(len(runner.lists), 1)
        self.assertEqual(list(screen.box_language.cget("values")), [app.t("text_lang_auto"), *LANGS])
        self.assertIn("readonly", screen.box_language.state())
        self.assertEqual((screen.language_var.get(), screen.language()), (app.t("text_lang_auto"), ""))
        app.apply_language("ru")
        self.assertEqual(screen.language_var.get(), app.t("text_lang_auto"))   # follows the interface language
        self.assertEqual(screen.language(), "")
        app.render()
        self.assertEqual(len(runner.lists), 1)                                 # asked once, not on every redraw
        screen.btn_languages.invoke()
        app._drain_jobs()
        self.assertEqual(len(runner.lists), 2)                                 # and again on request

    def test_the_chosen_language_belongs_to_this_window_whatever_other_tk_root_exists(self):
        """Found by the full suite: the language variable had no master, so with another Tk root around (Tk's
        "default" one) it lived in THAT interpreter and the choice made in this window never arrived."""
        assert tk is not None
        stray = tk.Tk()
        stray.withdraw()
        self.addCleanup(stray.destroy)
        setattr(tk, "_default_root", stray)                                    # what a second root does
        self.addCleanup(setattr, tk, "_default_root", None)
        runner = TextRunner()
        app = self.app(runner)
        screen = self.show(app)
        screen.box_language.set("ru")
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual(runner.language(), "ru")

    def test_a_reader_that_cannot_list_lets_a_language_be_typed_and_says_so(self):
        broken = TextRunner(listing=(json.dumps({"error": "broken"}), 4))
        app = self.app(broken)
        screen = self.show(app)
        self.assertNotIn("readonly", screen.box_language.state())
        self.assertIn(app.t("text_note_languages_failed"), screen.lbl_note.cget("text"))
        screen.language_var.set("sv-SE")
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual(broken.language(), "sv-SE")

    def test_no_installed_language_is_explained(self):
        app = self.app(TextRunner(languages=()))
        screen = self.show(app)
        self.assertIn(app.t("text_note_no_languages"), screen.lbl_note.cget("text"))

    # ---- reading ----------------------------------------------------------------------------------------------
    def test_reading_shows_the_text_the_language_and_that_confidence_is_not_measured(self):
        runner = TextRunner()
        app = self.app(runner, select=2)
        screen = self.show(app)
        screen.box_language.set("ru")
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual(self.shown_text(screen), f"{WORD} 2")
        title = reading_of(app.controller).target_title
        self.assertEqual(screen.lbl_result.cget("text"),
                         app.t("text_result_meta").format(count=1, name=title, language="ru")
                         + " · " + app.t("text_confidence_unmeasured"))
        self.assertEqual(runner.language(), "ru")
        self.assertEqual([target for target, _, _ in runner.reads], [2])
        self.assertEqual(screen.text.cget("state"), "disabled")               # read-only ...
        screen.text.tag_add("sel", "1.0", "1.4")                               # ... but selectable
        self.assertEqual(screen.text.get("sel.first", "sel.last"), WORD[:4])
        screen.btn_copy.invoke()
        self.assertEqual(self.copied, [WORD[:4]])                              # the selection
        screen.text.tag_remove("sel", "1.0", "end")
        screen.btn_copy.invoke()
        self.assertEqual(self.copied[-1], f"{WORD} 2")                         # or everything
        self.assertEqual(app.controller.status.text, app.t("msg_text_copied"))
        self.assertEqual((self.adb.log, self.adb.taps), ([], []))

    def said(self, answer, language, key, languages=LANGS):
        """Read once with the engine answering `answer`; the result area must say `key` and show no text."""
        app = self.app(TextRunner(lambda target, asked: answer, languages=languages))
        screen = self.show(app)
        screen.language_var.set(language)
        screen.btn_read.invoke()
        app._drain_jobs()
        expected = app.t(key).format(name=reading_of(app.controller).target_title, language=language)
        self.assertEqual(screen.lbl_result.cget("text"), expected)
        self.assertEqual(self.shown_text(screen), "")
        self.assertIn("disabled", screen.btn_copy.state())

    def test_a_screen_without_text_is_said_in_words(self):
        self.said((json.dumps({"lang": "ru", "lines": []}), 0), "ru", "text_result_none")

    def test_a_missing_language_pack_is_said_in_words(self):
        self.said((json.dumps({"error": "no OCR language pack for 'ja'"}), 3), "ja", "text_fail_language")

    def test_a_reader_failure_is_said_in_words(self):
        self.said(("not json", 0), "ru", "text_fail_reader")

    AUTOMATIC_REFUSED = (json.dumps({"error": "none of the user's languages has an OCR language pack"}), 3)

    def test_an_automatic_read_with_no_language_installed_is_said_in_words(self):
        self.said(self.AUTOMATIC_REFUSED, "", "text_fail_no_language", languages=())

    def test_an_automatic_read_that_finds_no_language_while_others_are_installed_is_said_in_words(self):
        self.said(self.AUTOMATIC_REFUSED, "", "text_fail_auto_language")

    def test_the_text_of_one_target_is_not_shown_or_copied_once_another_is_chosen(self):
        app = self.app(TextRunner(), select=1)
        screen = self.show(app)
        screen.box_language.set("ru")
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual(self.shown_text(screen), f"{WORD} 1")
        self.assertTrue(app.controller.select(self.entry(app.controller, 2).ref))
        app.render()
        self.assertEqual(self.shown_text(screen), "")
        self.assertEqual(screen.lbl_result.cget("text"), app.t("text_result_empty"))
        self.assertIn("disabled", screen.btn_copy.state())
        screen._copy()                                                         # even called directly: nothing
        self.assertEqual(self.copied, [])

    def test_the_emergency_stop_during_the_language_list_is_not_a_failure_and_the_list_comes_back(self):
        later = Later()
        runner = TextRunner()
        app = self.app(runner, preview_runner=later)
        screen = self.show(app)                                                # the list is asked, not yet run
        app.controller.emergency_stop()
        later.run_all()                                                        # it ends under the stop
        app._drain_jobs()
        app.render()
        self.assertEqual(runner.lists, [])
        self.assertEqual(later.pending, [])                                    # not asked again under the stop
        note = screen.lbl_note.cget("text")
        self.assertNotIn(app.t("text_note_languages_failed"), note)
        self.assertIn(app.t("text_note_stopped"), note)
        self.assertIn("disabled", screen.btn_languages.state())                # nothing offered that is refused
        self.assertEqual(app.controller.status.key, "msg_emergency_stop")
        self.assertTrue(app.controller.clear_emergency_stop())
        app.render()                                                           # asked again once cleared
        later.run_all()
        app._drain_jobs()
        self.assertEqual(len(runner.lists), 1)
        self.assertEqual(list(screen.box_language.cget("values")), [app.t("text_lang_auto"), *LANGS])

    def test_a_worker_thread_that_cannot_start_ends_the_request_and_holds_nothing_open(self):
        def no_threads(fn):
            raise RuntimeError("can't start new thread")
        runner = TextRunner()
        app = self.app(runner, preview_runner=no_threads)
        self.adb.screencaps.clear()
        screen = self.show(app)                                               # the list: refused, said so
        self.assertEqual(app.controller.text_languages_state, LANGS_FAILED)
        self.assertIn("can't start new thread", app.controller.text_languages_detail)
        screen.box_language.set("ru")
        screen._read()
        app._drain_jobs()
        app.render()
        reading = reading_of(app.controller)
        self.assertEqual((reading.state, reading.reason), (READ_FAILED, "error"))
        self.assertFalse(app.controller.reading_text)                          # a new reading may be asked for
        self.assertNotIn("disabled", screen.btn_read.state())
        self.assertEqual(app.controller._probes, set())                       # nothing registered as running
        started = time.monotonic()
        app.controller.shutdown()
        self.assertLess(time.monotonic() - started, 0.5)                       # so closing waits for nothing
        self.assertEqual((runner.reads, runner.lists, self.adb.screencaps), ([], [], []))

    def test_while_reading_it_is_busy_cancel_ends_it_and_a_late_answer_is_dropped(self):
        later = Later()
        runner = TextRunner()
        app = self.app(runner, preview_runner=later)
        screen = self.show(app)
        later.run_all()                                                        # the language list
        app._drain_jobs()
        app.render()
        screen.btn_read.invoke()
        self.assertTrue(app.controller.reading_text)
        self.assertIn("disabled", screen.btn_read.state())                    # no second request
        self.assertIn("disabled", screen.btn_target.state())
        self.assertNotIn("disabled", screen.btn_cancel.state())
        self.assertEqual(screen.progress.winfo_manager(), "pack")
        self.assertIn(app.t("text_result_reading").format(name=reading_of(app.controller).target_title),
                      screen.lbl_result.cget("text"))
        screen.btn_cancel.invoke()
        self.assertEqual(screen.progress.winfo_manager(), "")
        self.assertNotIn("disabled", screen.btn_read.state())
        self.assertEqual(screen.lbl_result.cget("text"), app.t("text_result_cancelled"))
        later.run_all()                                                        # the cancelled read ends now
        app._drain_jobs()
        self.assertEqual(reading_of(app.controller).state, READ_CANCELLED)
        self.assertEqual(self.shown_text(screen), "")
        self.assertEqual(runner.reads, [])                                     # its cancel was set first

    def test_reading_without_a_target_asks_for_exactly_one(self):
        runner = TextRunner()
        app = self.app(runner, select=0)
        screen = self.show(app)
        self.assertIn(app.t("text_note_no_target"), screen.lbl_note.cget("text"))
        self.assertEqual(screen.lbl_target.cget("text"), app.t("text_target_none"))
        chooser = self.stub_targets(())                                        # nothing chosen
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual((chooser.calls, chooser.kwargs), (1, {"single": True}))
        self.assertEqual((runner.reads, self.adb.screencaps), ([], []))
        self.assertIsNone(app.controller.reading)
        self.stub_targets((self.entry(app.controller, 2).ref,))
        screen.btn_read.invoke()
        app._drain_jobs()
        self.assertEqual([target for target, _, _ in runner.reads], [2])
        self.assertEqual(self.shown_text(screen), f"{WORD} 2")
        self.assertIn("-2", screen.lbl_target.cget("text"))

    def test_without_a_reader_the_screen_says_why_and_offers_nothing_to_click(self):
        app = self.app(None)
        screen = self.show(app)
        self.assertEqual(screen.lbl_note.cget("text"), app.t("text_note_no_reader"))
        for button in (screen.btn_read, screen.btn_cancel, screen.btn_languages, screen.btn_copy):
            self.assertIn("disabled", button.state())
        self.assertIn("disabled", screen.box_language.state())
        app.show_screen("macros")                                              # the rest of the app is unaffected
        self.assertEqual(app.screen, "macros")

    def test_the_emergency_stop_disables_reading_and_says_why(self):
        app = self.app(TextRunner())
        screen = self.show(app)
        app._on_estop()
        self.assertIn("disabled", screen.btn_read.state())
        self.assertIn(app.t("text_note_stopped"), screen.lbl_note.cget("text"))
        app._on_estop()                                                        # pressing the stop again: still stopped
        self.assertIn("disabled", screen.btn_read.state())
        app._on_reset_stop()                                                   # the separate, explicit reset
        self.assertNotIn("disabled", screen.btn_read.state())


class ScreenText(unittest.TestCase):
    def test_every_sentence_the_screen_can_show_exists_in_both_languages(self):
        """`tests.test_gui.I18nCoverage` scans the controller; this screen builds keys of its own, some of them
        (the reasons a reading failed) from data."""
        import re
        from app.gui.i18n import _STRINGS, LANGUAGES
        source = (Path(__file__).resolve().parents[1] / "app" / "gui" / "view" / "text.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'"((?:text|btn|msg)_[a-z_]+)"', source))
        keys |= {f"text_fail_{reason}" for reason in ("reader", "language", "no_language", "auto_language",
                                                      "capture", "target", "stop", "error")}
        self.assertGreater(len(keys), 30)
        self.assertEqual(sorted(k for k in keys if k not in _STRINGS), [])
        for key in keys:
            fields = [set(re.findall(r"\{(\w+)\}", _STRINGS[key][language])) for language in LANGUAGES]
            self.assertTrue(all(f == fields[0] for f in fields), key)


# ---- the whole chain for real ---------------------------------------------------------------------------------
class RealEngineOnADeviceScreen(RealChain):
    """A device screenshot of rendered Russian text, read by the REAL engine through the REAL runner, from the Text
    screen's own controller path. Skipped -- never failed -- where Windows PowerShell, the engine, the renderer or
    the Russian language pack is missing. The device is the test's fake: nothing is captured from the real screen
    and nothing is sent."""

    def render(self, tmp: Path, text: str) -> bytes:
        assert POWERSHELL is not None
        path = tmp / "device.png"
        script = ("Add-Type -AssemblyName System.Drawing;"
                  "$b = New-Object System.Drawing.Bitmap(1280,720);"
                  "$g = [System.Drawing.Graphics]::FromImage($b);"
                  "$g.Clear([System.Drawing.Color]::White);"
                  "$g.FillRectangle([System.Drawing.Brushes]::Red, 30, 20, 8, 8);"
                  "$g.TextRenderingHint = 'AntiAliasGridFit';"
                  "$f = New-Object System.Drawing.Font('Segoe UI', 40);"
                  f"$g.DrawString({ps_string(text)}, $f, [System.Drawing.Brushes]::Black, 200, 300);"
                  f"$g.Dispose(); $b.Save('{path}'); $b.Dispose()")
        CommandRunner(EmergencyStop()).run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                                           timeout=60)
        if not path.is_file():
            self.skipTest("could not render a device screen to read")
        # the device answers with ITS OWN encoding of the image, as a real screencap would
        return encode_png(decode_png(path.read_bytes()))

    def test_the_chosen_device_screen_is_read_in_russian_end_to_end(self):
        if POWERSHELL is None:
            self.skipTest("Windows PowerShell is not available")
        engine = WindowsOcrProvider(CommandRunner(self.stop), self.ocr_dir)
        if not engine.available():
            self.skipTest("the Windows OCR engine is not available on this machine")
        if "ru" not in {tag.split("-")[0] for tag in engine.languages()}:
            self.skipTest("no Russian OCR language pack on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            self.adb.screens[self.serial(1)] = self.render(Path(tmp), "Привет мир")
        c = AppController(self.services, self.library, history=self.history, ocr=engine)
        self.addCleanup(c.shutdown)
        c.refresh()
        self.assertTrue(c.select(self.entry(c, 1).ref))
        self.adb.screencaps.clear()
        job = begun(c, "ru")
        c.finish_read_text(outcome_of(job), job)
        reading = reading_of(c)
        self.assertEqual(reading.state, READ_DONE, reading.detail)
        self.assertIn("привет", reading.text.casefold())
        self.assertEqual(reading.read_in.split("-")[0], "ru")
        self.assertFalse(reading.confidence_measured)
        self.assertEqual(self.adb.screencaps, [self.serial(1)])
        self.assertEqual((self.adb.log, self.adb.taps, self.adb.keyevents, self.adb.texts), ([], [], [], []))
        self.assertEqual([p for p in self.ocr_dir.glob("*.png")], [])


if __name__ == "__main__":
    unittest.main()
