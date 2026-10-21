"""Windows input session: pointer, keyboard and text operations, all safety gates. Fake OS input only.

Nothing here touches the desktop: `FakeInput` records the messages that would have been posted.
"""
import ast
import inspect
import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import CapabilityNotAvailable, EmergencyStopped, TargetNotVerified
from app.providers.base import InputProvider, TargetIdentity
from app.providers.input import Key, KeyboardProvider, PointerProvider
from app.providers.target import Capability, Target, TargetVerification, VerificationState
from app.windows import keymap
from app.windows.backend import InputDeliveryError, KeyEvent, MouseEvent, WindowInputBackend
from app.windows.ids import CAPABILITIES
from app.windows.input import (INPUT_CAPABILITIES, MAX_SESSION_ACTIONS, InputBudgetExceeded, InputRefused, IntendedInput, IntendedTap,
                               PartialInputDelivery, WindowInputSession)
from app.windows.target import _MINT, WindowTarget, WindowVerifier
from tests.test_windows_provider import FakeDesktop, info

ROOT = Path(__file__).resolve().parents[1] / "app"
M, K = MouseEvent, KeyEvent


class FakeInput(WindowInputBackend):
    """Records the messages that would be posted. Has NO screen-coordinate entry point, like the real contract."""

    def __init__(self, desk: FakeDesktop, size=(60, 40)):
        self.desk, self.size, self.events = desk, size, []
        self.on_event = None          # called after each recorded message (simulates the world changing mid-operation)
        self.fail_at = None           # 1-based index of the message that the OS "refuses"

    def _record(self, event):
        if self.fail_at is not None and len(self.events) + 1 == self.fail_at:
            raise InputDeliveryError("refused")
        self.events.append(event)
        if self.on_event:
            self.on_event(self)

    def client_size(self, hwnd):
        return self.size if hwnd in self.desk.windows else None

    def post_mouse(self, hwnd, event, x, y):
        self._record(("mouse", hwnd, event, x, y))

    def post_key(self, hwnd, event, key):
        self._record(("key", hwnd, event, key))

    def post_char(self, hwnd, char):
        self._record(("char", hwnd, char))

    def release_primary(self):
        self._record(("release_primary",))

    def release_secondary(self):
        self._record(("release_secondary",))

    def release_key(self, key):
        self._record(("release_key", key))


def build(max_actions=20, dry_run=False, **window):
    desk = FakeDesktop(info(**window))
    stop = EmergencyStop()
    verifier = WindowVerifier(desk, stop)
    backend = FakeInput(desk)
    target = verifier.verify(100)
    return (WindowInputSession(target, verifier, backend, stop, max_actions=max_actions, dry_run=dry_run),
            desk, backend, stop, target)


# name -> (call, capability, expected messages for the call at (10, 20) / Key.A / "Hi")
POINT = (10, 20)
OPS = {
    "tap": (lambda s: s.tap(*POINT), Capability.TAP,
            [("mouse", 100, M.LEFT_DOWN, 10, 20), ("mouse", 100, M.LEFT_UP, 10, 20)]),
    "secondary_tap": (lambda s: s.secondary_tap(*POINT), Capability.SECONDARY_TAP,
                      [("mouse", 100, M.RIGHT_DOWN, 10, 20), ("mouse", 100, M.RIGHT_UP, 10, 20)]),
    "double_tap": (lambda s: s.double_tap(*POINT), Capability.DOUBLE_TAP,
                   [("mouse", 100, M.LEFT_DOWN, 10, 20), ("mouse", 100, M.LEFT_UP, 10, 20),
                    ("mouse", 100, M.LEFT_DOUBLE, 10, 20), ("mouse", 100, M.LEFT_UP, 10, 20)]),
    "move": (lambda s: s.move(*POINT), Capability.POINTER_MOVE, [("mouse", 100, M.MOVE, 10, 20)]),
    "button_down": (lambda s: s.button_down(*POINT), Capability.POINTER_BUTTON,
                    [("mouse", 100, M.LEFT_DOWN, 10, 20)]),
    "key_press": (lambda s: s.key_press(Key.A), Capability.KEY, [("key", 100, K.DOWN, Key.A), ("key", 100, K.UP, Key.A)]),
    "key_down": (lambda s: s.key_down(Key.ENTER), Capability.KEY, [("key", 100, K.DOWN, Key.ENTER)]),
    "type_text": (lambda s: s.type_text("Hi"), Capability.TEXT, [("char", 100, "H"), ("char", 100, "i")]),
}
POINTER_OPS = ("tap", "secondary_tap", "double_tap", "move", "button_down")


class ValidOperations(unittest.TestCase):
    def test_every_operation_generates_exactly_the_expected_messages_for_the_verified_handle(self):
        for name, (call, _cap, expected) in OPS.items():
            s, _, b, _, _ = build()
            call(s)
            self.assertEqual(b.events, expected, name)
            self.assertEqual(s.actions_sent, 1, name)               # one requested operation = one action

    def test_button_up_after_down_and_the_composed_drag_sequence(self):
        s, _, b, _, _ = build()
        s.move(1, 2)
        s.button_down(1, 2)
        self.assertTrue(s.left_button_held)
        s.move(30, 20)                                             # while held: the move carries the held button
        s.button_up(30, 20)
        self.assertFalse(s.left_button_held)
        self.assertEqual(b.events, [("mouse", 100, M.MOVE, 1, 2), ("mouse", 100, M.LEFT_DOWN, 1, 2),
                                    ("mouse", 100, M.MOVE_LEFT_HELD, 30, 20), ("mouse", 100, M.LEFT_UP, 30, 20)])

    def test_key_down_then_up_tracks_held_keys(self):
        s, _, b, _, _ = build()
        s.key_down(Key.SHIFT)
        self.assertEqual(s.held_keys, frozenset({Key.SHIFT}))
        s.key_up(Key.SHIFT)
        self.assertEqual(s.held_keys, frozenset())
        self.assertEqual(b.events, [("key", 100, K.DOWN, Key.SHIFT), ("key", 100, K.UP, Key.SHIFT)])

    def test_text_is_one_character_message_per_character_in_order(self):
        s, _, b, _, _ = build()
        s.type_text("a B-9 é")
        self.assertEqual([e[2] for e in b.events], list("a B-9 é"))
        self.assertEqual(s.actions_sent, 1)

    def test_satisfies_the_neutral_interfaces(self):
        s, *_ = build()
        for proto in (InputProvider, PointerProvider, KeyboardProvider):
            self.assertIsInstance(s, proto)

    def test_budget_counts_operations_not_messages(self):
        s, _, b, _, _ = build(max_actions=2)
        s.double_tap(1, 1)                                         # 4 messages, 1 action
        s.type_text("abc")                                         # 3 messages, 1 action
        with self.assertRaises(InputBudgetExceeded):
            s.tap(1, 1)
        self.assertEqual(len(b.events), 7)
        d, st = FakeDesktop(info()), EmergencyStop()
        v = WindowVerifier(d, st)
        for bad in (0, -1, MAX_SESSION_ACTIONS + 1, True, 1.0, None, "1"):
            with self.assertRaises(ValueError):
                WindowInputSession(v.verify(100), v, FakeInput(d), st, max_actions=bad)  # type: ignore[arg-type]


