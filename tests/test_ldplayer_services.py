"""`LDPlayerServices` (app.bootstrap.ldplayer_services): the real vertical integration of the LDPlayer/ADB
provider into the existing, unmodified `AutomationServices` contract the GUI already talks to -- mirroring
`WindowsServices` exactly. Fakes only (the same `two_instance_world`/console/registry/ADB fakes already used by
`tests/test_mapping.py`, `tests/test_adb_input.py`, `tests/test_ldplayer_playback.py`, `tests/test_vision.py`); no
real LDPlayer installation, no real adb.exe.

What this proves, concretely:
  * discover() lists registered instances with an explicit, best-known running/offline label, WITHOUT implying
    verification -- an offline instance is listed, but verify() on it fails exactly like it would for a real one;
  * verify() produces the SAME VerifiedTarget MappingService.verify() would, never a parallel identity type;
  * capture()/create_playback() delegate to the existing LDPlayerScreenshotProvider / create_ldplayer_playback,
    not a second implementation;
  * create_recorder() refuses cleanly (CapabilityNotAvailable), matching the fact that no LDPlayer target ever
    declares Capability.RECORD;
  * dry-run still enforces H.1's freshness gate through this new integration path, unchanged;
  * app/gui and app/playback still import neither app.ldplayer nor app.adb -- only the new bootstrap file does.
"""
import ast
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import CapabilityNotAvailable, SafetyViolation, TargetNotVerified
from app.instances.mapping import VerifiedTarget, adb_serial_ports
from app.ldplayer.config_reader import parse_instance_config
from app.playback.engine import PlaybackState as S
from app.playback.macro import Macro
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.providers.target import Target
from app.vision.bitmap import Bitmap
from tests.test_adb_input import RecordingAdb
from tests.test_mapping import Dev, Inst, World, two_instance_world
from tests.test_vision import encode_png

ROOT = Path(__file__).resolve().parents[1] / "app"


class FakeConsole:
    """Duck-types app.ldplayer.console.LDConsole's read-only surface, over a fakes.World."""

    def __init__(self, world: World) -> None:
        self.world = world

    def list_instances(self):
        return self.world.ld_instances()

    def running_names(self):
        return [i.name for i in self.world.instances if i.running]


class ScreenshotCapableAdb(RecordingAdb):
    """RecordingAdb (records taps) plus a real, decodable screenshot for capture() tests."""

    def screencap_png(self, serial, *, grant):
        return encode_png(Bitmap.solid(1280, 720, (10, 20, 30)))


def read_config_for(world: World):
    def read_config(index):
        inst = next(i for i in world.instances if i.index == index)
        data = {"propertySettings.phoneAndroidId": inst.android_id, "propertySettings.phoneIMEI": f"imei{index}",
               "propertySettings.macAddress": f"mac{index}"}
        return parse_instance_config(index, Path("x"), data)
    return read_config


def port_owners_for(world: World):
    def owners():
        out = {}
        for d in world.devices:
            if d.owner_pid is None:
                continue
            for p in adb_serial_ports(d.serial):
                out.setdefault(p, set()).add(d.owner_pid)
        return out
    return owners


def rig(world: World | None = None, *, screenshots: bool = False, stop: EmergencyStop | None = None):
    """`stop` lets a caller share one EmergencyStop across several services (see tests/test_composite_services.py);
    by default this builds its own, exactly as before."""
    from app.bootstrap.ldplayer_services import LDPlayerServices
    world = world or two_instance_world()
    adb = ScreenshotCapableAdb(world) if screenshots else RecordingAdb(world)
    stop = stop or EmergencyStop()
    svc = LDPlayerServices(FakeConsole(world), adb, read_config_for(world), port_owners_for(world), stop)
    return svc, adb, world, stop


def macro_for(target, *events):
    return Macro(Recording(target.provider_id, target.target_id, tuple(events)))


