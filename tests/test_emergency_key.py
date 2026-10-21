"""Esc as the Emergency Stop, in the application's own window (the spec's suggested default key).

The real application over the simulated Windows desktop of `tests.test_gui` (built but never shown). Esc stops what
is running, exactly as the button does; it does nothing while nothing runs, nothing in a dialog (where it closes the
dialog), and it never clears a stop. No real key is pressed: the handler gets the event Tk would hand it.

`TheStopBar`: the stop button only stops, the reset is a separate button that is unavailable while work is still
ending, and the bar is shown only while something runs or a stop is set. Where a test needs work that is still
ending, a stand-in engine takes the controller's own engine slot; nothing in this file can send input.
"""
import gc
import unittest

from app.gui.controller import PlaybackView
from app.playback.engine import ExecutionReport, PlaybackState
from tests import test_gui as gui_tests

tk = gui_tests.tk


class Event:
    def __init__(self, widget):
        self.widget = widget


class _Window(gui_tests.Base):
    """The real application window over the simulated desktop, built but never shown."""

    def setUp(self):
        super().setUp()
        assert tk is not None
        self.addCleanup(gc.collect)                   # Tk variables collected here, on the main thread
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True)
        self.c = self.app.controller
        self.c.refresh()
        self.addCleanup(self.app.close)

    def esc(self, widget=None):
        return self.app._key_estop(Event(widget if widget is not None else self.app.lbl_status))


