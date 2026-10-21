"""Tests for the controlled input path: InputSession -> AdbClient.input_tap/input_keyevent/input_text (fake
runner, no device)."""
import tempfile
import unittest
from pathlib import Path

from app.adb.client import AdbClient, AdbError, InputGrant
from app.adb.input import InputBudgetExceeded, InputSession, IntendedInput
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, TargetNotVerified
from tests.fakes import FakeRunner, result
from tests.test_adb_session import SessionAdb
from tests.test_mapping import two_instance_world, make_service


class RecordingAdb(SessionAdb):
    def __init__(self, w):
        super().__init__(w)
        self.taps = []
        self.keyevents = []
        self.texts = []

    def input_tap(self, serial, x, y, *, grant):
        self.taps.append((serial, x, y, grant))

    def input_keyevent(self, serial, keycode, *, grant):
        self.keyevents.append((serial, keycode, grant))

    def input_text(self, serial, text, *, grant):
        self.texts.append((serial, text, grant))


def build(max_actions=1, w=None, **kw):
    w = w or two_instance_world()
    svc = make_service(w)
    adb = RecordingAdb(w)
    svc._adb = adb
    stop = EmergencyStop()
    target = svc.verify("ld1")
    return w, adb, stop, InputSession(target, adb, svc, stop, max_actions=max_actions, **kw)


class InputSessionTests(unittest.TestCase):
    def test_requires_verified_target_and_explicit_budget(self):
        w, adb, stop, s = build()
        svc = make_service(w)
        with self.assertRaises(TargetNotVerified):
            InputSession("emulator-5554", adb, svc, stop, max_actions=1)
        for bad in (0, 1001, True, None, 1.5):
            with self.assertRaises((ValueError, TypeError)):
                InputSession(s.target, adb, svc, stop, max_actions=bad)

    def test_tap_goes_to_the_verified_serial_only(self):
        _, adb, _, s = build()
        s.tap(640, 360)
        self.assertEqual([(t[0], t[1], t[2]) for t in adb.taps], [("emulator-5554", 640, 360)])
        self.assertEqual(s.actions_sent, 1)

    def test_budget_is_enforced(self):
        _, adb, _, s = build(max_actions=1)
        s.tap(10, 10)
        with self.assertRaises(InputBudgetExceeded):
            s.tap(10, 10)
        self.assertEqual(len(adb.taps), 1)

    def test_out_of_bounds_and_bad_types_send_nothing(self):
        _, adb, _, s = build(max_actions=10)
        for x, y in ((1280, 0), (0, 720), (-1, 5), (5, -1), (10**6, 10**6)):
            with self.assertRaises(AdbError):
                s.tap(x, y)
        for x, y in ((1.5, 3), ("5", 3), (True, 3), (None, 3)):
            with self.assertRaises(AdbError):
                s.tap(x, y)
        self.assertEqual(adb.taps, [])
        self.assertEqual(s.actions_sent, 0)
        s.tap(1279, 719)  # last valid pixel
        self.assertEqual(len(adb.taps), 1)

    def test_edge_pixel_zero_zero_is_valid(self):
        _, adb, _, s = build()
        s.tap(0, 0)
        self.assertEqual(len(adb.taps), 1)

    def test_emergency_stop_blocks_tap(self):
        _, adb, stop, s = build()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            s.tap(5, 5)
        self.assertEqual(adb.taps, [])

    def test_offline_device_blocks_tap(self):
        from app.adb.client import DeviceState
        w, adb, _, s = build()
        w.devices[0].state = DeviceState.OFFLINE
        with self.assertRaises(TargetNotVerified):
            s.tap(5, 5)
        self.assertEqual(adb.taps, [])

    def test_other_instances_untouched(self):
        _, adb, _, s = build(max_actions=5)
        s.tap(1, 1); s.tap(2, 2)
        self.assertNotIn("emulator-5556", {t[0] for t in adb.taps})

    def test_session_exposes_no_unimplemented_input(self):
        """Deliberately absent (see app.adb.input's module docstring): swipe/drag, separate key_down/key_up,
        secondary/double tap, and anything shell/launch-shaped. `key_press` (the neutral, Key-typed name) is
        deliberately also absent here: this module never sees `Key` (see app.ldplayer.playback._LDPlayerInput,
        the bridge that offers it). `send_keyevent` (this module's own, keycode-typed primitive) and `type_text`
        DO exist (checked below)."""
        _, _, _, s = build()
        for name in ("swipe", "move", "button_down", "button_up", "secondary_tap", "double_tap", "key_down",
                     "key_up", "key_press", "shell", "launch", "start_app", "play", "input"):
            self.assertFalse(hasattr(s, name), name)
        self.assertTrue(callable(s.send_keyevent))
        self.assertTrue(callable(s.type_text))


