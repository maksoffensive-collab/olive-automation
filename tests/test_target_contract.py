"""Provider-neutral Target contract (app/providers/target.py) and its LDPlayer adaptation."""
import ast
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import CapabilityNotAvailable, TargetNotVerified
from app.instances.mapping import VerifiedTarget
from app.ldplayer.ids import CAPABILITIES, PROVIDER_ID
from app.ldplayer.provider import LDPlayerScreenshotProvider
from app.providers.base import TargetIdentity
from app.providers.target import (Capability, Target, TargetVerification, VerificationState, require_capability,
                                  require_verified)
from tests.test_mapping import make_service, two_instance_world

ROOT = Path(__file__).resolve().parents[1] / "app"


class WindowStyleTarget(Target):
    """A stand-in for a future desktop/window provider. Defined here only: no such provider exists."""

    def __init__(self, target_id="win-notepad-1", caps=(Capability.SCREENSHOT,), state=VerificationState.VERIFIED,
                 methods=("process_match", "window_match"), provider_id="desktop-window"):
        self._identity = TargetIdentity(provider_id, target_id, "Untitled - Editor")
        self._caps = frozenset(caps)
        self._verification = TargetVerification(state, methods if state is VerificationState.VERIFIED else (), 1.0)

    identity = property(lambda self: self._identity)
    capabilities = property(lambda self: self._caps)
    verification = property(lambda self: self._verification)


def ld_target(ld="ld1"):
    svc = make_service(two_instance_world())
    return svc.verify(ld), svc


class NeutralTargetRepresentation(unittest.TestCase):
    def test_a_window_style_target_is_representable_without_any_provider_implementation(self):
        t = WindowStyleTarget()
        self.assertEqual((t.provider_id, t.target_id), ("desktop-window", "win-notepad-1"))
        self.assertEqual(t.identity.display_name, "Untitled - Editor")
        self.assertIs(require_verified(t), t)
        self.assertIn("desktop-window:win-notepad-1", t.describe())
        # a whole-desktop target and a device-style target fit the same contract
        self.assertEqual(WindowStyleTarget("desktop-0", provider_id="desktop-screen").provider_id, "desktop-screen")

    def test_unverified_or_non_targets_are_refused(self):
        with self.assertRaises(TargetNotVerified):
            require_verified(WindowStyleTarget(state=VerificationState.UNVERIFIED))
        for bogus in ("emulator-5572", 9, None, ("p", "t"), object(), TargetIdentity("p", "t")):
            with self.assertRaises(TargetNotVerified):
                require_verified(bogus)

    def test_verification_provenance_is_validated(self):
        with self.assertRaises(ValueError):
            TargetVerification(VerificationState.VERIFIED, ())
        with self.assertRaises(ValueError):
            TargetVerification(VerificationState.VERIFIED, ("",))
        with self.assertRaises(ValueError):
            TargetVerification("verified", ("a",))  # type: ignore[arg-type]
        self.assertFalse(TargetVerification(VerificationState.UNVERIFIED).is_verified)
        self.assertEqual(TargetVerification(VerificationState.VERIFIED, ("a",), 10.0).age_s(now=15.0), 5.0)

    def test_the_abstract_target_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Target()  # type: ignore[abstract]

    def test_malformed_capability_declarations_are_refused(self):
        class Sloppy(WindowStyleTarget):
            capabilities = property(lambda self: {"screenshot"})  # strings, not Capability members
        with self.assertRaises(TargetNotVerified):
            require_verified(Sloppy())


class CapabilitiesAreExplicit(unittest.TestCase):
    def test_capabilities_are_exactly_what_is_declared(self):
        shot_only = WindowStyleTarget(caps=(Capability.SCREENSHOT,))
        tap_only = WindowStyleTarget(caps=(Capability.TAP,))
        none = WindowStyleTarget(caps=())
        self.assertTrue(shot_only.has_capability(Capability.SCREENSHOT))
        self.assertFalse(shot_only.has_capability(Capability.TAP))
        self.assertTrue(tap_only.has_capability(Capability.TAP))
        self.assertFalse(none.has_capability(Capability.SCREENSHOT))
        with self.assertRaises(CapabilityNotAvailable):
            require_capability(shot_only, Capability.TAP)
        self.assertIs(require_capability(shot_only, Capability.SCREENSHOT), shot_only)

    def test_same_provider_id_different_capabilities(self):
        a = WindowStyleTarget("a", caps=(Capability.SCREENSHOT, Capability.TAP))
        b = WindowStyleTarget("b", caps=(Capability.SCREENSHOT,))
        self.assertEqual(a.provider_id, b.provider_id)
        self.assertNotEqual(a.capabilities, b.capabilities)  # never derived from the provider id

    def test_a_provider_id_that_looks_like_ldplayer_gets_no_capabilities_for_free(self):
        t = WindowStyleTarget(provider_id=PROVIDER_ID, caps=())
        self.assertEqual(t.capabilities, frozenset())
        with self.assertRaises(CapabilityNotAvailable):
            require_capability(t, Capability.SCREENSHOT)

    def test_capability_gate_also_requires_verification(self):
        with self.assertRaises(TargetNotVerified):
            require_capability(WindowStyleTarget(state=VerificationState.UNVERIFIED), Capability.SCREENSHOT)

    def test_capability_vocabulary_is_closed_and_contains_only_the_documented_members(self):
        self.assertEqual({c.value for c in Capability}, {"screenshot", "tap", "secondary_tap", "double_tap",
                                                         "pointer_move", "pointer_button", "key", "text", "record"})


