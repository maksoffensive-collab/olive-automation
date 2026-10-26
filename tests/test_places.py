"""Where each click of a recording on the whole screen happened (the person's list of 2026-10-25, item 12, part B;
their answers of 2026-10-26): the place -- a program's window, as the provider names it -- and the point inside it,
kept with the recording and in its file. The neutral model and the file only; the provider's part is in
tests/test_full_windows_places.py."""
import json
import unittest

from app.providers.recording import (Place, PointerButton as B, RecordedEvent as E, RecordedEventKind as K,
                                     Recording, RecordingSession, clean_place_text)
from app.providers.recording_format import (RecordingFormatError, recording_from_dict, recording_from_json,
                                            recording_to_dict, recording_to_json)
from app.providers.base import TargetIdentity
from app.providers.input import Key

NOTES = Place("notes.exe", "NotesWnd", "Notes", 800, 600, True, 0)
DIALOG = Place("notes.exe", "#32770", "Save as", 400, 300, False, 1)


def placed():
    return Recording("windows", "desktop:x", (
        E(K.POINTER_MOVE, 0, 10, 10),                                         # a move with no button: no place
        E(K.BUTTON_DOWN, 5, 110, 220, B.PRIMARY, 1, place=(0, 100, 200)),
        E(K.BUTTON_UP, 5, 110, 220, B.PRIMARY, 1, place=(0, 100, 200)),
        E(K.BUTTON_DOWN, 50, 500, 400, B.PRIMARY, 1, place=(1, 40, 30)),
        E(K.BUTTON_UP, 5, 500, 400, B.PRIMARY, 1, place=(1, 40, 30)),
        E(K.KEY_DOWN, 5, key=Key.A),
    ), places=(NOTES, DIALOG))


class ThePlace(unittest.TestCase):
    def test_a_place_says_what_it_is_and_is_checked(self):
        self.assertEqual(NOTES.program, "notes.exe")
        for bad in (dict(program=""), dict(kind=""), dict(width=0), dict(height=-1), dict(width=True),
                    dict(at_start=1), dict(title=None), dict(program="x" * 300), dict(title="a\nb"),
                    dict(width=200_000), dict(window=-1), dict(window=True), dict(window=1.0)):
            fields = dict(program="p.exe", kind="K", title="T", width=10, height=10, at_start=True, window=0)
            fields.update(bad)
            with self.assertRaises((ValueError, TypeError), msg=bad):
                Place(**fields)

    def test_only_pointer_events_have_a_place_and_it_names_one_of_the_recording(self):
        with self.assertRaises(ValueError):
            E(K.KEY_DOWN, 0, key=Key.A, place=(0, 1, 1))
        for bad in ((0, 1), (-1, 1, 1), (0, 1.5, 1), (True, 1, 1), [0, 1, 1]):
            with self.assertRaises(ValueError, msg=bad):
                E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=bad)
        with self.assertRaises(ValueError):                                   # index 1 of one place
            Recording("windows", "desktop:x", (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(1, 0, 0)),),
                      places=(NOTES,))
        with self.assertRaises(ValueError):                                   # a place no event is in
            Recording("windows", "desktop:x", (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 0, 0)),),
                      places=(NOTES, DIALOG))
        self.assertEqual(placed().places, (NOTES, DIALOG))

    def test_a_press_made_where_the_window_could_not_be_identified_is_marked_so_and_only_so(self):
        from app.providers.recording import UNKNOWN_PLACE
        marked = E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(UNKNOWN_PLACE, 0, 0))
        rec = Recording("windows", "desktop:x", (marked, E(K.BUTTON_UP, 40, 1, 1, B.PRIMARY, 1)))
        self.assertEqual((rec.places, rec.events[0].place), ((), (UNKNOWN_PLACE, 0, 0)))
        self.assertEqual(recording_from_json(recording_to_json(rec)), rec)
        for bad in (dict(kind=K.BUTTON_DOWN, place=(UNKNOWN_PLACE, 5, 5)), dict(kind=K.BUTTON_DOWN, place=(-2, 0, 0)),
                    dict(kind=K.BUTTON_UP, place=(UNKNOWN_PLACE, 0, 0))):
            with self.assertRaises(ValueError, msg=bad):
                E(bad["kind"], 0, 1, 1, B.PRIMARY, 1, place=bad["place"])

    def test_the_places_of_one_recorded_window_agree_on_what_it_is(self):
        """One window, two places: its title and size may change, never its program, its class or whether it was
        there at the start."""
        resized = Place("notes.exe", "NotesWnd", "*Notes", 1600, 1200, True, 0)
        events = (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(0, 0, 0)),
                  E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1, place=(1, 0, 0)))
        self.assertEqual(Recording("windows", "desktop:x", events, places=(NOTES, resized)).places[1], resized)
        for other in (Place("other.exe", "NotesWnd", "Notes", 800, 600, True, 0),
                      Place("notes.exe", "Other", "Notes", 800, 600, True, 0),
                      Place("notes.exe", "NotesWnd", "Notes", 800, 600, False, 0)):
            with self.assertRaises(ValueError, msg=other):
                Recording("windows", "desktop:x", events, places=(NOTES, other))

    def test_a_title_is_kept_as_it_is_compared(self):
        self.assertEqual(clean_place_text("A\u200eB\tC"), "A B C")
        self.assertEqual(len(clean_place_text("x" * 1000)), 256)

    def test_a_recording_without_places_is_as_it_always_was(self):
        rec = Recording("windows", "w1", (E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1),))
        self.assertEqual(rec.places, ())
        self.assertNotIn("places", recording_to_dict(rec))
        self.assertNotIn("place", json.dumps(recording_to_dict(rec)))


