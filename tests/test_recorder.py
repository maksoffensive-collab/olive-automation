"""Recorder: neutral model, session lifecycle, serialization, Windows recorder logic, isolation.

Deterministic: a fake observation source and fake clocks; no real user input, no real hooks (except one lifecycle
test that installs and immediately removes pass-through hooks without generating any input)."""
import ast
import inspect
import json
import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.errors import CapabilityNotAvailable, EmergencyStopped, PlatformError, TargetNotVerified
from app.providers.base import TargetIdentity
from app.providers.input import Key
from app.providers.recording import (PointerButton, RecordedEvent, RecordedEventKind as K, Recorder, RecorderState,
                                     RecorderStateError, Recording, RecordingSession)
from app.providers.recording_format import (RecordingFormatError, recording_from_json, recording_to_json)
from app.providers.target import Capability
from app.windows import keymap
from app.windows.backend import (DoubleClickSettings, KeyObservation, MouseObservation, MouseObservationKind as M,
                                 WindowObservationBackend)
from app.windows.recorder import WindowRecorder
from app.windows.target import WindowVerifier
from tests.test_windows_provider import FakeDesktop, info, setup

ROOT = Path(__file__).resolve().parents[1] / "app"
MS = 1_000_000
ORIGIN, SIZE = (100, 200), (64, 48)


def imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class FakeObserver(WindowObservationBackend):
    def __init__(self, double=DoubleClickSettings(500, 4, 4)):
        self.double, self.started, self.starts, self.stops, self.fail = double, False, 0, 0, None
        self.hwnd = self.on_mouse = self.on_key = self.on_idle = None

    def double_click_settings(self):
        return self.double

    def start(self, hwnd, on_mouse, on_key, on_idle):
        if self.fail:
            raise self.fail
        self.hwnd, self.on_mouse, self.on_key, self.on_idle = hwnd, on_mouse, on_key, on_idle
        self.started = True
        self.starts += 1

    def stop(self):
        self.started = False
        self.stops += 1

    # what the operating system would report; nothing is delivered while not observing
    def mouse(self, kind, x, y, t_ms, *, hit=100, origin=ORIGIN, size=SIZE, injected=False):
        """(x, y) are CLIENT coordinates; converted to screen coordinates with `origin`."""
        if self.started:
            base = origin or ORIGIN
            self.on_mouse(MouseObservation(kind, base[0] + x, base[1] + y, t_ms * MS, hit, origin, size, injected))

    def key(self, vk, down, t_ms, *, fore=100, secure=False, injected=False):
        if self.started:
            self.on_key(KeyObservation(down, vk, t_ms * MS, fore, secure, injected))

    def idle(self):
        if self.started:
            self.on_idle()


VK_A, VK_CTRL, VK_LCTRL, VK_F10, VK_NUMPAD1, VK_SHIFT = 0x41, 0x11, 0xA2, 0x79, 0x61, 0x10


def make(*, windows=None, **kw):
    desk, stop, verifier = setup(*(windows or (info(),)))
    target = verifier.verify(100)
    obs = FakeObserver()
    rec = WindowRecorder(target, verifier, obs, stop, **kw)
    return rec, obs, desk, stop, target


def started(**kw):
    rec, obs, desk, stop, target = make(**kw)
    rec.start()
    return rec, obs, desk, stop, target


def brief(recording):
    return [(e.kind.value, e.delay_ms, e.x, e.y, e.button.value if e.button else None, e.click_count,
             e.key.value if e.key else None) for e in recording.events]


class NeutralModel(unittest.TestCase):
    def test_empty_recording_is_valid_and_serializes(self):
        r = Recording("prov", "t1", ())
        self.assertEqual((len(r), r.duration_ms, r.end_reason, r.skipped), (0, 0, "stopped", ()))
        self.assertEqual(recording_from_json(recording_to_json(r)), r)

    def test_event_vocabulary_and_validation(self):
        RecordedEvent(K.POINTER_MOVE, 0, 1, 2)
        RecordedEvent(K.BUTTON_DOWN, 5, -3, 4, PointerButton.SECONDARY, 2)
        RecordedEvent(K.KEY_UP, 1, key=Key.CTRL)
        RecordedEvent(K.TEXT, 1, text="héllo")
        bad = [lambda: RecordedEvent(K.POINTER_MOVE, 0), lambda: RecordedEvent(K.POINTER_MOVE, 0, 1.5, 2),
               lambda: RecordedEvent(K.POINTER_MOVE, 0, True, 2), lambda: RecordedEvent(K.POINTER_MOVE, -1, 1, 2),
               lambda: RecordedEvent(K.POINTER_MOVE, 0, 1, 2, key=Key.A),
               lambda: RecordedEvent(K.BUTTON_DOWN, 0, 1, 2, PointerButton.PRIMARY, 3),
               lambda: RecordedEvent(K.BUTTON_DOWN, 0, 1, 2, "left", 1), lambda: RecordedEvent(K.KEY_DOWN, 0, key="a"),
               lambda: RecordedEvent(K.TEXT, 0, text=""), lambda: RecordedEvent(K.TEXT, 0, text="a\nb"),
               lambda: RecordedEvent(K.TEXT, 0, text="x" * 257), lambda: RecordedEvent("move", 0, 1, 2)]
        for make_bad in bad:
            with self.assertRaises(ValueError):
                make_bad()

    def test_modifier_sequence_stays_four_separate_events(self):
        seq = tuple(RecordedEvent(k, d, key=key) for k, d, key in
                    [(K.KEY_DOWN, 0, Key.CTRL), (K.KEY_DOWN, 10, Key.A), (K.KEY_UP, 10, Key.A), (K.KEY_UP, 5, Key.CTRL)])
        r = Recording("p", "t", seq)
        self.assertEqual([(e.kind.value, e.key.value) for e in r.events],
                         [("key_down", "ctrl"), ("key_down", "a"), ("key_up", "a"), ("key_up", "ctrl")])
        self.assertEqual(r.duration_ms, 25)

    def test_recording_validation(self):
        with self.assertRaises(ValueError):
            Recording("p", "t", (RecordedEvent(K.POINTER_MOVE, 3, 1, 1),))          # first delay must be 0
        with self.assertRaises(ValueError):
            Recording("", "t", ())
        with self.assertRaises(ValueError):
            Recording("p", "t", [])                                                   # not a tuple
        with self.assertRaises(ValueError):
            Recording("p", "t", (), skipped=(("b", 1), ("a", 1)))                    # unsorted

    def test_target_identity_is_provider_and_target_id_only(self):
        r = Recording("prov", "t1", ())
        self.assertEqual(r.target, TargetIdentity("prov", "t1"))


class SessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = RecordingSession(clock_ns=self.clock)
        self.t = TargetIdentity("prov", "t1", "Name")

    def test_start_stop_completes_with_only_then_a_recording(self):
        self.assertEqual((self.s.state, self.s.recording), (RecorderState.IDLE, None))
        self.s.start(self.t)
        self.assertEqual(self.s.state, RecorderState.RECORDING)
        self.assertIsNone(self.s.recording)                 # nothing completed while running
        rec = self.s.stop()
        self.assertEqual((self.s.state, self.s.recording, len(rec)), (RecorderState.COMPLETED, rec, 0))

    def test_start_twice_and_stop_or_cancel_when_idle_are_rejected(self):
        for op in (self.s.stop, self.s.cancel, lambda: self.s.invalidate("x")):
            with self.assertRaises(RecorderStateError):
                op()
        self.s.start(self.t)
        with self.assertRaises(RecorderStateError):
            self.s.start(self.t)
        with self.assertRaises(RecorderStateError):
            self.s.discard()                                 # not while recording

    def test_discard_on_an_already_idle_session_is_a_harmless_no_op(self):
        """Unlike stop/cancel/invalidate, discard() is explicitly allowed from IDLE (it only refuses RECORDING) --
        forgetting nothing there is nothing to forget."""
        self.assertEqual(self.s.state, RecorderState.IDLE)
        self.s.discard()
        self.assertEqual((self.s.state, self.s.recording, self.s.event_count), (RecorderState.IDLE, None, 0))
        self.s.discard()                                     # repeated calls stay harmless
        self.assertEqual(self.s.state, RecorderState.IDLE)

    def test_start_requires_explicit_target(self):
        with self.assertRaises(RecorderStateError):
            self.s.start(None)
        with self.assertRaises(RecorderStateError):
            self.s.start("windows:1")

    def test_finished_session_needs_explicit_discard_before_a_new_recording(self):
        self.s.start(self.t)
        self.s.record(K.KEY_DOWN, key=Key.A)
        first = self.s.stop()
        with self.assertRaises(RecorderStateError):
            self.s.start(self.t)                             # no accidental reuse
        with self.assertRaises(RecorderStateError):
            self.s.record(K.KEY_UP, key=Key.A)               # cannot append to a completed recording
        self.s.discard()
        self.assertEqual((self.s.state, self.s.recording, self.s.event_count), (RecorderState.IDLE, None, 0))
        self.s.start(self.t)
        self.assertEqual(len(self.s.stop()), 0)              # the new recording holds nothing of the old one
        self.assertEqual(len(first), 1)

    def test_cancel_discards_events_and_exposes_reason_only(self):
        self.s.start(self.t)
        self.s.record(K.KEY_DOWN, key=Key.A)
        self.s.cancel("user_cancel", "because")
        self.assertEqual((self.s.state, self.s.recording, self.s.end_reason, self.s.end_detail, self.s.event_count),
                         (RecorderState.CANCELLED, None, "user_cancel", "because", 0))

    def test_invalid_target_ends_without_recording_and_keeps_reason(self):
        self.s.start(self.t)
        self.s.record(K.KEY_DOWN, key=Key.A)
        self.s.invalidate("window_gone", "gone")
        self.assertEqual((self.s.state, self.s.recording, self.s.end_reason), (RecorderState.INVALID_TARGET, None, "window_gone"))
        with self.assertRaises(RecorderStateError):
            self.s.record(K.KEY_UP, key=Key.A)

    def test_relative_timing_first_event_zero_rounding_and_ordering(self):
        self.s.start(self.t)
        self.clock.now = 5_000 * MS                          # a long wait before the first event does not count
        self.s.record(K.KEY_DOWN, key=Key.A)
        self.clock.now += 120 * MS
        self.s.record(K.KEY_UP, key=Key.A)
        self.s.record(K.KEY_DOWN, key=Key.B, at_ns=self.clock.now + 35 * MS + 499_999)
        self.s.record(K.KEY_UP, key=Key.B, at_ns=self.clock.now + 35 * MS + 499_999 + 1_500_000)
        rec = self.s.stop()
        self.assertEqual([(e.kind.value, e.delay_ms) for e in rec.events],
                         [("key_down", 0), ("key_up", 120), ("key_down", 35), ("key_up", 2)])   # 1.5 ms -> 2 (half up)
        self.assertEqual(rec.duration_ms, 157)

    def test_time_may_not_go_backwards(self):
        self.s.start(self.t)
        self.s.record(K.KEY_DOWN, key=Key.A, at_ns=10 * MS)
        with self.assertRaises(RecorderStateError):
            self.s.record(K.KEY_UP, key=Key.A, at_ns=9 * MS)
        self.assertEqual(self.s.event_count, 1)

    def test_invalid_event_leaves_the_log_untouched(self):
        self.s.start(self.t)
        with self.assertRaises(ValueError):
            self.s.record(K.POINTER_MOVE, x=1)
        self.assertEqual(self.s.event_count, 0)

    def test_event_limit_cancels_instead_of_growing_without_bound(self):
        s = RecordingSession(clock_ns=self.clock, max_events=2)
        s.start(self.t)
        s.record(K.KEY_DOWN, key=Key.A)
        s.record(K.KEY_UP, key=Key.A)
        with self.assertRaises(RecorderStateError):
            s.record(K.KEY_DOWN, key=Key.B)
        self.assertEqual((s.state, s.end_reason, s.recording), (RecorderState.CANCELLED, "event_limit", None))

    def test_skipped_counts_are_reported_sorted(self):
        self.s.start(self.t)
        self.s.note_skipped("unsupported_key")
        self.s.note_skipped("secure_input")
        self.s.note_skipped("secure_input")
        self.assertEqual(self.s.stop().skipped, (("secure_input", 2), ("unsupported_key", 1)))


