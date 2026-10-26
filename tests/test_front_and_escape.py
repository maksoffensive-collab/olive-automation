"""A run's window in front, and Esc anywhere stops a run (the person's decisions of 2026-10-25).

Their report: a script did not run while another window (Olive itself, a browser) was over its target. Before a run
the target's window is now restored and made the main window; before each look and each click it is raised (without
taking the keyboard focus); and because Olive's own window -- its Stop button, its Esc -- then ends up behind it,
Esc anywhere stops a live run.

Fakes only: the simulated desktop and the fake input observer of tests.test_gui; the Win32 logic is driven with
stand-ins for its system calls. Nothing is sent, no real window is touched.
"""
import contextlib
import unittest

from app.core.cancel import EmergencyStop
from app.core.errors import TargetNotVerified
from app.windows.backend import DESKTOP_HWND, KeyObservation
from app.windows.escape import VK_ESCAPE, EscapeWatch, EscapeWatchUnavailable
from tests import test_gui as gui_tests
from tests.test_recorder import FakeObserver


def key(vk, down=True, *, own=False):
    return KeyObservation(down, vk, 0, 100, own=own)


class TheEscapeWatch(unittest.TestCase):
    def setUp(self):
        self.observer, self.stop = FakeObserver(), EmergencyStop()
        self.watch = EscapeWatch(self.observer, self.stop)

    def test_the_persons_esc_anywhere_triggers_the_emergency_stop(self):
        self.watch.start()
        self.assertTrue(self.observer.started)
        self.assertIs(self.observer.hwnd, DESKTOP_HWND)              # the whole keyboard, wherever the focus is
        self.observer.on_key(key(VK_ESCAPE))
        self.assertTrue(self.stop.is_set())

    def test_only_the_persons_own_esc_press_counts(self):
        self.watch.start()
        for observation in (key(0x41), key(VK_ESCAPE, down=False), key(VK_ESCAPE, own=True)):
            self.observer.on_key(observation)                        # a letter, a release, Olive's own Esc
        self.assertFalse(self.stop.is_set())

    def test_it_renews_its_hook_and_a_renewal_that_fails_stops_the_run(self):
        import time
        watch = EscapeWatch(self.observer, self.stop, renew_s=0.05)
        watch.start()
        deadline = time.time() + 3
        while self.observer.starts < 3 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(self.observer.starts, 3)               # taken down and put up again
        self.assertFalse(self.stop.is_set())
        self.observer.fail = RuntimeError("hooks refused")
        deadline = time.time() + 3
        while not self.stop.is_set() and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.stop.is_set())
        self.assertFalse(watch.active)
        watch.stop()

    def test_it_ends_with_its_stop_and_a_watch_that_cannot_start_says_so(self):
        self.watch.start()
        self.watch.stop()
        self.watch.stop()                                            # idempotent
        self.assertEqual((self.observer.started, self.observer.stops), (False, 1))
        broken = FakeObserver()
        broken.fail = RuntimeError("hooks refused")
        with self.assertRaises(EscapeWatchUnavailable):
            EscapeWatch(broken, self.stop).start()


class TheServices(gui_tests.Base):
    def target(self, title="Notes"):
        self.c.refresh()
        return self.services.verify(self.entry(title))

    def test_a_window_is_brought_to_front_and_only_at_the_start_made_the_main_one(self):
        target = self.target()
        self.assertFalse(self.services.bring_to_front(target, activate=True))
        self.world.covered = True
        self.assertTrue(self.services.bring_to_front(target))
        self.assertEqual(self.world.fronted, [(100, True), (100, False)])

    def test_full_windows_has_no_window_to_bring_to_front(self):
        self.c.refresh()
        whole = self.services.verify(next(e for e in self.c.entries if e.whole_screen))
        self.assertFalse(self.services.bring_to_front(whole, activate=True))
        self.assertEqual(self.world.fronted, [])

    def test_a_window_that_is_gone_is_never_brought_to_front(self):
        target = self.target()
        del self.world.windows[100]
        with self.assertRaises(TargetNotVerified):
            self.services.bring_to_front(target, activate=True)
        self.assertEqual(self.world.fronted, [])

    def test_esc_is_watched_for_a_window_but_full_windows_watches_itself(self):
        self.c.refresh()
        window, whole = self.entry("Notes"), next(e for e in self.c.entries if e.whole_screen)
        self.assertIsInstance(self.services.escape_watch([window]), EscapeWatch)
        self.assertIsInstance(self.services.escape_watch([whole, window]), EscapeWatch)
        self.assertIsNone(self.services.escape_watch([whole]))