class TargetRefusals(unittest.TestCase):
    def setUp(self):
        self.desk = FakeDesktop(info())
        self.stop = EmergencyStop()
        self.v = WindowVerifier(self.desk, self.stop)
        self.backend = FakeInput(self.desk)

    def make(self, target):
        return WindowInputSession(target, self.v, self.backend, self.stop, max_actions=5)  # type: ignore[arg-type]

    def test_non_targets_unverified_and_foreign_targets_are_rejected(self):
        class Unverified(Target):
            identity = property(lambda s: TargetIdentity("windows", "win-x"))
            capabilities = property(lambda s: frozenset({Capability.TAP}))
            verification = property(lambda s: TargetVerification(VerificationState.UNVERIFIED))

        class Foreign(Target):
            identity = property(lambda s: TargetIdentity("other-provider", "x"))
            capabilities = property(lambda s: INPUT_CAPABILITIES)
            verification = property(lambda s: TargetVerification(VerificationState.VERIFIED, ("m",), 0.0))
        for bogus in ("win-x", 100, None, ("windows", "win-x"), object(), info(), Unverified(), Foreign()):
            with self.assertRaises(TargetNotVerified):
                self.make(bogus)

    def test_a_target_with_no_input_capability_is_rejected_at_construction(self):
        class ShotOnly(WindowTarget):
            capabilities = property(lambda self: frozenset({Capability.SCREENSHOT}))
        t = ShotOnly("win-x", 100, 10, 5000, "editor.exe", "EditorWnd", "", 0.0, _mint=_MINT)
        with self.assertRaises(CapabilityNotAvailable):
            self.make(t)

    def test_each_operation_needs_its_own_declared_capability(self):
        for name, (call, cap, _expected) in OPS.items():
            class Limited(WindowTarget):
                capabilities = property(lambda self, cap=cap: CAPABILITIES - {cap})
            t = Limited("win-x", 100, 10, 5000, "editor.exe", "EditorWnd", "", 0.0, _mint=_MINT)
            s = WindowInputSession(t, self.v, self.backend, self.stop, max_actions=5)
            with self.assertRaises(CapabilityNotAvailable, msg=name):
                call(s)
        self.assertEqual(self.backend.events, [])

    def test_button_up_and_key_up_are_covered_by_their_capabilities_too(self):
        class NoButton(WindowTarget):
            capabilities = property(lambda self: CAPABILITIES - {Capability.POINTER_BUTTON})
        t = NoButton("win-x", 100, 10, 5000, "editor.exe", "EditorWnd", "", 0.0, _mint=_MINT)
        s = WindowInputSession(t, self.v, self.backend, self.stop, max_actions=5)
        with self.assertRaises(InputRefused):                      # not held: refused before anything else
            s.button_up(1, 1)
        with self.assertRaises(CapabilityNotAvailable):
            s.button_down(1, 1)

    def test_forged_targets_cannot_be_created_and_backend_type_is_checked(self):
        with self.assertRaises(TargetNotVerified):
            WindowTarget("win-x", 100, 10, 5000, "editor.exe", "EditorWnd", "", 0.0)
        t = self.v.verify(100)
        for bad in (self.desk, object(), None):
            with self.assertRaises(TypeError):
                WindowInputSession(t, self.v, bad, self.stop, max_actions=1)  # type: ignore[arg-type]


class LiveStateRefusals(unittest.TestCase):
    def test_stale_destroyed_and_changed_targets_send_nothing(self):
        for name, (call, _cap, _exp) in OPS.items():
            s, desk, b, _, _ = build()
            del desk.windows[100]
            with self.assertRaises(TargetNotVerified, msg=name):
                call(s)
            self.assertEqual(b.events, [], name)

    def test_identity_changes_are_detected_for_every_operation(self):
        for changed in (info(pid=11), info(start=5001), info(name="other.exe"), info(cls="Different")):
            for name, (call, _cap, _exp) in OPS.items():
                s, desk, b, _, _ = build()
                desk.windows[100] = changed
                with self.assertRaises(TargetNotVerified, msg=(name, changed)):
                    call(s)
                self.assertEqual(b.events, [])

    def test_hidden_child_minimized_cloaked_or_disabled_windows_are_refused(self):
        cases = (({"visible": False}, TargetNotVerified), ({"top_level": False}, TargetNotVerified),
                 ({"minimized": True}, InputRefused), ({"cloaked": True}, InputRefused),
                 ({"enabled": False}, InputRefused))
        for kw, exc in cases:
            for name, (call, _cap, _exp) in OPS.items():
                s, desk, b, _, _ = build()
                desk.windows[100] = info(**kw)
                with self.assertRaises(exc, msg=(name, kw)):
                    call(s)
                self.assertEqual(b.events, [], (name, kw))

    def test_title_change_alone_does_not_block(self):
        s, desk, b, _, _ = build()
        desk.windows[100] = info(title="renamed")
        s.tap(1, 1)
        self.assertEqual(len(b.events), 2)

    def test_emergency_stop_prevents_every_operation_and_reset_reenables(self):
        for name, (call, _cap, expected) in OPS.items():
            s, _, b, stop, _ = build()
            stop.trigger("test")
            with self.assertRaises(EmergencyStopped, msg=name):
                call(s)
            self.assertEqual((b.events, s.actions_sent), ([], 0), name)
            stop.reset()
            call(s)
            self.assertEqual(b.events, expected, name)

    def test_emergency_stop_firing_during_the_gates_still_blocks(self):
        s, _, b, stop, _ = build()
        original = b.client_size
        b.client_size = lambda hwnd: (stop.trigger("mid-flight"), original(hwnd))[1]
        with self.assertRaises(EmergencyStopped):
            s.tap(1, 1)
        self.assertEqual(b.events, [])
        s2, _, b2, stop2, _ = build()
        real_recheck = s2._verifier.recheck
        s2._verifier.recheck = lambda t: (stop2.trigger("mid-flight"), real_recheck(t))[1]   # keyboard: no size read
        with self.assertRaises(EmergencyStopped):
            s2.key_press(Key.A)
        self.assertEqual(b2.events, [])


class MultiMessageOperations(unittest.TestCase):
    def test_double_tap_stops_and_reports_when_the_window_disappears_between_the_clicks(self):
        s, desk, b, _, _ = build()
        b.on_event = lambda be: desk.windows.pop(100, None) if len(be.events) == 2 else None
        with self.assertRaises(PartialInputDelivery) as ctx:
            s.double_tap(5, 5)
        self.assertEqual((ctx.exception.delivered, ctx.exception.total), (2, 4))
        self.assertEqual(len(b.events), 2)                          # the second click was NOT sent
        self.assertIsInstance(ctx.exception.__cause__, TargetNotVerified)

    def test_double_tap_stops_on_emergency_stop_between_the_clicks(self):
        s, _, b, stop, _ = build()
        b.on_event = lambda be: stop.trigger("mid") if len(be.events) == 2 else None
        with self.assertRaises(PartialInputDelivery) as ctx:
            s.double_tap(5, 5)
        self.assertIsInstance(ctx.exception.__cause__, EmergencyStopped)
        self.assertEqual(len(b.events), 2)

    def test_double_tap_stops_when_the_window_shrinks_so_the_point_is_outside(self):
        s, _, b, _, _ = build()
        def shrink(be):
            if len(be.events) == 2:
                be.size = (4, 4)
        b.on_event = shrink
        with self.assertRaises(PartialInputDelivery):
            s.double_tap(5, 5)
        self.assertEqual(len(b.events), 2)

    def test_os_refusal_mid_operation_is_reported_with_progress_and_nothing_further_is_sent(self):
        s, _, b, _, _ = build()
        b.fail_at = 3
        with self.assertRaises(PartialInputDelivery) as ctx:
            s.double_tap(5, 5)
        self.assertEqual((ctx.exception.delivered, ctx.exception.total), (2, 4))
        self.assertEqual(len(b.events), 2)
        s2, _, b2, _, _ = build()
        b2.fail_at = 1                                              # first message refused: plain error, nothing sent
        with self.assertRaises(InputDeliveryError) as ctx2:
            s2.tap(5, 5)
        self.assertNotIsInstance(ctx2.exception, PartialInputDelivery)
        self.assertEqual(b2.events, [])

    def test_text_revalidates_before_every_character(self):
        s, desk, b, _, _ = build()
        b.on_event = lambda be: desk.windows.update({100: info(pid=99)}) if len(be.events) == 2 else None
        with self.assertRaises(PartialInputDelivery) as ctx:
            s.type_text("abcde")
        self.assertEqual((ctx.exception.delivered, ctx.exception.total), (2, 5))
        self.assertEqual([e[2] for e in b.events], ["a", "b"])

    def test_a_failed_operation_still_uses_budget_but_dry_run_does_not(self):
        s, _, b, _, _ = build(max_actions=1)
        b.fail_at = 1
        with self.assertRaises(InputDeliveryError):
            s.tap(1, 1)
        with self.assertRaises(InputBudgetExceeded):
            s.tap(1, 1)