@unittest.skipIf(tk is None, "tkinter is not available")
class EscapeIsTheEmergencyStop(_Window):
    def test_esc_is_bound_for_the_whole_window(self):
        self.assertTrue(self.root.bind_all("<Escape>"))

    def test_esc_stops_a_recording_under_way_like_the_button(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        self.assertEqual(self.esc(), "break")
        self.assertTrue(self.c.emergency_active)
        self.assertFalse(self.c.recording_active)                              # the recording was ended
        self.assertEqual(self.c.status.category, "emergency_stop")
        self.assertEqual(self.app.btn_estop.cget("text"), self.app.t("btn_estop"))   # never turns into a reset
        self.assertTrue(self.app.btn_reset_stop.winfo_manager())                      # the reset is its own button

    def test_esc_stops_a_playback_under_way(self):
        self.record()
        self.assertTrue(self.c.play(), self.c.status)
        if not self.c.playing:
            self.skipTest("the playback ended before Esc could be pressed")
        self.esc()
        self.assertTrue(self.c.emergency_active)
        if self.c.engine is not None:
            self.c.engine.wait(5)
        self.c.poll()
        self.assertFalse(self.c.playing)

    def test_esc_does_nothing_while_nothing_runs(self):
        self.assertIsNone(self.esc())
        self.assertFalse(self.c.emergency_active)

    def test_esc_never_clears_a_stop(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        self.esc()
        self.assertIsNone(self.esc())                                          # a second Esc: still stopped
        self.assertTrue(self.c.emergency_active)

    def test_esc_in_a_dialog_only_closes_the_dialog(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        dialog = tk.Toplevel(self.root)
        self.addCleanup(dialog.destroy)
        self.assertIsNone(self.esc(tk.Frame(dialog)))
        self.assertFalse(self.c.emergency_active)
        self.assertIsNone(self.app._key_estop(Event(".!combobox.popdown")))     # a widget known by its path only
        self.assertFalse(self.c.emergency_active)
        self.assertTrue(self.c.cancel())

    def test_the_button_says_which_key_stops(self):
        self.assertIn("Esc", self.app.t("estop_hint"))
        self.app.apply_language("ru")
        self.assertIn("Esc", self.app.t("estop_hint"))

    def test_esc_never_resets_a_stop_once_everything_has_ended_either(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        self.esc()
        self.assertEqual(self.c.stop_phase, "stopped")
        self.assertIsNone(self.esc())
        self.assertTrue(self.c.emergency_active)


class SlowEngine:
    """A playback engine that is still ending: it reports RUNNING until the test says how it ended. Stands in for
    the real engine where a test needs "the stop is set, but work is still under way" -- no input exists here."""

    def __init__(self, total=3):
        self.total, self.state, self.cancel_reason, self.cancels = total, PlaybackState.RUNNING, None, []

    def end(self, state, reason=None):
        self.state, self.cancel_reason = state, reason

    def report(self):
        return ExecutionReport(self.state, False, self.total, 0, 0, None, self.cancel_reason, (), ())

    def cancel(self, reason="cancelled"):
        self.cancels.append(reason)

    def wait(self, timeout=None):
        return self.state is not PlaybackState.RUNNING


@unittest.skipIf(tk is None, "tkinter is not available")
class TheStopBar(_Window):
    """The Emergency Stop as the window shows it: stopping and resetting are two different buttons, the bar is only
    there while it means something, and it follows the controller's own state -- busy and the stop -- not a second
    copy of it."""

    def shown(self, widget) -> bool:
        """Placed by its geometry manager -- and, for anything inside the bar, the bar is placed too."""
        bar = self.app.stop_bar
        return bool(widget.winfo_manager()) and (widget is bar or bool(bar.winfo_manager()))

    def running(self) -> SlowEngine:
        """Put the controller into "a playback is under way" through its own engine slot."""
        engine = SlowEngine()
        self.c.engine = engine
        self.c.playback = PlaybackView(True, False, PlaybackState.RUNNING.value, "Slow", 0, engine.total)
        self.c._touch()
        self.app.render()
        self.assertTrue(self.c.busy)
        return engine

    def test_idle_with_no_stop_shows_no_stop_bar(self):
        self.app.render()
        self.assertEqual(self.c.stop_phase, "idle")
        self.assertFalse(self.shown(self.app.stop_bar))

    def test_work_under_way_shows_the_stop_and_no_reset(self):
        self.running()
        self.assertEqual(self.c.stop_phase, "active")
        self.assertTrue(self.shown(self.app.stop_bar))
        self.assertTrue(self.shown(self.app.btn_estop))
        self.assertNotIn("disabled", self.app.btn_estop.state())
        self.assertFalse(self.shown(self.app.btn_reset_stop))
        self.assertEqual(self.app.lbl_stop.cget("text"), self.app.t("stop_bar_active"))

    def test_a_stop_request_while_running_sets_the_emergency_stop_and_cancels_the_work(self):
        engine = self.running()
        self.app._on_estop()
        self.assertTrue(self.c.emergency_active)
        self.assertTrue(self.stop.is_set())                           # the platform's one stop, not a view flag
        self.assertEqual(self.c.status.category, "emergency_stop")
        self.assertEqual(self.c.stop_phase, "stopping")              # the engine has not ended yet
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        self.app.render()
        self.assertEqual(self.c.stop_phase, "stopped")

    def test_pressing_the_stop_again_and_again_never_clears_it(self):
        engine = self.running()
        for _ in range(3):
            self.app._on_estop()                                      # while the work is still ending
            self.assertTrue(self.c.emergency_active)
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        for _ in range(3):
            self.app._on_estop()                                      # and once everything has ended
            self.assertTrue(self.c.emergency_active)
        self.assertEqual(self.app.btn_estop.cget("text"), self.app.t("btn_estop"))

    def test_the_reset_is_offered_but_unavailable_while_work_is_still_ending(self):
        engine = self.running()
        self.app._on_estop()
        self.assertTrue(self.shown(self.app.btn_reset_stop))
        self.assertIn("disabled", self.app.btn_reset_stop.state())
        self.assertEqual(self.app.lbl_stop.cget("text"), self.app.t("stop_bar_stopping"))
        self.app._on_reset_stop()                                     # even if it is invoked anyway
        self.assertTrue(self.c.emergency_active)
        self.assertEqual(self.c.status.key, "msg_busy_clear_stop")
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        self.app.render()
        self.assertNotIn("disabled", self.app.btn_reset_stop.state())

    def test_the_stopped_state_says_so_and_only_the_separate_reset_clears_it(self):
        engine = self.running()
        self.app._on_estop()
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        self.app.render()
        self.assertTrue(self.shown(self.app.stop_bar))                # latched: still shown, though nothing runs
        self.assertEqual(self.app.lbl_stop.cget("text"), self.app.t("stop_bar_stopped"))
        self.assertTrue(self.shown(self.app.btn_estop))               # kept in place: pressing it only keeps the stop
        self.assertTrue(self.shown(self.app.btn_reset_stop))
        self.assertNotEqual(self.app.btn_reset_stop.cget("text"), self.app.btn_estop.cget("text"))
        self.app._on_reset_stop()
        self.assertFalse(self.c.emergency_active)
        self.assertEqual(self.c.stop_phase, "idle")
        self.assertFalse(self.shown(self.app.stop_bar))               # and no overlay is left behind

    def test_a_hurried_second_click_on_the_stop_always_lands_on_the_stop_never_on_the_reset(self):
        """Reviewer finding: the reset used to take the stop's place once everything had ended, so a double click on
        Emergency Stop could clear it. The stop now keeps the right-most place in every phase the bar is shown in,
        and the reset only ever appears to its left."""
        def columns():
            placed = {name: int(widget.grid_info()["column"]) for name, widget in
                      (("stop", self.app.btn_estop), ("reset", self.app.btn_reset_stop)) if self.shown(widget)}
            self.assertIn("stop", placed)
            return placed

        engine = self.running()
        where = columns()["stop"]                                     # where the person aims while it runs
        self.app._on_estop()
        stopping = columns()
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        self.app.render()
        stopped = columns()
        for placed in (stopping, stopped):
            self.assertEqual(placed["stop"], where)
            self.assertLess(placed["reset"], placed["stop"])
        in_bar = [w for w in self.app.stop_bar.grid_slaves() if w is not self.app.lbl_stop]
        self.assertEqual(max(in_bar, key=lambda w: int(w.grid_info()["column"])), self.app.btn_estop)
        for _ in range(3):
            self.app.btn_estop.invoke()                               # what the hurried clicks actually hit
        self.assertTrue(self.c.emergency_active)

    def test_a_batch_run_shows_the_bar_and_the_reset_waits_until_every_target_has_ended(self):
        from app.gui.controller import RunState, TargetRun
        engine = SlowEngine()
        self.c.run_results = (TargetRun("r1", "Target 1", RunState.RUNNING, total=engine.total),)
        self.c.run_active, self.c._running, self.c.run_macro_name = True, {"r1": engine}, "Batch"
        self.c._touch()
        self.app.render()
        self.assertEqual(self.c.stop_phase, "active")
        self.assertTrue(self.shown(self.app.btn_estop))
        self.app._on_estop()
        self.assertEqual(engine.cancels, ["emergency_stop"])          # the batch was told to stop
        self.assertEqual(self.c.stop_phase, "stopping")
        self.assertIn("disabled", self.app.btn_reset_stop.state())
        self.app._on_reset_stop()
        self.assertTrue(self.c.emergency_active)                      # refused while a target is still ending
        engine.end(PlaybackState.CANCELLED, "emergency_stop")
        self.c.poll()
        self.app.render()
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.c.stop_phase, "stopped")
        self.assertNotIn("disabled", self.app.btn_reset_stop.state())
        self.app._on_reset_stop()
        self.assertFalse(self.shown(self.app.stop_bar))

    def test_the_bar_is_drawn_before_the_screen_so_a_failing_screen_cannot_hide_it(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        screen = self.app.screens[self.app.screen]

        def broken():
            raise RuntimeError("a screen that fails to render")
        screen.render = broken
        self.addCleanup(lambda: screen.__dict__.pop("render", None))
        self.assertFalse(self.shown(self.app.stop_bar))               # not drawn since the recording started
        with self.assertRaises(RuntimeError):
            self.app.render()
        self.assertTrue(self.shown(self.app.stop_bar))
        self.assertTrue(self.shown(self.app.btn_estop))
        screen.__dict__.pop("render")
        self.assertTrue(self.c.cancel())

    def test_no_bar_is_left_behind_when_work_completes_fails_or_is_cancelled(self):
        for state, reason in ((PlaybackState.COMPLETED, None), (PlaybackState.FAILED, None),
                              (PlaybackState.CANCELLED, "cancelled")):
            engine = self.running()
            self.assertTrue(self.shown(self.app.stop_bar), state)
            engine.end(state, reason)
            self.c.poll()
            self.app.render()
            self.assertIsNone(self.c.engine, state)
            self.assertFalse(self.c.emergency_active, state)
            self.assertFalse(self.shown(self.app.stop_bar), state)

    def test_an_ordinary_cancel_while_stopping_leaves_no_bar_once_it_has_ended(self):
        engine = self.running()
        self.assertTrue(self.c.cancel())
        self.assertEqual(engine.cancels, ["cancelled"])
        self.app.render()
        self.assertTrue(self.shown(self.app.btn_estop))               # still ending: the stop stays reachable
        engine.end(PlaybackState.CANCELLED, "cancelled")
        self.c.poll()
        self.app.render()
        self.assertFalse(self.shown(self.app.stop_bar))

    def test_a_real_recording_shows_the_bar_and_the_stop_ends_it(self):
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        self.app.render()
        self.assertTrue(self.shown(self.app.btn_estop))
        self.app._on_estop()
        self.assertFalse(self.c.recording_active)
        self.assertEqual(self.c.stop_phase, "stopped")
        self.assertFalse(self.c.start_recording(discard_unsaved=True))   # nothing new starts until the reset
        self.app._on_reset_stop()
        self.assertTrue(self.c.start_recording(discard_unsaved=True), self.c.status)
        self.assertTrue(self.c.cancel())

    def test_every_new_sentence_exists_in_both_languages(self):
        from app.gui.i18n import _STRINGS, LANGUAGES
        for key in ("btn_reset_stop", "reset_stop_hint", "stop_bar_active", "stop_bar_stopping", "stop_bar_stopped"):
            for language in LANGUAGES:
                self.assertTrue(_STRINGS[key][language].strip(), (key, language))
        self.app.apply_language("ru")
        self.running()
        self.assertEqual(self.app.lbl_stop.cget("text"), _STRINGS["stop_bar_active"]["ru"])


if __name__ == "__main__":
    unittest.main()