class InARun(gui_tests.Base):
    """The controller: the watch lives exactly as long as a LIVE run, and the run's window is brought to the front."""

    def script(self, name, *steps):
        from app.decide import ConditionalMacro
        key = self.library.key_of(self.library.save(ConditionalMacro(name, steps)))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])

    def never(self):
        from app.decide import Condition, WaitUntil
        return WaitUntil(Condition.pixel((1, 1), (1, 2, 3)), 30, 0.05)            # never true here

    def finish(self, seconds=10.0):
        import time
        deadline = time.time() + seconds
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.assertFalse(self.c.run_active)

    def test_esc_anywhere_stops_a_live_run_and_the_watch_ends_with_it(self):
        import time
        self.c.refresh()
        self.script("Waits", self.never())
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertTrue(self.observer.started)
        self.assertIs(self.observer.hwnd, DESKTOP_HWND)
        time.sleep(0.2)
        self.observer.key(VK_ESCAPE, True, 0)                      # the person, in whatever window has the focus
        self.finish()
        self.assertEqual(self.c.run_results[0].state.value, "stopped")
        self.assertTrue(self.stop.is_set())
        self.assertFalse(self.observer.started)                   # nothing watched once the run is over

    def test_closing_the_application_during_a_run_ends_the_watch(self):
        self.c.refresh()
        self.script("Closed", self.never())
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertTrue(self.observer.started)
        self.c.shutdown()
        self.assertFalse(self.observer.started)

    def test_a_dry_run_is_watched_too_and_only_the_keyboard_is(self):
        """The review of 2026-10-25: a dry run brings the window to the front as well, so Esc must stop it too."""
        self.c.refresh()
        self.script("Dry", self.never())
        self.assertTrue(self.c.start_run(dry_run=True), self.c.status)
        self.assertTrue(self.observer.started)
        self.assertIsNone(self.observer.on_mouse)                  # no mouse hook at all
        self.c.cancel()
        self.finish()
        self.assertFalse(self.observer.started)

    def test_a_run_that_waits_to_start_watches_the_keyboard_only_once_it_acts(self):
        import time
        from app.decide import ConditionalMacro
        self.c.refresh()
        key = self.library.key_of(self.library.save(ConditionalMacro("Later", (self.never(),), start_delay_s=0.4)))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertFalse(self.observer.started)                    # during the delay: nothing watched
        deadline = time.time() + 5
        while not self.observer.started and time.time() < deadline:
            self.c.poll()
            time.sleep(0.02)
        self.assertTrue(self.observer.started)                     # it acts now: watched
        self.observer.key(VK_ESCAPE, True, 0)
        self.finish()
        self.assertEqual(self.c.run_results[0].state.value, "stopped")

    def test_the_first_look_after_the_start_uncovered_the_window_waits_for_it_to_draw(self):
        import time
        from app.decide import Condition, WaitUntil
        from app.observe import context as context_module
        self.c.refresh()
        here = WaitUntil(Condition.pixel((1, 1), (200, 60, 30)), 5, 0.05)
        self.script("First look", here)
        answers = iter([True])                                      # covered at the start, never again

        def raise_window(hwnd, *, activate):
            self.world.fronted.append((hwnd, activate))
            return next(answers, False)
        self.world.raise_window = raise_window
        captured = []
        original = self.world.capture
        self.world.capture = lambda *a: captured.append(time.monotonic()) or original(*a)
        started = time.monotonic()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.finish()
        self.assertEqual(self.c.run_results[0].state.value, "completed", self.c.run_results[0].detail)
        self.assertGreaterEqual(captured[0] - started, context_module.UNCOVERED_WAIT_S * 0.9)

    def test_a_target_that_starts_later_is_raised_but_never_given_the_focus(self):
        from app.playback.macro import Macro
        from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K
        from app.providers.recording import Recording
        self.c.refresh()
        target_id = self.services.verify(self.entry("Notes")).target_id
        click = Macro(Recording("windows", target_id, (E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1),
                                                       E(K.BUTTON_UP, 20, 5, 5, B.PRIMARY, 1))), "Click")
        key = self.library.key_of(self.library.save(click))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Notes").ref, self.entry("Calculator").ref])
        self.assertTrue(self.c.start_run(dry_run=True), self.c.status)
        self.finish()
        self.assertEqual([call for call in self.world.fronted if call[0] in (100, 101)][:2],
                         [(100, True), (101, False)])              # windows run one after another

    def test_a_stop_while_a_target_starts_is_a_stop_and_a_refused_raise_a_refusal(self):
        from app.core.errors import EmergencyStopped, TargetNotVerified
        self.c.refresh()
        self.script("Raised", self.never())
        for error, state in ((EmergencyStopped("stopped"), "stopped"), (TargetNotVerified("gone"), "refused")):
            def raise_window(hwnd, *, activate, error=error):
                raise error
            self.world.raise_window = raise_window
            with self.subTest(error=type(error).__name__):
                self.assertTrue(self.c.start_run(), self.c.status)
                self.finish()
                self.assertEqual(self.c.run_results[0].state.value, state)
                self.assertEqual(self.world.events, [])

    def test_a_watch_that_cannot_start_means_no_run(self):
        self.c.refresh()
        self.script("Unwatched", self.never())
        self.observer.fail = RuntimeError("hooks refused")
        self.assertFalse(self.c.start_run())
        self.assertEqual(self.c.status.key, "msg_escape_unavailable")
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.world.events, [])

    def test_the_window_is_made_the_main_one_at_the_start_and_raised_before_each_look(self):
        from app.decide import Condition, WaitUntil
        self.c.refresh()
        here = WaitUntil(Condition.pixel((1, 1), (200, 60, 30)), 5, 0.05)          # the fake window's colour
        self.script("Looks", here, here)
        self.assertTrue(self.c.start_run(), self.c.status)
        self.finish()
        self.assertEqual(self.c.run_results[0].state.value, "completed", self.c.run_results[0].detail)
        self.assertEqual(self.world.fronted[0], (100, True))      # once, at the start: the main window
        looks = self.world.fronted[1:]
        self.assertGreaterEqual(len(looks), 2)                     # then raised before every look ...
        self.assertTrue(all(call == (100, False) for call in looks))   # ... never taking the focus again