class CoordinateRules(unittest.TestCase):
    def test_invalid_coordinates_are_rejected_not_clamped_for_every_pointer_operation(self):
        for name in POINTER_OPS:
            method = name
            s, _, b, _, _ = build()
            for x, y in ((1.5, 2), (1, 2.0), ("1", 2), (None, 2), (True, 2), (1, False), ((1,), 2)):
                with self.assertRaises(InputRefused, msg=(name, x, y)):
                    getattr(s, method)(x, y)
            self.assertEqual(b.events, [])

    def test_out_of_bounds_coordinates_are_rejected_for_every_pointer_operation(self):
        for name in POINTER_OPS:
            s, _, b, _, _ = build()                                # client area 60 x 40
            for x, y in ((-1, 0), (0, -1), (60, 0), (0, 40), (60, 40), (10 ** 9, 5), (5, -10 ** 9)):
                with self.assertRaises(InputRefused, msg=(name, x, y)):
                    getattr(s, name)(x, y)
            self.assertEqual((b.events, s.actions_sent), ([], 0))

    def test_edge_pixels_are_valid(self):
        s, _, b, _, _ = build()
        s.move(0, 0)
        s.move(59, 39)
        self.assertEqual([e[3:] for e in b.events], [(0, 0), (59, 39)])

    def test_button_up_coordinates_are_checked_too_and_the_button_stays_held(self):
        s, _, b, _, _ = build()
        s.button_down(1, 1)
        with self.assertRaises(InputRefused):
            s.button_up(60, 1)
        self.assertTrue(s.left_button_held)
        self.assertEqual(len(b.events), 1)

    def test_bounds_follow_the_current_client_area_after_a_resize(self):
        s, _, b, _, _ = build()
        s.move(50, 30)
        b.size = (40, 20)
        with self.assertRaises(InputRefused):
            s.move(50, 30)
        s.move(39, 19)
        b.size = (400, 300)
        s.move(399, 299)
        self.assertEqual(len(b.events), 3)

    def test_unreadable_or_empty_client_area_is_rejected(self):
        s, _, b, _, _ = build()
        b.size = (0, 40)
        with self.assertRaises(InputRefused):
            s.tap(0, 0)
        b.client_size = lambda hwnd: None
        with self.assertRaises(InputRefused):
            s.tap(0, 0)
        self.assertEqual(b.events, [])

    def test_keyboard_operations_do_not_need_or_use_coordinates(self):
        s, _, b, _, _ = build()
        b.client_size = lambda hwnd: None                            # unreadable client area must not matter
        s.key_press(Key.TAB)
        s.type_text("ok")
        self.assertEqual(len(b.events), 4)


class HeldStateRules(unittest.TestCase):
    def test_button_state_machine(self):
        s, _, b, _, _ = build()
        with self.assertRaises(InputRefused):
            s.button_up(1, 1)                                       # no press yet
        s.button_down(1, 1)
        with self.assertRaises(InputRefused):
            s.button_down(1, 1)                                     # already held
        for click in (s.tap, s.secondary_tap, s.double_tap):
            with self.assertRaises(InputRefused):
                click(1, 1)                                         # no clicks while held
        self.assertEqual(len(b.events), 1)
        s.button_up(1, 1)
        s.tap(1, 1)
        self.assertEqual(len(b.events), 4)

    def test_key_state_machine(self):
        s, _, b, _, _ = build()
        with self.assertRaises(InputRefused):
            s.key_up(Key.A)
        s.key_down(Key.A)
        for again in (s.key_down, s.key_press):
            with self.assertRaises(InputRefused):
                again(Key.A)
        s.key_press(Key.B)                                          # other keys are independent
        s.key_up(Key.A)
        self.assertEqual(len(b.events), 4)

    def test_failed_press_does_not_mark_the_button_or_key_as_held(self):
        s, _, b, _, _ = build()
        b.fail_at = 1
        with self.assertRaises(InputDeliveryError):
            s.button_down(1, 1)
        with self.assertRaises(InputDeliveryError):
            s.key_down(Key.A)
        self.assertFalse(s.left_button_held)
        self.assertEqual(s.held_keys, frozenset())


class LettingGo(unittest.TestCase):
    """The end of a live run that left something pressed (the person's decision of 2026-10-20): the session lets go
    of exactly what IT holds -- where things are, no move, no focus change -- even with the emergency stop set."""

    def test_release_held_lets_go_of_exactly_what_the_session_holds_even_after_the_stop(self):
        s, _desk, backend, stop, _t = build()
        self.assertEqual((s.release_held(), backend.events), ((), []))          # nothing held: nothing sent
        s.button_down(10, 20)
        s.key_down(Key.SHIFT)
        stop.trigger("test")
        n = len(backend.events)
        self.assertEqual(s.release_held(), ("primary_button", "key:shift"))
        self.assertEqual(backend.events[n:], [("release_primary",), ("release_key", Key.SHIFT)])
        self.assertEqual((s.left_button_held, s.held_keys), (False, frozenset()))
        self.assertEqual((s.release_held(), len(backend.events)), ((), n + 2))   # a second time: nothing more

    def test_a_click_or_key_press_cut_off_halfway_is_let_go_too(self):
        """Its press delivered, its release refused: the button or key is down although no rule ever held it."""
        s, _desk, backend, _stop, _t = build()
        s.tap(1, 1)
        s.secondary_tap(1, 1)
        s.key_press(Key.A)
        self.assertEqual(s.release_held(), ())                                   # whole ones leave nothing down
        for call, released, sent in ((lambda: s.tap(10, 20), ("primary_button",), ("release_primary",)),
                                     (lambda: s.secondary_tap(10, 20), ("secondary_button",), ("release_secondary",)),
                                     (lambda: s.key_press(Key.A), ("key:a",), ("release_key", Key.A))):
            with self.subTest(released=released):
                backend.fail_at = len(backend.events) + 2              # the second message: the release
                with self.assertRaises(PartialInputDelivery):
                    call()
                backend.fail_at = None
                n = len(backend.events)
                self.assertEqual(s.release_held(), released)
                self.assertEqual(backend.events[n:], [sent])
                self.assertEqual(s.release_held(), ())

    def test_a_double_click_cut_off_before_its_last_release_is_let_go_too(self):
        """Its second press (the double-click message) delivered, the release after it refused: the button is down."""
        s, _desk, backend, _stop, _t = build()
        s.double_tap(1, 1)
        self.assertEqual(s.release_held(), ())                                   # a whole one leaves nothing down
        backend.fail_at = len(backend.events) + 4                                # the fourth message: the last release
        with self.assertRaises(PartialInputDelivery):
            s.double_tap(10, 20)
        backend.fail_at = None
        n = len(backend.events)
        self.assertEqual(s.release_held(), ("primary_button",))
        self.assertEqual(backend.events[n:], [("release_primary",)])

    def test_a_release_the_system_refuses_stays_held_and_a_dry_run_lets_go_of_nothing(self):
        s, _desk, backend, _stop, _t = build()
        s.key_down(Key.SHIFT)
        s.key_down(Key.CTRL)
        real = backend.release_key

        def release_key(key):
            if key is Key.CTRL:
                raise InputDeliveryError("refused")
            real(key)
        backend.release_key = release_key
        self.assertEqual(s.release_held(), ("key:shift",))
        self.assertEqual(s.held_keys, frozenset({Key.CTRL}))                    # still held, and said so
        s.button_down(10, 20)

        def refused():
            raise InputDeliveryError("refused")
        backend.release_primary = refused
        self.assertEqual(s.release_held(), ())
        self.assertTrue(s.left_button_held)
        dry, _desk, dry_backend, _stop, _t = build(dry_run=True)
        dry.button_down(10, 20)
        self.assertEqual((dry.release_held(), dry_backend.events), ((), []))

    def test_what_it_could_not_let_go_of_it_says_even_a_press_no_rule_held(self):
        """A tap cut off between its press and its release, its release at the end refused too: only the session
        knows the button is down, so it says so (`still_held`, read by the run's report)."""
        s, _desk, backend, _stop, _t = build()
        self.assertEqual(s.still_held, ())
        backend.fail_at = len(backend.events) + 2                                # the tap's release refused
        with self.assertRaises(PartialInputDelivery):
            s.tap(10, 20)
        backend.fail_at = None
        s.key_down(Key.SHIFT)
        real = backend.release_primary

        def refused():
            raise InputDeliveryError("refused")
        backend.release_primary = refused
        self.assertEqual(s.release_held(), ("key:shift",))
        self.assertEqual(s.still_held, ("primary_button",))
        backend.release_primary = real
        self.assertEqual((s.release_held(), s.still_held), (("primary_button",), ()))


