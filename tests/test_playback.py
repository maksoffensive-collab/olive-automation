"""Macro model, preflight, playback engine, Windows adapter, LDPlayer compatibility, isolation.

Deterministic: fake targets, fake input sessions, fake clock/sleep. The only real-time tests (pause / resume) use a
background thread with a few-millisecond tick. No real input anywhere."""
import ast
import inspect
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, TargetNotVerified
from app.playback import engine as engine_module
from app.playback.engine import PlaybackEngine, PlaybackRefused, PlaybackState as S, PlaybackStateError
from app.playback.format import (MacroFormatError, load_macro, macro_from_json, macro_to_json, save_macro)
from app.playback.macro import ActionOp as Op, Macro, compile_actions
from app.playback.preflight import preflight
from app.providers.base import TargetIdentity
from app.providers.input import Key
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.providers.recording_format import recording_to_dict
from app.providers.target import Capability as C, Target, TargetVerification, VerificationState
from app.windows.backend import InputDeliveryError, KeyEvent, MouseEvent
from app.windows.playback import create_window_playback
from app.windows.target import WindowVerifier
from tests.test_target_contract import WindowStyleTarget, ld_target
from tests.test_windows_input import FakeInput as FakeWindowBackend
from tests.test_windows_provider import FakeDesktop, info, setup

ROOT = Path(__file__).resolve().parents[1] / "app"
ALL = (C.TAP, C.SECONDARY_TAP, C.DOUBLE_TAP, C.POINTER_MOVE, C.POINTER_BUTTON, C.KEY, C.TEXT)
PROV, TID = "fake", "t-1"


def down(d, x, y, b=B.PRIMARY, n=1):
    return E(K.BUTTON_DOWN, d, x, y, b, n)


def up(d, x, y, b=B.PRIMARY, n=1):
    return E(K.BUTTON_UP, d, x, y, b, n)


def move(d, x, y):
    return E(K.POINTER_MOVE, d, x, y)


def kd(d, key):
    return E(K.KEY_DOWN, d, key=key)


def ku(d, key):
    return E(K.KEY_UP, d, key=key)


def macro(*events, prov=PROV, tid=TID, name=""):
    return Macro(Recording(prov, tid, tuple(events)), name)


def ops(m):
    return [a.op for a in m.actions]


class FakeTarget(WindowStyleTarget):
    def __init__(self, caps=ALL, target_id=TID, provider_id=PROV, state=VerificationState.VERIFIED):
        super().__init__(target_id, caps, state, provider_id=provider_id)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.now += dt
        if self.on_sleep:
            self.on_sleep(self)


class FakeInput:
    """Structural stand-in for a provider's input session: every neutral operation, records calls."""

    def __init__(self, target_id=TID, clock=None, dry_run=None):
        self.target_id, self.calls, self.clock = target_id, [], clock
        self.on_call = None
        self.raises = {}                    # 1-based call number -> exception
        if dry_run is not None:
            self.dry_run = dry_run

    def _rec(self, name, *args):
        n = len(self.calls) + 1
        if n in self.raises:
            raise self.raises[n]
        self.calls.append((name, args, self.clock.now if self.clock else None))
        if self.on_call:
            self.on_call(self, name)

    tap = lambda self, x, y: self._rec("tap", x, y)                     # noqa: E731
    secondary_tap = lambda self, x, y: self._rec("secondary_tap", x, y)  # noqa: E731
    double_tap = lambda self, x, y: self._rec("double_tap", x, y)        # noqa: E731
    move = lambda self, x, y: self._rec("move", x, y)                    # noqa: E731
    button_down = lambda self, x, y: self._rec("button_down", x, y)      # noqa: E731
    button_up = lambda self, x, y: self._rec("button_up", x, y)          # noqa: E731
    key_press = lambda self, k: self._rec("key_press", k)                # noqa: E731
    key_down = lambda self, k: self._rec("key_down", k)                  # noqa: E731
    key_up = lambda self, k: self._rec("key_up", k)                      # noqa: E731
    type_text = lambda self, t: self._rec("type_text", t)                # noqa: E731

    def names(self):
        return [c[0] for c in self.calls]


class TapOnly:
    def __init__(self, target_id):
        self.target_id, self.calls = target_id, []

    def tap(self, x, y):
        self.calls.append((x, y))


def rig(m, *, caps=ALL, dry_run=False, dry_input=None, **kw):
    clock, stop = Clock(), EmergencyStop()
    target = FakeTarget(caps)
    inp = FakeInput(clock=clock, dry_run=dry_input)
    eng = PlaybackEngine(m, target, inp, stop, dry_run=dry_run, clock=clock, sleep=clock.sleep, **kw)
    return eng, inp, clock, stop, target


SAMPLE = lambda: macro(move(0, 5, 5), down(100, 5, 5), up(40, 5, 5), kd(60, Key.A), ku(30, Key.A),   # noqa: E731
                       E(K.TEXT, 10, text="hi"))


