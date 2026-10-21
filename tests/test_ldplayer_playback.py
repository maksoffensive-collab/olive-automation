"""ADB-first input architecture: the generic `Macro`/`PlaybackEngine` bound to a REAL `app.adb.input.InputSession`
through `app.ldplayer.playback.create_ldplayer_playback`, exactly as `app.windows.playback.create_window_playback`
binds it to a real `WindowInputSession`. Fakes only (`SessionAdb`/`RecordingAdb`, a fake `ldconsole`/registry
world); no real adb.exe or device.

What this proves, concretely:
  * higher-level automation expresses `tap(target, x, y)` (a `Macro` compiled to `ActionOp.TAP` actions) without
    knowing the adb path, subprocess details, or the serial behind the target -- `create_ldplayer_playback` is the
    only place that even imports `AdbClient`;
  * the real `InputSession`'s own identity/freshness/budget gates run on every action, not just a fake's;
  * a macro recorded for one verified instance can never be silently delivered to another (wrong-target refusal,
    and a destroyed/changed identity is caught by `AdbSession`'s own re-verification);
  * the real ADB input backend can be swapped in for the fake `TapOnly` stand-in already used by
    `tests/test_playback.py::LDPlayerCompatibility` without any change to `Macro`, `PlaybackEngine` or `preflight`.
"""
import unittest

from app.adb.input import MAX_SESSION_ACTIONS, InputSession, IntendedInput
from app.core.cancel import EmergencyStop
from app.ldplayer.playback import create_ldplayer_playback
from app.playback.engine import PlaybackRefused, PlaybackState as S
from app.playback.macro import Macro
from app.providers.input import Key
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from tests.test_adb_input import RecordingAdb
from tests.test_mapping import make_service, two_instance_world
from tests.test_playback import Clock, down, up


def rig(*, max_age_s=60.0):
    """A real MappingService + a fake, call-recording AdbClient over two registered instances."""
    world = two_instance_world()
    svc = make_service(world)
    adb = RecordingAdb(world)
    svc._adb = adb
    stop = EmergencyStop()
    target = svc.verify("ld1")
    return world, svc, adb, stop, target, max_age_s


def macro_for(target, *events):
    return Macro(Recording(target.provider_id, target.target_id, tuple(events)))


class RealInputSessionIsWiredIn(unittest.TestCase):
    def test_playback_uses_the_real_input_session_not_a_stand_in(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 100, 200), up(30, 100, 200))
        clock = Clock()
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, clock=clock, sleep=clock.sleep)
        self.assertIsInstance(eng._input.session, InputSession)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([(t[0], t[1], t[2]) for t in adb.taps], [("emulator-5554", 100, 200)])

    def test_higher_level_code_only_ever_calls_tap_never_touches_adb_directly(self):
        """The macro/engine layer expresses the action generically; only the adapter imports AdbClient."""
        import inspect

        from app.ldplayer import playback as adapter_module
        from app.playback import engine as engine_module
        from app.playback import macro as macro_module
        for mod in (engine_module, macro_module):
            src = inspect.getsource(mod)
            for word in ("adb", "ldplayer", "serial", "verifiedtarget"):
                self.assertNotIn(word, src.lower(), f"{mod.__name__} mentions {word!r}")
        self.assertIn("AdbClient", inspect.getsource(adapter_module))