class KeyboardAndTextValidation(unittest.TestCase):
    def test_invalid_keys_are_rejected_deterministically(self):
        s, _, b, _, _ = build()
        for bad in ("a", "enter", 65, None, b"a", ("a",), Key.A.value, object()):
            for op in (s.key_press, s.key_down, s.key_up):
                with self.assertRaises(InputRefused, msg=(op.__name__, bad)):
                    op(bad)  # type: ignore[arg-type]
        self.assertEqual(b.events, [])
        with self.assertRaises(ValueError):
            Key.parse("f13")
        self.assertIs(Key.parse(" F5 "), Key.F5)

    def test_invalid_text_is_rejected_before_anything_is_sent(self):
        s, _, b, _, _ = build()
        for bad in ("", "x" * 257, "a\nb", "\t", "tab\there", "ab\r", "\x00", "smile \U0001F600", "\ud800", None, 5,
                    b"abc", ["a"]):
            with self.assertRaises(InputRefused, msg=repr(bad)):
                s.type_text(bad)  # type: ignore[arg-type]
        self.assertEqual((b.events, s.actions_sent), ([], 0))
        s.type_text("x" * 256)                                      # the limit itself is allowed
        self.assertEqual(len(b.events), 256)

    def test_keymap_is_total_and_free_of_ambiguity(self):
        codes = {k: keymap.virtual_key(k) for k in Key}
        self.assertEqual(len(codes), len(Key))
        self.assertEqual(len(set(codes.values())), len(Key))        # no two keys share a code
        self.assertEqual((codes[Key.A], codes[Key.Z], codes[Key.DIGIT_0], codes[Key.DIGIT_9]), (0x41, 0x5A, 0x30, 0x39))
        self.assertEqual((codes[Key.F1], codes[Key.F12]), (0x70, 0x7B))
        self.assertEqual((codes[Key.ENTER], codes[Key.ESCAPE], codes[Key.SPACE], codes[Key.TAB]), (0x0D, 0x1B, 0x20, 0x09))
        self.assertEqual((codes[Key.BACKSPACE], codes[Key.DELETE]), (0x08, 0x2E))
        self.assertEqual((codes[Key.LEFT], codes[Key.UP], codes[Key.RIGHT], codes[Key.DOWN]), (0x25, 0x26, 0x27, 0x28))
        self.assertEqual((codes[Key.SHIFT], codes[Key.CTRL], codes[Key.ALT]), (0x10, 0x11, 0x12))
        for bad in ("a", 65, None):
            with self.assertRaises(ValueError):
                keymap.virtual_key(bad)  # type: ignore[arg-type]

    def test_key_message_flags(self):
        down = keymap.key_lparam(0x1E, extended=False, system=False, up=False)
        up = keymap.key_lparam(0x1E, extended=False, system=False, up=True)
        self.assertEqual(down, 1 | (0x1E << 16))
        self.assertEqual(up, down | (1 << 30) | (1 << 31))
        self.assertEqual(keymap.key_lparam(0x53, extended=True, system=False, up=False) >> 24 & 1, 1)
        self.assertEqual(keymap.key_lparam(0x38, extended=False, system=True, up=False) >> 29 & 1, 1)
        self.assertTrue(keymap.is_extended(Key.LEFT) and keymap.is_extended(Key.DELETE) and not keymap.is_extended(Key.A))
        self.assertTrue(keymap.is_system(Key.ALT) and not keymap.is_system(Key.CTRL))
        with self.assertRaises(ValueError):
            keymap.key_lparam(256, extended=False, system=False, up=False)


class DryRun(unittest.TestCase):
    def test_every_operation_sends_zero_events_and_reports_provider_target_action_and_coordinate(self):
        s, _, b, _, t = build(dry_run=True)
        for name, (call, _cap, _exp) in OPS.items():
            call(s)
        self.assertEqual(b.events, [])
        self.assertEqual((s.actions_sent, s.dry_run), (0, True))
        self.assertEqual([i.action for i in s.intended], list(OPS))
        self.assertEqual(s.intended[0], IntendedTap("windows", t.target_id, "tap", 10, 20))
        self.assertEqual(s.intended[5], IntendedInput("windows", t.target_id, "key_press", key="a"))
        self.assertEqual(s.intended[7].text_length, 2)
        text = " ".join(i.describe() for i in s.intended)
        for part in ("windows", t.target_id, "tap at client (10, 20)", "key a", "type_text (2 characters)"):
            self.assertIn(part, text)

    def test_dry_run_never_records_the_text_itself(self):
        s, *_ = build(dry_run=True)
        s.type_text("s3cret-password")
        self.assertNotIn("s3cret", repr(s.intended) + s.intended[0].describe())

    def test_dry_run_applies_every_gate(self):
        s, desk, b, stop, _ = build(dry_run=True)
        with self.assertRaises(InputRefused):
            s.tap(60, 0)
        with self.assertRaises(InputRefused):
            s.tap(1.0, 0)  # type: ignore[arg-type]
        with self.assertRaises(InputRefused):
            s.key_press("a")  # type: ignore[arg-type]
        with self.assertRaises(InputRefused):
            s.type_text("a\nb")
        with self.assertRaises(InputRefused):
            s.button_up(1, 1)
        desk.windows[100] = info(pid=99)
        with self.assertRaises(TargetNotVerified):
            s.tap(1, 1)
        stop.trigger("x")
        with self.assertRaises(EmergencyStopped):
            s.tap(1, 1)
        self.assertEqual((b.events, s.intended), ([], ()))

    def test_dry_run_tracks_held_state_like_a_live_session_and_uses_no_budget(self):
        s, _, b, _, _ = build(max_actions=1, dry_run=True)
        s.button_down(1, 1)
        s.button_up(1, 1)
        s.key_down(Key.A)
        s.key_up(Key.A)
        for _ in range(5):
            s.tap(1, 1)
        self.assertEqual((len(s.intended), b.events), (9, []))
        live, *_ = build()
        live.tap(1, 1)
        self.assertEqual(live.intended, ())


