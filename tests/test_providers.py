"""Provider-independence tests.

Dependency direction that must hold:   adb -> (nothing generic)     ldplayer adapter -> adb + providers
                                        vision -> providers -> core    (vision/providers never import a target technology)
"""
import ast
import inspect
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from app.adb.input import InputSession
from app.adb.session import AdbSession
from app.core.cancel import EmergencyStop
from app.core.errors import SafetyViolation, TargetNotVerified
from app.ldplayer.provider import PROVIDER_ID, LDPlayerScreenshotProvider
from app.providers.base import (FrameSource, InputProvider, Screenshot, ScreenshotProvider, TargetIdentity)
from app.vision.bitmap import Bitmap, VisionError
from app.vision.change import compare_frames
from app.vision.color import find_color
from app.vision.frame import Frame
from app.vision.roi import Roi
from app.vision.template import match_template
from tests.test_adb_input import build as build_input
from tests.test_adb_session import SessionAdb
from tests.test_mapping import make_service, two_instance_world
from tests.test_vision import RED, _SizedAdb, encode_png, scene

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "app"


class SyntheticWindowProvider(ScreenshotProvider):
    """A stand-in for a future non-ADB provider: no ADB, no LDPlayer, no VerifiedTarget."""

    def __init__(self, provider_id: str, target_id: str, bitmap: Bitmap) -> None:
        self._identity = TargetIdentity(provider_id, target_id, "synthetic")
        self.bitmap = bitmap

    @property
    def identity(self) -> TargetIdentity:
        return self._identity

    def capture(self) -> Screenshot:
        return self._issue_screenshot(encode_png(self.bitmap), self.bitmap.width, self.bitmap.height, "opaque detail")


def ld_provider(bm=None, ld="ld1"):
    """The LDPlayer adapter on the fake world, built the only allowed way: from a VerifiedTarget."""
    bm = bm or Bitmap.solid(1280, 720, (10, 20, 30))
    w = two_instance_world()
    svc = make_service(w)
    adb = _SizedAdb(w, bm)
    svc._adb = adb
    stop = EmergencyStop()
    return LDPlayerScreenshotProvider(svc.verify(ld), adb, svc, stop), svc, adb, stop, w


