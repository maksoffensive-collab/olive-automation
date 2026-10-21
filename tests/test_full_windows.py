"""Full Windows macros: recording and playing on the WHOLE desktop, on the person's explicit decision of 2026-10-19,
with the safeguards they agreed to -- each one tested here:

  * only Full Windows, explicitly chosen, in ONE screen layout: another layout refuses the run, the recording and
    the picture;
  * never on Olive's own windows (a click there, or a key while Olive has the focus, is refused);
  * the person can take over at once: Esc, a click or the mouse moved by hand trigger the emergency stop while a
    live run acts -- and a run whose watch cannot start does not start;
  * a dry run sends nothing; a macro recorded in a window never plays on the desktop, nor the other way round.

Deterministic: the simulated desktop of tests.test_gui (`World` records what would be sent) and the fake observation
hooks of tests.test_recorder. Nothing here reaches the real screen, keyboard or mouse.
"""
import time
import unittest

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, TargetNotVerified
from app.playback.engine import PlaybackRefused, PlaybackState
from app.playback.macro import Macro
from app.providers.input import Key
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.windows.backend import KeyEvent, KeyObservation, MouseEvent, MouseObservation, MouseObservationKind as M
from app.windows.desktop import TAKEOVER_DISTANCE, VK_ESCAPE, DesktopInputSession, DesktopRunGuard
from app.windows.input import InputRefused
from app.windows.playback import create_desktop_playback, create_window_playback
from app.windows.recorder import DesktopRecorder
from app.windows.target import FullWindowsTarget, WindowVerifier, desktop_id, is_desktop_id
from tests import test_gui as gui_tests
from tests.test_gui import OWN_PID, RunState, TargetStatus, World
from tests.test_recorder import MS, FakeObserver, VK_A
from tests.test_windows_provider import info

LAYOUT = (-80, 0, 160, 90)                 # World's simulated virtual screen: two monitors, the left one at x < 0
OLIVE = 102                                # World's window of this application (pid OWN_PID)


def world():
    desk = World(info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10),
                 info(hwnd=OLIVE, title="Olive Automation", name="python.exe", cls="TkTopLevel", pid=OWN_PID))
    desk.desktop = LAYOUT
    return desk


def target(layout=LAYOUT, monitors=()):
    return FullWindowsTarget(time.monotonic(), layout, monitors)


def clicks(*points, layout=LAYOUT):
    """A macro recorded on the whole desktop (in `layout`): one click per point."""
    events = []
    for i, (x, y) in enumerate(points):
        events += [E(K.BUTTON_DOWN, 0 if not i else 200, x, y, B.PRIMARY, 1), E(K.BUTTON_UP, 40, x, y, B.PRIMARY, 1)]
    return Macro(Recording("windows", desktop_id(layout), tuple(events)), "On the desktop")


def desktop_sends(desk):
    return [e for e in desk.events if e[0].startswith("desktop_")]


_SEEN_AS = {MouseEvent.LEFT_DOWN: M.PRIMARY_DOWN, MouseEvent.LEFT_UP: M.PRIMARY_UP,
            MouseEvent.RIGHT_DOWN: M.SECONDARY_DOWN, MouseEvent.RIGHT_UP: M.SECONDARY_UP}


def echo_own_input(desk, observer):
    """Like the real hooks: every event the run sends is seen by the watch, carrying this application's mark."""
    def echo(d):
        event = d.events[-1]
        if not observer.started:
            return
        if event[0] == "desktop_mouse":
            observer.on_mouse(MouseObservation(_SEEN_AS.get(event[1], M.MOVE), event[2], event[3], 0, 100, None,
                                               None, True, True))
        elif event[0] in ("desktop_key", "desktop_char"):
            observer.on_key(KeyObservation(True, 0, 0, 100, own=True))
    desk.on_event = echo


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


TWO_MONITORS = ((-80, 0, 80, 90), (0, 0, 80, 60))      # inside LAYOUT; below the right one (x >= 0, y >= 60): nothing