class NoGlobalOrForegroundFallback(unittest.TestCase):
    def test_backend_contract_has_only_handle_addressed_message_primitives(self):
        """A WINDOW is addressed by its handle only. Input to the whole desktop (Full Windows) exists on the person's
        explicit decision of 2026-10-19 (docs/ARCHITECTURE.md, "Full Windows macros"): a separate, closed set of
        primitives that only the gated `DesktopInputSession` (app/windows/desktop.py) may call. Letting go at the end
        of a live run, on the person's decision of 2026-10-20: two releases that press nothing, which only
        `WindowInputSession.release_held` (app/windows/input.py) may call."""
        methods = {n for n in dir(WindowInputBackend) if not n.startswith("_")}
        window = {"client_size", "post_mouse", "post_key", "post_char"}
        desktop = {"window_at", "foreground_window", "desktop_mouse", "desktop_key", "desktop_char"}
        releasing = {"release_primary", "release_secondary", "release_key"}
        self.assertEqual(methods, window | desktop | releasing)
        for name in ("post_mouse", "post_key", "post_char"):
            self.assertEqual(list(inspect.signature(getattr(WindowInputBackend, name)).parameters)[:2], ["self", "hwnd"])
        for path in ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for sender in ("desktop_mouse(", "desktop_key(", "desktop_char("):
                if "." + sender in text:
                    self.assertEqual(path.relative_to(ROOT).as_posix(), "windows/desktop.py", sender)
            for sender in ("release_primary(", "release_secondary(", "release_key("):
                if "." + sender in text:
                    self.assertEqual(path.relative_to(ROOT).as_posix(), "windows/input.py", sender)

    def test_session_public_surface_is_exactly_the_documented_operations(self):
        public = {n for n in dir(WindowInputSession) if not n.startswith("_")}
        self.assertEqual(public, {"tap", "secondary_tap", "double_tap", "move", "button_down", "button_up", "key_press",
                                  "key_down", "key_up", "type_text", "target_id", "provider_id", "actions_sent", "action_limit",
                                  "dry_run", "intended", "left_button_held", "held_keys", "release_held",
                                  "still_held"})

    def test_all_messages_go_to_the_verified_handle_only(self):
        desk = FakeDesktop(info(100), info(200, pid=20, start=6000))
        stop = EmergencyStop()
        v = WindowVerifier(desk, stop)
        backend = FakeInput(desk)
        s = WindowInputSession(v.verify(100), v, backend, stop, max_actions=50)
        for name, (call, _cap, _exp) in OPS.items():
            call(s)
            if name == "button_down":
                s.button_up(*POINT)
            if name == "key_down":
                s.key_up(Key.ENTER)
        self.assertEqual({e[1] for e in backend.events}, {100})
        desk.windows[100] = replace(desk.windows[200], hwnd=100)    # the handle is now another program's window
        n = len(backend.events)
        for call, _cap, _exp in OPS.values():
            with self.assertRaises(TargetNotVerified):
                call(s)
        self.assertEqual(len(backend.events), n)                    # nothing reached the impostor