class Serialization(unittest.TestCase):
    def sample(self):
        return Recording("windows", "win-1", (
            RecordedEvent(K.POINTER_MOVE, 0, 10, 20), RecordedEvent(K.BUTTON_DOWN, 120, 10, 20, PointerButton.PRIMARY, 1),
            RecordedEvent(K.BUTTON_UP, 35, 11, 20, PointerButton.PRIMARY, 1), RecordedEvent(K.KEY_DOWN, 1, key=Key.CTRL),
            RecordedEvent(K.TEXT, 2, text="ok"), RecordedEvent(K.KEY_UP, 3, key=Key.CTRL)),
            "control_hotkey", (("secure_input", 3),))

    def test_round_trip_and_stable_bytes(self):
        r = self.sample()
        text = recording_to_json(r)
        self.assertEqual(recording_from_json(text), r)
        self.assertEqual(recording_to_json(recording_from_json(text)), text)
        self.assertTrue(text.endswith("\n") and text.isascii())
        data = json.loads(text)
        self.assertEqual((data["schema"], data["version"]), ("automation-recording", 1))
        self.assertEqual(list(data), sorted(data))
        self.assertEqual(data["events"][0], {"delay_ms": 0, "kind": "pointer_move", "x": 10, "y": 20})
        self.assertEqual(data["target"], {"provider_id": "windows", "target_id": "win-1"})

    def test_no_unstable_data_is_serialized(self):
        text = recording_to_json(self.sample()).lower()
        for word in ("hwnd", "title", "display", "0x", "time\":", "pid"):
            self.assertNotIn(word, text)

    def test_loading_is_strict(self):
        good = json.loads(recording_to_json(self.sample()))

        def mutate(fn):
            d = json.loads(json.dumps(good))
            fn(d)
            return json.dumps(d)
        cases = [mutate(lambda d: d.update(version=2)), mutate(lambda d: d.update(schema="other")),
                 mutate(lambda d: d.update(extra=1)), mutate(lambda d: d.pop("events")),
                 mutate(lambda d: d["events"][0].update(hwnd=5)), mutate(lambda d: d["events"][0].update(kind="teleport")),
                 mutate(lambda d: d["events"][1].update(key="a")), mutate(lambda d: d["events"][3].update(key="Ctrl")),
                 mutate(lambda d: d["events"][0].update(delay_ms=4)), mutate(lambda d: d["target"].update(x=1)),
                 mutate(lambda d: d.update(skipped={"secure_input": 0})), "not json", "[]", "null"]
        for text in cases:
            with self.assertRaises(RecordingFormatError, msg=text):
                recording_from_json(text)

    def test_a_json_boolean_is_never_accepted_where_an_integer_is_required(self):
        """JSON's `true`/`false` deserialize as Python bool, and bool is an int subclass: RecordedEvent's own
        _is_int() explicitly excludes bool, but that guarantee is only real if this format layer actually lets a
        boolean literal reach it instead of being caught earlier by some other check."""
        good = json.loads(recording_to_json(self.sample()))

        def mutate(fn):
            d = json.loads(json.dumps(good))
            fn(d)
            return json.dumps(d)
        cases = [mutate(lambda d: d["events"][0].update(delay_ms=True)),
                 mutate(lambda d: d["events"][0].update(x=True)),
                 mutate(lambda d: d["events"][0].update(y=False)),
                 mutate(lambda d: d["events"][1].update(click_count=True))]
        for text in cases:
            with self.assertRaises(RecordingFormatError, msg=text):
                recording_from_json(text)