class DryRunTests(unittest.TestCase):
    """H.1: a dry run must still run the real freshness/identity gate (AdbSession._gate(), reached through
    display()) and the real bounds query; only the actual `adb shell input tap` call and the budget are skipped."""

    def test_dry_run_sends_nothing_and_records_the_intended_tap(self):
        _, adb, _, s = build(dry_run=True)
        s.tap(640, 360)
        self.assertEqual(adb.taps, [])
        self.assertEqual(s.actions_sent, 0)
        self.assertEqual(s.intended, (IntendedInput("tap", x=640, y=360),))
        self.assertTrue(s.dry_run)

    def test_dry_run_still_queries_the_real_display_for_bounds(self):
        """Proof the gate actually ran, not just that nothing crashed: the fake's own wm size/density calls."""
        _, adb, _, s = build(dry_run=True)
        s.tap(1, 1)
        shell_calls = [c[2] for c in adb.calls if c[0] == "shell"]
        self.assertIn(("wm", "size"), shell_calls)
        self.assertIn(("wm", "density"), shell_calls)

    def test_dry_run_out_of_bounds_is_still_refused(self):
        _, adb, _, s = build(dry_run=True)
        with self.assertRaises(AdbError):
            s.tap(10**6, 10**6)
        self.assertEqual((adb.taps, s.intended), ([], ()))

    def test_dry_run_does_not_consume_the_budget(self):
        _, adb, _, s = build(max_actions=1, dry_run=True)
        s.tap(1, 1)
        s.tap(2, 2)                                    # would raise InputBudgetExceeded on a live session
        self.assertEqual(s.intended, (IntendedInput("tap", x=1, y=1), IntendedInput("tap", x=2, y=2)))
        self.assertEqual(adb.taps, [])

    def test_a_target_that_can_no_longer_be_revalidated_fails_the_dry_run_the_same_way_as_live(self):
        from dataclasses import replace

        from app.instances.profile import InstanceRegistry
        w, adb, stop, live = build()
        stale_target = replace(live.target, verified_at=0.0)
        svc = make_service(w)
        svc._adb = adb
        svc._registry = InstanceRegistry([])           # ld1's profile is gone: revalidation must fail
        dry = InputSession(stale_target, adb, svc, stop, max_actions=1, dry_run=True)
        live_stale = InputSession(stale_target, adb, svc, stop, max_actions=1)
        with self.assertRaises(TargetNotVerified) as dry_exc:
            dry.tap(1, 1)
        with self.assertRaises(TargetNotVerified) as live_exc:
            live_stale.tap(1, 1)
        self.assertEqual(dry_exc.exception.reason_code, live_exc.exception.reason_code)
        self.assertEqual((adb.taps, dry.intended), ([], ()))