def imports_of(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class Boundaries(unittest.TestCase):
    def test_windows_provider_does_not_import_ldplayer_adb_instances_or_macros(self):
        for path in (ROOT / "windows").glob("*.py"):
            for m in imports_of(path):
                self.assertFalse(m.startswith(("app.ldplayer", "app.adb", "app.instances", "app.macros",
                                               "app.automation")), f"{path.name}: {m}")
                if m.startswith("app."):
                    self.assertIn(m.split(".")[1], ("core", "providers", "playback", "windows"), f"{path.name}: {m}")

    def test_ldplayer_side_does_not_import_windows(self):
        for pkg in ("adb", "ldplayer", "instances", "macros", "automation"):
            for path in (ROOT / pkg).glob("*.py"):
                self.assertFalse({m for m in imports_of(path) if m.startswith("app.windows")}, path.name)

    def test_generic_layers_stay_provider_neutral_and_key_codes_stay_in_the_windows_package(self):
        for pkg in ("core", "logging", "recovery", "providers", "vision"):
            for path in (ROOT / pkg).glob("*.py"):
                self.assertFalse({m for m in imports_of(path) if m.startswith(("app.windows", "app.ldplayer", "app.adb"))},
                                 path.name)
        generic = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "providers").glob("*.py")).lower()
        for word in ("hwnd", "windows", "win32", "postmessage", "client area", "vk_", "0x0d", "virtual-key", "virtual_key"):
            self.assertNotIn(word, generic)
        for k in Key:
            self.assertIsInstance(k.value, str)                     # neutral names, no numeric codes

    def test_ldplayer_declares_its_own_smaller_capability_set(self):
        """LDPlayer's capability set is its own independent, deliberately smaller declaration -- not a mirror of
        Windows' -- and stays a strict subset of it (see app.ldplayer.ids and the ADB input capability stage)."""
        from app.ldplayer.ids import CAPABILITIES as LD_CAPS
        self.assertEqual(LD_CAPS, frozenset({Capability.SCREENSHOT, Capability.TAP, Capability.KEY, Capability.TEXT}))
        self.assertLess(LD_CAPS, CAPABILITIES)

    def test_capability_vocabulary_is_the_documented_one(self):
        self.assertEqual({c.value for c in Capability}, {"screenshot", "tap", "secondary_tap", "double_tap",
                                                         "pointer_move", "pointer_button", "key", "text", "record"})
        self.assertEqual(CAPABILITIES, frozenset(Capability))

    def test_clean_interpreter_loads_windows_input_without_other_providers(self):
        code = textwrap.dedent("""
            import sys
            import app.windows.input, app.providers.input
            bad = [m for m in sys.modules if m.split(".")[:2] in (["app","adb"],["app","ldplayer"],["app","instances"],["app","macros"])]
            assert not bad, bad
            print("CLEAN")
        """)
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "CLEAN", r.stderr)


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealBackendRefusesBadInput(unittest.TestCase):
    """Argument validation of the real backend: all of these are rejected BEFORE any message is posted."""

    def setUp(self):
        from app.windows.win32 import Win32InputBackend
        self.b = Win32InputBackend()

    def test_invalid_mouse_arguments(self):
        for hwnd, ev, x, y in ((0, M.MOVE, 1, 1), (-1, M.MOVE, 1, 1), (2 ** 40, M.MOVE, 1, 1), (True, M.MOVE, 1, 1),
                               ("1", M.MOVE, 1, 1), (100, "move", 1, 1), (100, None, 1, 1), (100, M.MOVE, -1, 1),
                               (100, M.MOVE, 1, -1), (100, M.MOVE, 0x8000, 1), (100, M.MOVE, 1.5, 1),
                               (100, M.MOVE, True, 1)):
            with self.assertRaises(InputDeliveryError, msg=(hwnd, ev, x, y)):
                self.b.post_mouse(hwnd, ev, x, y)  # type: ignore[arg-type]

    def test_invalid_key_and_char_arguments(self):
        for hwnd, ev, key in ((0, K.DOWN, Key.A), (100, "down", Key.A), (100, K.DOWN, "a"), (100, K.DOWN, 65),
                              (100, K.DOWN, None)):
            with self.assertRaises(InputDeliveryError, msg=(hwnd, ev, key)):
                self.b.post_key(hwnd, ev, key)  # type: ignore[arg-type]
        for hwnd, ch in ((0, "a"), (100, ""), (100, "ab"), (100, "\n"), (100, "\x00"), (100, "\U0001F600"), (100, None),
                         (100, 97)):
            with self.assertRaises(InputDeliveryError, msg=(hwnd, ch)):
                self.b.post_char(hwnd, ch)  # type: ignore[arg-type]

    def test_client_size_rejects_invalid_handles(self):
        for bad in (0, -1, 2 ** 40, None, "1", True):
            self.assertIsNone(self.b.client_size(bad))               # type: ignore[arg-type]


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class ChildWindowTargeting(unittest.TestCase):
    """`Win32InputBackend` resolves the real receiving descendant for a classic Win32 control (see
    docs/ARCHITECTURE.md "Real-click delivery for Calculator's XAML buttons") against a REAL, minimal window
    hierarchy this test creates and destroys itself, positioned far off-screen so it is never visible to the
    user, not a simulated one: `GetWindowRect`/`IsChild` are real OS calls with real, exact answers, exactly as
    they would be against a real application's real controls. A genuine `WindowFromPoint` cannot resolve a point
    that far off any real monitor, so `_WindowFromPoint` alone is replaced with a fake that answers exactly the
    way a real one would if these SAME real windows, with their SAME real rects, were on screen -- proven this
    way rather than by moving a visible window onto the user's real screen. `_SendInput`/`_SetWindowPos` are also
    replaced (never actually clicking or moving anything). KEYBOARD delivery no longer targets a specific
    descendant at all (see `KeyboardIsAlsoDeliveredAsRealInput` below for that mechanism) -- a real click
    already gives the clicked control real OS-level focus as a side effect, exactly like a person's own click,
    so there is no separate "which child" bookkeeping left in this class for this test to exercise."""

    _WS_OVERLAPPEDWINDOW = 0x00CF0000
    _WS_CHILD, _WS_VISIBLE = 0x40000000, 0x10000000
    _SW_SHOWNOACTIVATE = 4

    def setUp(self):
        import ctypes
        from ctypes import wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.top = self.user32.CreateWindowExW(0, "STATIC", "test-top", self._WS_OVERLAPPEDWINDOW,
                                               -32000, -32000, 400, 300, None, None, None, None)
        self.assertTrue(self.top, "could not create the test top-level window")
        self.addCleanup(self.user32.DestroyWindow, self.top)
        self.user32.ShowWindow(self.top, self._SW_SHOWNOACTIVATE)   # shown (IsWindowVisible needs that), never
                                                                    # brought to the foreground or activated
        # a real child EDIT control -- a classic control, exactly the case this fix addresses -- in the left
        # part of the parent's client area (10,10)-(160,40); the rest of the client area has no child at all.
        self.edit = self.user32.CreateWindowExW(0, "EDIT", "", self._WS_CHILD | self._WS_VISIBLE,
                                                10, 10, 150, 30, self.top, None, None, None)
        self.assertTrue(self.edit, "could not create the test child edit control")

        from app.windows.win32 import Win32InputBackend
        self.backend = Win32InputBackend()
        self.sent_inputs: list[int] = []
        self.backend._SendInput = lambda count, inputs, size: (self.sent_inputs.append(count) or count)
        self.set_window_pos_calls: list[tuple] = []
        self.backend._SetWindowPos = lambda *a: (self.set_window_pos_calls.append(a) or True)

        def fake_window_from_point(pt):
            for hwnd in (self.edit, self.top):
                rect = wintypes.RECT()
                if (self.user32.IsWindowVisible(hwnd) and self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        and rect.left <= pt.x < rect.right and rect.top <= pt.y < rect.bottom):
                    return hwnd
            return self.top
        self.backend._WindowFromPoint = fake_window_from_point

    def test_a_click_inside_the_child_control_resolves_a_real_click_to_the_child_not_the_parent(self):
        receiver, _, _ = self.backend._resolve_real_click_target(self.top, 30, 20)   # inside the EDIT rect
        self.assertEqual(receiver, self.edit)
        self.backend.post_mouse(self.top, M.MOVE, 30, 20)
        self.assertEqual(len(self.sent_inputs), 1)

    def test_a_click_outside_any_child_resolves_a_real_click_to_the_top_level_window_unchanged(self):
        receiver, _, _ = self.backend._resolve_real_click_target(self.top, 300, 250)   # outside the EDIT rect
        self.assertEqual(receiver, self.top)
        self.backend.post_mouse(self.top, M.MOVE, 300, 250)
        self.assertEqual(len(self.sent_inputs), 1)

    def test_window_class_is_never_consulted_only_the_real_hit_test_result_is(self):
        """Replaces an earlier test of this project's own, now-removed class-name-preference heuristic: the
        current mechanism is a real hit-test, so it does not matter what class the winning window is -- proven
        by a `WindowFromPoint` fake that always answers with the (real, but otherwise unremarkable) top-level
        window, and confirming that one is used even for a point that is geometrically inside the EDIT child."""
        self.backend._WindowFromPoint = lambda pt: self.top
        receiver, _, _ = self.backend._resolve_real_click_target(self.top, 30, 20)   # inside the EDIT rect
        self.assertEqual(receiver, self.top)