# ---- the session: every event at a screen point, gated -----------------------------------------------------------
class Session(unittest.TestCase):
    def setUp(self):
        self.desk, self.stop = world(), EmergencyStop()

    def session(self, *, dry_run=False, layout=LAYOUT):
        return DesktopInputSession(target(layout), self.desk, self.stop, max_actions=50, dry_run=dry_run,
                                   own_pid=OWN_PID)

    def test_a_click_lands_on_the_screen_point_of_the_picture_pixel(self):
        self.session().tap(30, 40)
        left, top = LAYOUT[:2]
        self.assertEqual(desktop_sends(self.desk), [("desktop_mouse", MouseEvent.LEFT_DOWN, left + 30, top + 40),
                                                    ("desktop_mouse", MouseEvent.LEFT_UP, left + 30, top + 40)])

    def test_keys_and_text_go_to_the_focused_window_that_is_not_olive(self):
        session = self.session()
        session.key_press(Key.ENTER)
        session.type_text("ok")
        self.assertEqual(desktop_sends(self.desk), [("desktop_key", KeyEvent.DOWN, Key.ENTER),
                                                    ("desktop_key", KeyEvent.UP, Key.ENTER),
                                                    ("desktop_char", "o"), ("desktop_char", "k")])

    def test_never_on_olives_own_window(self):
        self.desk.hit = OLIVE
        with self.assertRaises(InputRefused):
            self.session().tap(30, 40)
        self.desk.hit, self.desk.foreground = 100, OLIVE
        with self.assertRaises(InputRefused):
            self.session().key_press(Key.ENTER)                       # Olive has the focus: the key would go to it
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_changed_screen_layout_a_point_off_the_screen_or_on_no_window_sends_nothing(self):
        self.desk.desktop = (0, 0, 200, 90)
        with self.assertRaises(TargetNotVerified):
            self.session().tap(30, 40)
        self.desk.desktop = LAYOUT
        with self.assertRaisesRegex(InputRefused, "outside"):
            self.session().tap(LAYOUT[2], 10)                         # one past the right edge
        self.desk.hit = None
        with self.assertRaisesRegex(InputRefused, "No window is at"):
            self.session().tap(30, 40)
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_dry_run_checks_everything_and_sends_nothing_and_the_stop_comes_first(self):
        dry = self.session(dry_run=True)
        dry.tap(30, 40)
        self.assertEqual([(i.action, i.x, i.y) for i in dry.intended], [("tap", 30, 40)])
        self.stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            self.session().tap(30, 40)
        self.assertEqual(desktop_sends(self.desk), [])

    def test_only_full_windows_is_accepted(self):
        window = WindowVerifier(self.desk, self.stop).verify(100)
        with self.assertRaises(TargetNotVerified):
            DesktopInputSession(window, self.desk, self.stop, max_actions=1)

    def test_when_it_cannot_be_told_whose_window_it_is_nothing_is_sent(self):
        """No window with the focus (it is changing hands), or a window whose owner cannot be read (it just
        closed): it could be about to be Olive's, so the key or click is refused rather than let through."""
        self.desk.foreground = None
        with self.assertRaises(InputRefused):
            self.session().key_press(Key.ENTER)
        self.desk.foreground = 555                                    # no such window any more
        with self.assertRaises(InputRefused):
            self.session().key_press(Key.ENTER)
        self.desk.foreground, self.desk.hit = 100, 555
        with self.assertRaises(InputRefused):
            self.session().tap(30, 40)
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_key_it_holds_is_let_go_even_while_olive_has_the_focus(self):
        """A release presses nothing (and Olive reacts to no key release), so refusing it would only leave the key
        held for the whole desktop; every key PRESS into Olive stays refused."""
        session = self.session()
        session.key_down(Key.SHIFT)
        self.desk.foreground = OLIVE
        with self.assertRaises(InputRefused):
            session.key_down(Key.CTRL)
        session.key_up(Key.SHIFT)
        self.assertEqual(desktop_sends(self.desk), [("desktop_key", KeyEvent.DOWN, Key.SHIFT),
                                                    ("desktop_key", KeyEvent.UP, Key.SHIFT)])
        self.assertEqual(session.held_keys, frozenset())

    def test_monitors_rearranged_inside_the_same_box_or_a_point_between_them_sends_nothing(self):
        self.desk.monitors = lambda: TWO_MONITORS
        session = DesktopInputSession(target(monitors=TWO_MONITORS), self.desk, self.stop, max_actions=50,
                                      own_pid=OWN_PID)
        session.tap(100, 30)                                          # on the right monitor
        with self.assertRaises(InputRefused):
            session.tap(100, 70)                                      # in the box, on no monitor
        self.desk.monitors = lambda: ((-80, 0, 80, 90), (0, 30, 80, 60))   # the right one moved down
        with self.assertRaises(TargetNotVerified):
            session.tap(100, 40)
        self.assertEqual([e[2:] for e in desktop_sends(self.desk)], [(20, 30), (20, 30)])

    def test_olives_own_console_counts_as_olive(self):
        """Started from a console, Olive's console window belongs to the console host, not to Olive -- but Ctrl+C
        or a click there reaches Olive."""
        self.desk.windows[103] = info(hwnd=103, title="python", name="conhost.exe", cls="ConsoleWindowClass", pid=50)
        self.desk.console_windows = lambda: (103,)
        self.desk.hit = 103
        with self.assertRaises(InputRefused):
            self.session().tap(30, 40)
        self.desk.hit, self.desk.foreground = 100, 103
        with self.assertRaises(InputRefused):
            self.session().key_press(Key.C)
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_watch_that_stopped_seeing_the_runs_own_input_stops_the_run(self):
        """Every event the run sends carries its mark and the watch must see it; when it has not within a second,
        the hooks are gone (Windows removes a slow hook without a word) and Esc could no longer stop the run."""
        clock, observer = Clock(), FakeObserver()
        guard = DesktopRunGuard(observer, self.stop, clock=clock)
        session = DesktopInputSession(target(), self.desk, self.stop, max_actions=50, own_pid=OWN_PID, watch=guard)
        with guard:
            echo_own_input(self.desk, observer)
            session.tap(30, 40)
            clock.sleep(5)
            session.key_press(Key.ENTER)                              # seen: a long pause is not a lost watch
            clock.sleep(5)
            session.tap(30, 40)                                       # its own key was seen too
            self.desk.on_event = None                                 # the hooks go blind
            session.tap(30, 40)
            clock.sleep(0.5)
            session.tap(30, 40)                                       # half a second: still within the grace
            clock.sleep(1.0)
            with self.assertRaises(EmergencyStopped):
                session.tap(30, 40)
        self.assertTrue(self.stop.is_set())
        self.assertEqual(len(desktop_sends(self.desk)), 10)