def imports_of(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


class GenericContract(unittest.TestCase):
    def test_target_identity_validation_and_source(self):
        ident = TargetIdentity("prov", "t1", "Name")
        self.assertEqual(ident.source("d"), FrameSource("prov", "t1", "d"))
        for bad in (("", "t"), ("p", ""), (None, "t"), ("p", 5)):
            with self.assertRaises(ValueError):
                TargetIdentity(*bad)
        with self.assertRaises(ValueError):
            FrameSource("", "t")

    def test_detail_is_informational_only(self):
        self.assertTrue(FrameSource("p", "t", "one").same_target(FrameSource("p", "t", "two")))
        self.assertFalse(FrameSource("p", "t").same_target(FrameSource("q", "t")))

    def test_screenshots_only_come_from_providers(self):
        with self.assertRaises(SafetyViolation):
            Screenshot(b"x", 1, 1, FrameSource("p", "t"))
        with self.assertRaises(TypeError):
            ScreenshotProvider()  # abstract

    def test_provider_stamps_its_own_identity(self):
        shot = SyntheticWindowProvider("prov", "t1", scene()).capture()
        self.assertEqual(shot.source, FrameSource("prov", "t1", "opaque detail"))

    def test_contract_has_no_way_to_pass_a_raw_address(self):
        """capture() takes no target argument at all; the target is fixed when the provider is built."""
        self.assertEqual(list(inspect.signature(ScreenshotProvider.capture).parameters), ["self"])
        self.assertEqual(list(inspect.signature(ScreenshotProvider._issue_screenshot).parameters),
                         ["self", "png", "width", "height", "detail"])
        for bogus in ("emulator-5554", 5554, ("emulator-5554", "ld1")):
            with self.assertRaises(VisionError):
                Frame.from_screenshot(bogus)
            with self.assertRaises((SafetyViolation, TypeError, AttributeError)):
                Screenshot(b"png", 1, 1, bogus)


class VisionWorksForAnyProvider(unittest.TestCase):
    def test_full_vision_pipeline_on_a_non_adb_provider(self):
        prov = SyntheticWindowProvider("prov-a", "target-1", scene())
        f = Frame.from_screenshot(prov.capture())
        self.assertEqual((f.source.provider_id, f.target_id, f.width, f.height), ("prov-a", "target-1", 200, 100))
        self.assertEqual(find_color(f, RED).bounds, Roi(30, 20, 10, 10))
        (m,) = match_template(f, scene().crop(28, 18, 14, 14), min_confidence=0.99)
        self.assertEqual((m.x, m.y), (28, 18))
        self.assertFalse(compare_frames(f, Frame.from_screenshot(prov.capture())).changed)

    def test_frames_of_different_providers_or_targets_are_never_compared(self):
        a = Frame.from_screenshot(SyntheticWindowProvider("prov-a", "t1", scene()).capture())
        for prov, tid in (("prov-b", "t1"), ("prov-a", "t2")):
            b = Frame.from_screenshot(SyntheticWindowProvider(prov, tid, scene()).capture())
            with self.assertRaises(VisionError):
                compare_frames(a, b)

    def test_generic_pipeline_runs_in_a_clean_interpreter_without_any_target_technology_loaded(self):
        code = textwrap.dedent('''
            import struct, sys, zlib
            from app.providers.base import ScreenshotProvider, TargetIdentity
            from app.vision.frame import Frame
            from app.vision.color import find_color

            def png(w, h, rgb):
                def chunk(k, d):
                    return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xFFFFFFFF)
                raw = b"".join(b"\\x00" + bytes(rgb) * w for _ in range(h))
                return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

            class P(ScreenshotProvider):
                identity = TargetIdentity("clean-provider", "t1")
                def capture(self):
                    return self._issue_screenshot(png(4, 3, (9, 8, 7)), 4, 3)

            f = Frame.from_screenshot(P().capture())
            assert find_color(f, (9, 8, 7)).count == 12 and f.source.provider_id == "clean-provider"
            bad = [m for m in sys.modules if m.startswith(("app.adb", "app.ldplayer", "app.instances", "app.macros"))]
            assert not bad, bad
            print("clean")
        ''')
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "clean"), out.stderr)