class TargetIdentityIsPreserved(unittest.TestCase):
    def test_a_macro_recorded_for_one_instance_cannot_run_on_a_different_one_unbound(self):
        _, svc, adb, stop, target, max_age = rig()
        other = svc.verify("ld2")
        m = macro_for(other, down(0, 1, 1), up(5, 1, 1))                     # recorded for ld2
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)   # bound to ld1
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("target_mismatch", [i.code for i in cm.exception.issues])
        self.assertEqual(adb.taps, [], "nothing may be sent before the refusal")

    def test_explicit_rebinding_still_only_taps_the_bound_serial(self):
        _, svc, adb, stop, target, max_age = rig()
        other = svc.verify("ld2")
        m = macro_for(other, down(0, 5, 5), up(5, 5, 5))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, allow_different_target=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([t[0] for t in adb.taps], ["emulator-5554"])          # ld1's serial, never ld2's

    def test_a_wrong_provider_macro_cannot_silently_reach_ldplayer(self):
        _, svc, adb, stop, target, max_age = rig()
        m = Macro(Recording("windows", "win-1", (down(0, 1, 1), up(5, 1, 1))))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, allow_different_target=True)
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("wrong_provider", [i.code for i in cm.exception.issues])
        self.assertEqual(adb.taps, [])


class UnsupportedOperationsAreRefusedBeforeAnyInput(unittest.TestCase):
    def test_a_keyboard_or_mouse_move_macro_is_refused_by_preflight(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1), E(K.POINTER_MOVE, 10, 2, 2))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("unsupported_capability", [i.code for i in cm.exception.issues])
        self.assertEqual(adb.taps, [])

    def test_a_held_key_is_refused_even_though_the_target_declares_key(self):
        """The target declares Capability.KEY (key_press works), but InputSession has no key_down/key_up method
        (see app.adb.input's module docstring): a held key -- KEY_DOWN not immediately released -- must still be
        refused by preflight's provider_lacks_operation, before the macro's leading, otherwise-valid tap is sent."""
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1), E(K.KEY_DOWN, 10, key=Key.A))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("provider_lacks_operation", [i.code for i in cm.exception.issues])
        self.assertEqual(adb.taps, [])