class Compilation(unittest.TestCase):
    def test_click_becomes_one_tap_with_the_delay_of_its_press(self):
        m = macro(move(0, 1, 1), down(120, 10, 20), up(35, 11, 21))
        self.assertEqual([(a.op, a.delay_ms, a.x, a.y) for a in m.actions], [(Op.MOVE, 0, 1, 1), (Op.TAP, 120, 10, 20)])

    def test_secondary_click_and_double_click(self):
        self.assertEqual(ops(macro(down(0, 1, 1, B.SECONDARY), up(9, 1, 1, B.SECONDARY))), [Op.SECONDARY_TAP])
        m = macro(down(0, 5, 5), up(50, 5, 5), down(80, 5, 6, n=2), up(40, 5, 6, n=2))
        self.assertEqual([(a.op, a.delay_ms) for a in m.actions], [(Op.DOUBLE_TAP, 0)])

    def test_press_release_far_apart_or_held_long_is_a_drag_not_a_tap(self):
        self.assertEqual(ops(macro(down(0, 5, 5), up(20, 50, 5))), [Op.BUTTON_DOWN, Op.BUTTON_UP])
        self.assertEqual(ops(macro(down(0, 5, 5), up(2000, 5, 5))), [Op.BUTTON_DOWN, Op.BUTTON_UP])
        self.assertEqual(ops(macro(down(0, 5, 5), move(5, 9, 9), up(5, 9, 9))), [Op.BUTTON_DOWN, Op.MOVE, Op.BUTTON_UP])

    def test_secondary_hold_is_kept_visible_as_an_operation_no_provider_offers(self):
        self.assertEqual(ops(macro(down(0, 5, 5, B.SECONDARY), move(5, 9, 9), up(5, 9, 9, B.SECONDARY))),
                         [Op.SECONDARY_DOWN, Op.MOVE, Op.SECONDARY_UP])

    def test_keys_and_modifier_chord(self):
        m = macro(kd(0, Key.CTRL), kd(10, Key.A), ku(20, Key.A), ku(5, Key.CTRL), kd(9, Key.B), ku(900, Key.B))
        self.assertEqual([(a.op, a.key, a.delay_ms) for a in m.actions],
                         [(Op.KEY_DOWN, Key.CTRL, 0), (Op.KEY_PRESS, Key.A, 10), (Op.KEY_UP, Key.CTRL, 5),
                          (Op.KEY_DOWN, Key.B, 9), (Op.KEY_UP, Key.B, 900)])

    def test_text_and_moves_and_sources_and_duration(self):
        m = SAMPLE()
        self.assertEqual(ops(m), [Op.MOVE, Op.TAP, Op.KEY_PRESS, Op.TYPE_TEXT])
        self.assertEqual([a.source for a in m.actions], [0, 1, 3, 5])
        self.assertEqual(m.duration_ms, 0 + 100 + 60 + 10)
        self.assertEqual(m.actions, compile_actions(m.recording))

    def test_action_descriptions_never_echo_typed_text(self):
        self.assertNotIn("hi", SAMPLE().actions[-1].describe())

    def test_macro_validation(self):
        with self.assertRaises(ValueError):
            Macro("not a recording")
        with self.assertRaises(ValueError):
            macro(name="x" * 101)
        with self.assertRaises(ValueError):
            macro(name="line\nbreak")


class Serialization(unittest.TestCase):
    def test_round_trip_stable_bytes_and_wraps_the_recording_format(self):
        m = macro(*SAMPLE().recording.events, name="demo")
        text = macro_to_json(m)
        self.assertEqual(macro_from_json(text), m)
        self.assertEqual(macro_to_json(macro_from_json(text)), text)
        data = json.loads(text)
        self.assertEqual((data["schema"], data["version"], data["name"]), ("automation-macro", 1, "demo"))
        self.assertEqual(data["recording"], recording_to_dict(m.recording))
        self.assertTrue(text.isascii() and text.endswith("\n"))
        self.assertEqual(macro_from_json(text).actions, m.actions)          # actions are recomputed, never stored

    def test_save_and_load_files(self):
        m = SAMPLE()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.json"
            save_macro(m, path)
            self.assertEqual(load_macro(path), m)
            self.assertEqual(path.read_bytes(), macro_to_json(m).encode())

    def test_loading_is_strict(self):
        good = json.loads(macro_to_json(SAMPLE()))

        def mut(fn):
            d = json.loads(json.dumps(good))
            fn(d)
            return json.dumps(d)
        for text in (mut(lambda d: d.update(version=2)), mut(lambda d: d.update(schema="automation-recording")),
                     mut(lambda d: d.update(extra=1)), mut(lambda d: d.pop("name")), mut(lambda d: d.update(name=5)),
                     mut(lambda d: d["recording"]["events"][0].update(hwnd=1)),
                     mut(lambda d: d["recording"]["events"][1].update(kind="teleport")),
                     mut(lambda d: d["recording"].update(version=9)), "nope", "[]"):
            with self.assertRaises(MacroFormatError, msg=text):
                macro_from_json(text)

    def test_no_platform_data_in_the_file(self):
        text = macro_to_json(SAMPLE()).lower()
        for word in ("hwnd", "vk", "serial", "emulator", "ldplayer", "adb", "0x"):
            self.assertNotIn(word, text)


class Preflight(unittest.TestCase):
    def check(self, m, target=None, inp=None, **kw):
        return preflight(m, target or FakeTarget(), inp, **kw)

    def test_compatible_macro_passes(self):
        r = self.check(SAMPLE(), inp=FakeInput())
        self.assertEqual((r.ok, r.issues), (True, ()))

    def test_all_unsupported_actions_are_reported_not_just_the_first(self):
        r = self.check(SAMPLE(), FakeTarget(caps=(C.TAP,)))
        self.assertFalse(r.ok)
        self.assertEqual([(i.code, i.index) for i in r.errors],
                         [("unsupported_capability", 0), ("unsupported_capability", 2), ("unsupported_capability", 3)])

    def test_secondary_hold_is_always_rejected(self):
        r = self.check(macro(down(0, 5, 5, B.SECONDARY), up(500 + 1, 5, 5, B.SECONDARY)))
        self.assertEqual(r.codes(), ("unsupported_operation", "unsupported_operation", "held_at_end")[:2])

    def test_not_a_macro_target_not_verified_and_not_a_target(self):
        self.assertEqual(preflight(object(), FakeTarget()).codes(), ("not_a_macro",))
        self.assertIn("target_not_verified", self.check(SAMPLE(), FakeTarget(state=VerificationState.UNVERIFIED)).codes())
        self.assertIn("target_not_verified", self.check(SAMPLE(), object()).codes())

    def test_wrong_provider_and_identity_mismatch_and_explicit_rebinding(self):
        self.assertEqual(self.check(SAMPLE(), FakeTarget(provider_id="other")).codes(), ("wrong_provider",))
        self.assertEqual(self.check(SAMPLE(), FakeTarget(target_id="t-2")).codes(), ("target_mismatch",))
        self.assertTrue(self.check(SAMPLE(), FakeTarget(target_id="t-2"), allow_different_target=True).ok)
        self.assertFalse(self.check(SAMPLE(), FakeTarget(provider_id="other"), allow_different_target=True).ok)

    def test_invalid_coordinates_are_rejected_not_repaired(self):
        r = self.check(macro(move(0, -3, 4)))
        self.assertEqual([(i.code, i.index) for i in r.errors], [("invalid_coordinates", 0)])

    def test_empty_macro(self):
        self.assertEqual(self.check(macro()).codes(), ("empty_macro",))

    def test_sequence_rules_mirror_the_providers(self):
        cases = [(macro(down(0, 1, 1), move(600, 40, 40), down(0, 2, 2)), "button_already_held"),
                 (macro(move(0, 1, 1), up(0, 1, 1)), "button_not_held"),
                 (macro(down(0, 1, 1), move(600, 40, 40), up(5, 40, 40), kd(0, Key.A), ku(600, Key.A)), None),
                 (macro(kd(0, Key.A), kd(1, Key.A)), "key_already_held"),
                 (macro(ku(0, Key.A)), "key_not_held"),
                 (macro(kd(0, Key.A), ku(600, Key.A)), None)]
        for m, code in cases:
            codes = [i.code for i in self.check(m).errors]
            self.assertEqual(codes[:1], [code] if code else [], (code, codes))

    def test_click_while_button_held_and_key_press_of_held_key(self):
        held = macro(down(0, 1, 1), move(600, 30, 30), down(600, 4, 4), up(600, 40, 40))
        self.assertIn("button_already_held", self.check(held).codes())
        m = macro(kd(0, Key.A), move(600, 1, 1), kd(0, Key.A))
        self.assertIn("key_already_held", self.check(m).codes())

    def test_held_at_end_is_only_a_warning(self):
        r = self.check(macro(kd(0, Key.SHIFT)))
        self.assertEqual((r.ok, [i.code for i in r.warnings]), (True, ["held_at_end"]))

    def test_input_binding_and_missing_operations(self):
        self.assertIn("input_bound_to_other_target", self.check(SAMPLE(), inp=FakeInput("t-9")).codes())
        r = self.check(macro(down(0, 1, 1), up(1, 1, 1)), inp=object.__new__(type("NoTap", (), {"target_id": TID})))
        self.assertIn("provider_lacks_operation", r.codes())