class SessionThroughWindowRecorder(unittest.TestCase):
    def test_start_stop_empty_recording(self):
        rec, obs, *_ = make()
        self.assertEqual(rec.state, RecorderState.IDLE)
        rec.start()
        self.assertEqual((rec.state, obs.started, obs.hwnd), (RecorderState.RECORDING, True, 100))
        result = rec.stop()
        self.assertEqual((len(result), rec.state, rec.recording, obs.started, rec.end_reason),
                         (0, RecorderState.COMPLETED, result, False, "stopped"))

    def test_start_twice_and_stop_when_idle_are_deterministic(self):
        rec, obs, *_ = make()
        with self.assertRaises(RecorderStateError):
            rec.stop()
        with self.assertRaises(RecorderStateError):
            rec.cancel()
        rec.start()
        with self.assertRaises(RecorderStateError):
            rec.start()
        self.assertEqual(obs.starts, 1)
        rec.stop()
        with self.assertRaises(RecorderStateError):
            rec.stop()
        with self.assertRaises(RecorderStateError):
            rec.start()                                       # must discard first
        rec.discard()
        rec.start()
        self.assertEqual(obs.starts, 2)

    def test_cancel_and_discard(self):
        rec, obs, *_ = started()
        obs.key(VK_A, True, 1)
        rec.cancel()
        self.assertEqual((rec.state, rec.recording, rec.end_reason, obs.started), (RecorderState.CANCELLED, None, "cancelled", False))
        rec.discard()
        self.assertEqual(rec.state, RecorderState.IDLE)

    def test_target_identity_is_stamped_and_neutral(self):
        rec, obs, _, _, target = started()
        obs.key(VK_A, True, 1)
        obs.key(VK_A, False, 2)
        result = rec.stop()
        self.assertEqual((result.provider_id, result.target_id), (target.provider_id, target.target_id))
        self.assertEqual(rec.target_identity, target.identity)
        text = recording_to_json(result)
        for secret in ("5000", "EditorWnd", "editor.exe", "Untitled"):
            self.assertNotIn(secret, text)

    def test_windows_recorder_is_a_neutral_recorder(self):
        rec, *_ = make()
        self.assertIsInstance(rec, Recorder)

    def test_requires_a_verified_window_target_and_the_record_capability(self):
        with self.assertRaises(TargetNotVerified):
            WindowRecorder(object(), None, FakeObserver(), None)
        rec, obs, desk, stop, target = make()
        no_record = type("NoRecord", (type(target),), {"capabilities": property(lambda s: frozenset({Capability.TAP}))})
        forged = object.__new__(no_record)
        for f in ("target_id_value", "hwnd", "pid", "process_start", "process_name", "class_name", "title", "verified_at", "_mint"):
            object.__setattr__(forged, f, getattr(target, f))
        rec2 = WindowRecorder(forged, WindowVerifier(desk, stop), FakeObserver(), stop)
        with self.assertRaises(CapabilityNotAvailable):
            rec2.start()

    def test_start_refuses_a_dead_target_and_emergency_stop_and_changes_nothing(self):
        rec, obs, desk, stop, _ = make()
        del desk.windows[100]
        with self.assertRaises(TargetNotVerified):
            rec.start()
        self.assertEqual((rec.state, obs.starts), (RecorderState.IDLE, 0))
        rec2, obs2, _, stop2, _ = make()
        stop2.trigger("test")
        with self.assertRaises(EmergencyStopped):
            rec2.start()
        self.assertEqual((rec2.state, obs2.starts), (RecorderState.IDLE, 0))

    def test_observation_failure_at_start_leaves_a_cancelled_recorder(self):
        rec, obs, *_ = make()
        obs.fail = PlatformError("hooks refused")
        with self.assertRaises(PlatformError):
            rec.start()
        self.assertEqual((rec.state, rec.end_reason, rec.recording), (RecorderState.CANCELLED, "start_failed", None))
        obs.fail = OSError("boom")
        rec.discard()
        with self.assertRaises(PlatformError):
            rec.start()
        self.assertEqual(rec.state, RecorderState.CANCELLED)

    def test_nothing_is_recorded_before_start_or_after_stop(self):
        rec, obs, *_ = make()
        obs.key(VK_A, True, 1)                                # not observing: nothing is even delivered
        rec.start()
        rec.stop()
        obs.key(VK_A, True, 5)
        self.assertEqual(len(rec.recording), 0)


class MouseRecording(unittest.TestCase):
    def test_client_coordinate_conversion_and_ordering_and_delays(self):
        rec, obs, *_ = started()
        obs.mouse(M.MOVE, 5, 6, 1000)
        obs.mouse(M.PRIMARY_DOWN, 10, 20, 1120)
        obs.mouse(M.PRIMARY_UP, 10, 20, 1155)
        self.assertEqual(brief(rec.stop()), [("pointer_move", 0, 5, 6, None, None, None),
                                             ("button_down", 120, 10, 20, "primary", 1, None),
                                             ("button_up", 35, 10, 20, "primary", 1, None)])

    def test_right_click(self):
        rec, obs, *_ = started()
        obs.mouse(M.SECONDARY_DOWN, 1, 2, 0)
        obs.mouse(M.SECONDARY_UP, 1, 2, 10)
        self.assertEqual([(e.kind.value, e.button.value) for e in rec.stop().events],
                         [("button_down", "secondary"), ("button_up", "secondary")])

    def test_double_click_detection(self):
        rec, obs, *_ = started()
        for t, kind in ((0, M.PRIMARY_DOWN), (10, M.PRIMARY_UP), (200, M.PRIMARY_DOWN), (210, M.PRIMARY_UP),
                        (300, M.PRIMARY_DOWN), (310, M.PRIMARY_UP),       # third press: single again
                        (2000, M.PRIMARY_DOWN), (2010, M.PRIMARY_UP)):    # too late
            obs.mouse(kind, 5, 5, t)
        self.assertEqual([e.click_count for e in rec.stop().events], [1, 1, 2, 2, 1, 1, 1, 1])

    def test_no_double_click_when_far_apart_or_other_button(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 0)
        obs.mouse(M.PRIMARY_UP, 5, 5, 5)
        obs.mouse(M.PRIMARY_DOWN, 20, 5, 100)
        obs.mouse(M.PRIMARY_UP, 20, 5, 105)
        obs.mouse(M.SECONDARY_DOWN, 20, 5, 150)
        obs.mouse(M.SECONDARY_UP, 20, 5, 155)
        self.assertEqual([e.click_count for e in rec.stop().events], [1] * 6)

    def test_window_move_keeps_recorded_coordinates_target_relative(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 10, 20, 0, origin=(100, 200))
        obs.mouse(M.PRIMARY_UP, 10, 20, 5, origin=(100, 200))
        obs.mouse(M.PRIMARY_DOWN, 10, 20, 900, origin=(700, 50))   # window moved on screen
        obs.mouse(M.PRIMARY_UP, 10, 20, 905, origin=(700, 50))
        self.assertEqual({(e.x, e.y) for e in rec.stop().events}, {(10, 20)})

    def test_resize_uses_the_size_observed_at_that_moment_and_never_clamps(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 100, 10, 0, size=(64, 48))       # outside the old client area: ignored
        obs.mouse(M.PRIMARY_DOWN, 100, 10, 10, size=(200, 100))    # after enlarging: recorded as observed
        self.assertEqual(brief(rec.stop()), [("button_down", 0, 100, 10, "primary", 1, None)])

    def test_events_outside_client_area_or_over_other_windows_are_ignored(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, -1, 5, 0)                        # title bar / border
        obs.mouse(M.PRIMARY_DOWN, 5, 48, 1)
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 2, hit=999)                # another window covers the point
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 3, hit=None)
        obs.mouse(M.MOVE, 5, 5, 4, hit=999)
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 5, origin=None, size=None)
        self.assertEqual(len(rec.stop()), 0)

    def test_release_without_recorded_press_is_ignored_and_press_only_pairs_up(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_UP, 5, 5, 0)
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 1)
        obs.mouse(M.PRIMARY_DOWN, 6, 6, 2)                         # duplicate press while held
        self.assertEqual([e.kind.value for e in rec.stop().events], ["button_down"])

    def test_drag_outside_the_window_is_followed_unclamped_then_released(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 60, 40, 0)
        obs.mouse(M.MOVE, 90, 60, 50, hit=999)                     # outside, over another window, button held
        obs.mouse(M.PRIMARY_UP, -20, 70, 100, hit=None)
        obs.mouse(M.MOVE, -30, 70, 200, hit=999)                   # nothing held any more: ignored
        self.assertEqual(brief(rec.stop()), [("button_down", 0, 60, 40, "primary", 1, None),
                                             ("pointer_move", 50, 90, 60, None, None, None),
                                             ("button_up", 50, -20, 70, "primary", 1, None)])

    def test_moves_are_rate_limited_and_deduplicated(self):
        rec, obs, *_ = started(min_move_interval_ms=10)
        obs.mouse(M.MOVE, 1, 1, 0)
        obs.mouse(M.MOVE, 2, 1, 5)                                 # too soon
        obs.mouse(M.MOVE, 3, 1, 10)
        obs.mouse(M.MOVE, 3, 1, 30)                                # same position
        obs.mouse(M.MOVE, 4, 1, 31)
        self.assertEqual([(e.x, e.delay_ms) for e in rec.stop().events], [(1, 0), (3, 10), (4, 21)])

    def test_injected_input_is_ignored_by_default_but_can_be_enabled(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 0, injected=True)
        obs.key(VK_A, True, 1, injected=True)
        self.assertEqual(len(rec.stop()), 0)
        rec2, obs2, *_ = started(ignore_injected=False)
        obs2.mouse(M.PRIMARY_DOWN, 5, 5, 0, injected=True)
        self.assertEqual(len(rec2.stop()), 1)