class LDPlayerAdapter(unittest.TestCase):
    def test_requires_a_verified_target(self):
        _, svc, adb, stop, _ = ld_provider()
        for bad in ("emulator-5554", 5554, None, ("emulator-5554", "ld1")):
            with self.assertRaises(TargetNotVerified):
                LDPlayerScreenshotProvider(bad, adb, svc, stop)

    def test_is_a_generic_provider_with_generic_identity(self):
        p, *_ = ld_provider()
        self.assertIsInstance(p, ScreenshotProvider)
        self.assertEqual(p.identity, TargetIdentity(PROVIDER_ID, "ld1", "LDPlayer-1"))
        self.assertEqual(PROVIDER_ID, "ldplayer-adb")

    def test_capture_returns_generic_screenshot_and_keeps_provenance_through_frame_and_vision(self):
        bm = Bitmap.solid(1280, 720, (10, 20, 30)).with_rect(100, 50, 20, 10, RED)
        p, *_ = ld_provider(bm)
        shot = p.capture()
        self.assertIsInstance(shot, Screenshot)
        self.assertEqual((shot.width, shot.height), (1280, 720))
        self.assertEqual((shot.source.provider_id, shot.source.target_id), ("ldplayer-adb", "ld1"))
        frame = Frame.from_screenshot(shot)
        self.assertEqual(frame.source, shot.source)             # provenance survives provider -> Frame
        self.assertEqual(frame.target_id, "ld1")
        self.assertEqual(find_color(frame, RED).bounds, Roi(100, 50, 20, 10))  # ...and Frame -> vision works
        self.assertTrue(frame.source.same_target(p.identity.source()))
        # a later frame of the same provider/target is comparable; one of another target is not
        again = Frame.from_screenshot(p.capture())
        self.assertFalse(compare_frames(frame, again).changed)
        other, *_ = ld_provider(bm, ld="ld2")
        with self.assertRaises(VisionError):
            compare_frames(frame, Frame.from_screenshot(other.capture()))

    def test_generic_screenshot_does_not_carry_a_usable_serial_field(self):
        p, *_ = ld_provider()
        shot = p.capture()
        self.assertFalse(hasattr(shot, "serial"))
        self.assertFalse(hasattr(shot.source, "serial"))
        self.assertFalse(hasattr(Frame.from_screenshot(shot), "serial"))

    def test_safety_gate_still_applies_through_the_adapter(self):
        from app.adb.client import DeviceState
        p, _, adb, stop, w = ld_provider()
        stop.trigger()
        with self.assertRaises(Exception) as cm:
            p.capture()
        self.assertEqual(type(cm.exception).__name__, "EmergencyStopped")
        stop.reset()
        w.devices[0].state = DeviceState.OFFLINE
        with self.assertRaises(TargetNotVerified):
            p.capture()

    def test_invalid_png_is_never_wrapped_into_a_screenshot(self):
        p, _, adb, _, _ = ld_provider()
        adb.screencap_png = lambda serial, *, grant: b"garbage"
        with self.assertRaises(Exception):
            p.capture()

    def test_adb_input_session_satisfies_the_generic_input_interface(self):
        _, _, _, s = build_input()
        self.assertIsInstance(s, InputProvider)
        self.assertEqual(s.target_id, "ld1")
        self.assertNotIsInstance(SyntheticWindowProvider("p", "t", scene()), InputProvider)  # screenshots only

    def test_adb_session_is_not_a_generic_provider(self):
        self.assertFalse(issubclass(AdbSession, ScreenshotProvider))