# ---- the person taking over -----------------------------------------------------------------------------------
class Guard(unittest.TestCase):
    def setUp(self):
        self.observer, self.stop = FakeObserver(), EmergencyStop()
        self.guard = DesktopRunGuard(self.observer, self.stop)

    def move(self, x, y, *, own=False, kind=M.MOVE):
        self.observer.on_mouse(MouseObservation(kind, x, y, 0, 100, None, None, True, own))

    def test_esc_a_click_or_the_mouse_moved_by_hand_stop_the_run_and_its_own_input_never_does(self):
        for act in (lambda: self.observer.on_key(KeyObservation(True, VK_ESCAPE, 0, 100)),
                    lambda: self.move(143, 82, kind=M.PRIMARY_DOWN),            # a click right where it is
                    lambda: (self.move(10, 10), self.move(10 + TAKEOVER_DISTANCE + 1, 10))):
            with self.subTest(act=act):
                self.stop.reset()
                with self.guard:
                    self.assertTrue(self.observer.started)
                    self.observer.on_key(KeyObservation(True, VK_ESCAPE, 0, 100, own=True))   # its own Esc
                    self.move(50, 50, own=True)                                              # its own moves
                    self.move(140, 80, own=True, kind=M.PRIMARY_DOWN)
                    self.observer.on_key(KeyObservation(True, VK_A, 0, 100))               # another key: not Esc
                    self.move(140 + 3, 80 + 2)                                              # a bump of the desk
                    self.assertFalse(self.stop.is_set())
                    act()
                    self.assertTrue(self.stop.is_set())
                self.assertFalse(self.observer.started)                                      # the watch ended too

    def test_a_watch_that_cannot_start_refuses_the_run(self):
        self.observer.fail = RuntimeError("hooks refused")
        with self.assertRaises(PlaybackRefused) as caught:
            self.guard.start()
        self.assertEqual([i.code for i in caught.exception.issues], ["takeover_watch_unavailable"])

    def test_the_person_moving_between_the_runs_own_moves_still_stops_it(self):
        """A recorded macro moves the pointer every few milliseconds, and each of its moves puts the pointer
        somewhere new: the person's own movement adds up across them, so taking over needs no superhuman speed."""
        with self.guard:
            for step in range(1, 6):
                self.move(10 * step, 10, own=True)                    # the run moves the pointer ...
                self.move(10 * step + 10, 10)                         # ... and the person moves it 10 px further
                if step < 3:
                    self.assertFalse(self.stop.is_set(), step)
            self.assertTrue(self.stop.is_set())                       # by the third: 30 px of the person's own

    def test_small_movements_far_apart_are_forgotten(self):
        clock = Clock()
        guard = DesktopRunGuard(self.observer, self.stop, clock=clock)
        with guard:
            self.move(10, 10)
            self.move(30, 10)                                         # 20 px: a bump
            clock.sleep(1.5)
            self.move(50, 10)                                         # another, much later: forgotten in between
            self.assertFalse(self.stop.is_set())
            clock.sleep(0.2)
            self.move(60, 10)                                         # 30 px within a second: taken over
            self.assertTrue(self.stop.is_set())