class Discovery(unittest.TestCase):
    def test_registered_instances_appear_with_a_running_label(self):
        svc, *_ = rig()
        entries = svc.discover()
        self.assertEqual({e.ref for e in entries}, {"ld1", "ld2"})
        self.assertTrue(all(e.provider_id == "ldplayer-adb" and e.kind == "instance" for e in entries))
        self.assertTrue(all("running" in e.size for e in entries))

    def test_instances_are_offered_as_usable_while_their_window_is_not_in_front(self):
        """This provider reaches its target over its own connection, so the GUI may tell a person that choosing
        it does not take over their screen. Verified end to end on a live instance: a tap sent while the window
        was minimised changed the device's screen. Windows-window candidates must NOT claim this."""
        svc, *_ = rig()
        self.assertTrue(all(e.background for e in svc.discover()))

    def test_every_instance_shares_one_group_regardless_of_how_many_are_registered(self):
        """The GUI groups a provider's candidates by TargetEntry.group into one row instead of one-per-instance
        (see app/gui/app.py's _render_targets); every instance this provider lists must actually share that one
        group name, offline or not, for that grouping to hold together as ONE group and not several."""
        world = World(instances=[Inst(1, "LDPlayer-1", running=True, pid=100, vbox=200, android_id="a1"),
                                 Inst(2, "LDPlayer-2", running=False, pid=-1, vbox=-1, android_id="a2"),
                                 Inst(3, "LDPlayer-3", running=True, pid=101, vbox=201, android_id="a3")],
                      devices=[])
        svc, *_ = rig(world)
        entries = svc.discover()
        self.assertEqual(len(entries), 3)
        self.assertEqual({e.group for e in entries}, {"LDPlayer"})       # one group, not one per instance

    def test_an_offline_instance_is_listed_but_never_appears_verified(self):
        world = World(instances=[Inst(1, "LDPlayer-1", running=False, pid=-1, vbox=-1, android_id="aaaa1111")],
                     devices=[])
        svc, *_ = rig(world)
        entries = svc.discover()
        self.assertEqual(len(entries), 1)
        self.assertIn("offline", entries[0].size)
        self.assertNotIn("running", entries[0].size)
        self.assertEqual(entries[0].group, "LDPlayer")                  # still grouped, even though it is offline
        with self.assertRaises(TargetNotVerified) as cm:
            svc.verify(entries[0])
        self.assertEqual(cm.exception.reason_code, "not_running")

    def test_discovery_never_silently_equates_registered_running_and_adb_present(self):
        """A running LDPlayer process with NO matching ADB device must still fail verification distinctly."""
        world = World(instances=[Inst(1, "LDPlayer-1", running=True, pid=1100, vbox=1200, android_id="aaaa1111")],
                     devices=[])                                       # running, but no ADB device at all
        svc, *_ = rig(world)
        entries = svc.discover()
        self.assertIn("running", entries[0].size)                      # discovery's best-known label says running
        with self.assertRaises(TargetNotVerified) as cm:
            svc.verify(entries[0])                                     # verification still requires a real device
        self.assertEqual(cm.exception.reason_code, "no_adb_device")


class Verification(unittest.TestCase):
    def test_a_valid_instance_produces_the_existing_verified_target_type(self):
        svc, *_ = rig()
        entries = svc.discover()
        target = svc.verify(next(e for e in entries if e.ref == "ld1"))
        self.assertIsInstance(target, VerifiedTarget)
        self.assertIsInstance(target, Target)
        self.assertEqual((target.logical_id, target.adb_serial), ("ld1", "emulator-5554"))

    def test_an_entry_not_in_the_current_list_is_refused_not_guessed(self):
        svc, *_ = rig()
        svc.discover()
        from app.gui.services import TargetEntry
        fake_entry = TargetEntry("ld999", "Ghost", "LDPlayer", "instance", "index 999, running", "ldplayer-adb",
                                 "LDPlayer")
        with self.assertRaises(TargetNotVerified):
            svc.verify(fake_entry)

    def test_verifying_before_any_discovery_is_refused_not_a_crash(self):
        svc, *_ = rig()
        from app.gui.services import TargetEntry
        with self.assertRaises(TargetNotVerified):
            svc.verify(TargetEntry("ld1", "x", "LDPlayer", "instance", "index 1, running", "ldplayer-adb",
                                   "LDPlayer"))

    def test_gui_code_cannot_construct_a_verified_target_directly(self):
        """The only way to get a VerifiedTarget is through the service -> MappingService.verify() chain."""
        with self.assertRaises(TargetNotVerified):
            VerifiedTarget("ld1", 1, "LDPlayer-1", "emulator-5554", 1, 1, 0, (), 0.0)


class Recheck(unittest.TestCase):
    def test_a_valid_target_can_be_revalidated(self):
        svc, *_ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        entry = svc.recheck(target)
        self.assertEqual(entry.ref, "ld1")
        self.assertIn("running", entry.size)

    def test_recheck_of_a_wrong_target_type_is_refused(self):
        svc, *_ = rig()
        svc.discover()
        with self.assertRaises(SafetyViolation):
            svc.recheck("not a target")

    def test_recheck_fails_the_same_way_verify_would_if_the_instance_disappeared(self):
        world = two_instance_world()
        svc, adb, _, stop = rig(world)
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        world.instances.clear()                          # ld1 no longer exists in LDPlayer at all
        with self.assertRaises(TargetNotVerified):
            svc.recheck(target)


