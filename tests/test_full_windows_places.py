"""Full Windows recordings, click by click in the window each click was in (the person's list of 2026-10-25, item 12,
part B, their answers of 2026-10-26, and the independent review of 2026-10-26):

  * recording: each press and release keeps the window under it -- its program, class, title, client size, whether
    it was open when the recording started, a number for that window -- and the point inside its client area;
  * playing: a window is found again by program, class AND title (a lone look-alike is not it), and the later clicks
    of the same recorded window follow the window found for it; the click goes to the same place of it -- moved, or
    resized in the same proportions (scaled; a point outside the client area keeps its distance from the edge);
    other proportions are refused before that click;
  * a window open when the recording started must be there before the first click; one that opened during the
    recording must open during the run, and is waited for (bounded) when its click comes -- the click then keeps its
    pause; the run stops before that click if it does not come;
  * before a press the window is brought to the front and given a moment to come up; a press is refused when another
    window is on top at the point; a release never is; a drag follows its press, a drop goes into its window;
  * a dry run raises nothing and waits for nothing; never Olive's own window.

Deterministic: the simulated desktop of tests.test_gui and the fake hooks of tests.test_recorder; nothing real.
"""
import time
import unittest
from types import SimpleNamespace

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackRefused, PlaybackState
from app.playback.macro import Macro
from app.providers.recording import (UNKNOWN_PLACE, Place, PointerButton as B, RecordedEvent as E,
                                     RecordedEventKind as K, Recording)
from app.windows.backend import MouseEvent, MouseObservation, MouseObservationKind as M
from app.windows.places import PLACE_WAIT_S, RAISE_SETTLE_S
from app.windows.playback import create_desktop_playback
from app.windows.recorder import DesktopRecorder
from app.windows.target import desktop_id
from tests import test_gui as gui_tests
from tests.test_full_windows import LAYOUT, OLIVE, Clock, desktop_sends, echo_own_input, target, world
from tests.test_gui import OWN_PID, RunState
from tests.test_recorder import MS, FakeObserver
from tests.test_windows_provider import info

NOTES = 100
LEFT, TOP = LAYOUT[:2]


def with_rects(desk, rects):
    """Each window's client area on the screen: (left, top, width, height)."""
    desk.client_screen_rect = lambda hwnd: rects.get(hwnd)
    return desk


def chain(desk, hook):
    """Run `hook(desk)` after every event, after what already runs then (the watch's echo)."""
    before = desk.on_event

    def both(d):
        if before:
            before(d)
        hook(d)
    desk.on_event = both


# ---- recording ---------------------------------------------------------------------------------------------------
class Recording_(unittest.TestCase):
    def setUp(self):
        self.desk, self.stop, self.observer = world(), EmergencyStop(), FakeObserver()
        self.rects = {NOTES: (-70, 10, 60, 40)}
        with_rects(self.desk, self.rects)
        self.recorder = DesktopRecorder(target(), self.desk, self.observer, self.stop, own_pid=OWN_PID,
                                        ignore_injected=False)

    def mouse(self, kind, sx, sy, t, *, hit=NOTES):
        """(sx, sy): a screen point, as the hooks report it."""
        self.observer.on_mouse(MouseObservation(kind, sx, sy, t * MS, hit, None, None, False, False))

    def test_a_press_and_its_release_keep_the_window_under_them_a_move_only_its_point(self):
        self.recorder.start()
        self.mouse(M.MOVE, -65, 15, 0)
        self.mouse(M.PRIMARY_DOWN, -60, 30, 10)
        self.mouse(M.PRIMARY_UP, -60, 30, 50)
        rec = self.recorder.stop()
        self.assertEqual(rec.places, (Place("notes.exe", "NotesWnd", "Notes", 60, 40, True, 0),))
        down, up = (e for e in rec.events if e.kind in (K.BUTTON_DOWN, K.BUTTON_UP))
        self.assertEqual((down.x, down.y), (-60 - LEFT, 30 - TOP))      # the screen point is kept as it was
        self.assertEqual((down.place, up.place), ((0, 10, 20), (0, 10, 20)))
        self.assertIsNone(next(e for e in rec.events if e.kind is K.POINTER_MOVE).place)

    def test_a_window_opened_during_the_recording_is_marked_so_and_numbered(self):
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.PRIMARY_UP, -60, 30, 40)
        self.desk.windows[300] = info(hwnd=300, title="Save as", name="notes.exe", cls="#32770", pid=10)
        self.rects[300] = (0, 20, 40, 30)
        self.mouse(M.PRIMARY_DOWN, 5, 25, 100, hit=300)
        self.mouse(M.PRIMARY_UP, 5, 25, 140, hit=300)
        rec = self.recorder.stop()
        self.assertEqual(rec.places[1], Place("notes.exe", "#32770", "Save as", 40, 30, False, 1))
        self.assertEqual(rec.events[2].place, (1, 5, 5))

    def test_one_window_whose_title_and_size_changed_keeps_its_number(self):
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.PRIMARY_UP, -60, 30, 40)
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="*Notes", name="notes.exe", cls="NotesWnd", pid=10)
        self.rects[NOTES] = (-80, 0, 120, 80)
        self.mouse(M.PRIMARY_DOWN, -50, 30, 100)
        self.mouse(M.PRIMARY_UP, -50, 30, 140)
        rec = self.recorder.stop()
        self.assertEqual([(p.title, p.width, p.window) for p in rec.places], [("Notes", 60, 0), ("*Notes", 120, 0)])

    def test_a_drag_dropped_over_another_window_keeps_that_window_for_its_release(self):
        self.desk.windows[200] = info(hwnd=200, title="Archive", name="files.exe", cls="FolderWnd", pid=20)
        self.rects[200] = (0, 0, 70, 60)
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.MOVE, 10, 35, 30, hit=200)
        self.mouse(M.PRIMARY_UP, 12, 36, 60, hit=200)
        rec = self.recorder.stop()
        self.assertEqual([p.title for p in rec.places], ["Notes", "Archive"])
        self.assertEqual([e.place for e in rec.events], [(0, 10, 20), None, (1, 12, 36)])

    def test_a_window_that_cannot_be_described_keeps_only_the_screen_point(self):
        for changed in (dict(name=""), dict(dpi_ambiguous=True)):
            self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", cls="NotesWnd", pid=10,
                                            **{"name": "notes.exe", **changed})
            self.observer = FakeObserver()
            recorder = DesktopRecorder(target(), self.desk, self.observer, self.stop, own_pid=OWN_PID,
                                       ignore_injected=False)
            recorder.start()
            self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
            self.mouse(M.PRIMARY_UP, -60, 30, 40)
            rec = recorder.stop()
            self.assertEqual((rec.places, [e.place for e in rec.events]), ((), [(UNKNOWN_PLACE, 0, 0), None]),
                             changed)                                     # the press marked: never its old point

    def test_a_release_over_olives_own_window_keeps_no_window(self):
        self.rects[OLIVE] = (0, 0, 60, 40)                                # a window that could be described
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.PRIMARY_UP, 30, 30, 900, hit=OLIVE)                  # dragged out over Olive
        rec = self.recorder.stop()
        self.assertEqual([p.title for p in rec.places], ["Notes"])
        self.assertIsNone(rec.events[-1].place)

    def test_a_refused_second_start_keeps_the_window_numbers(self):
        from app.providers.recording import RecorderStateError
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.PRIMARY_UP, -60, 30, 40)
        with self.assertRaises(RecorderStateError):
            self.recorder.start()
        self.desk.windows[300] = info(**DIALOG)
        self.rects[300] = (0, 20, 40, 30)
        self.mouse(M.PRIMARY_DOWN, 5, 25, 100, hit=300)
        self.mouse(M.PRIMARY_UP, 5, 25, 140, hit=300)
        self.assertEqual([p.window for p in self.recorder.stop().places], [0, 1])

    def test_a_title_is_kept_cleaned_as_it_is_compared(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="No‎tes", name="notes.exe", cls="NotesWnd", pid=10)
        self.recorder.start()
        self.mouse(M.PRIMARY_DOWN, -60, 30, 0)
        self.mouse(M.PRIMARY_UP, -60, 30, 40)
        self.assertEqual(self.recorder.stop().places[0].title, "No tes")