# ---- playback: only desktop macros, in their own layout, watched -----------------------------------------------
class Playback(unittest.TestCase):
    def setUp(self):
        self.desk, self.stop, self.observer = world(), EmergencyStop(), FakeObserver()

    def engine(self, macro, *, dry_run=False, layout=LAYOUT, observer=True):
        return create_desktop_playback(macro, target(layout), self.desk, self.stop,
                                       observer=self.observer if observer else None, dry_run=dry_run,
                                       own_pid=OWN_PID, sleep=lambda s: None)

    def test_a_desktop_macro_plays_at_its_screen_points_while_watched(self):
        engine = self.engine(clicks((10, 20), (150, 80)))
        engine.start_in_background()
        self.assertTrue(self.observer.started)                         # watched from before the first event
        self.assertTrue(engine.wait(5))
        self.assertEqual(engine.report().state, PlaybackState.COMPLETED)
        self.assertEqual([e[2:] for e in desktop_sends(self.desk) if e[1] is MouseEvent.LEFT_DOWN],
                         [(-80 + 10, 20), (-80 + 150, 80)])
        self.assertFalse(self.observer.started)                        # and the watch ended with the run

    def test_a_dry_run_sends_nothing_and_needs_no_watch(self):
        engine = self.engine(clicks((10, 20)), dry_run=True, observer=False)
        engine.run()
        self.assertEqual(desktop_sends(self.desk), [])
        self.assertEqual(self.observer.starts, 0)

    def test_window_macros_another_layout_or_no_watch_are_refused_before_anything_is_sent(self):
        window_macro = Macro(Recording("windows", "win-0123456789abcdef", clicks((10, 20)).recording.events), "W")
        cases = {"recorded_in_a_window": self.engine(window_macro),
                 "screen_layout_changed": self.engine(clicks((10, 20), layout=(0, 0, 160, 90)), layout=LAYOUT),
                 "takeover_watch_unavailable": self.engine(clicks((10, 20)), observer=False)}
        for code, engine in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, engine.preflight().codes())
                with self.assertRaises(PlaybackRefused):
                    engine.start_in_background()
        self.assertEqual(desktop_sends(self.desk), [])
        self.assertEqual(self.observer.starts, 0)

    def test_a_desktop_macro_is_never_played_in_a_window(self):
        window = WindowVerifier(self.desk, self.stop).verify(100)
        engine = create_window_playback(clicks((10, 20)), window, WindowVerifier(self.desk, self.stop), self.desk,
                                        self.stop, allow_different_target=True)
        self.assertIn("recorded_on_full_windows", engine.preflight().codes())
        with self.assertRaises(PlaybackRefused):
            engine.start_in_background()
        self.assertEqual(self.desk.events, [])

    def test_esc_during_the_run_stops_it_before_the_next_click(self):
        engine = self.engine(clicks((10, 20), (30, 40), (50, 60)))

        def person_presses_esc(desk):
            if len(desktop_sends(desk)) == 2:                          # right after the first click
                self.observer.on_key(KeyObservation(True, VK_ESCAPE, 0, 100))
        self.desk.on_event = person_presses_esc
        engine.start_in_background()
        self.assertTrue(engine.wait(5))
        self.assertTrue(self.stop.is_set())
        self.assertEqual(len(desktop_sends(self.desk)), 2)             # the first click only
        self.assertNotEqual(engine.report().state, PlaybackState.COMPLETED)

    def test_a_run_stopped_mid_drag_lets_go_of_what_it_held_and_sends_nothing_else(self):
        """Esc while the run holds Shift and drags (the person's decision of 2026-10-20): the run stops at once and
        lets go of exactly those two -- where they are -- and nothing of the macro follows."""
        events = (E(K.KEY_DOWN, 0, key=Key.SHIFT), E(K.BUTTON_DOWN, 10, 10, 20, B.PRIMARY, 1),
                  E(K.POINTER_MOVE, 10, 30, 40), E(K.POINTER_MOVE, 10, 50, 60),
                  E(K.BUTTON_UP, 10, 50, 60, B.PRIMARY, 1), E(K.KEY_UP, 10, key=Key.SHIFT))
        engine = self.engine(Macro(Recording("windows", desktop_id(LAYOUT), events), "Drag"))

        def person_presses_esc(desk):
            if desk.events[-1][:2] == ("desktop_mouse", MouseEvent.MOVE_LEFT_HELD):
                self.observer.on_key(KeyObservation(True, VK_ESCAPE, 0, 100))
        self.desk.on_event = person_presses_esc
        engine.start_in_background()
        self.assertTrue(engine.wait(5))
        report = engine.report()
        self.assertEqual((report.state, report.cancel_reason), (PlaybackState.CANCELLED, "emergency_stop"))
        self.assertEqual([e[:2] for e in self.desk.events],
                         [("desktop_key", KeyEvent.DOWN), ("desktop_mouse", MouseEvent.LEFT_DOWN),
                          ("desktop_mouse", MouseEvent.MOVE_LEFT_HELD), ("release_primary",),
                          ("release_key", Key.SHIFT)])
        self.assertEqual((report.released_at_end, report.held_at_end), (("primary_button", "key:shift"), ()))

    def test_a_watch_that_cannot_start_means_no_run(self):
        self.observer.fail = RuntimeError("hooks refused")
        engine = self.engine(clicks((10, 20)))
        with self.assertRaises(PlaybackRefused):
            engine.start_in_background()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_run_whose_thread_cannot_start_leaves_no_watch_behind(self):
        """Otherwise the hooks would stay installed, and every later click of the person's would trigger the
        emergency stop."""
        import threading
        from unittest import mock

        class NoThread(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")
        engine = self.engine(clicks((10, 20)))
        with mock.patch("app.playback.engine.threading.Thread", NoThread):
            with self.assertRaises(RuntimeError):
                engine.start_in_background()
        self.assertEqual(self.observer.starts, 1)
        self.assertFalse(self.observer.started)
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_run_whose_watch_went_blind_stops_before_the_next_event(self):
        """Two clicks 1.5 s apart. When the watch saw the first, the second follows; when it did not, the run is
        stopped before the second -- the playback's own session reports every send to the run's watch."""
        events = (E(K.BUTTON_DOWN, 0, 10, 20, B.PRIMARY, 1), E(K.BUTTON_UP, 40, 10, 20, B.PRIMARY, 1),
                  E(K.BUTTON_DOWN, 1500, 30, 40, B.PRIMARY, 1), E(K.BUTTON_UP, 40, 30, 40, B.PRIMARY, 1))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events), "Slow")
        for seen in (True, False):
            with self.subTest(seen=seen):
                self.desk.events.clear()
                self.stop.reset()
                self.desk.on_event = None
                if seen:
                    echo_own_input(self.desk, self.observer)
                clock = Clock()
                engine = create_desktop_playback(macro, target(), self.desk, self.stop, observer=self.observer,
                                                 own_pid=OWN_PID, clock=clock, sleep=clock.sleep)
                engine.run()
                downs = [e for e in desktop_sends(self.desk) if e[1] is MouseEvent.LEFT_DOWN]
                self.assertEqual(len(downs), 2 if seen else 1)
                self.assertEqual(self.stop.is_set(), not seen)