class InvalidTextIsRefusedAtPreflightNotAtRuntime(unittest.TestCase):
    """The generic MacroAction/RecordedEvent layer accepts any printable text up to 256 characters -- broader than
    ADB's own SAFE_TEXT_PATTERN. Without a value-level preflight check, such a macro would pass preflight and only
    fail once InputSession.type_text actually ran it, mid-macro, after any earlier action had already been sent.
    _LDPlayerInput.unsupported_reason (preflight's optional value hook) closes that gap. These tests prove the
    macro is refused BEFORE playback starts, not caught as an AdbError after some input already went out."""

    def test_a_leading_valid_tap_followed_by_invalid_text_sends_zero_taps(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 9, 9), up(10, 9, 9), E(K.TEXT, 5, text="it's ok"))   # apostrophe: not allowed
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        report = eng.preflight()
        self.assertFalse(report.ok)
        self.assertIn("unsupported_value", [i.code for i in report.errors])
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("unsupported_value", [i.code for i in cm.exception.issues])
        self.assertEqual(adb.taps, [])
        self.assertEqual(adb.texts, [])

    def test_several_invalid_text_categories_are_all_refused_before_any_input(self):
        """Categories reachable through a REAL recorded macro: RecordedEvent's own `isprintable()` check already
        rejects control characters (newline, tab) upstream of this stage's fix (see the direct hook-level test
        below for those), so this covers what a genuine recording could actually contain that ADB still refuses:
        a quote/apostrophe (would break the adapter's shell single-quoting), '%' (Android's own text-decoding
        escape character on some versions), and non-ASCII text."""
        _, svc, adb, stop, target, max_age = rig()
        for bad in ("café", "50% off", "say \"hi\"", "it's fine", "a;b"):
            m = macro_for(target, down(0, 1, 1), up(5, 1, 1), E(K.TEXT, 5, text=bad))
            eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
            with self.assertRaises(PlaybackRefused) as cm:
                eng.run()
            self.assertIn("unsupported_value", [i.code for i in cm.exception.issues], bad)
        self.assertEqual((adb.taps, adb.texts), ([], []))

    def test_control_characters_are_refused_by_the_hook_itself_defense_in_depth(self):
        """Newline/tab can never reach this point through a real recording (RecordedEvent's own printable check
        already refuses them earlier). Tested directly against the hook anyway, as defense in depth: it must not
        rely solely on that upstream check to stay safe."""
        from app.ldplayer.playback import _LDPlayerInput
        from app.playback.macro import ActionOp, MacroAction

        bridge = _LDPlayerInput(session=None)   # unsupported_reason touches no session state
        for bad in ("line1\nline2", "tab\there"):
            action = MacroAction(ActionOp.TYPE_TEXT, 0, text=bad)
            self.assertIsNotNone(bridge.unsupported_reason(action), bad)
        self.assertIsNone(bridge.unsupported_reason(MacroAction(ActionOp.TYPE_TEXT, 0, text="fine")))

    def test_valid_ascii_text_still_completes_exactly_as_before(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1), E(K.TEXT, 5, text="hello world 123"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertTrue(eng.preflight().ok)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([(t[0], t[1]) for t in adb.texts], [("emulator-5554", "hello world 123")])
        self.assertEqual(len(adb.taps), 1)

    def test_dry_run_of_invalid_text_is_also_refused_before_the_freshness_gate_result_would_matter(self):
        """Preflight runs before _begin() ever touches the input session, dry or live alike, so an invalid-text
        macro is refused identically in both modes -- this is a value problem, not a freshness problem."""
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, E(K.TEXT, 0, text="bad'text"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        with self.assertRaises(PlaybackRefused) as cm:
            eng.run()
        self.assertIn("unsupported_value", [i.code for i in cm.exception.issues])
        self.assertEqual((adb.taps, adb.texts), ([], []))

    def test_dry_run_of_valid_text_still_enforces_freshness_and_records_no_text_content(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, E(K.TEXT, 0, text="ok"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        report = eng.run()
        self.assertEqual(report.state, S.COMPLETED)
        self.assertEqual(adb.texts, [])
        self.assertEqual(eng._input.intended, (IntendedInput("type_text", text_length=2),))
        self.assertNotIn("ok", repr(eng._input.intended))

    def test_unsupported_reason_is_a_generic_duck_typed_hook_not_an_ldplayer_import_in_preflight(self):
        """Architecture check for this stage's own fix: app/playback/preflight.py must not import or name
        anything ADB/LDPlayer-specific; it only calls an optional method on whatever `input` object it is given."""
        import inspect

        from app.playback import preflight as preflight_module
        src = inspect.getsource(preflight_module)
        for word in ("adb", "ldplayer", "safe_text_pattern"):
            self.assertNotIn(word, src.lower(), f"preflight.py mentions {word!r}")
        self.assertIn("unsupported_reason", src)


class BudgetMatchesTheSessionsOwnLimit(unittest.TestCase):
    def test_action_limit_is_the_input_sessions_own_bound(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertEqual(eng._input.action_limit, MAX_SESSION_ACTIONS)

    def test_a_macro_above_the_limit_is_a_typed_refusal_not_a_bare_value_error(self):
        _, svc, adb, stop, target, max_age = rig()
        events = tuple(E(K.BUTTON_DOWN if i % 2 == 0 else K.BUTTON_UP, 0 if i == 0 else 1, i % 50, 1, B.PRIMARY, 1)
                       for i in range(2 * (MAX_SESSION_ACTIONS + 1)))
        m = macro_for(target, *events)
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)      # must not raise here
        report = eng.preflight()
        self.assertIn("too_many_actions", [i.code for i in report.errors])
        with self.assertRaises(PlaybackRefused):
            eng.run()
        self.assertEqual(adb.taps, [])


class ProviderSubstitutability(unittest.TestCase):
    """The same Macro that already worked against the fake TapOnly stand-in (test_playback.py) also runs, unchanged,
    against the real adapter -- proving higher-level automation logic does not need to know which one is bound."""

    def test_same_macro_shape_runs_against_the_real_adapter(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 100, 200), up(40, 100, 200), down(500, 5, 5), up(30, 5, 5))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([(t[1], t[2]) for t in adb.taps], [(100, 200), (5, 5)])


class KeyAndTextRunThroughTheRealAdapter(unittest.TestCase):
    """The generic engine drives InputSession.key_press/type_text exactly the way it drives tap: through the real
    adapter, with the real freshness/budget gates, and zero ADB imports anywhere above create_ldplayer_playback."""

    def test_a_key_press_macro_completes_and_sends_the_android_keycode(self):
        from app.ldplayer.keymap import android_keycode

        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, E(K.KEY_DOWN, 0, key=Key.ENTER), E(K.KEY_UP, 20, key=Key.ENTER))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([(k[0], k[1]) for k in adb.keyevents], [("emulator-5554", android_keycode(Key.ENTER))])
        self.assertEqual(adb.taps, [])

    def test_a_text_macro_completes_and_sends_exactly_that_text(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, E(K.TEXT, 0, text="hi there"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual([(t[0], t[1]) for t in adb.texts], [("emulator-5554", "hi there")])

    def test_a_mixed_macro_runs_tap_key_and_text_in_order(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 9, 9), up(10, 9, 9), E(K.KEY_DOWN, 5, key=Key.A), E(K.KEY_UP, 20, key=Key.A),
                      E(K.TEXT, 5, text="ok"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual((len(adb.taps), len(adb.keyevents), len(adb.texts)), (1, 1, 1))

    def test_dry_run_sends_neither_a_keyevent_nor_text(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, E(K.KEY_DOWN, 0, key=Key.A), E(K.KEY_UP, 20, key=Key.A), E(K.TEXT, 5, text="ok"))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual((adb.keyevents, adb.texts), ([], []))
        self.assertEqual(len(eng._input.intended), 2)

    def test_dry_run_sends_nothing_and_still_completes(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 9, 9), up(10, 9, 9))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual(adb.taps, [])


class EmergencyStopAndIdentityChangeStillApply(unittest.TestCase):
    def test_emergency_stop_before_the_run_sends_nothing(self):
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1))
        stop.trigger("test")
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age)
        report = eng.run()
        self.assertEqual((report.state, report.cancel_reason), (S.CANCELLED, "emergency_stop"))
        self.assertEqual(adb.taps, [])

    def test_a_target_that_can_no_longer_be_reverified_is_refused_not_silently_swapped(self):
        import dataclasses

        from app.instances.profile import InstanceRegistry
        _, svc, adb, stop, target, max_age = rig()
        stale = dataclasses.replace(target, verified_at=0.0)      # its proof is now (artificially) ancient
        m = macro_for(stale, down(0, 1, 1), up(5, 1, 1))
        svc._registry = InstanceRegistry([])         # ld1's profile is gone: the next re-verification must fail
        eng = create_ldplayer_playback(m, stale, adb, svc, stop, max_age_s=max_age)
        report = eng.run()                            # the stale proof is caught mid-run, not raised out of run()
        self.assertEqual(report.state, S.FAILED)
        self.assertIsNotNone(report.failure)
        self.assertEqual(adb.taps, [])


class DryRunEnforcesTargetFreshness(unittest.TestCase):
    """H.1: a dry run must never report success for a target whose identity/freshness cannot currently be
    revalidated. It may still avoid sending real input, but the same real freshness/identity gate the live path
    uses (AdbSession._gate(), reached through InputSession.tap()'s own display() bounds check) must run first."""

    def stale_unrevalidatable(self):
        import dataclasses

        from app.instances.profile import InstanceRegistry
        _, svc, adb, stop, target, max_age = rig()
        stale = dataclasses.replace(target, verified_at=0.0)     # its proof is now (artificially) ancient
        svc._registry = InstanceRegistry([])                     # ld1's profile is gone: revalidation must fail
        return svc, adb, stop, stale, max_age

    def test_a_stale_target_must_not_report_completed_under_dry_run(self):
        svc, adb, stop, stale, max_age = self.stale_unrevalidatable()
        m = macro_for(stale, down(0, 1, 1), up(5, 1, 1))
        eng = create_ldplayer_playback(m, stale, adb, svc, stop, max_age_s=max_age, dry_run=True)
        report = eng.run()
        self.assertNotEqual(report.state, S.COMPLETED)
        self.assertEqual(report.state, S.FAILED)
        self.assertIsNotNone(report.failure)
        self.assertEqual(adb.taps, [])

    def test_a_target_that_cannot_be_revalidated_fails_the_same_way_live_and_dry(self):
        """The instance/device mapping has disappeared: dry-run and live must fail identically, not just 'not
        COMPLETED' -- the same gate, the same typed failure code, in both modes."""
        svc, adb, stop, stale, max_age = self.stale_unrevalidatable()
        m = macro_for(stale, down(0, 1, 1), up(5, 1, 1))
        dry_report = create_ldplayer_playback(m, stale, adb, svc, stop, max_age_s=max_age, dry_run=True).run()
        live_report = create_ldplayer_playback(m, stale, adb, svc, stop, max_age_s=max_age).run()
        self.assertEqual(dry_report.state, S.FAILED)
        self.assertEqual(live_report.state, S.FAILED)
        self.assertEqual(dry_report.failure.code, live_report.failure.code)
        self.assertEqual(adb.taps, [])

    def test_a_fresh_valid_target_still_completes_a_dry_run_and_the_gate_actually_ran(self):
        """The freshness/bounds gate must run even when nothing is wrong: proven by the real wm size/density
        queries appearing in the fake's call log, not merely inferred from the report."""
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 100, 200), up(30, 100, 200))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        report = eng.run()
        self.assertEqual(report.state, S.COMPLETED)
        self.assertEqual(adb.taps, [])
        self.assertEqual(eng._input.intended, (IntendedInput("tap", x=100, y=200),))
        shell_calls = [c[2] for c in adb.calls if c[0] == "shell"]
        self.assertIn(("wm", "size"), shell_calls)
        self.assertIn(("wm", "density"), shell_calls)

    def test_dry_run_never_calls_the_real_adb_input_delivery_method(self):
        """RecordingAdb.input_tap is the only place a real 'adb shell input tap' would be issued from; dry run
        must never reach it, however many actions the macro has."""
        _, svc, adb, stop, target, max_age = rig()
        m = macro_for(target, down(0, 1, 1), up(5, 1, 1), down(500, 20, 20), up(30, 20, 20))
        eng = create_ldplayer_playback(m, target, adb, svc, stop, max_age_s=max_age, dry_run=True)
        self.assertEqual(eng.run().state, S.COMPLETED)
        self.assertEqual(adb.taps, [])
        self.assertEqual(len(eng._input.intended), 2)

    def test_live_path_still_enforces_the_same_freshness_gate_and_serial_addressed_delivery(self):
        """Regression guard: this stage must not weaken the live gate to make dry-run and live look similar."""
        svc, adb, stop, stale, max_age = self.stale_unrevalidatable()
        m = macro_for(stale, down(0, 1, 1), up(5, 1, 1))
        report = create_ldplayer_playback(m, stale, adb, svc, stop, max_age_s=max_age).run()
        self.assertEqual(report.state, S.FAILED)
        self.assertEqual(adb.taps, [])
        # a fresh target's live run still delivers to exactly its own serial, unchanged from before this stage
        _, svc2, adb2, stop2, fresh, max_age2 = rig()
        ok = macro_for(fresh, down(0, 7, 7), up(5, 7, 7))
        live_report = create_ldplayer_playback(ok, fresh, adb2, svc2, stop2, max_age_s=max_age2).run()
        self.assertEqual(live_report.state, S.COMPLETED)
        self.assertEqual([t[0] for t in adb2.taps], ["emulator-5554"])


if __name__ == "__main__":
    unittest.main()