class KeyboardIsAlsoDeliveredAsRealInput(unittest.TestCase):
    """`Win32InputBackend.post_key`/`post_char` deliver real, hardware-level key/character input (see the
    class's own docstring), which needs the target to actually hold SYSTEM FOCUS first -- proven here against a
    real, off-screen window (focus is a logical OS concept, not tied to screen position, but every test window
    in this module is kept off-screen regardless, by this project's own convention).
    `_GetForegroundWindow`/`_SetForegroundWindow`/`_SendInput` are replaced so nothing here depends on this
    process's own ability to actually steal focus -- which Windows itself restricts, and this project's own
    sandbox is known not to grant to a backgrounded process -- or affects the real desktop."""

    _WS_OVERLAPPEDWINDOW = 0x00CF0000
    _WS_CHILD, _WS_VISIBLE = 0x40000000, 0x10000000
    _SW_SHOWNOACTIVATE = 4
    _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x0002, 0x0004

    def setUp(self):
        import ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.top = self.user32.CreateWindowExW(0, "STATIC", "test-top", self._WS_OVERLAPPEDWINDOW,
                                               -32000, -32000, 400, 300, None, None, None, None)
        self.assertTrue(self.top, "could not create the test top-level window")
        self.addCleanup(self.user32.DestroyWindow, self.top)
        self.user32.ShowWindow(self.top, self._SW_SHOWNOACTIVATE)

        from app.windows.win32 import Win32InputBackend
        self.backend = Win32InputBackend()
        self.foreground = [123456]              # some other, unrelated window handle -- mutated per test
        self.set_foreground_calls: list[int] = []

        def fake_set_foreground(hwnd):
            self.set_foreground_calls.append(hwnd)
            self.foreground[0] = hwnd           # simulate a SUCCESSFUL foreground switch by default
            return True
        self.backend._GetForegroundWindow = lambda: self.foreground[0]
        self.backend._SetForegroundWindow = fake_set_foreground
        self.sent: list[list[tuple[int, int, int]]] = []   # one entry per SendInput call: [(vk, scan, flags), ...]

        def fake_send_input(count, inputs, size):
            self.sent.append([(inputs[i].ki.wVk, inputs[i].ki.wScan, inputs[i].ki.dwFlags) for i in range(count)])
            return count
        self.backend._SendInput = fake_send_input

    def test_no_foreground_switch_is_attempted_when_the_target_is_already_foreground(self):
        self.foreground[0] = self.top
        self.backend.post_key(self.top, K.DOWN, Key.A)
        self.assertEqual(self.set_foreground_calls, [])
        self.assertEqual(len(self.sent), 1)

    def test_a_real_child_already_holding_focus_counts_as_the_target_holding_it_too(self):
        child = self.user32.CreateWindowExW(0, "EDIT", "", self._WS_CHILD | self._WS_VISIBLE, 10, 10, 100, 20,
                                            self.top, None, None, None)
        self.assertTrue(child, "could not create the test child")
        self.addCleanup(self.user32.DestroyWindow, child)
        self.foreground[0] = child
        self.backend.post_key(self.top, K.DOWN, Key.A)
        self.assertEqual(self.set_foreground_calls, [])
        self.assertEqual(len(self.sent), 1)

    def test_the_target_is_brought_to_the_foreground_first_when_it_is_not_already(self):
        self.backend.post_key(self.top, K.DOWN, Key.A)
        self.assertEqual(self.set_foreground_calls, [self.top])
        self.assertEqual(len(self.sent), 1)

    def test_a_key_is_refused_not_misdirected_if_the_foreground_switch_does_not_actually_take_effect(self):
        # Windows can report the call succeeded while the real foreground window still does not change (its own
        # focus-stealing prevention) -- the fake below models exactly that, distinctly from a plain failure.
        self.backend._SetForegroundWindow = lambda hwnd: self.set_foreground_calls.append(hwnd) or True
        with self.assertRaises(InputDeliveryError):
            self.backend.post_key(self.top, K.DOWN, Key.A)
        self.assertEqual(self.sent, [])

    def test_a_character_is_also_refused_not_misdirected_if_the_foreground_switch_does_not_take_effect(self):
        self.backend._SetForegroundWindow = lambda hwnd: self.set_foreground_calls.append(hwnd) or True
        with self.assertRaises(InputDeliveryError):
            self.backend.post_char(self.top, "A")
        self.assertEqual(self.sent, [])

    def test_a_character_is_sent_once_focus_is_established(self):
        self.foreground[0] = self.top
        self.backend.post_char(self.top, "A")
        self.assertEqual(len(self.sent), 1)

    def test_key_down_sends_one_event_without_the_keyup_flag(self):
        self.foreground[0] = self.top
        self.backend.post_key(self.top, K.DOWN, Key.A)
        [(vk, _, flags)] = self.sent[0]
        self.assertEqual(vk, ord("A"))
        self.assertFalse(flags & self._KEYEVENTF_KEYUP)

    def test_key_up_sends_one_event_with_the_keyup_flag(self):
        self.foreground[0] = self.top
        self.backend.post_key(self.top, K.UP, Key.A)
        [(_, _, flags)] = self.sent[0]
        self.assertTrue(flags & self._KEYEVENTF_KEYUP)

    def test_a_character_sends_a_unicode_down_up_pair(self):
        self.foreground[0] = self.top
        self.backend.post_char(self.top, "A")
        events = self.sent[0]
        self.assertEqual(len(events), 2)
        self.assertEqual([scan for _, scan, _ in events], [ord("A"), ord("A")])
        self.assertTrue(all(flags & self._KEYEVENTF_UNICODE for _, _, flags in events))
        self.assertEqual([bool(flags & self._KEYEVENTF_KEYUP) for _, _, flags in events], [False, True])


import ctypes as _ctypes                                                                  # noqa: E402
from ctypes import wintypes as _wintypes                                                  # noqa: E402

_WNDPROC = _ctypes.WINFUNCTYPE(_ctypes.c_long, _wintypes.HWND, _ctypes.c_uint, _wintypes.WPARAM, _wintypes.LPARAM)


class _WNDCLASS(_ctypes.Structure):
    _fields_ = [("style", _ctypes.c_uint), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", _ctypes.c_int),
               ("cbWndExtra", _ctypes.c_int), ("hInstance", _wintypes.HINSTANCE), ("hIcon", _wintypes.HICON),
               ("hCursor", _wintypes.HICON), ("hbrBackground", _wintypes.HBRUSH),
               ("lpszMenuName", _wintypes.LPCWSTR), ("lpszClassName", _wintypes.LPCWSTR)]