# ---- recording the whole desktop --------------------------------------------------------------------------------
class Recorder(unittest.TestCase):
    def setUp(self):
        self.desk, self.stop, self.observer = world(), EmergencyStop(), FakeObserver()
        self.recorder = DesktopRecorder(target(), self.desk, self.observer, self.stop, own_pid=OWN_PID,
                                        ignore_injected=False)

    def mouse(self, kind, x, y, t, *, hit=100, own=False):
        """(x, y) are pixels of the Full Windows picture; the hooks report screen points."""
        self.observer.on_mouse(MouseObservation(kind, LAYOUT[0] + x, LAYOUT[1] + y, t * MS, hit, None, None,
                                                False, own))

    def test_it_records_anywhere_in_picture_pixels_except_on_olive_and_its_own_input(self):
        self.recorder.start()
        self.assertIsNone(self.observer.hwnd)                          # the whole desktop, no window
        self.mouse(M.PRIMARY_DOWN, 10, 20, 0)
        self.mouse(M.PRIMARY_UP, 10, 20, 40)
        self.mouse(M.PRIMARY_DOWN, 150, 80, 100, hit=OLIVE)             # a click on Olive (its Stop button, say)
        self.mouse(M.PRIMARY_UP, 150, 80, 140, hit=OLIVE)
        self.mouse(M.PRIMARY_DOWN, 30, 30, 200, own=True)               # input Olive sent itself
        self.observer.on_key(KeyObservation(True, VK_A, 300 * MS, 100))
        self.observer.on_key(KeyObservation(False, VK_A, 330 * MS, 100))
        self.observer.on_key(KeyObservation(True, VK_A, 400 * MS, OLIVE))   # typed into Olive itself
        recording = self.recorder.stop()
        self.assertTrue(is_desktop_id(recording.target_id))
        self.assertEqual(recording.target_id, desktop_id(LAYOUT))
        self.assertEqual([(e.kind, e.x, e.y) for e in recording.events if e.kind in (K.BUTTON_DOWN, K.BUTTON_UP)],
                         [(K.BUTTON_DOWN, 10, 20), (K.BUTTON_UP, 10, 20)])
        self.assertEqual([e.kind for e in recording.events if e.key is Key.A], [K.KEY_DOWN, K.KEY_UP])

    def test_a_recording_of_the_whole_desktop_keeps_no_size_its_layout_is_where_it_belongs(self):
        """It is never scaled (another layout is refused), so "Adapt to the size" is not offered for it."""
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, 10, 20, 0)
        self.mouse(M.PRIMARY_UP, 10, 20, 40)
        self.assertIsNone(self.recorder.stop().size)

    def test_a_changed_screen_layout_ends_the_recording(self):
        self.recorder.start()
        self.desk.desktop = (0, 0, 200, 90)
        self.mouse(M.PRIMARY_DOWN, 10, 20, 0)
        self.assertEqual(self.recorder.end_reason, "layout_changed")
        self.assertIsNone(self.recorder.recording)

    def test_monitors_rearranged_inside_the_same_box_end_the_recording(self):
        self.recorder.start()
        self.desk.monitors = lambda: TWO_MONITORS                     # the box is the same; the monitors are not
        self.mouse(M.PRIMARY_DOWN, 10, 20, 0)
        self.assertEqual(self.recorder.end_reason, "layout_changed")

    def test_a_handle_reused_by_olive_and_olives_console_are_never_recorded(self):
        """Whose a window is, is asked again for every window that was not ours: a handle can be reused by a new
        window -- an Olive dialog, say -- during a recording."""
        self.desk.windows[200] = info(hwnd=200, title="Old", name="old.exe", cls="OldWnd", pid=10)
        self.desk.windows[103] = info(hwnd=103, title="python", name="conhost.exe", cls="ConsoleWindowClass", pid=50)
        self.desk.console_windows = lambda: (103,)
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, 10, 20, 0, hit=200)
        self.mouse(M.PRIMARY_UP, 10, 20, 40, hit=200)
        self.desk.windows[200] = info(hwnd=200, title="Olive dialog", name="python.exe", cls="TkTopLevel", pid=OWN_PID)
        self.mouse(M.PRIMARY_DOWN, 50, 60, 100, hit=200)
        self.mouse(M.PRIMARY_UP, 50, 60, 140, hit=200)
        self.mouse(M.PRIMARY_DOWN, 70, 70, 200, hit=103)              # Olive's console
        self.mouse(M.PRIMARY_UP, 70, 70, 240, hit=103)
        recording = self.recorder.stop()
        self.assertEqual([(e.kind, e.x, e.y) for e in recording.events if e.kind in (K.BUTTON_DOWN, K.BUTTON_UP)],
                         [(K.BUTTON_DOWN, 10, 20), (K.BUTTON_UP, 10, 20)])