class TheLookWaitsForAnUncoveredTarget(unittest.TestCase):
    def test_a_target_that_had_to_be_uncovered_gets_a_moment_before_its_picture_is_taken(self):
        import time
        from app.observe import context as context_module
        from tests.test_observe import FakeScreenshots
        events = []
        stop = EmergencyStop()

        class Recorded(FakeScreenshots):
            def capture(self):
                events.append("capture")
                return super().capture()
        provider = Recorded()
        ctx = context_module.ObservationContext(provider, stop=stop, prepare=lambda: events.append("prepare") or True)
        started = time.monotonic()
        ctx.capture()
        self.assertGreaterEqual(time.monotonic() - started, context_module.UNCOVERED_WAIT_S * 0.9)
        self.assertEqual(events, ["prepare", "capture"])
        ctx = context_module.ObservationContext(provider, stop=stop, prepare=lambda: False)
        started = time.monotonic()
        ctx.capture()
        self.assertLess(time.monotonic() - started, context_module.UNCOVERED_WAIT_S * 0.5)

    def test_a_target_that_cannot_be_brought_forward_is_not_looked_at(self):
        from app.core.errors import TargetNotVerified
        from app.observe import ObservationUnavailable
        from app.observe import context as context_module
        from tests.test_observe import FakeScreenshots
        provider = FakeScreenshots()

        def gone():
            raise TargetNotVerified("The window was closed.", reason_code="window_gone")
        ctx = context_module.ObservationContext(provider, stop=EmergencyStop(), prepare=gone)
        with self.assertRaises(ObservationUnavailable):
            ctx.capture()
        self.assertEqual(provider.captures, 0)