class SendKeyeventTests(unittest.TestCase):
    """This module's own, keycode-typed primitive (see app.ldplayer.keymap/app.ldplayer.playback for the
    Key-typed bridge that calls it)."""

    def test_send_keyevent_goes_to_the_verified_serial_only(self):
        _, adb, _, s = build()
        s.send_keyevent(29)                             # KEYCODE_A
        self.assertEqual([(k[0], k[1]) for k in adb.keyevents], [("emulator-5554", 29)])
        self.assertEqual(s.actions_sent, 1)

    def test_send_keyevent_and_tap_share_one_budget(self):
        _, adb, _, s = build(max_actions=2)
        s.tap(1, 1)
        s.send_keyevent(66)                             # KEYCODE_ENTER
        with self.assertRaises(InputBudgetExceeded):
            s.send_keyevent(66)
        self.assertEqual((len(adb.taps), len(adb.keyevents)), (1, 1))

    def test_bad_keycode_is_refused_before_anything_is_sent(self):
        _, adb, _, s = build()
        for bad in (-1, 1000, True, 1.5, "66", None):
            with self.assertRaises(AdbError):
                s.send_keyevent(bad)
        self.assertEqual(adb.keyevents, [])

    def test_emergency_stop_blocks_send_keyevent(self):
        _, adb, stop, s = build()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            s.send_keyevent(29)
        self.assertEqual(adb.keyevents, [])

    def test_offline_device_blocks_send_keyevent(self):
        from app.adb.client import DeviceState
        w, adb, _, s = build()
        w.devices[0].state = DeviceState.OFFLINE
        with self.assertRaises(TargetNotVerified):
            s.send_keyevent(29)
        self.assertEqual(adb.keyevents, [])

    def test_dry_run_sends_no_keyevent_but_still_runs_the_freshness_gate(self):
        _, adb, _, s = build(dry_run=True)
        s.send_keyevent(66)
        self.assertEqual(adb.keyevents, [])
        self.assertEqual(s.actions_sent, 0)
        self.assertEqual(s.intended, (IntendedInput("key_event", keycode=66),))
        self.assertIn(("list_devices",), adb.calls, "the freshness gate must still query the device state")

    def test_dry_run_does_not_consume_the_budget(self):
        _, adb, _, s = build(max_actions=1, dry_run=True)
        s.send_keyevent(29)
        s.send_keyevent(30)                              # would raise InputBudgetExceeded on a live session
        self.assertEqual(adb.keyevents, [])

    def test_a_target_that_can_no_longer_be_revalidated_fails_the_dry_run_the_same_way_as_live(self):
        from dataclasses import replace

        from app.instances.profile import InstanceRegistry
        w, adb, stop, live = build()
        stale_target = replace(live.target, verified_at=0.0)
        svc = make_service(w)
        svc._adb = adb
        svc._registry = InstanceRegistry([])
        dry = InputSession(stale_target, adb, svc, stop, max_actions=1, dry_run=True)
        live_stale = InputSession(stale_target, adb, svc, stop, max_actions=1)
        with self.assertRaises(TargetNotVerified) as dry_exc:
            dry.send_keyevent(29)
        with self.assertRaises(TargetNotVerified) as live_exc:
            live_stale.send_keyevent(29)
        self.assertEqual(dry_exc.exception.reason_code, live_exc.exception.reason_code)
        self.assertEqual(adb.keyevents, [])


class TypeTextTests(unittest.TestCase):
    def test_text_goes_to_the_verified_serial_only(self):
        _, adb, _, s = build()
        s.type_text("hello world")
        self.assertEqual([(t[0], t[1]) for t in adb.texts], [("emulator-5554", "hello world")])
        self.assertEqual(s.actions_sent, 1)

    def test_problematic_characters_are_refused_before_anything_is_sent(self):
        """Characters that are special to a POSIX shell, to adb's own argument joining, or to Android's text
        decoder -- none may reach AdbClient.input_text."""
        _, adb, _, s = build()
        for bad in ("it's", 'say "hi"', "a;b", "a&&b", "a|b", "$(whoami)", "`whoami`", "a\\b", "a\nb", "a\tb",
                    "50%", "a<b>c", "a*b", "a?b", "~root", "a{b}c", "café", "hello\n", "a\rb", "hello\r"):
            with self.assertRaises(AdbError):
                s.type_text(bad)
        self.assertEqual(adb.texts, [])

    def test_leading_and_trailing_spaces_are_accepted_and_preserved(self):
        _, adb, _, s = build()
        s.type_text("  padded  ")
        self.assertEqual([(t[0], t[1]) for t in adb.texts], [("emulator-5554", "  padded  ")])

    def test_empty_and_oversized_text_is_refused(self):
        _, adb, _, s = build()
        with self.assertRaises(AdbError):
            s.type_text("")
        with self.assertRaises(AdbError):
            s.type_text("x" * 257)
        self.assertEqual(adb.texts, [])
        s.type_text("x" * 256)                          # the limit itself is allowed
        self.assertEqual(len(adb.texts), 1)

    def test_emergency_stop_blocks_type_text(self):
        _, adb, stop, s = build()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            s.type_text("hi")
        self.assertEqual(adb.texts, [])

    def test_dry_run_sends_no_text_but_never_records_its_content(self):
        _, adb, _, s = build(dry_run=True)
        s.type_text("secret search term")
        self.assertEqual(adb.texts, [])
        self.assertEqual(s.intended, (IntendedInput("type_text", text_length=len("secret search term")),))
        self.assertNotIn("secret", repr(s.intended))

    def test_a_target_that_can_no_longer_be_revalidated_fails_the_dry_run_the_same_way_as_live(self):
        from dataclasses import replace

        from app.instances.profile import InstanceRegistry
        w, adb, stop, live = build()
        stale_target = replace(live.target, verified_at=0.0)
        svc = make_service(w)
        svc._adb = adb
        svc._registry = InstanceRegistry([])
        dry = InputSession(stale_target, adb, svc, stop, max_actions=1, dry_run=True)
        live_stale = InputSession(stale_target, adb, svc, stop, max_actions=1)
        with self.assertRaises(TargetNotVerified) as dry_exc:
            dry.type_text("hi")
        with self.assertRaises(TargetNotVerified) as live_exc:
            live_stale.type_text("hi")
        self.assertEqual(dry_exc.exception.reason_code, live_exc.exception.reason_code)
        self.assertEqual(adb.texts, [])


class ClientInputTapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adb = Path(self.tmp.name) / "adb.exe"
        self.adb.write_bytes(b"")
        self.stop = EmergencyStop()

    def client(self, handler):
        self.runner = FakeRunner(handler)
        return AdbClient(self.adb, self.runner, self.stop, retries=3, backoff_s=0)

    def grant(self, serial="emulator-5554"):
        from app.adb import client as c
        return InputGrant(serial, c._INPUT_MINT)

    def test_grant_cannot_be_forged(self):
        with self.assertRaises(SafetyViolation):
            InputGrant("emulator-5554")

    def test_exact_command_shape(self):
        c = self.client(lambda a: result(""))
        c.input_tap("emulator-5554", 12, 34, grant=self.grant())
        (argv, _), = self.runner.calls
        self.assertEqual(argv[1:], ("-s", "emulator-5554", "shell", "input", "tap", "12", "34"))

    def test_requires_grant_for_same_serial(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.input_tap("emulator-5554", 1, 1, grant=None)
        with self.assertRaises(SafetyViolation):
            c.input_tap("emulator-5554", 1, 1, grant=self.grant("emulator-5556"))
        self.assertEqual(self.runner.calls if hasattr(self, "runner") else [], [])

    def test_never_retried_even_on_transient_error(self):
        c = self.client(lambda a: result("", "error: device offline", rc=1))
        with self.assertRaises(AdbError):
            c.input_tap("emulator-5554", 1, 1, grant=self.grant())
        self.assertEqual(len(self.runner.calls), 1)

    def test_timeout_reported_and_not_retried(self):
        c = self.client(lambda a: result("", timed_out=True))
        with self.assertRaises(AdbError):
            c.input_tap("emulator-5554", 1, 1, grant=self.grant())
        self.assertEqual(len(self.runner.calls), 1)

    def test_bad_coordinates_and_stop_run_nothing(self):
        c = self.client(lambda a: result(""))
        for x, y in ((-1, 0), (0, "1"), (1.0, 1), (True, 1), (200000, 1)):
            with self.assertRaises(AdbError):
                c.input_tap("emulator-5554", x, y, grant=self.grant())
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            c.input_tap("emulator-5554", 1, 1, grant=self.grant())
        self.assertEqual(self.runner.calls, [])

    def test_read_only_allowlist_still_refuses_input(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.shell_query("emulator-5554", "input", "tap", "1", "1")
        self.assertEqual(self.runner.calls, [])


class ClientInputKeyeventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adb = Path(self.tmp.name) / "adb.exe"
        self.adb.write_bytes(b"")
        self.stop = EmergencyStop()

    def client(self, handler):
        self.runner = FakeRunner(handler)
        return AdbClient(self.adb, self.runner, self.stop, retries=3, backoff_s=0)

    def grant(self, serial="emulator-5554"):
        from app.adb import client as c
        return InputGrant(serial, c._INPUT_MINT)

    def test_exact_command_shape(self):
        c = self.client(lambda a: result(""))
        c.input_keyevent("emulator-5554", 66, grant=self.grant())          # KEYCODE_ENTER
        (argv, _), = self.runner.calls
        self.assertEqual(argv[1:], ("-s", "emulator-5554", "shell", "input", "keyevent", "66"))

    def test_requires_grant_for_same_serial(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.input_keyevent("emulator-5554", 66, grant=None)
        with self.assertRaises(SafetyViolation):
            c.input_keyevent("emulator-5554", 66, grant=self.grant("emulator-5556"))
        self.assertEqual(self.runner.calls if hasattr(self, "runner") else [], [])

    def test_never_retried_even_on_transient_error(self):
        c = self.client(lambda a: result("", "error: device offline", rc=1))
        with self.assertRaises(AdbError):
            c.input_keyevent("emulator-5554", 66, grant=self.grant())
        self.assertEqual(len(self.runner.calls), 1)

    def test_bad_keycode_and_stop_run_nothing(self):
        c = self.client(lambda a: result(""))
        for bad in (-1, 1000, True, 1.5, "66", None):
            with self.assertRaises(AdbError):
                c.input_keyevent("emulator-5554", bad, grant=self.grant())
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            c.input_keyevent("emulator-5554", 66, grant=self.grant())
        self.assertEqual(self.runner.calls, [])

    def test_read_only_allowlist_still_refuses_input(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.shell_query("emulator-5554", "input", "keyevent", "66")
        self.assertEqual(self.runner.calls, [])


class ClientInputTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adb = Path(self.tmp.name) / "adb.exe"
        self.adb.write_bytes(b"")
        self.stop = EmergencyStop()

    def client(self, handler):
        self.runner = FakeRunner(handler)
        return AdbClient(self.adb, self.runner, self.stop, retries=3, backoff_s=0)

    def grant(self, serial="emulator-5554"):
        from app.adb import client as c
        return InputGrant(serial, c._INPUT_MINT)

    def test_exact_command_shape_is_single_quoted(self):
        c = self.client(lambda a: result(""))
        c.input_text("emulator-5554", "hello world", grant=self.grant())
        (argv, _), = self.runner.calls
        self.assertEqual(argv[1:], ("-s", "emulator-5554", "shell", "input", "text", "'hello world'"))

    def test_requires_grant_for_same_serial(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.input_text("emulator-5554", "hi", grant=None)
        with self.assertRaises(SafetyViolation):
            c.input_text("emulator-5554", "hi", grant=self.grant("emulator-5556"))
        self.assertEqual(self.runner.calls if hasattr(self, "runner") else [], [])

    def test_never_retried_even_on_transient_error(self):
        c = self.client(lambda a: result("", "error: device offline", rc=1))
        with self.assertRaises(AdbError):
            c.input_text("emulator-5554", "hi", grant=self.grant())
        self.assertEqual(len(self.runner.calls), 1)

    def test_unsafe_text_and_stop_run_nothing(self):
        c = self.client(lambda a: result(""))
        for bad in ("it's", 'say "hi"', "a;rm -rf /", "$(id)", "", "x" * 257, None, 5, "a\rb", "line\r\n"):
            with self.assertRaises(AdbError):
                c.input_text("emulator-5554", bad, grant=self.grant())
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            c.input_text("emulator-5554", "hi", grant=self.grant())
        self.assertEqual(self.runner.calls, [])

    def test_leading_and_trailing_spaces_survive_the_single_quoting_exactly(self):
        c = self.client(lambda a: result(""))
        c.input_text("emulator-5554", "  padded  ", grant=self.grant())
        (argv, _), = self.runner.calls
        self.assertEqual(argv[1:], ("-s", "emulator-5554", "shell", "input", "text", "'  padded  '"))

    def test_read_only_allowlist_still_refuses_input(self):
        c = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.shell_query("emulator-5554", "input", "text", "hi")
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