class KeyboardRecording(unittest.TestCase):
    def test_keymap_is_total_and_never_substitutes(self):
        for k in Key:
            self.assertIs(keymap.key_from_virtual_key(keymap.virtual_key(k)), k)
        for vk, key in ((0xA0, Key.SHIFT), (0xA1, Key.SHIFT), (0xA2, Key.CTRL), (0xA3, Key.CTRL), (0xA4, Key.ALT),
                        (0xA5, Key.ALT), (0x70, Key.F1), (0x7B, Key.F12), (0x30, Key.DIGIT_0), (0x0D, Key.ENTER)):
            self.assertIs(keymap.key_from_virtual_key(vk), key)
        for vk in (VK_NUMPAD1, 0x5B, 0x14, 0xFF, 0, -1, "a", True, None):
            self.assertIsNone(keymap.key_from_virtual_key(vk))

    def test_modifier_chord_is_four_separate_ordered_events(self):
        rec, obs, *_ = started()
        obs.key(VK_LCTRL, True, 0)
        obs.key(VK_A, True, 30)
        obs.key(VK_A, False, 60)
        obs.key(VK_LCTRL, False, 70)
        self.assertEqual(brief(rec.stop()), [("key_down", 0, None, None, None, None, "ctrl"),
                                             ("key_down", 30, None, None, None, None, "a"),
                                             ("key_up", 30, None, None, None, None, "a"),
                                             ("key_up", 10, None, None, None, None, "ctrl")])

    def test_only_recorded_while_the_target_has_focus(self):
        rec, obs, *_ = started()
        obs.key(VK_A, True, 0, fore=555)                           # another application has focus
        obs.key(VK_A, False, 1, fore=555)
        obs.key(VK_A, True, 2, fore=None)
        self.assertEqual(len(rec.stop()), 0)

    def test_key_up_after_focus_moved_still_closes_a_recorded_press(self):
        rec, obs, *_ = started()
        obs.key(VK_A, True, 0)
        obs.key(VK_A, False, 10, fore=555)
        obs.key(VK_SHIFT, False, 20, fore=555)                     # release of a key never recorded: ignored
        self.assertEqual([e.kind.value for e in rec.stop().events], ["key_down", "key_up"])

    def test_auto_repeat_is_recorded_once(self):
        rec, obs, *_ = started()
        for t in (0, 500, 530, 560):
            obs.key(VK_A, True, t)
        obs.key(VK_A, False, 600)
        self.assertEqual([(e.kind.value, e.delay_ms) for e in rec.stop().events], [("key_down", 0), ("key_up", 600)])

    def test_unsupported_keys_are_counted_never_substituted(self):
        rec, obs, *_ = started()
        obs.key(VK_NUMPAD1, True, 0)
        obs.key(VK_NUMPAD1, False, 1)
        obs.key(0x5B, True, 2)
        obs.key(VK_NUMPAD1, True, 3, fore=555)                     # unrelated application: not even counted
        result = rec.stop()
        self.assertEqual((len(result), result.skipped), (0, (("unsupported_key", 2),)))

    def test_secure_input_is_never_recorded_only_counted(self):
        rec, obs, *_ = started()
        obs.key(VK_A, True, 0, secure=True)
        obs.key(VK_A, False, 1, secure=True)
        obs.key(VK_NUMPAD1, True, 2, secure=True)
        obs.key(VK_A, True, 3)
        obs.key(VK_A, False, 4)
        result = rec.stop()
        self.assertEqual(([e.kind.value for e in result.events], result.skipped), (["key_down", "key_up"], (("secure_input", 2),)))

    def test_the_windows_recorder_never_emits_text_events(self):
        rec, obs, *_ = started()
        for i, vk in enumerate(range(0x41, 0x5B)):
            obs.key(vk, True, 2 * i)
            obs.key(vk, False, 2 * i + 1)
        self.assertNotIn(K.TEXT, {e.kind for e in rec.stop().events})