class ArchitectureBoundaries(unittest.TestCase):
    TARGET_SPECIFIC = ("app.adb", "app.ldplayer", "app.instances", "app.macros", "app.automation", "app.recovery",
                       "app.windows")
    NEUTRAL_WORDS = ("ldplayer", "adb", "windows", "viking", "serial", "subprocess", "socket", "urllib",
                     "requests", "http")

    def test_vision_and_providers_do_not_import_any_target_technology(self):
        for pkg in ("vision", "providers"):
            for path in (ROOT / pkg).glob("*.py"):
                bad = {m for m in imports_of(path) if m.startswith(self.TARGET_SPECIFIC)}
                self.assertEqual(bad, set(), f"{path.name} imports {bad}")

    def test_vision_only_uses_core_providers_and_itself(self):
        for path in (ROOT / "vision").glob("*.py"):
            for m in imports_of(path):
                if m.startswith("app."):
                    self.assertIn(m.split(".")[1], ("core", "providers", "vision"), f"{path.name}: {m}")

    def test_providers_only_depend_on_core(self):
        for path in (ROOT / "providers").glob("*.py"):
            for m in imports_of(path):
                if m.startswith("app."):
                    self.assertIn(m.split(".")[1], ("core", "providers"), f"{path.name}: {m}")

    def test_neutral_layers_do_not_even_mention_target_technologies(self):
        for pkg in ("vision", "providers"):
            text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / pkg).glob("*.py")).lower()
            for word in self.NEUTRAL_WORDS:
                self.assertNotIn(word, text, f"'{word}' found in app/{pkg}")
            # the LDPlayer ".record" file extension; the neutral ".recording" vocabulary is not that
            self.assertIsNone(re.search(r"\.record\b", text), f"'.record' found in app/{pkg}")

    def test_adb_layer_does_not_depend_on_the_generic_provider_layer_and_adapter_bridges_both(self):
        for path in (ROOT / "adb").glob("*.py"):
            self.assertFalse({m for m in imports_of(path) if m.startswith(("app.providers", "app.vision"))}, path.name)
        bridge = imports_of(ROOT / "ldplayer" / "provider.py")
        self.assertIn("app.adb.session", bridge)
        self.assertIn("app.providers.base", bridge)

    def test_ldplayer_record_coordinates_live_outside_the_generic_layers(self):
        self.assertTrue((ROOT / "macros" / "record_coords.py").is_file())
        self.assertNotIn("RecordPoint", (ROOT / "vision" / "coords.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class ArchitectureAudit(unittest.TestCase):
    """Freezes the audited boundaries so new accidental coupling fails a test and has to be documented."""
    import re as _re
    NEUTRAL_PACKAGES = ("core", "logging", "providers", "vision", "recovery", "playback", "automation", "ai", "ocr",
                        "observe", "decide")
    TECH_PACKAGES = ("adb", "ldplayer", "instances", "windows")
    TECH_WORDS = _re.compile(r"(?<![a-z])(adb|ldplayer|leidian|ldconsole|viking|emulator)|\.record\b", _re.I)
    # KNOWN, DOCUMENTED, TEMPORARY coupling (docs/ARCHITECTURE.md "Remaining LDPlayer coupling points").
    # A-01 (orchestrator typed on the LDPlayer VerifiedTarget) was removed: automation is neutral, no edge remains.
    KNOWN_EDGES = {("adb", "instances"), ("instances", "adb"), ("instances", "ldplayer"),
                   ("ldplayer", "adb"), ("ldplayer", "instances"), ("macros", "instances"),
                   # composition root: the ONE deliberate place that wires the GUI to each provider
                   ("bootstrap", "windows"), ("bootstrap", "ldplayer"), ("bootstrap", "adb"),
                   ("bootstrap", "instances")}

    def _package_of(self, path: Path) -> str:
        return path.relative_to(ROOT).parts[0]

    def test_neutral_packages_import_no_technology_and_use_no_technology_words(self):
        for pkg in self.NEUTRAL_PACKAGES:
            for path in (ROOT / pkg).glob("*.py"):
                bad = {m for m in imports_of(path) if m.startswith(("app.adb", "app.ldplayer", "app.instances",
                                                                     "app.macros", "app.automation", "app.windows"))}
                self.assertEqual(bad, set(), f"{path.name} imports {bad}")
                hits = self.TECH_WORDS.findall(path.read_text(encoding="utf-8"))
                self.assertEqual(hits, [], f"app/{pkg}/{path.name} mentions {hits}")

    def test_core_logging_and_recovery_do_not_depend_on_other_app_packages_except_core(self):
        for pkg in ("core", "logging", "recovery"):
            for path in (ROOT / pkg).glob("*.py"):
                for m in imports_of(path):
                    if m.startswith("app."):
                        self.assertIn(m.split(".")[1], ("core", pkg), f"{path.name}: {m}")

    def test_dependencies_on_technology_packages_are_exactly_the_documented_ones(self):
        edges = set()
        for path in ROOT.rglob("*.py"):
            src = self._package_of(path)
            for m in imports_of(path):
                parts = m.split(".")
                if parts[0] == "app" and len(parts) > 1 and parts[1] in self.TECH_PACKAGES and parts[1] != src:
                    edges.add((src, parts[1]))
        self.assertEqual(edges, self.KNOWN_EDGES,
                         "New dependency on a technology package: fix it or document it as coupling and update KNOWN_EDGES")

    def test_no_game_specific_knowledge_in_application_code(self):
        for path in ROOT.rglob("*.py"):
            self.assertNotIn("viking", path.read_text(encoding="utf-8").lower(), str(path))

    def test_log_events_have_no_adb_specific_field(self):
        from app.logging.structured import LogEvent, Logger
        self.assertNotIn("adb_serial", LogEvent.__dataclass_fields__)
        self.assertIn("target", LogEvent.__dataclass_fields__)
        self.assertNotIn("adb_serial", inspect.signature(Logger.bind).parameters)