class MouseIsAlwaysDeliveredAsARealClick(unittest.TestCase):
    """Widened, on an explicit user decision, from a narrow exception scoped to Calculator's own
    `Windows.UI.Core.CoreWindow` (confirmed live to ignore every posted mouse message -- see
    app/windows/win32.py's module and class docstrings) to the universal mechanism for EVERY window, once it was
    clear a per-app-class allowlist could not cover "any Windows program". Proven here against a real,
    self-registered window class whose name contains "corewindow" (the historical, now-vestigial special case)
    AND an ordinary classic `EDIT`-classed window, confirming BOTH now take the exact same real-click path.
    `_SendInput`/`_WindowFromPoint`/`_SetWindowPos` are replaced (never actually moving the real cursor or
    clicking anything) so nothing here can affect the desktop; `_ClientToScreenBase` and `_GetSystemMetrics`
    stay real, since they only ever read geometry, never send anything."""

    _WS_OVERLAPPEDWINDOW = 0x00CF0000
    _WS_CHILD, _WS_VISIBLE = 0x40000000, 0x10000000
    _SW_SHOWNOACTIVATE = 4
    _CLASS_NAME = "TestCoreWindowClass"

    @classmethod
    def setUpClass(cls):
        cls.user32 = _ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)
        cls.user32.DefWindowProcW.restype = _ctypes.c_long
        cls.user32.DefWindowProcW.argtypes = [_wintypes.HWND, _ctypes.c_uint, _wintypes.WPARAM, _wintypes.LPARAM]
        cls._wndproc = _WNDPROC(lambda h, m, w, l: cls.user32.DefWindowProcW(h, m, w, l))   # kept alive on the class
        wc = _WNDCLASS()
        wc.lpfnWndProc = cls._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = cls._CLASS_NAME
        cls.user32.RegisterClassW.restype = _wintypes.ATOM
        atom = cls.user32.RegisterClassW(_ctypes.byref(wc))
        if not atom:
            raise OSError("could not register the test CoreWindow-like class")

    def setUp(self):
        self.core = self.user32.CreateWindowExW(0, self._CLASS_NAME, "test-core", self._WS_OVERLAPPEDWINDOW,
                                                -32000, -32000, 400, 300, None, None, None, None)
        self.assertTrue(self.core, "could not create the test CoreWindow-like window")
        self.addCleanup(self.user32.DestroyWindow, self.core)
        self.user32.ShowWindow(self.core, self._SW_SHOWNOACTIVATE)

        # an ordinary EDIT-class sibling top-level window -- proves the real-click path is now taken for a
        # classic control's window too, not just the historical CoreWindow-only special case
        self.classic = self.user32.CreateWindowExW(0, "EDIT", "test-classic", self._WS_OVERLAPPEDWINDOW,
                                                   -32000, -31000, 400, 300, None, None, None, None)
        self.assertTrue(self.classic, "could not create the test classic window")
        self.addCleanup(self.user32.DestroyWindow, self.classic)
        self.user32.ShowWindow(self.classic, self._SW_SHOWNOACTIVATE)

        from app.windows.win32 import Win32InputBackend
        self.backend = Win32InputBackend()
        self.posted: list[tuple[int, int, int, int]] = []
        self.backend._PostMessage = lambda hwnd, msg, w, l: (self.posted.append((hwnd, msg, w, l)) or True)
        self.sent_inputs: list[list[tuple[int, int, int]]] = []   # one entry per SendInput call: [(dx,dy,flags), ...]

        def fake_send_input(count, inputs, size):
            self.sent_inputs.append([(inputs[i].mi.dx, inputs[i].mi.dy, inputs[i].mi.dwFlags) for i in range(count)])
            return count
        self.backend._SendInput = fake_send_input
        self.window_from_point_result = [self.core]   # what WindowFromPoint should report; mutated per test
        self.backend._WindowFromPoint = lambda pt: self.window_from_point_result[0]
        self.set_window_pos_calls: list[tuple] = []
        self.backend._SetWindowPos = lambda *a: (self.set_window_pos_calls.append(a) or True)
        self.backend._clock = lambda: 100.0                    # a still clock and a set double-click time: nothing
        self.backend._GetDoubleClickTime = lambda: 500         # here depends on this machine's speed or setting

    def test_a_click_on_the_corewindow_like_window_uses_a_real_click_not_a_posted_message(self):
        self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        self.assertEqual(self.posted, [])
        self.assertEqual(len(self.sent_inputs), 1)

    def test_a_click_on_an_ordinary_classic_window_also_uses_a_real_click_now_not_a_posted_message(self):
        self.window_from_point_result[0] = self.classic
        self.backend.post_mouse(self.classic, M.LEFT_DOWN, 30, 20)
        self.assertEqual(self.posted, [])
        self.assertEqual(len(self.sent_inputs), 1)

    def test_the_window_is_raised_in_z_order_without_being_activated(self):
        self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        self.assertEqual(len(self.set_window_pos_calls), 1)
        args = self.set_window_pos_calls[0]
        SWP_NOACTIVATE = 0x0010
        self.assertTrue(args[-1] & SWP_NOACTIVATE, "must never activate/steal focus while raising Z order")

    def test_a_click_covered_by_another_window_is_refused_not_misdirected(self):
        self.window_from_point_result[0] = self.classic     # something else is on top at that screen point
        with self.assertRaises(InputDeliveryError):
            self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        self.assertEqual(self.sent_inputs, [])

    def test_left_down_sends_exactly_one_move_and_one_down_event(self):
        self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        MOVE, LEFTDOWN = 0x0001, 0x0002
        flags = [f for _, _, f in self.sent_inputs[0]]
        self.assertTrue(flags[0] & MOVE)
        self.assertTrue(any(f & LEFTDOWN for f in flags[1:]))
        self.assertEqual(len(flags), 2)

    def test_left_double_is_the_second_press_of_a_double_click(self):
        """The double-click message that follows a press and a release (`MouseEvent.LEFT_DOUBLE`) is one more press,
        which Windows itself makes the double click -- not a whole double click of its own."""
        self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        self.backend.post_mouse(self.core, M.LEFT_UP, 30, 20)
        self.backend.post_mouse(self.core, M.LEFT_DOUBLE, 30, 20)
        MOVE, LEFTDOWN, LEFTUP = 0x0001, 0x0002, 0x0004
        flags = [f for _, _, f in self.sent_inputs[2]]
        self.assertEqual(len(flags), 2)                       # one move + one press
        self.assertTrue(flags[0] & MOVE)
        self.assertTrue(flags[1] & LEFTDOWN and not flags[1] & LEFTUP)

    def test_a_second_press_too_late_or_away_for_windows_to_make_a_double_click_is_not_sent(self):
        """Windows makes the double click only when the second press follows the first within ITS double-click time
        and rectangle (a slow target stretches the gap: the window is raised and checked again between the two; it may
        even move). Otherwise it would be two single clicks reported as a double click -- so it is refused, and the
        double tap ends as cut off (2 of 4) instead. Measured from the first PRESS, two of Windows' ~16 ms ticks short
        of the limit, against the setting as it is now; a second press with no first is no double click either."""
        now = [100.0]
        real_metrics = self.backend._GetSystemMetrics
        self.backend._clock = lambda: now[0]
        self.backend._GetDoubleClickTime = lambda: 300                       # not the default 500: read, not assumed
        self.backend._GetSystemMetrics = lambda i: 4 if i in (36, 37) else real_metrics(i)   # SM_C[XY]DOUBLECLK

        def first_click(release_after):
            self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
            now[0] += release_after
            self.backend.post_mouse(self.core, M.LEFT_UP, 30, 20)

        def second_press(after, x=30, *, sent):
            now[0] += after
            calls = len(self.sent_inputs)
            if sent:
                self.backend.post_mouse(self.core, M.LEFT_DOUBLE, x, 20)
                self.backend.post_mouse(self.core, M.LEFT_UP, x, 20)
            else:
                with self.assertRaises(InputDeliveryError):
                    self.backend.post_mouse(self.core, M.LEFT_DOUBLE, x, 20)
            self.assertEqual(len(self.sent_inputs), calls + (2 if sent else 0))

        first_click(0.1)
        second_press(0.15, sent=True)                       # 250 ms after the press: a double click
        second_press(0.0, sent=False)                       # no first press before it
        first_click(0.1)
        second_press(0.18, sent=False)                      # 280 ms: within Windows' own tick margin of the limit
        first_click(0.2)
        second_press(0.09, sent=False)                      # 290 ms after the press, though 90 after the release
        first_click(0.05)
        second_press(0.05, x=31, sent=True)                 # a pixel away: inside its 4 px rectangle
        first_click(0.05)
        second_press(0.05, x=33, sent=False)                # 3 px away: outside it (the window moved?)

    def test_a_double_tap_is_exactly_two_clicks_through_the_real_backend(self):
        """What the session sends for a double tap, through the real backend: two presses and two releases. Seen
        live on 2026-10-20 as three clicks and a stray release, when the double-click message was sent as a whole
        double click after the first press and release."""
        s, _desk, fake, _stop, _t = build()
        s.double_tap(*POINT)
        for _kind, _hwnd, event, _x, _y in fake.events:
            self.backend.post_mouse(self.core, event, 30, 20)
        LEFTDOWN, LEFTUP = 0x0002, 0x0004
        flags = [f for call in self.sent_inputs for _, _, f in call]
        self.assertEqual((sum(bool(f & LEFTDOWN) for f in flags), sum(bool(f & LEFTUP) for f in flags)), (2, 2))

    def test_absolute_coordinates_are_normalised_against_the_virtual_screen(self):
        self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        vx = self.backend._GetSystemMetrics(76)     # SM_XVIRTUALSCREEN
        vy = self.backend._GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
        vw = self.backend._GetSystemMetrics(78)     # SM_CXVIRTUALSCREEN
        vh = self.backend._GetSystemMetrics(79)     # SM_CYVIRTUALSCREEN
        point = _wintypes.POINT(30, 20)
        self.user32.ClientToScreen(self.core, _ctypes.byref(point))
        expected_x = round((point.x - vx) * 65535 / max(vw - 1, 1))
        expected_y = round((point.y - vy) * 65535 / max(vh - 1, 1))
        dx, dy, _ = self.sent_inputs[0][0]
        self.assertEqual((dx, dy), (expected_x, expected_y))

    def test_a_bad_screen_geometry_read_is_refused_not_sent_with_garbage_coordinates(self):
        self.backend._GetSystemMetrics = lambda index: 0    # simulate an unreadable virtual-screen geometry
        with self.assertRaises(InputDeliveryError):
            self.backend.post_mouse(self.core, M.LEFT_DOWN, 30, 20)
        self.assertEqual(self.sent_inputs, [])


if __name__ == "__main__":
    unittest.main()