# ---- the whole application: choose Full Windows, record, run ----------------------------------------------------
class EndToEnd(gui_tests.Base):
    def setUp(self):
        super().setUp()
        self.world.desktop = LAYOUT
        echo_own_input(self.world, self.observer)

    def recorded_on_full_windows(self):
        self.pick("Full Windows")
        self.assertTrue(self.c.can_record())
        self.assertTrue(self.c.start_recording(), self.c.status)
        for kind, t in ((M.PRIMARY_DOWN, 0), (M.PRIMARY_UP, 40)):
            self.observer.on_mouse(MouseObservation(kind, LAYOUT[0] + 30, LAYOUT[1] + 40, t * MS, 100, None, None))
        self.assertTrue(self.c.stop_recording(), self.c.status)
        self.assertTrue(self.c.save("Desktop click"), self.c.status)
        self.c.refresh_macros()
        key = next(i.key for i in self.c.macros if i.name == "Desktop click")
        self.c.select_macro(key)
        self.c.set_run_refs([self.entry("Full Windows").ref])
        return key

    def run_to_end(self):
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.c.poll()
        self.assertFalse(self.c.run_active)

    def test_a_macro_recorded_on_full_windows_runs_there_at_the_same_screen_point(self):
        self.recorded_on_full_windows()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        self.assertIn(("desktop_mouse", MouseEvent.LEFT_DOWN, LAYOUT[0] + 30, LAYOUT[1] + 40), self.world.events)

    def test_after_the_screen_layout_changed_nothing_runs_and_full_windows_must_be_chosen_again(self):
        self.recorded_on_full_windows()
        self.world.desktop = (0, 0, 200, 90)                          # a monitor changed
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.target_status, TargetStatus.STALE)   # the chosen Full Windows: another layout now
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.REFUSED])
        self.assertIn("screen_layout_changed", self.c.run_results[0].detail)
        self.assertFalse([e for e in self.world.events if e[0] == "desktop_mouse"])

    def test_monitors_rearranged_since_recording_refuse_the_run(self):
        self.recorded_on_full_windows()                               # one monitor, the whole box
        self.world.monitors = lambda: TWO_MONITORS                    # same box, two monitors now
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.target_status, TargetStatus.STALE)
        self.pick("Full Windows")                                     # chosen again, in the new arrangement
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.REFUSED])
        self.assertIn("screen_layout_changed", self.c.run_results[0].detail)
        self.assertFalse([e for e in self.world.events if e[0] == "desktop_mouse"])

    def test_a_script_on_full_windows_is_watched_for_its_whole_run(self):
        from app.decide import RunMacro
        from app.decide.format import ConditionalMacro
        macro = clicks((30, 40))
        key = self.library.key_of(self.library.save(ConditionalMacro("Script", (RunMacro(macro), RunMacro(macro)))))
        self.c.refresh_macros()
        self.c.select_macro(key)
        self.c.refresh()
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        # one watch around the whole script, and one around each recorded macro it plays
        self.assertEqual(self.observer.starts, 3)
        self.assertEqual(len([e for e in self.world.events if e[1] is MouseEvent.LEFT_DOWN]), 2)