class Capture(unittest.TestCase):
    def test_capture_delegates_to_the_existing_screenshot_provider_and_keeps_provenance(self):
        svc, adb, _, _ = rig(screenshots=True)
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        shot = svc.capture(target)
        self.assertEqual((shot.width, shot.height), (1280, 720))
        self.assertEqual((shot.source.provider_id, shot.source.target_id), ("ldplayer-adb", "ld1"))


class Playback(unittest.TestCase):
    def test_create_playback_returns_the_existing_generic_engine(self):
        from app.playback.engine import PlaybackEngine
        svc, adb, _, _ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        m = macro_for(target, E(K.BUTTON_DOWN, 0, 1, 1, B.PRIMARY, 1), E(K.BUTTON_UP, 5, 1, 1, B.PRIMARY, 1))
        eng = svc.create_playback(m, target, dry_run=False, allow_different_target=False)
        self.assertIsInstance(eng, PlaybackEngine)

    def test_tap_only_macro_executes_through_the_fake_adb_path(self):
        svc, adb, _, _ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        m = macro_for(target, E(K.BUTTON_DOWN, 0, 100, 200, B.PRIMARY, 1), E(K.BUTTON_UP, 5, 100, 200, B.PRIMARY, 1))
        report = svc.create_playback(m, target, dry_run=False, allow_different_target=False).run()
        self.assertEqual(report.state, S.COMPLETED)
        self.assertEqual([(t[1], t[2]) for t in adb.taps], [(100, 200)])

    def test_dry_run_produces_no_input_delivery_and_still_enforces_the_freshness_gate(self):
        """Reuses H.1: this integration path must not duplicate or bypass that gate."""
        import dataclasses

        svc, adb, world, stop = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        m = macro_for(target, E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1), E(K.BUTTON_UP, 5, 5, 5, B.PRIMARY, 1))

        ok_report = svc.create_playback(m, target, dry_run=True, allow_different_target=False).run()
        self.assertEqual(ok_report.state, S.COMPLETED)
        self.assertEqual(adb.taps, [])

        stale = dataclasses.replace(target, verified_at=0.0)
        world.instances.clear()                           # the instance is now gone: revalidation must fail
        stale_macro = macro_for(stale, E(K.BUTTON_DOWN, 0, 5, 5, B.PRIMARY, 1), E(K.BUTTON_UP, 5, 5, 5, B.PRIMARY, 1))
        bad_report = svc.create_playback(stale_macro, stale, dry_run=True, allow_different_target=False).run()
        self.assertNotEqual(bad_report.state, S.COMPLETED)
        self.assertEqual(adb.taps, [])


class RecorderRefusal(unittest.TestCase):
    def test_ldplayer_has_no_record_capability(self):
        svc, *_ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        self.assertNotIn("record", {c.value for c in target.capabilities})

    def test_create_recorder_is_refused_through_the_typed_capability_error(self):
        svc, *_ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        with self.assertRaises(CapabilityNotAvailable):
            svc.create_recorder(target)

    def test_the_controllers_own_capability_gate_would_refuse_it_first_anyway(self):
        """Belt and suspenders: require_capability (what AppController.start_recording calls before
        create_recorder) already refuses an LDPlayer target for RECORD, independent of the service."""
        from app.core.errors import CapabilityNotAvailable as CNA
        from app.providers.target import Capability, require_capability
        svc, *_ = rig()
        target = svc.verify(next(e for e in svc.discover() if e.ref == "ld1"))
        with self.assertRaises(CNA):
            require_capability(target, Capability.RECORD)


class ProviderNeutralityIsPreserved(unittest.TestCase):
    """The GUI/controller and the generic playback engine must not have learned about LDPlayer or ADB."""

    def imports_of(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
        return out

    def test_gui_imports_neither_ldplayer_nor_adb(self):
        for path in (ROOT / "gui").glob("*.py"):
            bad = {m for m in self.imports_of(path) if m.startswith(("app.ldplayer", "app.adb"))}
            self.assertEqual(bad, set(), f"gui/{path.name} imports {bad}")

    def test_playback_imports_neither_ldplayer_nor_adb(self):
        for path in (ROOT / "playback").glob("*.py"):
            bad = {m for m in self.imports_of(path) if m.startswith(("app.ldplayer", "app.adb"))}
            self.assertEqual(bad, set(), f"playback/{path.name} imports {bad}")

    def test_only_the_new_bootstrap_file_imports_the_ldplayer_provider(self):
        for path in (ROOT / "bootstrap").glob("*.py"):
            imports_ldplayer = "app.ldplayer" in "\n".join(self.imports_of(path))
            if imports_ldplayer:
                self.assertIn(path.name, ("ldplayer_services.py", "main.py"), path.name)


if __name__ == "__main__":
    unittest.main()