class ControlHotkey(unittest.TestCase):
    def test_hotkey_stops_and_is_excluded_from_the_stream(self):
        rec, obs, *_ = started()
        obs.key(VK_A, True, 0)
        obs.key(VK_A, False, 5)
        obs.key(VK_F10, True, 10)
        self.assertEqual(rec.state, RecorderState.RECORDING)      # the press alone does not stop
        obs.key(VK_F10, False, 20)
        self.assertEqual((rec.state, rec.end_reason, obs.started), (RecorderState.COMPLETED, "control_hotkey", False))
        self.assertEqual([e.key.value for e in rec.recording.events], ["a", "a"])
        self.assertEqual(rec.recording.skipped, ())

    def test_hotkey_works_when_another_window_has_focus_and_stop_after_it_is_rejected(self):
        rec, obs, *_ = started()
        obs.key(VK_F10, True, 0, fore=555)
        obs.key(VK_F10, False, 1, fore=555)
        self.assertEqual(rec.state, RecorderState.COMPLETED)
        with self.assertRaises(RecorderStateError):
            rec.stop()

    def test_hotkey_can_be_recorded_when_configured_and_can_be_disabled_or_changed(self):
        rec, obs, *_ = started(record_control_key=True)
        obs.key(VK_F10, True, 0)
        obs.key(VK_F10, False, 5)
        self.assertEqual([(e.kind.value, e.key.value) for e in rec.recording.events], [("key_down", "f10"), ("key_up", "f10")])
        rec2, obs2, *_ = started(control_key=None)
        obs2.key(VK_F10, True, 0)
        obs2.key(VK_F10, False, 5)
        self.assertEqual(rec2.state, RecorderState.RECORDING)
        self.assertEqual(len(rec2.stop()), 2)
        rec3, obs3, *_ = started(control_key=Key.ESCAPE)
        obs3.key(0x1B, True, 0)
        obs3.key(0x1B, False, 1)
        self.assertEqual((rec3.state, len(rec3.recording)), (RecorderState.COMPLETED, 0))

    def test_hotkey_is_never_blocked_or_injected(self):
        # The observation contract has no way to alter input; the fake shows the recorder returns nothing to suppress with.
        rec, obs, *_ = started()
        self.assertIsNone(obs.on_key(KeyObservation(True, VK_F10, 0, 100)))


class TargetInvalidation(unittest.TestCase):
    def check_invalid(self, mutate, code):
        rec, obs, desk, *_ = started()
        obs.key(VK_A, True, 0)
        mutate(desk)
        obs.key(VK_A, False, 5)
        self.assertEqual((rec.state, rec.end_reason, rec.recording, obs.started), (RecorderState.INVALID_TARGET, code, None, False))
        obs.key(VK_A, True, 9)                                     # nothing more is recorded
        self.assertIsNone(rec.recording)
        with self.assertRaises(RecorderStateError):
            rec.stop()
        return rec

    def test_window_destroyed(self):
        self.check_invalid(lambda d: d.windows.pop(100), "window_gone")

    def test_process_identity_change(self):
        self.check_invalid(lambda d: d.windows.update({100: replace(d.windows[100], pid=99, process_start=9)}), "process_changed")

    def test_window_class_change(self):
        self.check_invalid(lambda d: d.windows.update({100: replace(d.windows[100], class_name="Other")}), "class_changed")

    def test_window_hidden(self):
        self.check_invalid(lambda d: d.windows.update({100: replace(d.windows[100], visible=False)}), "not_visible")

    def test_title_change_alone_is_not_invalidation(self):
        rec, obs, desk, *_ = started()
        desk.windows[100] = replace(desk.windows[100], title="Renamed")
        obs.key(VK_A, True, 0)
        self.assertEqual((rec.state, rec.end_detail), (RecorderState.RECORDING, ""))

    def test_invalidation_is_detected_on_idle_ticks_without_input(self):
        rec, obs, desk, *_ = started()
        del desk.windows[100]
        obs.idle()
        self.assertEqual((rec.state, rec.end_reason), (RecorderState.INVALID_TARGET, "window_gone"))

    def test_recorder_never_switches_to_another_window(self):
        rec, obs, desk, *_ = started(windows=[info(), info(hwnd=200, title="other")])
        desk.windows.pop(100)
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 0, hit=200)                # the other window is simply not the target
        obs.idle()
        self.assertEqual((rec.state, obs.hwnd), (RecorderState.INVALID_TARGET, 100))
        self.assertIsNone(rec.recording)

    def test_recycled_handle_with_a_different_process_is_invalid(self):
        rec = self.check_invalid(lambda d: d.windows.update({100: info(hwnd=100, pid=77, start=1234, name="evil.exe")}), "process_changed")
        self.assertIsNone(rec.recording)


class EmergencyStopBehaviour(unittest.TestCase):
    def test_stop_during_an_event_cancels_and_records_nothing_more(self):
        rec, obs, _, stop, _ = started()
        obs.key(VK_A, True, 0)
        stop.trigger("panic")
        obs.key(VK_A, False, 1)
        self.assertEqual((rec.state, rec.end_reason, rec.recording, obs.started), (RecorderState.CANCELLED, "emergency_stop", None, False))
        self.assertEqual(rec.end_detail, "panic")

    def test_stop_is_noticed_on_idle_and_state_polling(self):
        rec, obs, _, stop, _ = started()
        stop.trigger()
        obs.idle()
        self.assertEqual(rec.state, RecorderState.CANCELLED)
        rec2, obs2, _, stop2, _ = started()
        stop2.trigger()
        self.assertEqual((rec2.state, obs2.started), (RecorderState.CANCELLED, False))

    def test_explicit_stop_under_emergency_cancels_and_raises(self):
        rec, obs, _, stop, _ = started()
        obs.key(VK_A, True, 0)
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            rec.stop()
        self.assertEqual((rec.state, rec.recording), (RecorderState.CANCELLED, None))

    def test_after_reset_a_new_recording_can_start_only_after_discard(self):
        rec, obs, _, stop, _ = started()
        stop.trigger()
        self.assertEqual(rec.state, RecorderState.CANCELLED)
        stop.reset()
        with self.assertRaises(RecorderStateError):
            rec.start()
        rec.discard()
        rec.start()
        self.assertEqual(rec.state, RecorderState.RECORDING)