class Layout(unittest.TestCase):
    def test_the_id_names_each_monitor_only_when_there_are_several(self):
        self.assertEqual(desktop_id(LAYOUT), "win-full-desktop@-80,0,160x90")
        self.assertEqual(desktop_id(LAYOUT, (LAYOUT,)), desktop_id(LAYOUT))
        self.assertEqual(desktop_id(LAYOUT, TWO_MONITORS[::-1]),
                         "win-full-desktop@-80,0,160x90;-80,0,80x90;0,0,80x60")
        self.assertEqual(target(monitors=TWO_MONITORS).target_id, desktop_id(LAYOUT, TWO_MONITORS))
        self.assertTrue(is_desktop_id(desktop_id(LAYOUT, TWO_MONITORS)))

    def test_a_picture_from_another_arrangement_of_the_monitors_is_refused(self):
        from app.windows.backend import WindowCaptureError
        from app.windows.provider import DesktopScreenshotProvider
        desk, stop = world(), EmergencyStop()
        desk.monitors = lambda: TWO_MONITORS
        DesktopScreenshotProvider(target(monitors=TWO_MONITORS), desk, stop).capture()
        desk.monitors = lambda: ((-80, 0, 80, 90), (0, 30, 80, 60))
        with self.assertRaises(WindowCaptureError):
            DesktopScreenshotProvider(target(monitors=TWO_MONITORS), desk, stop).capture()