class TheOtherProviders(unittest.TestCase):
    def test_a_device_has_nothing_to_bring_forward_and_a_combined_app_watches_esc_for_it(self):
        from tests.test_composite_services import two_provider_composite
        composite, _stop = two_provider_composite()
        entries = composite.discover()
        device = next(e for e in entries if e.title.startswith("LDPlayer"))
        target = composite.verify(device)
        self.assertFalse(composite.bring_to_front(target, activate=True))
        self.assertIsInstance(composite.escape_watch([device]), EscapeWatch)     # Esc is the person's, anywhere
        self.assertIsNone(composite.escape_watch([e for e in entries if e.whole_screen]))
        ld = next(s for s in composite._services if s.provider_id != "windows")
        self.assertIsNone(ld.escape_watch([device]))


class Win32BringToFront(unittest.TestCase):
    """The Win32 backend's own logic, its system calls replaced by stand-ins (no real window is touched)."""

    def backend(self, *, minimized=False, foreground=7, hit=5):
        import sys
        if sys.platform != "win32":
            self.skipTest("the Win32 backend exists only on Windows")
        from app.windows.win32 import Win32InputBackend
        b = Win32InputBackend()
        calls = []
        b._dpi_aware = contextlib.nullcontext
        b._IsWindow = lambda h: True
        b._IsIconic = lambda h: minimized
        b._ShowWindowAsync = lambda h, how: calls.append(("show", h, how)) or True
        b._GetForegroundWindow = lambda: foreground
        b._SetForegroundWindow = lambda h: calls.append(("foreground", h)) or True
        b._SetWindowPos = lambda h, after, x, y, w, hh, flags: calls.append(("raise", h, after, flags)) or True
        b._IsChild = lambda parent, child: False

        def client_rect(h, rect):
            rect._obj.right, rect._obj.bottom = 200, 100
            return True

        def to_screen(h, point):
            point._obj.x += 1000
            point._obj.y += 500
            return True
        b._GetClientRect, b._ClientToScreenBase = client_rect, to_screen
        b._WindowFromPoint = lambda point: calls.append(("hit", point.x, point.y)) or hit
        return b, calls

    def test_at_the_start_it_is_made_the_main_window_and_raised_without_a_click(self):
        b, calls = self.backend(foreground=7, hit=5)                   # window 5, uncovered, another one in front
        self.assertFalse(b.raise_window(5, activate=True))
        self.assertEqual([c[0] for c in calls], ["hit", "foreground", "raise"])
        self.assertEqual(calls[0][1:], (1100, 550))                     # the middle of its client area, on screen
        self.assertEqual(calls[-1][3] & 0x10, 0x10)                      # raised with SWP_NOACTIVATE
        self.assertEqual(calls[-1][3] & 0x4000, 0x4000)                  # ... and never waits for the target

    def test_during_a_run_it_is_only_raised_and_a_covered_one_says_so(self):
        b, calls = self.backend(foreground=7, hit=9)                    # window 9 over its middle
        self.assertTrue(b.raise_window(5, activate=False))
        self.assertEqual([c[0] for c in calls], ["hit", "raise"])     # the focus is never taken during a run

    def test_a_minimised_window_is_restored(self):
        b, calls = self.backend(minimized=True, foreground=5)
        self.assertTrue(b.raise_window(5, activate=True))
        self.assertEqual(calls[0], ("show", 5, 9))                      # SW_RESTORE at the start
        b, calls = self.backend(minimized=True)
        b.raise_window(5, activate=False)
        self.assertEqual(calls[0], ("show", 5, 4))                      # SW_SHOWNOACTIVATE during a run
        self.assertNotIn("foreground", [c[0] for c in calls])


if __name__ == "__main__":
    unittest.main()