# ---- playing -----------------------------------------------------------------------------------------------------
NOTES_PLACE = Place("notes.exe", "NotesWnd", "Notes", 60, 40, True, 0)
DIALOG_PLACE = Place("notes.exe", "#32770", "Save as", 40, 30, False, 1)
DIALOG = dict(hwnd=300, title="Save as", name="notes.exe", cls="#32770", pid=10)


def placed_macro(*clicks, places):
    """`clicks`: (place index, x, y) each -- a click recorded in that window at that point of its client area,
    200 ms after the one before."""
    events = []
    for i, (p, x, y) in enumerate(clicks):
        events += [E(K.BUTTON_DOWN, 0 if not i else 200, 1, 1, B.PRIMARY, 1, place=(p, x, y)),
                   E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(p, x, y))]
    return Macro(Recording("windows", desktop_id(LAYOUT), tuple(events), places=tuple(places)), "Placed")


class Playing(unittest.TestCase):
    def setUp(self):
        self.desk, self.stop, self.observer = world(), EmergencyStop(), FakeObserver()
        self.rects = {NOTES: (0, 20, 60, 40)}                             # Notes has moved since the recording
        with_rects(self.desk, self.rects)
        echo_own_input(self.desk, self.observer)
        self.clock = Clock()
        self.downs_at = []
        chain(self.desk, lambda d: self.downs_at.append(self.clock.now) if d.events[-1][1] is MouseEvent.LEFT_DOWN
              else None)

    def engine(self, macro, *, dry_run=False):
        return create_desktop_playback(macro, target(), self.desk, self.stop, observer=self.observer,
                                       own_pid=OWN_PID, dry_run=dry_run, clock=self.clock, sleep=self.clock.sleep)

    def downs(self):
        return [e[2:] for e in desktop_sends(self.desk) if e[1] is MouseEvent.LEFT_DOWN]

    def after_sleeps(self, count, change):
        """`change()` once the playback has slept `count` times since now (a window that takes a while to come)."""
        real, slept = self.clock.sleep, [0]

        def sleep(seconds):
            real(seconds)
            slept[0] += 1
            if slept[0] == count:
                change()
        self.clock.sleep = sleep
        return sleep

    # -- where a click goes
    def test_a_click_goes_to_the_same_place_of_its_window_where_the_window_is_now(self):
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40)])                        # (0 + 10, 20 + 20) on the screen
        self.assertIn((NOTES, False), self.desk.fronted)                 # brought to the front, focus not taken

    def test_a_window_resized_in_the_same_proportions_is_clicked_at_the_scaled_point(self):
        self.rects[NOTES] = (0, 0, 120, 80)
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(20, 40)])

    def test_a_point_outside_the_client_area_keeps_its_distance_from_the_edge(self):
        """The independent review: a title bar or a border does not grow with the window."""
        self.rects[NOTES] = (-60, 20, 120, 80)
        report = self.engine(placed_macro((0, 10, -8), (0, 65, 5), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(-40, 12), (65, 30)])           # x scaled, y kept; x from the right edge

    def test_other_proportions_are_refused_before_anything_is_sent(self):
        self.rects[NOTES] = (0, 0, 60, 80)
        engine = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]))
        self.assertIn("place_size", [i.code for i in engine.preflight().errors])
        with self.assertRaises(PlaybackRefused):
            engine.run()
        self.assertEqual(desktop_sends(self.desk), [])

    # -- which window
    def test_a_window_open_at_the_start_that_is_not_there_refuses_the_run_and_is_named(self):
        del self.desk.windows[NOTES]
        engine = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]))
        issue = next(i for i in engine.preflight().errors if i.code == "place_missing")
        self.assertEqual(dict(issue.params), {"program": "notes.exe", "title": "Notes"})
        self.assertTrue(issue.deferrable)
        with self.assertRaises(PlaybackRefused):
            engine.run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_lone_look_alike_with_another_title_is_not_the_window(self):
        """The review's blocker: another folder, another hosted app shares program and class."""
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Documents", name="notes.exe", cls="NotesWnd", pid=10)
        with self.assertRaises(PlaybackRefused):
            self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_alike_windows_are_told_apart_by_the_title_or_nothing_is_guessed(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Other notes", name="notes.exe", cls="NotesWnd", pid=10)
        self.desk.windows[101] = info(hwnd=101, title="Notes", name="notes.exe", cls="NotesWnd", pid=11)
        self.rects[101] = (-60, 0, 60, 40)
        self.desk.hit = 101
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(-50, 20)])
        self.desk.events.clear()
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10)
        with self.assertRaises(PlaybackRefused):                         # two called "Notes"
            self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_two_recorded_windows_are_never_the_same_window(self):
        twin = Place("notes.exe", "NotesWnd", "Notes", 60, 40, True, 1)
        with self.assertRaises(PlaybackRefused):
            self.engine(placed_macro((0, 10, 20), (1, 5, 5), places=[NOTES_PLACE, twin])).run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_the_later_clicks_of_a_window_follow_it_after_its_title_changed(self):
        retitled = Place("notes.exe", "NotesWnd", "*Notes", 60, 40, True, 0)
        report = self.engine(placed_macro((0, 10, 20), (1, 30, 30), places=[NOTES_PLACE, retitled])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40), (30, 50)])

    def test_a_window_whose_scale_cannot_be_established_is_refused(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        dpi_ambiguous=True)
        engine = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]))
        self.assertIn("place_dpi", [i.code for i in engine.preflight().errors])
        with self.assertRaises(PlaybackRefused):
            engine.run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_title_is_compared_as_the_recorder_kept_it(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="No‎tes", name="notes.exe", cls="NotesWnd", pid=10)
        kept = Place("notes.exe", "NotesWnd", "No tes", 60, 40, True, 0)
        report = self.engine(placed_macro((0, 10, 20), places=[kept])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)

    def test_only_the_window_found_for_a_place_is_ever_raised(self):
        from app.windows.places import PlaceFinder, PlaceRefused
        finder = PlaceFinder(self.desk, OWN_PID)
        self.rects[OLIVE] = (0, 0, 60, 40)
        for hwnd in (OLIVE, 999):                                         # Olive's own; a window that is gone
            with self.assertRaises(PlaceRefused):
                finder.bring_to_front(NOTES_PLACE, hwnd)
        self.assertEqual(self.desk.fronted, [])
        self.assertFalse(finder.bring_to_front(NOTES_PLACE, NOTES))
        self.assertEqual(self.desk.fronted, [(NOTES, False)])

    def test_olives_own_window_is_never_a_place(self):
        olive = Place("python.exe", "TkTopLevel", "Olive Automation", 60, 40, True, 0)
        self.rects[OLIVE] = (0, 0, 60, 40)
        with self.assertRaises(PlaybackRefused):
            self.engine(placed_macro((0, 10, 20), places=[olive])).run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_recording_in_which_the_window_changed_size_runs(self):
        """The review: one window at two sizes (maximised by the first click) -- only its first place is checked
        before the run; the second at its own click."""
        maximised = Place("notes.exe", "NotesWnd", "Notes", 120, 80, True, 0)

        def maximise(d):
            if d.events[-1][1] is MouseEvent.LEFT_UP:
                self.rects[NOTES] = (-60, 0, 120, 80)
        chain(self.desk, maximise)
        report = self.engine(placed_macro((0, 10, 20), (1, 100, 60), places=[NOTES_PLACE, maximised])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40), (40, 60)])

    # -- waiting for a window
    def test_a_window_an_earlier_click_opens_is_waited_for_and_its_click_keeps_its_pause(self):
        appeared = []

        def opens():
            self.desk.windows[300] = info(**DIALOG)
            self.rects[300] = (30, 30, 40, 30)
            appeared.append(self.clock.now)
        def under(x, y):                                                  # the dialog where it is, Notes elsewhere
            return 300 if 300 in self.desk.windows and 30 <= x < 70 and 30 <= y < 60 else NOTES
        self.desk.window_at = under
        macro = placed_macro((0, 10, 20), (1, 5, 6), (0, 10, 20), places=[NOTES_PLACE, DIALOG_PLACE])
        engine = self.engine(macro)
        self.assertTrue(engine.preflight().ok, engine.preflight().errors)   # not there yet, and that is fine
        sleep = self.after_sleeps(8, opens)                             # well after its recorded 200 ms
        engine = create_desktop_playback(macro, target(), self.desk, self.stop, observer=self.observer,
                                         own_pid=OWN_PID, clock=self.clock, sleep=sleep)
        report = engine.run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs()[:2], [(10, 40), (35, 36)])
        self.assertGreaterEqual(self.downs_at[1] - appeared[0], 0.2)   # the recorded pause, after it came
        # and what follows keeps its own pause after that click (the recorded 40 ms release + 200 ms)
        self.assertGreaterEqual(self.downs_at[2] - self.downs_at[1], 0.2)

    def test_an_old_window_open_when_the_run_began_is_not_taken_for_a_new_one(self):
        """The review's second scenario: a dialog of the same name already open is not the one the click opens."""
        self.desk.windows[300] = info(**DIALOG)
        self.rects[300] = (30, 30, 40, 30)
        report = self.engine(placed_macro((0, 10, 20), (1, 5, 6), places=[NOTES_PLACE, DIALOG_PLACE])).run()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_missing"))
        self.assertEqual(len(self.downs()), 1)

    def test_a_second_new_window_is_never_the_first_one_again(self):
        """Two dialogs of the same name were opened during the recording; the run opens only one: the clicks of the
        second wait for a window of their own, and do not go into the first."""
        def opens(d):
            if d.events[-1][1] is MouseEvent.LEFT_UP and 300 not in d.windows:
                d.windows[300] = info(**DIALOG)
                self.rects[300] = (30, 30, 40, 30)
                d.hit = 300
        chain(self.desk, opens)
        second = Place("notes.exe", "#32770", "Save as", 40, 30, False, 2)
        report = self.engine(placed_macro((0, 10, 20), (1, 5, 6), (2, 5, 6),
                                          places=[NOTES_PLACE, DIALOG_PLACE, second])).run()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_missing"))
        self.assertEqual(self.downs(), [(10, 40), (35, 36)])

    def test_a_window_that_never_opens_stops_the_run_before_its_click(self):
        macro = placed_macro((0, 10, 20), (1, 5, 6), places=[NOTES_PLACE, DIALOG_PLACE])
        before = self.clock.now
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.FAILED)
        self.assertEqual(report.failure.code, "place_missing")
        self.assertEqual(dict(report.failure.params), {"program": "notes.exe", "title": "Save as"})
        self.assertEqual(len(self.downs()), 1)                            # the first click only
        self.assertGreaterEqual(self.clock.now - before, PLACE_WAIT_S)
        self.assertLess(self.clock.now - before, PLACE_WAIT_S + 2)       # waited, but bounded

    def test_the_stop_ends_the_wait_for_a_window_at_once(self):
        macro = placed_macro((0, 10, 20), (1, 5, 6), places=[NOTES_PLACE, DIALOG_PLACE])
        stopped_at = []

        def stop():
            self.stop.trigger("test")
            stopped_at.append(self.clock.now)
        sleep = self.after_sleeps(10, stop)
        engine = create_desktop_playback(macro, target(), self.desk, self.stop, observer=self.observer,
                                         own_pid=OWN_PID, clock=self.clock, sleep=sleep)
        report = engine.run()
        self.assertEqual((report.state, report.cancel_reason), (PlaybackState.CANCELLED, "emergency_stop"))
        self.assertEqual(len(self.downs()), 1)
        self.assertLess(self.clock.now - stopped_at[0], 0.5)              # heard at once, not after the wait

    def test_a_window_that_does_not_take_clicks_yet_is_waited_for_then_refused(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        enabled=False)
        sleep = self.after_sleeps(3, lambda: self.desk.windows.update({NOTES: info(
            hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10)}))
        engine = create_desktop_playback(placed_macro((0, 10, 20), places=[NOTES_PLACE]), target(), self.desk,
                                         self.stop, observer=self.observer, own_pid=OWN_PID, clock=self.clock,
                                         sleep=sleep)
        self.assertEqual(engine.run().state, PlaybackState.COMPLETED)
        self.desk.events.clear()
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        enabled=False)
        self.clock.sleep = Clock.sleep.__get__(self.clock)
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_disabled"))
        self.assertEqual(desktop_sends(self.desk), [])

    # -- bringing it up, and what is on top
    def test_a_press_is_refused_when_another_window_stays_on_top_at_the_point(self):
        self.desk.windows[200] = info(hwnd=200, title="Popup", name="other.exe", cls="PopWnd", pid=20)
        self.desk.hit, self.desk.covered = 200, True
        before = self.clock.now
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_covered"))
        self.assertEqual(desktop_sends(self.desk), [])
        self.assertLess(self.clock.now - before, RAISE_SETTLE_S + 0.5)   # given its moment, then refused

    def test_a_window_brought_up_from_behind_is_clicked_once_it_is_up(self):
        self.desk.covered = True                                          # raising it takes a moment
        answers = iter([200, 200, NOTES])
        self.desk.window_at = lambda x, y: next(answers, NOTES)
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40)])

    def test_a_minimised_window_is_brought_back_and_clicked(self):
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        minimized=True)
        rect = self.rects.pop(NOTES)

        def restore(hwnd, *, activate):
            self.rects[hwnd] = rect
            return True
        self.desk.raise_window = restore
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40)])

    def test_a_double_clicks_second_press_is_checked_too(self):
        """The first click opened a window over the point: the second press would press on it -- refused."""
        def popup_after_first_click(desk):
            if desk.events[-1][1] is MouseEvent.LEFT_UP and 200 not in desk.windows:
                desk.windows[200] = info(hwnd=200, title="Menu", name="notes.exe", cls="#32768", pid=10)
                desk.hit = 200
        chain(self.desk, popup_after_first_click)
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_DOWN, 40, 1, 1, B.PRIMARY, 2, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 2, place=(0, 10, 20)))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Double")
        self.assertEqual([a.op.value for a in macro.actions], ["double_tap"])
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.FAILED)
        self.assertEqual([e[1] for e in desktop_sends(self.desk)], [MouseEvent.LEFT_DOWN, MouseEvent.LEFT_UP])

    def test_a_release_under_a_window_the_press_opened_is_still_sent(self):
        """The person's decision of 2026-10-26: a release presses nothing. A held press, so the release is its own
        action with a gate of its own."""
        def popup_after_press(desk):
            if desk.events[-1][1] is MouseEvent.LEFT_DOWN:
                desk.windows[200] = info(hwnd=200, title="Menu", name="notes.exe", cls="#32768", pid=10)
                desk.hit = 200
        chain(self.desk, popup_after_press)
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 900, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Hold")
        self.assertEqual([a.op.value for a in macro.actions], ["button_down", "button_up"])
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual([e[1:] for e in desktop_sends(self.desk)],
                         [(MouseEvent.LEFT_DOWN, 10, 40), (MouseEvent.LEFT_UP, 10, 40)])

    # -- drags
    def test_a_window_dragged_by_its_title_bar_moves_by_the_recorded_distance(self):
        """The review: a drag follows its press, so a window moved by its title bar does move."""
        events = (E(K.BUTTON_DOWN, 0, 20, 2, B.PRIMARY, 1, place=(0, 10, -8)),
                  E(K.POINTER_MOVE, 30, 30, 2), E(K.POINTER_MOVE, 30, 40, 2),
                  E(K.BUTTON_UP, 30, 40, 2, B.PRIMARY, 1, place=(0, 10, -8)))     # the window moved along
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Move it")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual([e[2:] for e in desktop_sends(self.desk)], [(10, 12), (20, 12), (30, 12), (30, 12)])

    def test_a_drop_goes_into_the_window_it_was_recorded_over_where_that_window_is_now(self):
        archive = Place("files.exe", "FolderWnd", "Archive", 30, 20, True, 1)
        self.desk.windows[200] = info(hwnd=200, title="Archive", name="files.exe", cls="FolderWnd", pid=20)
        self.rects[200] = (40, 50, 30, 20)                                # it moved since the recording
        self.desk.window_at = lambda x, y: 200 if 40 <= x < 70 and 50 <= y < 70 else NOTES
        events = (E(K.BUTTON_DOWN, 0, 20, 40, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.POINTER_MOVE, 30, 50, 60), E(K.POINTER_MOVE, 30, 60, 70),
                  E(K.BUTTON_UP, 30, 60, 70, B.PRIMARY, 1, place=(1, 5, 5)))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE, archive)), "Drop")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        sent = desktop_sends(self.desk)
        self.assertEqual(sent[0][1:], (MouseEvent.LEFT_DOWN, 10, 40))
        self.assertEqual(sent[-1][1:], (MouseEvent.LEFT_UP, 45, 55))

    def drag_into_archive(self):
        archive = Place("files.exe", "FolderWnd", "Archive", 30, 20, True, 1)
        events = (E(K.BUTTON_DOWN, 0, 20, 40, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.POINTER_MOVE, 30, 50, 60), E(K.BUTTON_UP, 30, 60, 70, B.PRIMARY, 1, place=(1, 5, 5)))
        return Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE, archive)), "Drop")

    def test_a_drag_is_not_pressed_when_its_drop_window_is_covered_at_the_drop_point(self):
        """The person's decision of 2026-10-26: checked before the press -- once pressed, the button would be let
        go wherever the pointer is."""
        self.desk.windows[200] = info(hwnd=200, title="Archive", name="files.exe", cls="FolderWnd", pid=20)
        self.rects[200] = (40, 50, 30, 20)
        report = self.engine(self.drag_into_archive()).run()            # Notes is what the fake shows everywhere
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_drop_covered"))
        self.assertEqual(dict(report.failure.params)["title"], "Archive")
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_drag_whose_drop_window_closed_is_not_pressed(self):
        self.desk.windows[200] = info(hwnd=200, title="Archive", name="files.exe", cls="FolderWnd", pid=20)
        self.rects[200] = (40, 50, 30, 20)
        engine = self.engine(self.drag_into_archive())
        engine._begin()                                                   # it was there when the run began
        del self.desk.windows[200]
        engine._loop()
        report = engine.report()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_missing"))
        self.assertEqual(desktop_sends(self.desk), [])

    # -- what a review found in round 2
    def test_a_press_whose_window_was_not_identified_refuses_the_run(self):
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_DOWN, 200, 30, 30, B.PRIMARY, 1, place=(UNKNOWN_PLACE, 0, 0)),
                  E(K.BUTTON_UP, 40, 30, 30, B.PRIMARY, 1))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Unknown")
        engine = self.engine(macro)
        issue = next(i for i in engine.preflight().errors if i.code == "place_unknown")
        self.assertEqual((dict(issue.params), issue.deferrable), ({"number": 2}, False))
        with self.assertRaises(PlaybackRefused):
            engine.run()
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_click_added_by_hand_goes_to_the_screen_point_picked(self):
        """The person's decision of 2026-10-26."""
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_DOWN, 200, 100, 30, B.PRIMARY, 1), E(K.BUTTON_UP, 40, 100, 30, B.PRIMARY, 1))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "By hand")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(10, 40), (LEFT + 100, TOP + 30)])

    def test_a_minimised_window_coming_back_a_moment_later_is_waited_for(self):
        """The review: Windows restores a window a moment after it is asked to (ShowWindowAsync)."""
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        minimized=True)
        rect = self.rects.pop(NOTES)
        pending = []
        self.desk.raise_window = lambda hwnd, *, activate: pending.append(hwnd) or True
        sleep = self.after_sleeps(2, lambda: self.rects.update({NOTES: rect}))
        engine = create_desktop_playback(placed_macro((0, 10, 20), places=[NOTES_PLACE]), target(), self.desk,
                                         self.stop, observer=self.observer, own_pid=OWN_PID, clock=self.clock,
                                         sleep=sleep)
        report = engine.run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual((pending, self.downs()), ([NOTES], [(10, 40)]))

    def test_a_window_that_never_comes_back_says_the_real_reason(self):
        """At the end of the wait, the last reason the click could not be aimed -- here other proportions."""
        self.desk.windows[NOTES] = info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd", pid=10,
                                        minimized=True)
        self.rects[NOTES] = (0, 0, 60, 80)
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_size"))
        self.assertEqual(desktop_sends(self.desk), [])

    def test_a_window_covered_only_at_the_click_point_is_given_its_moment(self):
        """raise_window says "covered" only for the middle of a window; the point itself is what is checked."""
        answers = iter([200, 200, NOTES])
        self.desk.window_at = lambda x, y: next(answers, NOTES)          # raise_window answers False here
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)

    def test_the_clicks_after_a_wait_for_the_window_keep_their_pauses(self):
        """Two taps 600 ms apart stay 600 ms apart after the first waited for its window (two taps closer than
        Windows' double-click time would be one double click)."""
        answers = iter([200, 200, 200, NOTES])
        self.desk.window_at = lambda x, y: next(answers, NOTES)
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_DOWN, 560, 1, 1, B.PRIMARY, 1, place=(0, 30, 30)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 30, 30)))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Two taps")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertGreaterEqual(self.downs_at[1] - self.downs_at[0], 0.559)

    def test_a_stop_while_a_window_is_looked_for_is_a_stop_not_a_failure(self):
        engine = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]))
        engine._begin()
        listing = self.desk.enumerate

        def gone_and_stopped():                                           # the window closes as Stop is pressed
            self.desk.windows.pop(NOTES, None)
            self.stop.trigger("test")
            return listing()
        self.desk.enumerate = gone_and_stopped
        engine._loop()
        report = engine.report()
        self.assertEqual((report.state, report.cancel_reason), (PlaybackState.CANCELLED, "emergency_stop"))

    def test_a_move_with_no_button_held_follows_the_last_press(self):
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.POINTER_MOVE, 30, 6, 1))                            # 5 to the right of the click
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Hover")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(desktop_sends(self.desk)[-1][1:], (MouseEvent.MOVE, 15, 40))

    def test_a_move_before_the_first_press_follows_that_press_window(self):
        events = (E(K.POINTER_MOVE, 0, 6, 1), E(K.BUTTON_DOWN, 30, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)),
                  E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1, place=(0, 10, 20)))
        macro = Macro(Recording("windows", desktop_id(LAYOUT), events, places=(NOTES_PLACE,)), "Hover first")
        report = self.engine(macro).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(desktop_sends(self.desk)[0][1:], (MouseEvent.MOVE, 15, 40))   # 5 right of where it clicks

    def test_a_window_the_run_itself_opened_is_not_taken_for_one_open_at_the_start(self):
        other = Place("other.exe", "OtherWnd", "Other", 60, 40, True, 1)
        self.desk.windows[200] = info(hwnd=200, title="Other", name="other.exe", cls="OtherWnd", pid=20)
        self.rects[200] = (-80, 0, 60, 40)
        self.desk.window_at = lambda x, y: 200 if x < -20 else NOTES

        def second_notes(d):
            if d.events[-1][1] is MouseEvent.LEFT_UP and 101 not in d.windows:
                d.windows[101] = info(hwnd=101, title="Notes", name="notes.exe", cls="NotesWnd", pid=11)
                self.rects[101] = (40, 50, 60, 40)
        chain(self.desk, second_notes)
        report = self.engine(placed_macro((1, 5, 5), (0, 10, 20), places=[NOTES_PLACE, other])).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs()[1], (10, 40))                        # the Notes open at the start

    def test_a_window_hidden_for_a_moment_is_waited_for_one_that_closed_is_not(self):
        retitled = Place("notes.exe", "NotesWnd", "Notes", 60, 40, True, 0)
        macro = placed_macro((0, 10, 20), (1, 30, 30), places=[NOTES_PLACE, retitled])
        shown, hidden = self.desk.windows[NOTES], info(hwnd=NOTES, title="Notes", name="notes.exe", cls="NotesWnd",
                                                       pid=10, visible=False)
        for closed in (False, True):
            self.desk.windows[NOTES], self.desk.events[:] = shown, []
            back_at = []

            def gone(d, closed=closed):
                if d.events[-1][1] is MouseEvent.LEFT_UP and len(self.downs()) == 1:
                    if closed:
                        del d.windows[NOTES]
                    else:
                        d.windows[NOTES] = hidden
                        back_at.append(self.clock.now + 0.6)              # well after its next click is due
            self.desk.on_event = None
            echo_own_input(self.desk, self.observer)
            chain(self.desk, gone)

            def sleep(seconds):
                Clock.sleep(self.clock, seconds)
                if back_at and self.clock.now >= back_at[0] and not closed:
                    self.desk.windows[NOTES] = shown
            before = self.clock.now
            engine = create_desktop_playback(macro, target(), self.desk, self.stop, observer=self.observer,
                                             own_pid=OWN_PID, clock=self.clock, sleep=sleep)
            report = engine.run()
            if closed:                                                    # closed: refused at once, not waited for
                self.assertEqual((report.state, report.failure.code), (PlaybackState.FAILED, "place_missing"))
                self.assertLess(self.clock.now - before, 1.0)
            else:
                self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
                self.assertEqual(len(self.downs()), 2)

    def test_a_refused_second_start_keeps_what_the_run_found(self):
        engine = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]))
        engine.run()
        bound = dict(engine._bound)
        self.assertTrue(bound)
        with self.assertRaises(Exception):
            engine.run()
        self.assertEqual(engine._bound, bound)

    # -- a dry run
    def test_a_dry_run_raises_nothing_waits_for_nothing_and_sends_nothing(self):
        macro = placed_macro((0, 10, 20), (1, 5, 6), places=[NOTES_PLACE, DIALOG_PLACE])
        before = self.clock.now
        report = self.engine(macro, dry_run=True).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual((desktop_sends(self.desk), self.desk.fronted), ([], []))
        self.assertLess(self.clock.now - before, 1)                     # the dialog was not waited for
        del self.desk.windows[NOTES]
        with self.assertRaises(PlaybackRefused):                          # a window there at the start: checked
            self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]), dry_run=True).run()

    def test_a_dry_run_checks_the_clicks_where_their_windows_are_now(self):
        """The review: the old screen points may be under Olive's own window now -- a dry run aims as a run does."""
        self.desk.window_at = lambda x, y: OLIVE if x < -50 else NOTES     # the recorded point (1, 1) is under Olive
        report = self.engine(placed_macro((0, 10, 20), places=[NOTES_PLACE]), dry_run=True).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.desk.fronted, [])

    def test_a_recording_without_places_plays_at_its_screen_points_as_before(self):
        from tests.test_full_windows import clicks
        report = self.engine(clicks((30, 40))).run()
        self.assertEqual(report.state, PlaybackState.COMPLETED, report.failure)
        self.assertEqual(self.downs(), [(LEFT + 30, TOP + 40)])