class Lifecycle(unittest.TestCase):
    def test_idle_run_complete_and_report(self):
        eng, inp, clock, *_ = rig(SAMPLE())
        self.assertEqual((eng.state, eng.current_index, eng.completed_count), (S.IDLE, 0, 0))
        rep = eng.run()
        self.assertEqual((rep.state, rep.actions_total, rep.completed_count, rep.current_index, rep.failure),
                         (S.COMPLETED, 4, 4, 4, None))
        self.assertEqual(inp.names(), ["move", "tap", "key_press", "type_text"])
        self.assertEqual([s.index for s in rep.steps], [0, 1, 2, 3])
        self.assertEqual(rep.held_at_end, ())

    def test_start_twice_and_restart_after_finish_are_rejected(self):
        eng, inp, *_ = rig(SAMPLE())
        eng.run()
        with self.assertRaises(PlaybackStateError):
            eng.run()
        with self.assertRaises(PlaybackStateError):
            eng.start_in_background()
        self.assertEqual(len(inp.calls), 4)

    def test_cancel_pause_resume_when_idle_or_finished_are_deterministic(self):
        eng, *_ = rig(SAMPLE())
        for op in (eng.cancel, eng.pause, eng.resume):
            with self.assertRaises(PlaybackStateError):
                op()
        eng.run()
        for op in (eng.cancel, eng.pause, eng.resume):
            with self.assertRaises(PlaybackStateError):
                op()
        self.assertEqual(eng.state, S.COMPLETED)

    def test_invalid_tick(self):
        with self.assertRaises(ValueError):
            PlaybackEngine(SAMPLE(), FakeTarget(), FakeInput(), EmergencyStop(), tick_s=0)