class TheSession(unittest.TestCase):
    def test_the_session_keeps_each_place_once_and_events_point_at_it(self):
        clock = iter(range(0, 10**9, 10**6))
        s = RecordingSession(clock_ns=lambda: next(clock))
        s.start(TargetIdentity("windows", "desktop:x"))
        first = s.place_of(NOTES)
        self.assertEqual((first, s.place_of(NOTES), s.place_of(DIALOG)), (0, 0, 1))
        s.record(K.BUTTON_DOWN, x=110, y=220, button=B.PRIMARY, click_count=1, place=(first, 100, 200))
        s.record(K.BUTTON_UP, x=110, y=220, button=B.PRIMARY, click_count=1, place=(first, 100, 200))
        rec = s.stop()
        self.assertEqual(rec.places, (NOTES,))                  # the dialog no event was in is not kept
        self.assertEqual(rec.events[0].place, (0, 100, 200))

    def test_places_only_while_recording(self):
        s = RecordingSession()
        with self.assertRaises(Exception):
            s.place_of(NOTES)


class TheFile(unittest.TestCase):
    def test_places_go_into_the_file_and_come_back_the_same(self):
        rec = placed()
        data = recording_to_dict(rec)
        self.assertEqual(data["places"][1], {"program": "notes.exe", "kind": "#32770", "title": "Save as",
                                             "width": 400, "height": 300, "at_start": False, "window": 1})
        self.assertEqual(data["events"][1]["place"], [0, 100, 200])
        self.assertNotIn("place", data["events"][0])
        self.assertEqual(recording_from_json(recording_to_json(rec)), rec)

    def test_a_file_with_wrong_places_is_refused(self):
        good = recording_to_dict(placed())
        for change in (lambda d: d["places"].append({"program": "x"}),
                       lambda d: d["places"][0].update(extra=1),
                       lambda d: d["places"][0].pop("at_start"),
                       lambda d: d["places"][0].pop("window"),
                       lambda d: d["places"][0].update(width=200_000),
                       lambda d: d.update(places=[]),
                       lambda d: d.update(places=None),
                       lambda d: d["events"][1].update(place=[5, 1, 1]),
                       lambda d: d["events"][1].update(place=[0, 1]),
                       lambda d: d["events"][5].update(place=[0, 1, 1])):
            data = json.loads(json.dumps(good))
            change(data)
            with self.assertRaises(RecordingFormatError, msg=change):
                recording_from_dict(data)


class TheEditor(unittest.TestCase):
    """The macro editor keeps each click's window: unchanged, moved with the point, dropped with the last click in
    it; a click added by hand has none (it goes to the screen point picked)."""

    def document(self):
        from app.gui import macro_edit
        from app.playback.macro import Macro
        return macro_edit, macro_edit.document_of(Macro(placed(), "Placed"))

    def test_a_placed_recording_comes_back_unchanged(self):
        me, doc = self.document()
        self.assertEqual(me.recording_of(doc), placed())

    def test_moving_a_click_moves_it_inside_its_window_by_the_same_amount(self):
        me, doc = self.document()
        tap = next(i for i, a in enumerate(doc.actions) if a.op.value == "tap")
        moved = me.recording_of(me.set_point(doc, tap, 115, 230))          # was (110, 220): 5 right, 10 down
        self.assertEqual([e.place for e in moved.events if e.kind is K.BUTTON_DOWN][0], (0, 105, 210))

    def test_deleting_the_last_click_in_a_window_drops_that_window(self):
        me, doc = self.document()
        taps = [i for i, a in enumerate(doc.actions) if a.op.value == "tap"]
        rec = me.recording_of(me.delete(doc, taps[0]))
        self.assertEqual(rec.places, (DIALOG,))
        self.assertEqual([e.place for e in rec.events if e.place is not None], [(0, 40, 30), (0, 40, 30)])

    def test_a_click_added_by_hand_has_no_window(self):
        from app.playback.macro import ActionOp
        me, doc = self.document()
        rec = me.recording_of(me.insert(doc, len(doc.steps), ActionOp.TAP, x=3, y=4))
        self.assertIsNone(rec.events[-1].place)
        self.assertEqual(rec.places, (NOTES, DIALOG))


if __name__ == "__main__":
    unittest.main()