@unittest.skipUnless(__import__("sys").platform == "win32", "the real Win32 backend's own code")
class OwnInputMark(unittest.TestCase):
    """Every event this application sends carries its mark, and the hooks recognise it: that is how a person's own
    input -- physical, or over remote control, where Windows flags it as injected too -- is told apart from the
    run's. `_SendInput` is replaced, so nothing is actually sent."""

    def test_every_sent_event_is_marked_and_only_the_mark_counts_as_own(self):
        from app.windows import win32
        backend = win32.Win32InputBackend()
        extras = []

        def fake_send_input(count, inputs, size):
            for i in range(count):
                extras.append(inputs[i].mi.dwExtraInfo if inputs[i].type == 0 else inputs[i].ki.dwExtraInfo)
            return count
        backend._SendInput = fake_send_input
        backend.desktop_mouse(MouseEvent.LEFT_DOWN, 5, 5)
        backend.desktop_key(KeyEvent.DOWN, Key.A)
        backend.desktop_char("x")
        self.assertEqual(len(extras), 2 + 1 + 2)                     # move + press, one key, char down + up
        self.assertTrue(all(win32._own_input(extra) for extra in extras), extras)
        self.assertFalse(win32._own_input(0))                          # a person's input carries nothing of ours
        self.assertFalse(win32._own_input(win32._OWN_INPUT + 1))

    def test_a_release_presses_nothing_moves_nothing_and_is_marked_as_ours(self):
        """Letting go at the end of a live run (the person's decision of 2026-10-20): one button-up where the pointer
        is -- no move, no coordinates -- and one key-up, neither changing the focus, each carrying the mark."""
        from app.windows import win32
        backend = win32.Win32InputBackend()
        sent = []

        def fake_send_input(count, inputs, size):
            for i in range(count):
                if inputs[i].type == 0:
                    mi = inputs[i].mi
                    sent.append(("mouse", mi.dwFlags, mi.dx, mi.dy, mi.dwExtraInfo))
                else:
                    ki = inputs[i].ki
                    sent.append(("key", ki.dwFlags, ki.wVk, ki.dwExtraInfo))
            return count
        backend._SendInput = fake_send_input
        backend._SetForegroundWindow = lambda hwnd: self.fail("a release must not change the focus")
        backend.release_primary()
        backend.release_key(Key.SHIFT)
        mouse, key = sent
        self.assertEqual(mouse[:4], ("mouse", win32._MOUSEEVENTF_LEFTUP, 0, 0))     # no MOVE, no ABSOLUTE
        self.assertEqual((key[0], key[1] & 0x0002), ("key", 0x0002))                  # KEYEVENTF_KEYUP
        self.assertTrue(all(win32._own_input(event[-1]) for event in sent), sent)

    def test_monitors_and_the_console_are_read_without_touching_anything(self):
        """Geometry and window handles only: no picture, no input."""
        from app.windows import win32
        backend = win32.Win32Backend()
        left, top, width, height = backend.virtual_screen()
        monitors = backend.monitors()
        self.assertTrue(monitors)
        for ml, mt, mw, mh in monitors:
            self.assertTrue(mw > 0 and mh > 0)
            self.assertTrue(left <= ml and top <= mt and ml + mw <= left + width and mt + mh <= top + height)
        self.assertTrue(all(isinstance(h, int) and h > 0 for h in backend.console_windows()))