class Binding(unittest.TestCase):
    def refused(self, eng):
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertEqual(eng.state, S.IDLE)
        return [i.code for i in cm.exception.issues]

    def test_bound_to_the_explicit_target_and_its_session_only(self):
        eng, inp, *_ = rig(SAMPLE())
        eng.run()
        self.assertEqual(len(inp.calls), 4)

    def test_unverified_wrong_provider_and_mismatched_targets_send_nothing(self):
        for kw, code in ((dict(state=VerificationState.UNVERIFIED), "target_not_verified"),
                         (dict(provider_id="other"), "wrong_provider"), (dict(target_id="t-2"), "target_mismatch")):
            inp = FakeInput()
            eng = PlaybackEngine(SAMPLE(), FakeTarget(**kw), inp, EmergencyStop())
            self.assertIn(code, self.refused(eng))
            self.assertEqual(inp.calls, [])

    def test_input_session_of_another_target_is_refused(self):
        inp = FakeInput("t-9")
        eng = PlaybackEngine(SAMPLE(), FakeTarget(), inp, EmergencyStop())
        self.assertIn("input_bound_to_other_target", self.refused(eng))
        self.assertEqual(inp.calls, [])

    def test_live_run_needs_an_input_and_refuses_a_dry_run_session(self):
        self.assertIn("no_input", self.refused(PlaybackEngine(SAMPLE(), FakeTarget(), None, EmergencyStop())))
        inp = FakeInput(dry_run=True)
        self.assertIn("input_is_dry_run", self.refused(PlaybackEngine(SAMPLE(), FakeTarget(), inp, EmergencyStop())))
        self.assertEqual(inp.calls, [])

    def test_explicit_rebinding_to_another_target_of_the_same_provider(self):
        clock, inp = Clock(), FakeInput("t-2")
        eng = PlaybackEngine(SAMPLE(), FakeTarget(target_id="t-2"), inp, EmergencyStop(), clock=clock,
                             sleep=clock.sleep, allow_different_target=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual(len(inp.calls), 4)

    def test_target_invalidation_mid_run_fails_without_switching_or_further_input(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.raises[2] = TargetNotVerified("The window does not exist (any more).", reason_code="window_gone")
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.failure.action_index, rep.completed_count, rep.current_index),
                         (S.FAILED, "window_gone", 1, 1, 1))
        self.assertEqual(inp.names(), ["move"])                      # nothing after the invalidation, no retargeting

    def test_input_rebound_to_another_target_between_actions_fails(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.on_call = lambda i, name: setattr(i, "target_id", "t-9")
        rep = eng.run()
        self.assertEqual((rep.state, rep.completed_count, rep.failure.action_index), (S.FAILED, 1, 1))
        self.assertEqual(len(inp.calls), 1)


class PreflightGuaranteesZeroInput(unittest.TestCase):
    def test_one_unsupported_action_rejects_everything_before_any_input(self):
        eng, inp, clock, *_ = rig(SAMPLE(), caps=(C.TAP, C.POINTER_MOVE, C.KEY))     # no TEXT
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertEqual([(i.code, i.index) for i in cm.exception.issues], [("unsupported_capability", 3)])
        self.assertEqual((inp.calls, clock.sleeps, eng.state), ([], [], S.IDLE))

    def test_malformed_and_invalid_coordinate_macros_send_nothing(self):
        eng, inp, *_ = rig(macro(move(0, 1, 1), move(10, -1, 5)))
        with self.assertRaises(PlaybackRefused):
            eng.run()
        self.assertEqual(inp.calls, [])
        eng2 = PlaybackEngine("junk", FakeTarget(), FakeInput(), EmergencyStop())
        with self.assertRaises(PlaybackRefused):
            eng2.run()


class Execution(unittest.TestCase):
    def test_order_target_arguments_and_delays_use_the_fake_clock(self):
        m = macro(move(0, 5, 6), down(100, 7, 8), up(40, 7, 8), kd(60, Key.A), ku(30, Key.A), E(K.TEXT, 10, text="hi"))
        eng, inp, clock, *_ = rig(m)
        eng.run()
        self.assertEqual([(n, a) for n, a, _ in inp.calls],
                         [("move", (5, 6)), ("tap", (7, 8)), ("key_press", (Key.A,)), ("type_text", ("hi",))])
        self.assertEqual([round(t, 3) for _, _, t in inp.calls], [0.0, 0.1, 0.16, 0.17])
        self.assertAlmostEqual(sum(clock.sleeps), 0.17)
        self.assertTrue(all(dt <= 0.05 + 1e-9 for dt in clock.sleeps))    # sliced, never one long uninterruptible sleep

    def test_late_actions_do_not_accumulate_delay(self):
        m = macro(move(0, 1, 1), move(100, 2, 2), move(100, 3, 3))
        eng, inp, clock, *_ = rig(m)
        inp.on_call = lambda i, name: setattr(clock, "now", clock.now + 0.08)    # each action "takes" 80 ms
        eng.run()
        self.assertEqual([round(t, 3) for _, _, t in inp.calls], [0.0, 0.1, 0.2])   # scheduled 100 ms apart

    def test_failure_stops_later_actions_and_keeps_partial_execution_visible(self):
        m = macro(*[move(10 if i > 1 else 0, i, i) for i in range(1, 10)])
        eng, inp, *_ = rig(m)
        inp.raises[8] = RuntimeError("boom")
        rep = eng.run()
        self.assertEqual((rep.state, rep.completed_count, rep.current_index, rep.actions_total), (S.FAILED, 7, 7, 9))
        self.assertEqual((rep.failure.code, rep.failure.message, rep.failure.action_index), ("RuntimeError", "boom", 7))
        self.assertEqual(len(inp.calls), 7)
        self.assertEqual([s.index for s in rep.steps], list(range(7)))
        self.assertEqual((eng.state, eng.failure), (S.FAILED, rep.failure))

    def test_the_very_first_action_failing_completes_nothing(self):
        m = macro(move(0, 1, 1), move(10, 2, 2), move(10, 3, 3))
        eng, inp, *_ = rig(m)
        inp.raises[1] = RuntimeError("boom")                        # 1-based: the very first call
        rep = eng.run()
        self.assertEqual((rep.state, rep.completed_count, rep.current_index), (S.FAILED, 0, 0))
        self.assertEqual(inp.calls, [])                             # nothing is recorded as completed
        self.assertEqual(rep.steps, ())

    def test_the_very_last_action_failing_still_reports_every_earlier_action_completed(self):
        m = macro(move(0, 1, 1), move(10, 2, 2), move(10, 3, 3))
        eng, inp, *_ = rig(m)
        inp.raises[3] = RuntimeError("boom")                        # 1-based: the third and final call
        rep = eng.run()
        self.assertEqual((rep.state, rep.completed_count, rep.current_index, rep.actions_total), (S.FAILED, 2, 2, 3))
        self.assertEqual(len(inp.calls), 2)                         # the two earlier actions were genuinely sent
        self.assertEqual([s.index for s in rep.steps], [0, 1])

    def test_partial_delivery_reported_by_the_provider_is_preserved(self):
        eng, inp, *_ = rig(SAMPLE())
        err = InputDeliveryError("stopped after 1 of 2")
        err.delivered, err.total = 1, 2
        inp.raises[2] = err
        rep = eng.run()
        self.assertEqual((rep.failure.delivered, rep.failure.total, rep.failure.action_index), (1, 2, 1))

    def test_provider_refusal_at_execution_time_fails_the_run(self):
        from app.windows.input import InputRefused
        eng, inp, *_ = rig(SAMPLE())
        inp.raises[1] = InputRefused("outside the client area; nothing sent.")
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.completed_count), (S.FAILED, "InputRefused", 0))

    def test_emergency_stop_before_start_of_run_cancels_before_any_input(self):
        eng, inp, _, stop, _ = rig(SAMPLE())
        stop.trigger("panic")
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason, inp.calls), (S.CANCELLED, "emergency_stop", []))

    def test_emergency_stop_between_actions_and_during_a_delay(self):
        eng, inp, _, stop, _ = rig(SAMPLE())
        inp.on_call = lambda i, name: stop.trigger()
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason, len(inp.calls), rep.current_index), (S.CANCELLED, "emergency_stop", 1, 1))
        eng2, inp2, clock2, stop2, _ = rig(SAMPLE())
        clock2.on_sleep = lambda c: stop2.trigger()
        rep2 = eng2.run()
        self.assertEqual((rep2.state, len(inp2.calls), rep2.completed_count), (S.CANCELLED, 1, 1))   # nothing sent after
        self.assertLess(len(clock2.sleeps), 3)                                                        # woke promptly

    def test_provider_raising_the_stop_error_is_a_cancel_not_a_failure(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.raises[2] = EmergencyStopped("Emergency stop is active")
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason, rep.failure), (S.CANCELLED, "emergency_stop", None))

    def test_cancellation_during_a_delay_prevents_the_next_action(self):
        eng, inp, clock, *_ = rig(SAMPLE())
        clock.on_sleep = lambda c: eng.cancel("user_cancel")
        rep = eng.run()
        self.assertEqual((rep.state, rep.cancel_reason, inp.names()), (S.CANCELLED, "user_cancel", ["move"]))
        with self.assertRaises(PlaybackStateError):
            eng.cancel()

    def test_no_input_after_cancellation_or_failure(self):
        eng, inp, *_ = rig(SAMPLE())
        inp.on_call = lambda i, name: eng.cancel()
        eng.run()
        self.assertEqual(len(inp.calls), 1)
        self.assertEqual(eng.state, S.CANCELLED)

    def test_an_input_that_cannot_let_go_is_left_as_it_is_and_the_report_says_what_is_held(self):
        """FakeInput offers no `release_held` (like a provider that can hold nothing): nothing is invented for it."""
        m = macro(down(0, 5, 5), move(600, 40, 40), kd(10, Key.SHIFT), move(10, 9, 9))
        eng, inp, *_ = rig(m)
        inp.on_call = lambda i, name: eng.cancel() if name == "key_down" else None
        rep = eng.run()
        self.assertEqual(inp.names(), ["button_down", "move", "key_down"])       # no synthetic releases
        self.assertEqual((rep.held_at_end, rep.released_at_end), (("key:shift", "primary_button"), ()))