class LDPlayerAdaptation(unittest.TestCase):
    def test_verified_target_is_a_neutral_target(self):
        vt, _ = ld_target()
        self.assertIsInstance(vt, Target)
        self.assertEqual(vt.identity, TargetIdentity(PROVIDER_ID, "ld1", vt.ld_name))
        self.assertEqual(vt.capabilities, CAPABILITIES)
        self.assertEqual(vt.capabilities, frozenset({Capability.SCREENSHOT, Capability.TAP, Capability.KEY,
                                                     Capability.TEXT}))
        self.assertIs(require_verified(vt), vt)

    def test_verification_provenance_carries_both_independent_checks(self):
        vt, _ = ld_target()
        v = vt.verification
        self.assertTrue(v.is_verified)
        self.assertEqual(set(v.methods), {"config_identity", "process_owner"})
        self.assertEqual(v.verified_at, vt.verified_at)

    def test_neutral_view_exposes_no_serial_or_process_data(self):
        vt, _ = ld_target()
        text = repr((vt.identity, vt.capabilities, vt.verification, vt.describe()))
        self.assertNotIn(vt.adb_serial, repr((vt.identity, vt.capabilities, vt.verification)))
        self.assertFalse(hasattr(vt.identity, "adb_serial"))
        self.assertTrue(text)

    def test_ldplayer_targets_still_cannot_be_forged(self):
        vt, _ = ld_target()
        with self.assertRaises(TargetNotVerified):
            VerifiedTarget("ld1", 1, "n", "emulator-5554", 1, 2, 3, vt.evidence, 0.0)
        with self.assertRaises(TargetNotVerified):
            VerifiedTarget("ld1", 1, "n", "emulator-5554", 1, 2, 3, vt.evidence, 0.0, _mint=object())

    def test_a_neutral_target_from_another_provider_is_not_accepted_by_the_ldplayer_adapter(self):
        svc = make_service(two_instance_world())
        for foreign in (WindowStyleTarget(), TargetIdentity(PROVIDER_ID, "ld1"), "emulator-5572"):
            with self.assertRaises(TargetNotVerified):
                LDPlayerScreenshotProvider(foreign, svc._adb, svc, EmergencyStop())  # type: ignore[arg-type]

    def test_ldplayer_mapping_still_refuses_unverifiable_targets(self):
        w = two_instance_world()
        svc = make_service(w)
        w.instances.clear()  # the instance vanished: identity cannot be proven
        with self.assertRaises(TargetNotVerified):
            svc.verify("ld1")

    def test_adapter_identity_comes_from_the_target_contract(self):
        vt, svc = ld_target()
        p = LDPlayerScreenshotProvider(vt, svc._adb, svc, EmergencyStop())
        self.assertEqual(p.identity, vt.identity)


class GenericCodeIsTechnologyFree(unittest.TestCase):
    def _imports(self, path):
        out = set()
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
        return out

    def test_target_contract_imports_only_core_and_the_generic_base(self):
        allowed = {"app.core.errors", "app.providers.base"}
        bad = {m for m in self._imports(ROOT / "providers" / "target.py") if m.startswith("app.")} - allowed
        self.assertEqual(bad, set())

    def test_vision_and_core_never_mention_ldplayer_target_types(self):
        for pkg in ("core", "vision", "providers"):
            for path in (ROOT / pkg).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("VerifiedTarget", text, path.name)
                self.assertNotIn("adb_serial", text, path.name)

    def test_clean_interpreter_uses_targets_and_vision_with_no_technology_module_loaded(self):
        code = textwrap.dedent("""
            import sys
            from app.providers.base import TargetIdentity
            from app.providers.target import Capability, Target, TargetVerification, VerificationState, require_capability
            import app.vision.frame, app.vision.color

            class T(Target):
                identity = property(lambda s: TargetIdentity("x", "y"))
                capabilities = property(lambda s: frozenset({Capability.SCREENSHOT}))
                verification = property(lambda s: TargetVerification(VerificationState.VERIFIED, ("m",), 0.0))

            require_capability(T(), Capability.SCREENSHOT)
            bad = [m for m in sys.modules if m.split(".")[:2] in (["app","adb"],["app","ldplayer"],["app","instances"],["app","macros"])]
            assert not bad, bad
            print("CLEAN")
        """)
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "CLEAN", r.stderr)


if __name__ == "__main__":
    unittest.main()