class InternalFaults(unittest.TestCase):
    def test_a_capture_bug_cancels_instead_of_producing_a_corrupt_recording(self):
        rec, obs, *_ = started()
        obs.mouse(M.PRIMARY_DOWN, 5, 5, 100)
        obs.mouse(M.PRIMARY_UP, 5, 5, 50)                          # time going backwards from a broken source
        self.assertEqual((rec.state, rec.end_reason, rec.recording, obs.started), (RecorderState.CANCELLED, "internal_error", None, False))


class IncompleteBackendShutdownDuringStop(unittest.TestCase):
    """P-21 fixed Win32ObservationBackend.stop() so it reports False (rather than silently True) when a hook or
    dispatch thread outlives its join timeout -- but, as documented at the time, WindowRecorder itself was left
    unchanged: it already ignored the return value, and still does. This is a deliberate, acknowledged scope
    boundary, not a hidden bug; these tests prove it stays exactly that (documented and stable), so a future
    change either wires the signal further up on purpose, or this test is the one that has to be updated to say
    so -- it can never drift silently."""

    class IncompleteShutdownObserver(FakeObserver):
        def stop(self):
            super().stop()
            return False                    # a thread outlived its join timeout; the observer still deactivates

    def _recorder(self):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        return WindowRecorder(target, verifier, self.IncompleteShutdownObserver(), stop)

    def test_stop_still_returns_a_normal_completed_recording_even_when_the_backend_reports_incomplete_shutdown(self):
        rec = self._recorder()
        rec.start()
        recording = rec.stop()
        self.assertEqual(rec.state, RecorderState.COMPLETED)
        self.assertIsNotNone(recording)
        self.assertEqual(rec.end_reason, "stopped")

    def test_cancel_also_does_not_surface_an_incomplete_shutdown(self):
        rec = self._recorder()
        rec.start()
        rec.cancel()
        self.assertEqual(rec.state, RecorderState.CANCELLED)