# ---- the whole application: record on Full Windows, the window moves, run ----------------------------------------
class InTheApplication(gui_tests.Base):
    def setUp(self):
        super().setUp()
        self.world.desktop = LAYOUT
        echo_own_input(self.world, self.observer)
        self.rects = {NOTES: (-70, 10, 60, 40)}
        with_rects(self.world, self.rects)

    def run_to_end(self):
        deadline = time.time() + 15
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.c.poll()
        self.assertFalse(self.c.run_active)

    def recorded_in_notes(self):
        """A click recorded on Full Windows at the screen point (-60, 30): (10, 20) inside Notes."""
        self.pick("Full Windows")
        self.assertTrue(self.c.start_recording(), self.c.status)
        for kind, t in ((M.PRIMARY_DOWN, 0), (M.PRIMARY_UP, 40)):
            self.observer.on_mouse(MouseObservation(kind, -60, 30, t * MS, NOTES, None, None))
        self.assertTrue(self.c.stop_recording(), self.c.status)
        self.assertTrue(self.c.save("In Notes"), self.c.status)
        self.c.refresh_macros()
        key = next(i.key for i in self.c.macros if i.name == "In Notes")
        self.c.select_macro(key)
        self.assertEqual(self.c.run_refs, (self.entry("Full Windows").ref,))     # remembered (part A)
        return key

    def test_the_click_follows_its_window_after_the_window_moved(self):
        key = self.recorded_in_notes()
        self.assertEqual(self.c.library.load(self.c.macro_by_key(key).path).recording.places,
                         (Place("notes.exe", "NotesWnd", "Notes", 60, 40, True, 0),))   # kept in the file
        self.rects[NOTES] = (0, 20, 60, 40)                               # Notes moved
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        self.assertIn(("desktop_mouse", MouseEvent.LEFT_DOWN, 10, 40), self.world.events)

    def test_a_window_that_is_not_there_refuses_the_run_and_the_history_names_it_in_russian(self):
        from app.gui.view import fmt
        from app.gui.i18n import Translator
        self.recorded_in_notes()
        del self.world.windows[NOTES]
        self.assertTrue(self.c.refresh())
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.REFUSED])
        self.assertFalse([e for e in self.world.events if e[0] == "desktop_mouse"])
        lines = fmt.explain_lines(SimpleNamespace(t=Translator("ru")), self.c.run_results[0].detail)
        self.assertIn("Окно «Notes» программы notes.exe", lines[0])

    def script_playing(self, key, *, wait_s=0.0):
        from app.decide import Edge, MacroNode, Script, StartNode, WaitNode
        nodes, edges = [StartNode("start")], []
        if wait_s:
            nodes.append(WaitNode("wait1", seconds=wait_s))
            edges.append(Edge("start", "next", "wait1"))
        nodes.append(MacroNode("macro1", macro=key))
        edges.append(Edge(nodes[-2].id, "next", "macro1"))
        script_key = self.library.key_of(self.library.save(Script("Uses it", tuple(nodes), tuple(edges))))
        self.c.refresh_macros()
        return script_key

    def test_a_script_block_that_plays_it_says_which_window_is_missing_and_where(self):
        from app.gui import explain
        script_key = self.script_playing(self.recorded_in_notes())
        del self.world.windows[NOTES]
        self.assertTrue(self.c.refresh())
        self.c.select_macro(script_key)
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        detail = explain.parse(self.c.run_results[0].detail)
        self.assertEqual((detail.reason, detail.node), ("place_missing", "macro1"))
        self.assertEqual((detail.params["program"], detail.params["title"]), ("notes.exe", "Notes"))
        self.assertFalse([e for e in self.world.events if e[0] == "desktop_mouse"])

    def test_a_script_does_not_ask_at_its_start_for_a_window_an_earlier_block_may_open(self):
        """The review: the window is checked when the block's turn comes, not when the script starts."""
        notes = self.world.windows[NOTES]
        script_key = self.script_playing(self.recorded_in_notes(), wait_s=0.3)
        del self.world.windows[NOTES]                                     # not open yet when the script starts
        self.assertTrue(self.c.refresh())
        self.c.select_macro(script_key)
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        self.assertEqual(self.c.run_results[0].state, RunState.RUNNING)   # not refused at the start
        self.world.windows[NOTES] = notes                                 # opened during the wait block
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED], self.c.run_results[0].detail)
        self.assertTrue([e for e in self.world.events if e[0] == "desktop_mouse"])

    def test_a_run_that_fails_at_a_click_names_the_window_in_the_history(self):
        """A window that opened during the recording is checked when its click comes: here it is there, at other
        proportions -- the run fails before that click, and the history says which window and both sizes."""
        from app.gui import explain
        self.pick("Full Windows")
        self.assertTrue(self.c.start_recording(), self.c.status)
        for kind, t in ((M.PRIMARY_DOWN, 0), (M.PRIMARY_UP, 40)):           # a click in Notes opens the dialog
            self.observer.on_mouse(MouseObservation(kind, -60, 30, t * MS, NOTES, None, None))
        self.world.windows[300] = info(**DIALOG)
        self.rects[300] = (0, 20, 40, 30)
        for kind, t in ((M.PRIMARY_DOWN, 300), (M.PRIMARY_UP, 340)):
            self.observer.on_mouse(MouseObservation(kind, 5, 25, t * MS, 300, None, None))
        self.assertTrue(self.c.stop_recording(), self.c.status)
        self.assertTrue(self.c.save("Dialog"), self.c.status)
        self.c.refresh_macros()
        self.c.select_macro(next(i.key for i in self.c.macros if i.name == "Dialog"))
        del self.world.windows[300]                                     # closed; the run's first click opens it

        def opens(world):
            if world.events[-1][1] is MouseEvent.LEFT_UP and 300 not in world.windows:
                world.windows[300] = info(**DIALOG)
                self.rects[300] = (0, 20, 40, 60)                       # at other proportions
                world.hit = 300
        chain(self.world, opens)
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.FAILED])
        detail = explain.parse(self.c.run_results[0].detail)
        self.assertEqual((detail.reason, detail.params["was"], detail.params["now"]), ("place_size", "40x30", "40x60"))
        self.assertEqual(len([e for e in self.world.events if e[0] == "desktop_mouse"
                              and e[1] is MouseEvent.LEFT_DOWN]), 1)    # the first click only

    def test_a_dry_run_that_is_refused_says_why_in_its_notice(self):
        """A dry run is in no history: its notice is where the person reads why (seen in the visual check)."""
        from app.gui.app import App
        from app.gui.i18n import Translator
        self.recorded_in_notes()
        del self.world.windows[NOTES]
        self.assertTrue(self.c.refresh())
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(dry_run=True), self.c.status)
        self.run_to_end()
        text = App.status_text(SimpleNamespace(t=Translator("ru")), self.c.status)
        self.assertIn("ничего не отправлено", text)
        self.assertIn("Почему: Окно «Notes» программы notes.exe", text)

    def test_the_panel_names_the_windows_the_clicks_go_into(self):
        from app.gui.view import fmt
        from app.gui.i18n import Translator
        key = self.recorded_in_notes()
        places = self.c.library.load(self.c.macro_by_key(key).path).recording.places
        ru = SimpleNamespace(t=Translator("ru"))
        self.assertEqual(fmt.places_lines(ru, places), ["Клики идут в окна:", "  «Notes» (notes.exe)"])
        many = [Place("notes.exe", "NotesWnd", f"Notes {i}", 60, 40, True, i // 2) for i in range(12)]
        lines = fmt.places_lines(ru, many)                                # 6 windows, each under two titles
        self.assertEqual(lines[1:], ["  «Notes 0» (notes.exe)", "  «Notes 2» (notes.exe)", "  «Notes 4» (notes.exe)",
                                     "  «Notes 6» (notes.exe)", "  … и ещё 2"])


if __name__ == "__main__":
    unittest.main()