class LetsGo(FakeInput):
    """An input session that can let go of what it holds, as the Windows sessions can (`release_held`)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.holding, self.releases, self.engine, self.fail = [], [], None, False

    def _rec(self, name, *args):
        super()._rec(name, *args)
        if name in ("button_down", "button_up"):
            item = "primary_button"
        elif name in ("key_down", "key_up"):
            item = f"key:{args[0].value}"
        else:
            return
        if name.endswith("_down"):
            self.holding.append(item)
        else:
            self.holding.remove(item)

    def release_held(self):
        self.releases.append(self.engine.state if self.engine is not None else None)
        if self.fail:
            raise InputDeliveryError("the system refused")
        let_go, self.holding = tuple(reversed(self.holding)), []
        return let_go

    @property
    def still_held(self):
        return tuple(self.holding)


def letting_go_rig(m, *, dry_run=False, dry_input=None):
    clock, stop = Clock(), EmergencyStop()
    inp = LetsGo(clock=clock, dry_run=dry_input)
    eng = PlaybackEngine(m, FakeTarget(), inp, stop, dry_run=dry_run, clock=clock, sleep=clock.sleep)
    inp.engine = eng
    return eng, inp, stop


class LettingGo(unittest.TestCase):
    """The person's decision of 2026-10-20, in place of P-04: a LIVE run that ends with a button or key still held
    -- stopped, failed, or recorded so -- has its input session let go of exactly that, before the run is seen to
    end. Nothing is invented for a session that cannot let go; a dry run lets go of nothing."""

    @staticmethod
    def drag():
        return macro(kd(0, Key.SHIFT), down(10, 5, 5), move(10, 9, 9))

    def test_a_run_that_ends_holding_something_lets_go_of_it_before_it_is_seen_to_end(self):
        eng, inp, _ = letting_go_rig(self.drag())
        rep = eng.run()
        self.assertEqual(rep.state, S.COMPLETED)
        self.assertEqual(inp.releases, [S.RUNNING])                       # once, while still under way
        self.assertEqual((rep.released_at_end, rep.held_at_end), (("primary_button", "key:shift"), ()))

    def test_a_stopped_cancelled_or_failed_run_lets_go_too_even_after_the_emergency_stop(self):
        def stopped(eng, inp, stop):
            inp.on_call = lambda i, name: stop.trigger("test") if name == "button_down" else None

        def cancelled(eng, inp, stop):
            inp.on_call = lambda i, name: eng.cancel("user_cancel") if name == "button_down" else None

        def failed(eng, inp, stop):
            inp.raises[3] = InputDeliveryError("refused")                 # the move after the press

        for name, arrange, state in (("stopped", stopped, S.CANCELLED), ("cancelled", cancelled, S.CANCELLED),
                                     ("failed", failed, S.FAILED)):
            with self.subTest(name):
                eng, inp, stop = letting_go_rig(self.drag())
                arrange(eng, inp, stop)
                rep = eng.run()
                self.assertEqual(rep.state, state)
                self.assertEqual(inp.names(), ["key_down", "button_down"])  # nothing more of the macro
                self.assertEqual(inp.releases, [S.RUNNING])
                self.assertEqual((rep.released_at_end, rep.held_at_end), (("primary_button", "key:shift"), ()))

    def test_a_dry_run_or_a_run_that_let_go_itself_lets_go_of_nothing(self):
        eng, inp, _ = letting_go_rig(self.drag(), dry_run=True, dry_input=True)
        rep = eng.run()
        self.assertEqual((inp.releases, rep.released_at_end), ([], ()))
        eng, inp, _ = letting_go_rig(SAMPLE())
        rep = eng.run()                                                   # the session is asked; it holds nothing
        self.assertEqual((rep.state, rep.released_at_end, rep.held_at_end), (S.COMPLETED, (), ()))

    def test_what_the_session_holds_without_the_run_knowing_is_let_go_too(self):
        """A click cut off between its press and its release: the engine never saw a button held, the session did."""
        eng, inp, _ = letting_go_rig(macro(down(0, 5, 5), up(40, 5, 5)))

        def cut_off(i, name):
            if name == "tap":
                i.holding.append("primary_button")
                raise InputDeliveryError("the release was refused")
        inp.on_call = cut_off
        rep = eng.run()
        self.assertEqual((rep.state, rep.released_at_end), (S.FAILED, ("primary_button",)))

    def test_what_the_session_could_not_let_go_of_is_said_even_when_the_run_never_saw_it_held(self):
        """A click cut off between its press and its release, and the release at the end refused too: the engine
        never saw a button held, the session did -- so the report says it is still down (`still_held`)."""
        eng, inp, _ = letting_go_rig(macro(down(0, 5, 5), up(40, 5, 5)))

        def cut_off(i, name):
            if name == "tap":
                i.holding.append("primary_button")
                raise InputDeliveryError("the release was refused")
        inp.on_call = cut_off
        inp.fail = True
        rep = eng.run()
        self.assertEqual((rep.state, rep.released_at_end, rep.held_at_end), (S.FAILED, (), ("primary_button",)))

    def test_a_release_that_fails_changes_nothing_about_the_run(self):
        eng, inp, _ = letting_go_rig(self.drag())
        inp.fail = True
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure), (S.COMPLETED, None))
        self.assertEqual((rep.released_at_end, rep.held_at_end), ((), ("key:shift", "primary_button")))


class DryRun(unittest.TestCase):
    def test_plan_walk_without_any_input_or_real_waiting(self):
        eng, inp, clock, *_ = rig(SAMPLE(), dry_run=True)
        rep = eng.run()
        self.assertEqual((rep.state, rep.dry_run, rep.completed_count, rep.actions_total), (S.COMPLETED, True, 4, 4))
        self.assertEqual((inp.calls, clock.sleeps), ([], []))
        self.assertEqual([s.op for s in rep.steps], ["move", "tap", "key_press", "type_text"])

    def test_dry_run_still_validates_binding_capabilities_and_macro(self):
        for kw in (dict(caps=(C.TAP,)),):
            eng, inp, *_ = rig(SAMPLE(), dry_run=True, **kw)
            with self.assertRaises(PlaybackRefused):
                eng.run()
        eng2 = PlaybackEngine(SAMPLE(), FakeTarget(target_id="t-2"), None, EmergencyStop(), dry_run=True)
        with self.assertRaises(PlaybackRefused):
            eng2.run()

    def test_dry_run_needs_no_input_and_ignores_a_live_looking_input(self):
        eng = PlaybackEngine(SAMPLE(), FakeTarget(), None, EmergencyStop(), dry_run=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        live = FakeInput()
        eng2 = PlaybackEngine(SAMPLE(), FakeTarget(), live, EmergencyStop(), dry_run=True)
        eng2.run()
        self.assertEqual(live.calls, [])                          # a non-dry input is never touched in a dry run

    def test_dry_run_goes_through_a_provider_dry_run_session_so_its_gates_run(self):
        eng, inp, clock, *_ = rig(SAMPLE(), dry_run=True, dry_input=True)
        eng.run()
        self.assertEqual(inp.names(), ["move", "tap", "key_press", "type_text"])   # a dry session records intent only
        self.assertEqual(clock.sleeps, [])
        eng2, inp2, *_ = rig(SAMPLE(), dry_run=True, dry_input=True)
        inp2.raises[2] = TargetNotVerified("gone", reason_code="window_gone")
        rep = eng2.run()
        self.assertEqual((rep.state, rep.failure.code, rep.completed_count), (S.FAILED, "window_gone", 1))

    def test_emergency_stop_applies_to_a_dry_run_too(self):
        eng, inp, _, stop, _ = rig(SAMPLE(), dry_run=True)
        stop.trigger()
        self.assertEqual(eng.run().state, S.CANCELLED)


class PauseResume(unittest.TestCase):
    def wait_for(self, cond, what):
        end = time.monotonic() + 5
        while not cond():
            if time.monotonic() > end:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.002)

    def test_pause_takes_effect_between_actions_and_resume_continues(self):
        m = macro(*[move(0, i, i) for i in range(1, 6)])
        eng, inp, clock, *_ = rig(m, tick_s=0.005)
        gate = threading.Event()

        def on_call(i, name):
            if len(i.calls) == 2:
                eng.pause()
                self.assertTrue(eng.pause_pending)
        inp.on_call = on_call
        eng.start_in_background()
        self.wait_for(lambda: eng.state is S.PAUSED, "pause")
        self.assertEqual((len(inp.calls), eng.completed_count, eng.current_index), (2, 2, 2))
        time.sleep(0.03)
        self.assertEqual(len(inp.calls), 2)                     # nothing is sent while paused
        with self.assertRaises(PlaybackStateError):
            eng.pause()
        eng.resume()
        self.assertTrue(eng.wait(5))
        self.assertEqual((eng.state, len(inp.calls)), (S.COMPLETED, 5))
        del gate

    def test_pause_is_deferred_while_a_button_or_key_is_held(self):
        m = macro(kd(0, Key.A), move(600, 1, 1), ku(0, Key.A), move(700, 2, 2), move(0, 3, 3))
        eng, inp, *_ = rig(m, tick_s=0.005)
        inp.on_call = lambda i, name: eng.pause() if len(i.calls) == 1 else None      # requested while A is held
        eng.start_in_background()
        self.wait_for(lambda: eng.state is S.PAUSED, "pause")
        self.assertEqual(inp.names(), ["key_down", "move", "key_up"])                # only after the key was released
        eng.resume()
        self.assertTrue(eng.wait(5))
        self.assertEqual(eng.state, S.COMPLETED)

    def test_cancel_while_paused_and_resume_of_a_pending_pause(self):
        m = macro(*[move(0, i, i) for i in range(1, 4)])
        eng, inp, *_ = rig(m, tick_s=0.005)
        inp.on_call = lambda i, name: eng.pause() if len(i.calls) == 1 else None
        eng.start_in_background()
        self.wait_for(lambda: eng.state is S.PAUSED, "pause")
        eng.cancel("stop_it")
        self.assertTrue(eng.wait(5))
        self.assertEqual((eng.state, eng.report().cancel_reason, len(inp.calls)), (S.CANCELLED, "stop_it", 1))

    def test_emergency_stop_while_paused_cancels(self):
        m = macro(*[move(0, i, i) for i in range(1, 4)])
        eng, inp, _, stop, _ = rig(m, tick_s=0.005)
        inp.on_call = lambda i, name: eng.pause() if len(i.calls) == 1 else None
        eng.start_in_background()
        self.wait_for(lambda: eng.state is S.PAUSED, "pause")
        stop.trigger()
        self.assertTrue(eng.wait(5))
        self.assertEqual((eng.state, eng.report().cancel_reason), (S.CANCELLED, "emergency_stop"))


class WindowsAdapter(unittest.TestCase):
    def build(self, m, *, dry_run=False, size=(60, 40), **kw):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        backend = FakeWindowBackend(desk, size)
        clock = Clock()
        eng = create_window_playback(m, target, verifier, backend, stop, dry_run=dry_run, clock=clock,
                                     sleep=clock.sleep, **kw)
        return eng, backend, desk, stop, target

    def recorded_for(self, target, *events):
        return Macro(Recording(target.provider_id, target.target_id, tuple(events)))

    def make(self, *events, **kw):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        m = self.recorded_for(target, *events)
        backend = FakeWindowBackend(desk, kw.pop("size", (60, 40)))
        clock = Clock()
        eng = create_window_playback(m, target, verifier, backend, stop, clock=clock, sleep=clock.sleep, **kw)
        return eng, backend, desk, stop

    def test_reuses_the_window_input_session_and_sends_the_expected_messages_to_the_window_only(self):
        from app.windows.input import WindowInputSession
        eng, backend, *_ = self.make(move(0, 5, 6), down(50, 10, 20), up(20, 10, 20), kd(30, Key.A), ku(10, Key.A),
                                     E(K.TEXT, 5, text="Hi"))
        self.assertIsInstance(eng._input, WindowInputSession)
        self.assertEqual(eng.run().state, S.COMPLETED)
        M, K_ = MouseEvent, KeyEvent
        self.assertEqual(backend.events, [("mouse", 100, M.MOVE, 5, 6), ("mouse", 100, M.LEFT_DOWN, 10, 20),
                                          ("mouse", 100, M.LEFT_UP, 10, 20), ("key", 100, K_.DOWN, Key.A),
                                          ("key", 100, K_.UP, Key.A), ("char", 100, "H"), ("char", 100, "i")])

    def test_double_and_secondary_clicks_and_drag(self):
        eng, backend, *_ = self.make(down(0, 5, 5, B.SECONDARY), up(9, 5, 5, B.SECONDARY),
                                     down(50, 8, 8), up(20, 8, 8), down(60, 8, 8, n=2), up(20, 8, 8, n=2),
                                     down(50, 9, 9), move(600, 30, 30), up(600, 30, 30))
        self.assertEqual(eng.run().state, S.COMPLETED)
        kinds = [e[2] for e in backend.events]
        M = MouseEvent
        self.assertEqual(kinds, [M.RIGHT_DOWN, M.RIGHT_UP, M.LEFT_DOWN, M.LEFT_UP, M.LEFT_DOUBLE, M.LEFT_UP,
                                 M.LEFT_DOWN, M.MOVE_LEFT_HELD, M.LEFT_UP])

    def test_dry_run_sends_zero_messages_and_uses_the_session_dry_run(self):
        eng, backend, *_ = self.make(move(0, 5, 6), down(50, 10, 20), up(20, 10, 20), kd(30, Key.A), ku(10, Key.A))
        dry = create_window_playback(eng._macro, eng._target, eng._input._verifier, backend, eng._stop, dry_run=True)
        rep = dry.run()
        self.assertEqual((rep.state, rep.completed_count, backend.events), (S.COMPLETED, 3, []))
        self.assertEqual([i.action for i in dry._input.intended], ["move", "tap", "key_press"])

    def test_target_becoming_invalid_mid_run_fails_and_sends_no_more(self):
        eng, backend, desk, *_ = self.make(move(0, 1, 1), move(10, 2, 2), move(10, 3, 3))
        backend.on_event = lambda b: desk.windows.pop(100, None)
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.completed_count), (S.FAILED, "window_gone", 1))
        self.assertEqual(len(backend.events), 1)

    def test_out_of_bounds_point_fails_at_that_action_with_nothing_sent_for_it(self):
        eng, backend, *_ = self.make(move(0, 1, 1), move(10, 500, 1), move(10, 3, 3))
        rep = eng.run()
        self.assertEqual((rep.state, rep.failure.code, rep.failure.action_index), (S.FAILED, "InputRefused", 1))
        self.assertEqual(len(backend.events), 1)

    def test_emergency_stop_blocks_all_input(self):
        eng, backend, _, stop = self.make(move(0, 1, 1))
        stop.trigger()
        self.assertEqual(eng.run().state, S.CANCELLED)
        self.assertEqual(backend.events, [])

    def test_window_session_budget_equals_the_action_count(self):
        eng, backend, *_ = self.make(move(0, 1, 1), move(5, 2, 2))
        self.assertEqual(eng._input._budget, 2)
        eng.run()
        self.assertEqual(eng._input.actions_sent, 2)

    def test_macro_for_another_window_needs_explicit_rebinding_and_a_provider_match(self):
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        other = Macro(Recording(target.provider_id, "win-somewhere-else", (move(0, 1, 1),)))
        backend = FakeWindowBackend(desk)
        with self.assertRaises(PlaybackRefused):
            create_window_playback(other, target, verifier, backend, stop).run()
        self.assertEqual(backend.events, [])
        eng = create_window_playback(other, target, verifier, backend, stop, allow_different_target=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        foreign = Macro(Recording("ldplayer", target.target_id, (move(0, 1, 1),)))
        with self.assertRaises(PlaybackRefused):
            create_window_playback(foreign, target, verifier, backend, stop, allow_different_target=True).run()

    def test_more_actions_than_the_session_allows_is_refused_up_front(self):
        from app.windows.input import MAX_SESSION_ACTIONS
        desk, stop, verifier = setup(info())
        target = verifier.verify(100)
        n = MAX_SESSION_ACTIONS + 1
        big = Macro(Recording(target.provider_id, target.target_id, tuple(move(0 if i == 0 else 1, i % 50, 1) for i in range(n))))
        backend = FakeWindowBackend(desk)
        with self.assertRaises(PlaybackRefused) as cm:                 # a typed preflight refusal, not a bare ValueError
            create_window_playback(big, target, verifier, backend, stop).run()
        self.assertIn("too_many_actions", [i.code for i in cm.exception.issues])
        self.assertEqual(backend.events, [])


class LDPlayerCompatibility(unittest.TestCase):
    def test_tap_only_macro_is_compatible_with_the_existing_tap_only_target(self):
        target, _ = ld_target()
        self.assertEqual(target.capabilities, frozenset({C.SCREENSHOT, C.TAP, C.KEY, C.TEXT}))
        m = Macro(Recording(target.provider_id, target.target_id, (down(0, 100, 200), up(40, 100, 200), down(500, 5, 5), up(30, 5, 5))))
        self.assertEqual(ops(m), [Op.TAP, Op.TAP])
        inp = TapOnly(target.target_id)
        self.assertTrue(preflight(m, target, inp).ok)
        clock = Clock()
        rep = PlaybackEngine(m, target, inp, EmergencyStop(), clock=clock, sleep=clock.sleep).run()
        self.assertEqual((rep.state, inp.calls), (S.COMPLETED, [(100, 200), (5, 5)]))

    def test_keyboard_and_mouse_actions_fail_preflight_before_any_input(self):
        target, _ = ld_target()
        m = Macro(Recording(target.provider_id, target.target_id,
                            (down(0, 1, 1), up(5, 1, 1), move(10, 2, 2), kd(10, Key.A), ku(10, Key.A),
                             down(10, 3, 3), move(600, 40, 40), up(10, 40, 40))))
        inp = TapOnly(target.target_id)
        report = preflight(m, target, inp)
        self.assertFalse(report.ok)
        self.assertEqual({i.code for i in report.errors}, {"unsupported_capability", "provider_lacks_operation"})
        with self.assertRaises(PlaybackRefused):
            PlaybackEngine(m, target, inp, EmergencyStop()).run()
        self.assertEqual(inp.calls, [])

    def test_a_windows_macro_cannot_be_bound_to_an_ldplayer_target(self):
        target, _ = ld_target()
        m = macro(down(0, 1, 1), up(5, 1, 1), prov="windows", tid="win-1")
        self.assertIn("wrong_provider", preflight(m, target, allow_different_target=True).codes())


def imports_of(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class Isolation(unittest.TestCase):
    def test_generic_playback_imports_only_core_and_providers(self):
        for path in (ROOT / "playback").glob("*.py"):
            for m in imports_of(path):
                if m.startswith("app."):
                    self.assertIn(m.split(".")[1], ("core", "providers", "playback"), f"{path.name}: {m}")

    def test_generic_playback_has_no_provider_or_platform_vocabulary_or_type_branching(self):
        for path in (ROOT / "playback").glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in ("hwnd", "windows", "win32", "vk_", "virtual-key", "adb", "ldplayer", "emulator", "serial",
                         "windowtarget", "verifiedtarget", "foreground", "getcursor", "sendinput", "postmessage"):
                if word == "foreground" and path.name in ("engine.py", "preflight.py"):
                    continue                           # only in the sentence that says there is no foreground fallback
                self.assertNotIn(word, text, f"{path.name}: {word}")
        src = inspect.getsource(engine_module)
        self.assertNotIn("isinstance(self._target", src)

    def test_the_only_foreground_mention_is_the_documented_absence(self):
        for path in (ROOT / "playback").glob("*.py"):
            for line in path.read_text(encoding="utf-8").lower().splitlines():
                if "foreground" in line:
                    self.assertIn("no foreground", line)

    def test_each_providers_adapter_lives_in_its_own_package_and_no_other_technology_imports_it(self):
        """Exactly one file per provider binds the generic engine to that provider's real input session
        (app/windows/playback.py, app/ldplayer/playback.py); no other technology package may import either
        adapter or the generic engine/macro directly, so a provider's playback wiring cannot leak sideways."""
        self.assertTrue((ROOT / "windows" / "playback.py").is_file())
        self.assertTrue((ROOT / "ldplayer" / "playback.py").is_file())
        for path in ROOT.rglob("*.py"):
            top = path.relative_to(ROOT).parts[0]
            if top == "bootstrap":                                              # composition root
                continue
            if top == "windows" and path.name == "playback.py":
                continue
            if top == "ldplayer" and path.name == "playback.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("app.windows.playback", text, str(path))
            self.assertNotIn("app.ldplayer.playback", text, str(path))
        for pkg, own_adapter in (("adb", None), ("ldplayer", "playback.py"), ("instances", None)):
            for path in (ROOT / pkg).glob("*.py"):
                if path.name == own_adapter:
                    continue
                self.assertFalse({m for m in imports_of(path) if m.startswith("app.playback")}, path.name)

    def test_adapter_adds_no_second_input_mechanism(self):
        text = (ROOT / "windows" / "playback.py").read_text(encoding="utf-8")
        self.assertIn("WindowInputSession", text)
        for word in ("ctypes", "PostMessage", "SendInput", "post_mouse", "post_key", "post_char", "SetCursorPos",
                     "GetForegroundWindow", "SetForegroundWindow"):
            self.assertNotIn(word, text)

    def test_ldplayer_adapter_adds_no_second_input_mechanism(self):
        text = (ROOT / "ldplayer" / "playback.py").read_text(encoding="utf-8")
        self.assertIn("InputSession", text)
        for word in ("subprocess", "Popen", "adb.exe", "shell_query", "input tap", "CommandRunner"):
            self.assertNotIn(word, text)

    def test_serialization_carries_no_provider_types_and_engine_is_technology_free_in_a_clean_interpreter(self):
        code = textwrap.dedent("""
            import sys
            import app.playback.engine, app.playback.format, app.playback.preflight
            bad = [m for m in sys.modules if m.split(".")[:2] in (["app","adb"],["app","ldplayer"],["app","instances"],["app","windows"],["app","macros"])]
            assert not bad, bad
            print("CLEAN")
        """)
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "CLEAN", r.stderr)

    def test_no_fallback_target_selection_exists_in_the_engine(self):
        src = inspect.getsource(engine_module)
        for word in ("find_window", "enum_windows", "all_targets", "GetForeground", "for target in"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