class Isolation(unittest.TestCase):
    RECORDER_BANNED = ("sendinput", "postmessage", "sendmessage", "mouse_event", "keybd_event", "setcursorpos",
                       "setforegroundwindow", "setfocus", "setactivewindow", "blockinput", "setwindowpos", "movewindow",
                       "showwindow", "attachthreadinput", "enablewindow", "closewindow", "destroywindow",
                       "createprocess", "registerhotkey")

    def test_generic_recording_code_imports_no_provider(self):
        for name in ("recording.py", "recording_format.py"):
            bad = {m for m in imports_of(ROOT / "providers" / name) if m.startswith(("app.windows", "app.ldplayer", "app.adb",
                                                                                      "app.instances", "app.macros"))}
            self.assertEqual(bad, set(), name)
            allowed = {m for m in imports_of(ROOT / "providers" / name) if m.startswith("app.")}
            self.assertLessEqual(allowed, {"app.core.errors", "app.providers.base", "app.providers.input",
                                           "app.providers.recording", "app.providers.strict_json"}, name)  # neutral JSON helper

    def test_generic_recording_code_leaks_no_platform_vocabulary(self):
        for name in ("recording.py", "recording_format.py"):
            text = (ROOT / "providers" / name).read_text(encoding="utf-8").lower()
            for word in ("hwnd", "windows", "win32", "vk_", "virtual-key", "virtual_key", "hook", "wm_", "adb", "ldplayer",
                         "serial", "postmessage"):
                self.assertNotIn(word, text, f"{name}: {word}")

    def test_windows_capture_code_lives_only_under_app_windows(self):
        for path in ROOT.rglob("*.py"):
            if "windows" in path.relative_to(ROOT).parts[:1]:
                continue
            text = path.read_text(encoding="utf-8")
            root_ok = path.relative_to(ROOT).parts[0] == "bootstrap"      # composition root may name the recorder class
            for word in ("SetWindowsHookEx", "WH_MOUSE_LL", "WH_KEYBOARD_LL", "MouseObservation", "WindowRecorder",
                         "key_from_virtual_key"):
                if root_ok and word == "WindowRecorder":
                    continue
                self.assertNotIn(word, text, f"{path}: {word}")
        self.assertTrue((ROOT / "windows" / "recorder.py").is_file())

    def test_recorder_and_observation_backend_contain_no_input_injection_or_window_management(self):
        texts = {"recorder.py": (ROOT / "windows" / "recorder.py").read_text(encoding="utf-8")}
        from app.windows import win32
        texts["Win32ObservationBackend"] = inspect.getsource(win32.Win32ObservationBackend)
        texts["backend observation types"] = inspect.getsource(sys.modules["app.windows.backend"].WindowObservationBackend)
        for name, text in texts.items():
            for word in self.RECORDER_BANNED:
                self.assertNotIn(word, text.lower(), f"{name}: {word}")

    def test_hook_procedures_always_pass_the_event_on_and_never_return_a_block_value(self):
        from app.windows import win32
        src = inspect.getsource(win32.Win32ObservationBackend._run_hooks)
        self.assertEqual(src.count("return self._CallNext(None, n_code, w_param, l_param)"), 2)
        self.assertNotIn("return 1", src)
        self.assertNotIn("return -1", src)

    def test_recorder_module_has_no_ctypes_and_windows_still_imports_only_allowed_packages(self):
        text = (ROOT / "windows" / "recorder.py").read_text(encoding="utf-8")
        self.assertNotIn("ctypes", text)
        for m in imports_of(ROOT / "windows" / "recorder.py"):
            if m.startswith("app."):
                self.assertIn(m.split(".")[1], ("core", "providers", "playback", "windows"))

    def test_clean_interpreter_loads_the_generic_recording_without_any_provider(self):
        code = textwrap.dedent("""
            import sys
            import app.providers.recording, app.providers.recording_format
            bad = [m for m in sys.modules if m.split(".")[:2] in (["app","adb"],["app","ldplayer"],["app","instances"],["app","windows"],["app","macros"])]
            assert not bad, bad
            print("CLEAN")
        """)
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "CLEAN", r.stderr)

    def test_capability_vocabulary_and_provider_declarations(self):
        from app.ldplayer.ids import CAPABILITIES as LD_CAPS
        from app.windows.ids import CAPABILITIES
        self.assertIn(Capability.RECORD, CAPABILITIES)
        self.assertNotIn(Capability.RECORD, LD_CAPS)                 # no LDPlayer recorder in this stage
        self.assertEqual(LD_CAPS, frozenset({Capability.SCREENSHOT, Capability.TAP, Capability.KEY, Capability.TEXT}))


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealBackendLifecycle(unittest.TestCase):
    """Installs the real pass-through hooks and removes them at once. It generates and needs NO input; any input the
    user happens to make in these milliseconds is only counted, never altered."""

    def test_hooks_install_and_release_cleanly_and_stop_is_idempotent(self):
        from app.windows.win32 import Win32ObservationBackend
        b = Win32ObservationBackend()
        settings = b.double_click_settings()
        self.assertTrue(settings.time_ms > 0 and settings.dx > 0 and settings.dy > 0)
        b.start(0x12345, lambda o: None, lambda o: None, lambda: None)
        with self.assertRaises(PlatformError):
            b.start(0x12345, lambda o: None, lambda o: None, lambda: None)      # already running
        b.stop()
        b.stop()
        b.start(0x12345, lambda o: None, lambda o: None, lambda: None)          # restartable
        b.stop()

    def test_invalid_window_identifier_is_refused_before_hooking(self):
        from app.windows.win32 import Win32ObservationBackend
        b = Win32ObservationBackend()
        for bad in (0, -1, True, "1", 2 ** 40):
            with self.assertRaises(PlatformError):
                b.start(bad, lambda o: None, lambda o: None, lambda: None)

    def test_stop_reports_a_clean_shutdown_when_the_real_threads_terminate_promptly(self):
        """P-21 baseline: the normal case must keep reporting success, now explicitly (True), not just silently."""
        from app.windows.win32 import Win32ObservationBackend
        b = Win32ObservationBackend()
        b.start(0x12345, lambda o: None, lambda o: None, lambda: None)
        self.assertIs(b.stop(), True)
        self.assertIs(b.stop(), True)                                # idempotent no-op: also reports clean


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class StopSurfacesAnIncompleteShutdown(unittest.TestCase):
    """P-21: Win32ObservationBackend.stop() must not report success when a thread fails to terminate. No real
    hooks are installed here; the hook/dispatch threads are replaced with controllable stand-ins so the abnormal
    case (a thread still alive after its join) is deterministic and does not need a real 3-second wait."""

    class NeverTerminates:
        """Stands in for a thread stop() cannot get to end within its join timeout."""
        name = "stuck"

        def is_alive(self):
            return True

        def join(self, timeout=None):
            pass                                                     # the real join would time out; nothing to do

    class EndsPromptly:
        """Stands in for a thread that ends as soon as it is asked to, like the normal case."""
        name = "prompt"

        def __init__(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            self._alive = False

    def rig(self, hook, dispatch):
        from app.windows.win32 import Win32ObservationBackend
        b = Win32ObservationBackend()
        b._active = True
        b._hook_thread, b._dispatch_thread, b._hook_tid = hook, dispatch, 0    # tid 0: no real thread message posted
        return b

    def test_a_thread_that_outlives_the_join_timeout_is_reported_not_hidden(self):
        b = self.rig(self.EndsPromptly(), self.NeverTerminates())
        self.assertIs(b.stop(), False)

    def test_both_threads_failing_to_terminate_is_also_reported(self):
        b = self.rig(self.NeverTerminates(), self.NeverTerminates())
        self.assertIs(b.stop(), False)

    def test_both_threads_terminating_promptly_still_reports_success(self):
        """The normal path must be unchanged: a clean shutdown still reports True, not just "no exception"."""
        b = self.rig(self.EndsPromptly(), self.EndsPromptly())
        self.assertIs(b.stop(), True)

    def test_a_thread_already_finished_before_stop_is_not_joined_and_still_reports_success(self):
        finished = self.EndsPromptly()
        finished._alive = False                                      # already ended before stop() was called
        b = self.rig(finished, self.EndsPromptly())
        self.assertIs(b.stop(), True)

    def test_stop_called_from_its_own_dispatch_thread_is_not_counted_as_a_failure(self):
        """`stop()` is documented as callable from inside a callback (running on the dispatch thread); that thread
        cannot join itself, and this must not be misreported as an incomplete shutdown."""
        import threading
        result = {}

        def as_the_dispatch_thread():
            b = self.rig(self.EndsPromptly(), threading.current_thread())
            result["clean"] = b.stop()

        t = threading.Thread(target=as_the_dispatch_thread)
        t.start()
        t.join(5)
        self.assertIs(result.get("clean"), True)

    def test_nothing_active_reports_success_without_touching_the_stand_ins(self):
        b = self.rig(self.NeverTerminates(), self.NeverTerminates())
        b._active = False
        self.assertIs(b.stop(), True)


if __name__ == "__main__":
    unittest.main()
