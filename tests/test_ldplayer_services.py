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
from unittest import mock
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
        if inst.adb_debug is not None:                  # on (1) unless a test says otherwise; None: not in the file
            data["basicSettings.adbDebug"] = inst.adb_debug
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
    def test_the_applications_client_may_start_the_adb_server_and_the_tools_do_not(self):
        import tempfile
        from unittest import mock
        import app.bootstrap.ldplayer_services as module
        from app.adb.client import AdbClient
        from app.config.settings import load_settings
        with tempfile.TemporaryDirectory() as folder:
            for name in ("ldconsole.exe", "adb.exe"):
                (Path(folder) / name).write_bytes(b"")
            settings = load_settings(env={"LDPLAYER_DIR": folder})
            with mock.patch("app.config.settings.load_settings", return_value=settings):
                services = module.LDPlayerServices.create_default(EmergencyStop())
        self.assertTrue(services._adb.may_start_server)
        self.assertFalse(AdbClient(None, None, EmergencyStop()).may_start_server)      # the default: never

    def test_the_adb_server_is_started_in_the_background_when_an_instance_is_running(self):
        """So that by the time a person chooses an instance, the server is up (2026-10-21); never when nothing runs,
        never by a client that may not start it, never waited for."""
        from app.bootstrap.ldplayer_services import LDPlayerServices

        class Starting(RecordingAdb):
            may_start_server = True
            asked = 0

            def start_server_soon(self):
                self.asked += 1
                return True
        for running, may, expected in ((True, True, 1), (False, True, 0), (True, False, 0)):
            with self.subTest(running=running, may=may):
                world = two_instance_world()
                for inst in world.instances:
                    inst.running = running
                adb = Starting(world)
                adb.may_start_server = may
                LDPlayerServices(FakeConsole(world), adb, read_config_for(world), port_owners_for(world),
                                 EmergencyStop()).discover()
                self.assertEqual(adb.asked, expected)

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


class SeveralWays(unittest.TestCase):
    """The person's rule of 2026-10-27 in the application: an instance reached over several ADB serials is listed
    once per way, rows that look the same -- so a remembered target is not chosen by itself and the person chooses
    which -- and the row chosen is the way used, proven again at every re-check."""

    @staticmethod
    def world():
        from tests.test_mapping import two_way_world
        return two_way_world()

    def test_each_way_is_a_row_of_its_own_that_looks_like_the_others(self):
        svc, *_ = rig(self.world())
        entries = svc.discover()
        ld1 = [e for e in entries if e.ref.startswith("ld1")]
        self.assertEqual([e.ref for e in ld1], ["ld1@emulator-5554", "ld1@127.0.0.1:5555"])   # LDPlayer's own first
        self.assertEqual(len({(e.provider_id, e.group, e.title, e.process, e.kind) for e in ld1}), 1)
        for entry in ld1:
            self.assertIn(entry.ref.split("@", 1)[1], entry.size)               # the row says which way it is
        self.assertEqual([e.ref for e in entries if e.ref.startswith("ld2")], ["ld2"])

    def test_the_row_chosen_is_the_way_used_and_shown_again(self):
        svc, *_ = rig(self.world())
        entries = {e.ref: e for e in svc.discover()}
        for ref, serial in (("ld1@emulator-5554", "emulator-5554"), ("ld1@127.0.0.1:5555", "127.0.0.1:5555")):
            with self.subTest(ref=ref):
                target = svc.verify(entries[ref])
                self.assertEqual(target.adb_serial, serial)
                self.assertEqual(svc.recheck(target).ref, ref)

    def test_a_row_whose_way_closed_is_refused_never_replaced_by_the_other(self):
        svc, _, world, _ = rig(self.world())
        entries = {e.ref: e for e in svc.discover()}
        world.devices = [d for d in world.devices if d.serial != "127.0.0.1:5555"]
        with self.assertRaises(TargetNotVerified) as cm:
            svc.verify(entries["ld1@127.0.0.1:5555"])
        self.assertEqual(cm.exception.reason_code, "way_not_proven")

    def test_a_remembered_instance_reached_two_ways_is_offered_not_chosen(self):
        from app.gui import target_memory as tm
        svc, *_ = rig(self.world())
        entries = svc.discover()
        memory = tm.remember(next(e for e in entries if e.ref == "ld1@emulator-5554"))
        self.assertIsNone(tm.find(memory, entries))
        self.assertIsNone(tm.suggestion(memory, entries))

    def test_listing_never_fails_and_asks_nothing_when_it_cannot_or_need_not(self):
        from app.adb.client import AdbError, AdbServerNotRunning
        from app.core.net import NetstatError

        def broken(exc):
            def fail(*_a, **_k):
                raise exc
            return fail
        for case in ("adb fails", "server down", "ports unreadable", "nothing runs"):
            with self.subTest(case=case):
                world = self.world()
                svc, adb, _, stop = rig(world)
                asked = []
                listed = adb.list_devices
                adb.list_devices = lambda: asked.append(1) or listed()
                if case == "adb fails":
                    adb.list_devices = broken(AdbError("boom"))
                elif case == "server down":
                    adb.list_devices = broken(AdbServerNotRunning("down"))
                elif case == "ports unreadable":
                    svc._port_owners = broken(NetstatError("denied"))
                elif case == "nothing runs":
                    for inst in world.instances:
                        inst.running = False
                self.assertEqual({e.ref for e in svc.discover()}, {"ld1", "ld2"})
                if case == "nothing runs":
                    self.assertEqual(asked, [])

    def test_the_emergency_stop_is_the_stop_while_the_ways_are_listed(self):
        """Never turned into "no ways" (the review of 2026-10-27): a stop set before, or one that ended `adb
        devices` or the port listing, is raised as the stop -- and adb is not asked once it is set."""
        from app.adb.client import AdbError
        from app.core.errors import EmergencyStopped
        from app.core.net import NetstatError
        for case in ("set before", "ends adb", "ends the port listing"):
            with self.subTest(case=case):
                svc, adb, _, stop = rig(self.world())
                asked = []
                listed = adb.list_devices

                def listing():
                    asked.append(1)
                    if case == "ends adb":
                        stop.trigger("test")
                        raise AdbError("ADB did not answer: cancelled")
                    return listed()
                adb.list_devices = listing
                if case == "set before":
                    stop.trigger("test")
                elif case == "ends the port listing":
                    def ports():
                        stop.trigger("test")
                        raise NetstatError("cancelled")
                    svc._port_owners = ports
                with self.assertRaises(EmergencyStopped):
                    svc.discover()
                if case == "set before":
                    self.assertEqual(asked, [])

    def test_a_stop_while_the_ways_are_listed_keeps_the_last_listing_whole(self):
        """The second review of 2026-10-27: never half a new listing (a new registry with the old rows)."""
        from app.adb.client import AdbError
        from app.core.errors import EmergencyStopped
        svc, adb, _, stop = rig(self.world())
        svc.discover()
        registry, mapping, rows = svc._registry, svc._mapping, dict(svc._rows)

        def listing():
            stop.trigger("test")
            raise AdbError("ADB did not answer: cancelled")
        adb.list_devices = listing
        with self.assertRaises(EmergencyStopped):
            svc.discover()
        self.assertIs(svc._registry, registry)
        self.assertIs(svc._mapping, mapping)
        self.assertEqual(svc._rows, rows)

    def test_a_row_says_its_note_in_russian_too(self):
        svc, *_ = rig(self.world())
        notes = {e.ref: dict(e.size_in).get("ru") for e in svc.discover()}
        self.assertEqual(notes["ld2"], "№ 2, запущен")
        self.assertEqual(notes["ld1@127.0.0.1:5555"], "№ 1, запущен, ADB 127.0.0.1:5555")


def cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" for ch in text)


class SaidInRussian(unittest.TestCase):
    """The person's item 3 of 2026-10-27: LDPlayer's messages in Russian. The provider writes its own words in the
    other languages (`PlatformError.saying`); the screens show them in the person's language and never hold them."""

    def test_the_words_go_with_the_error_and_english_stays_its_message(self):
        from app.core.errors import PlatformError, said_in, said_params
        exc = TargetNotVerified("Gone.", reason_code="x").saying(ru="Пропало.", de="")
        self.assertEqual((str(exc), exc.reason_code, exc.said), ("Gone.", "x", {"ru": "Пропало."}))
        self.assertEqual((said_in(exc, "ru"), said_in(exc, "en"), said_in(ValueError("v"), "ru")),
                         ("Пропало.", "Gone.", "v"))
        self.assertEqual(said_params(exc), {"message_ru": "Пропало."})
        self.assertEqual(PlatformError("plain").said, {})                # one error's words are never another's

    def test_every_refusal_of_choosing_an_instance_is_said_in_russian(self):
        from app.adb.client import AdbError
        from tests.test_mapping import FakeAdb, make_service, two_way_world

        def failing_list(exc):
            class Failing(FakeAdb):
                def list_devices(self):
                    raise exc
            return Failing
        cases = {
            "adb_debug_off": lambda w: (setattr(w.instances[0], "adb_debug", 0), w.devices.clear()),
            "not_running": lambda w: setattr(w.instances[0], "running", False),
            "no_adb_device": lambda w: w.devices.clear(),
            "ambiguous": lambda w: w.devices.append(Dev("127.0.0.1:5555", "aaaa1111", 1200)),
            "name_mismatch": lambda w: setattr(w.instances[0], "name", "Renamed"),
            "no_verified_serial": lambda w: setattr(w.devices[0], "android_id", "other"),
            "identity_not_unique": lambda w: setattr(w.instances[1], "android_id", "aaaa1111"),
        }
        for code, change in cases.items():
            with self.subTest(code=code):
                w = two_instance_world()
                svc = make_service(w)                     # its registry is made before the change
                change(w)
                with self.assertRaises(TargetNotVerified) as cm:
                    svc.verify("ld1")
                self.assertEqual(cm.exception.reason_code, code)
                self.assertFalse(cyrillic(str(cm.exception)), str(cm.exception))
                self.assertTrue(cyrillic(cm.exception.said.get("ru", "")), cm.exception.said)
        with self.subTest(code="way_not_proven"):
            with self.assertRaises(TargetNotVerified) as cm:
                make_service(two_way_world()).verify("ld1", serial="emulator-5570")
            self.assertTrue(cyrillic(cm.exception.said["ru"]))
        with self.subTest(code="adb_error"):
            w = two_instance_world()
            failing = AdbError("boom").saying(ru="бум")
            with self.assertRaises(TargetNotVerified) as cm:
                make_service(w, adb=failing_list(failing)(w)).verify("ld1")
            self.assertEqual(cm.exception.said["ru"], "Не удалось опросить ADB: бум")

    def test_the_status_line_says_it_in_the_persons_language(self):
        from types import SimpleNamespace

        from app.gui.app import App
        from app.gui.controller import AppController
        from app.gui.i18n import Translator
        from app.gui.library import MacroLibrary
        world = two_instance_world()
        world.instances[0].adb_debug = 0
        world.devices = world.devices[1:]
        svc, *_ = rig(world)
        c = AppController(svc, MacroLibrary(Path("unused")))
        c.refresh()
        self.assertFalse(c.select("ld1"))
        ru = App.status_text(SimpleNamespace(t=Translator("ru")), c.status)
        en = App.status_text(SimpleNamespace(t=Translator("en")), c.status)
        self.assertIn("выключена отладка ADB", ru)
        self.assertIn("has ADB debugging turned off", en)


class Instances(FakeConsole):
    """LDPlayer as seen on 2026-10-27: an instance shows on ADB only while it runs with ADB debugging on. `quit` and
    `launch` change the world at once (or never, `stuck`); every call is recorded."""

    def __init__(self, world: World, *, stuck: bool = False, keeps: bool = True) -> None:
        super().__init__(world)
        self.calls: list[tuple] = []
        self.stuck, self.keeps = stuck, keeps
        self.devices = {d.android_id: d for d in world.devices}
        self.sync()

    def sync(self) -> None:
        on = {i.android_id for i in self.world.instances if i.running and i.adb_debug not in (0, None)}
        self.world.devices = [d for a, d in self.devices.items() if a in on]

    def quit_instance(self, index):
        self.calls.append(("quit", index))
        if not self.stuck:
            next(i for i in self.world.instances if i.index == index).running = False
            self.sync()

    def launch_instance(self, index):
        self.calls.append(("launch", index))
        inst = next(i for i in self.world.instances if i.index == index)
        inst.running = True
        if not self.keeps:
            inst.adb_debug = 0                       # LDPlayer wrote its own setting back
        self.sync()


class Held:
    """The instance's processes held from before `quit` (app.core.procs.hold_processes): running while their ids are
    in the rig's `alive`; every hold is let go of (`released`)."""

    def __init__(self, rig, pids) -> None:
        self.rig, self.pids = rig, tuple(pids)
        rig.held.append(self.pids)

    def running(self) -> bool:
        return any(pid in self.rig.alive for pid in self.pids)

    def close(self) -> None:
        self.rig.released.append(self.pids)


class DebuggingRig(unittest.TestCase):
    """LDPlayer-1 running with ADB debugging off, LDPlayer services that may turn it on, every step recorded."""

    def setUp(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices
        self.world = two_instance_world()
        self.world.instances[0].adb_debug = 0
        self.console = Instances(self.world)
        self.adb = RecordingAdb(self.world)
        self.stop = EmergencyStop()
        self.written: list[tuple] = []
        self.checked: list[tuple] = []
        self.alive: set[int] = set()                 # process ids still running (none: they end with the instance)
        self.held: list[tuple] = []
        self.released: list[tuple] = []

        def enable(index, name):
            inst = next(i for i in self.world.instances if i.index == index)
            self.written.append((index, name, inst.running, frozenset(self.alive)))
            inst.adb_debug = 1
        self.enable = enable
        self.check = lambda index, name: self.checked.append((index, name))

        def file(index, name, check_only=False):
            return self.check(index, name) if check_only else self.enable(index, name)
        self.svc = LDPlayerServices(self.console, self.adb, read_config_for(self.world),
                                    port_owners_for(self.world), self.stop, enable_debugging=file,
                                    hold_processes=lambda pids: Held(self, pids))

    def remedy(self):
        from app.providers.remedy import remedy_of
        entries = {e.ref: e for e in self.svc.discover()}
        with self.assertRaises(TargetNotVerified) as cm:
            self.svc.verify(entries["ld1"])
        self.assertEqual(cm.exception.reason_code, "adb_debug_off")
        return remedy_of(cm.exception)


class TurningDebuggingOn(DebuggingRig):
    """The person's item 4 of 2026-10-27: Olive turns an instance's ADB debugging on itself -- offered with the
    refusal, done only on the person's yes: close it, change the one setting, start it, wait until it is proven."""

    def test_the_refusal_offers_it_in_both_languages_and_nothing_is_done_before_a_yes(self):
        remedy = self.remedy()
        self.assertIsNotNone(remedy)
        for words in (remedy.title, remedy.question, remedy.doing, remedy.done):
            self.assertTrue(words["en"] and cyrillic(words["ru"]), words)
        self.assertIn("closed", remedy.question["en"])                  # what it costs is said before the yes
        self.assertIn("закроется", remedy.question["ru"])
        self.assertEqual((self.console.calls, self.written), ([], []))

    def test_no_offer_without_a_way_to_write_or_for_another_refusal(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices
        from app.providers.remedy import remedy_of
        plain = LDPlayerServices(self.console, self.adb, read_config_for(self.world), port_owners_for(self.world),
                                 self.stop)
        entries = {e.ref: e for e in plain.discover()}
        with self.assertRaises(TargetNotVerified) as cm:
            plain.verify(entries["ld1"])
        self.assertIsNone(remedy_of(cm.exception))
        self.world.instances[1].running = False
        self.console.sync()
        entries = {e.ref: e for e in self.svc.discover()}
        with self.assertRaises(TargetNotVerified) as cm:
            self.svc.verify(entries["ld2"])
        self.assertEqual(cm.exception.reason_code, "not_running")
        self.assertIsNone(remedy_of(cm.exception))

    def test_yes_closes_it_changes_the_setting_while_it_is_closed_starts_it_and_it_is_proven(self):
        self.remedy().run()
        self.assertEqual(self.console.calls, [("quit", 1), ("launch", 1)])
        self.assertEqual(self.written, [(1, "LDPlayer-1", False, frozenset())])   # only while it was closed
        entries = {e.ref: e for e in self.svc.discover()}
        self.assertEqual(self.svc.verify(entries["ld1"]).adb_serial, "emulator-5554")

    def test_an_instance_closed_meanwhile_is_not_closed_again(self):
        remedy = self.remedy()
        self.world.instances[0].running = False
        remedy.run()
        self.assertEqual(self.console.calls, [("launch", 1)])
        self.assertEqual(self.written, [(1, "LDPlayer-1", False, frozenset())])

    def test_an_instance_renamed_or_gone_since_the_offer_is_left_alone(self):
        for change in ("renamed", "gone"):
            with self.subTest(change=change):
                self.setUp()
                remedy = self.remedy()
                if change == "renamed":
                    self.world.instances[0].name = "Other"
                else:
                    self.world.instances.pop(0)
                with self.assertRaises(TargetNotVerified) as cm:
                    remedy.run()
                self.assertEqual(cm.exception.reason_code, "debugging_not_turned_on")
                self.assertIn("Nothing was changed", str(cm.exception))
                self.assertEqual((self.console.calls, self.written), ([], []))

    def test_the_emergency_stop_ends_it_as_the_stop_and_says_what_was_done(self):
        from app.core.errors import EmergencyStopped
        with self.subTest(when="before"):
            remedy = self.remedy()
            self.stop.trigger("test")
            with self.assertRaises(EmergencyStopped) as cm:
                remedy.run()
            self.assertIn("Nothing was changed", str(cm.exception))
            self.assertEqual((self.console.calls, self.written), ([], []))
        with self.subTest(when="waiting for it to close"):
            self.setUp()
            self.console.stuck = True
            remedy = self.remedy()
            original = self.console.list_instances
            self.console.list_instances = lambda: (self.stop.trigger("test") if self.console.calls else None,
                                                   original())[1]
            with self.assertRaises(EmergencyStopped) as cm:
                remedy.run()
            self.assertIn("asked to close", str(cm.exception))
            self.assertIn("закрыться", cm.exception.said["ru"])
            self.assertEqual((self.console.calls, self.written), ([("quit", 1)], []))
        with self.subTest(when="waiting for it to start"):
            self.setUp()
            remedy = self.remedy()
            launch = self.console.launch_instance

            def launch_then_stop(index):
                self.stop.trigger("test")
                self.console.calls.append(("launch", index))
            self.console.launch_instance = launch_then_stop
            with self.assertRaises(EmergencyStopped) as cm:
                remedy.run()
            self.assertIn("asked to start again", str(cm.exception))      # the launch was sent
            self.assertEqual(self.written, [(1, "LDPlayer-1", False, frozenset())])
            del launch

    def test_every_wait_is_bounded_and_says_what_was_done(self):
        import app.bootstrap.ldplayer_services as module
        from unittest import mock
        with self.subTest(wait="close"), mock.patch.object(module, "CLOSE_WAIT_S", 0.0):
            self.console.stuck = True
            with self.assertRaises(TargetNotVerified) as cm:
                self.remedy().run()
            self.assertIn("did not close", str(cm.exception))
            self.assertEqual(self.written, [])
        with self.subTest(wait="reach"), mock.patch.object(module, "REACH_WAIT_S", 0.0):
            self.setUp()
            remedy = self.remedy()
            self.console.devices.clear()             # started, but never seen over ADB
            with self.assertRaises(TargetNotVerified) as cm:
                remedy.run()
            self.assertIn("could not be reached", str(cm.exception))
            self.assertIn("started again", str(cm.exception))

    def test_a_setting_ldplayer_did_not_keep_is_said(self):
        self.console.keeps = False
        with self.assertRaises(TargetNotVerified) as cm:
            self.remedy().run()
        self.assertIn("did not keep the setting", str(cm.exception))
        self.assertTrue(cyrillic(cm.exception.said["ru"]))

    def test_a_setting_that_could_not_be_written_leaves_the_instance_started_again(self):
        from app.ldplayer.config_writer import ConfigWriteRefused

        def refuse(index, name):
            raise ConfigWriteRefused("Not in LDPlayer's form.").saying(ru="Не в том виде.")
        self.enable = refuse
        with self.assertRaises(TargetNotVerified) as cm:
            self.remedy().run()
        self.assertEqual(self.console.calls, [("quit", 1), ("launch", 1)])
        self.assertIn("Not in LDPlayer's form", str(cm.exception))
        self.assertIn("started again", str(cm.exception))
        self.assertIn("Не в том виде", cm.exception.said["ru"])


class TurningDebuggingOnCarefully(DebuggingRig):
    """What the live check and the review of 2026-10-27 found: the file waits for the instance's processes, the waits
    count the time their checks take, a stop met anywhere is the stop, and every "what was done" is true."""

    def test_the_file_waits_until_the_instances_own_processes_are_gone(self):
        """Live, LDPlayer wrote the file itself 0.5 s after `quit`, while `list2` already said stopped at 0.8 s and
        its processes ran until 2.2 s: a write before they end can be written over."""
        remedy = self.remedy()
        polls = []
        original = self.console.list_instances

        def listing():
            polls.append(1)
            if len(polls) == 4:
                self.alive.clear()                  # the processes end a few polls after the list says stopped
            return original()
        self.console.list_instances = listing
        self.alive.update({1100, 1200})
        quit_instance = self.console.quit_instance
        self.console.quit_instance = lambda index: quit_instance(index)     # listed stopped at once, processes on
        with mock.patch.object(self.stop, "wait", return_value=False):
            remedy.run()
        self.assertEqual(self.written, [(1, "LDPlayer-1", False, frozenset())])   # never while one still ran

    def test_processes_that_never_end_mean_no_write(self):
        import app.bootstrap.ldplayer_services as module
        remedy = self.remedy()
        self.alive.update({1100})
        with mock.patch.object(module, "CLOSE_WAIT_S", 0.0):
            with self.assertRaises(TargetNotVerified) as cm:
                remedy.run()
        self.assertIn("listed as stopped, but its processes did not end", str(cm.exception))
        self.assertIn("процессы не завершились", cm.exception.said["ru"])
        self.assertEqual(self.written, [])
        self.assertEqual((self.held, self.released), ([(1100, 1200)], [(1100, 1200)]))   # its own, let go of

    def test_a_wait_counts_the_time_its_checks_take(self):
        """A check that takes 30 s (a hung adb, a slow list) counts: CLOSE_WAIT_S = 60 ends after about two, not
        after 60 sleeps of a second each."""
        remedy = self.remedy()
        self.console.stuck = True
        now = [0.0]
        self.svc._clock = lambda: now[0]
        original = self.console.list_instances

        def slow():
            now[0] += 30.0
            return original()
        self.console.list_instances = slow
        with mock.patch.object(self.stop, "wait", return_value=False):
            with self.assertRaises(TargetNotVerified) as cm:
                remedy.run()
        self.assertIn("did not close", str(cm.exception))
        self.assertLessEqual(now[0], 120.0)

    def test_a_stop_while_the_ports_are_listed_to_reach_it_is_the_stop(self):
        from app.core.errors import EmergencyStopped
        from app.core.net import NetstatError
        remedy = self.remedy()
        owners = self.svc._port_owners

        def ports():
            if ("launch", 1) in self.console.calls:
                self.stop.trigger("test")
                raise NetstatError("cancelled")
            return owners()
        self.svc._port_owners = ports
        self.svc._mapping._port_owners = ports
        with self.assertRaises(EmergencyStopped) as cm:
            remedy.run()
        self.assertIn("started again", str(cm.exception))

    def test_a_close_that_failed_on_its_way_still_says_it_was_asked(self):
        from app.ldplayer.console import LDConsoleError
        remedy = self.remedy()

        def times_out(index):
            self.console.calls.append(("quit", index))
            raise LDConsoleError("ldconsole 'quit' did not answer within 20 s.").saying(ru="не ответил")
        self.console.quit_instance = times_out
        with self.assertRaises(TargetNotVerified) as cm:
            remedy.run()
        self.assertIn("asked to close", str(cm.exception))
        self.assertEqual(self.written, [])

    def test_a_file_olive_could_not_change_is_said_before_anything_is_closed(self):
        from app.ldplayer.config_writer import ConfigWriteRefused

        def refused(index, name):
            raise ConfigWriteRefused("Not in LDPlayer's form.").saying(ru="Не в том виде.")
        self.check = refused
        with self.assertRaises(TargetNotVerified) as cm:
            self.remedy().run()
        self.assertIn("Nothing was changed", str(cm.exception))
        self.assertEqual((self.console.calls, self.written), ([], []))

    def test_a_file_written_but_not_reading_back_is_said_so_and_the_instance_started_again(self):
        from app.ldplayer.config_writer import ConfigWriteRefused

        def unconfirmed(index, name):
            error = ConfigWriteRefused("Does not read back as written.").saying(ru="Не читается.")
            error.written = True
            raise error
        self.enable = unconfirmed
        with self.assertRaises(TargetNotVerified) as cm:
            self.remedy().run()
        self.assertIn("was written but did not read back", str(cm.exception))
        self.assertIn("started again", str(cm.exception))
        self.assertEqual(self.console.calls, [("quit", 1), ("launch", 1)])

    def test_an_unexpected_error_while_writing_does_not_leave_it_closed(self):
        def broken(index, name):
            raise OSError("disk gone")
        self.enable = broken
        with self.assertRaises(TargetNotVerified) as cm:
            self.remedy().run()
        self.assertEqual(self.console.calls, [("quit", 1), ("launch", 1)])
        self.assertIn("disk gone", str(cm.exception))


class TurningDebuggingOnInTheApplication(DebuggingRig):
    """The same, through the application: the offer comes with the refusal, is asked once, runs only on a yes, holds
    everything else off while it runs, and ends with the instance chosen -- or says what it had done."""

    def setUp(self):
        import tempfile
        from app.gui.controller import AppController
        from app.gui.library import MacroLibrary
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.library = MacroLibrary(Path(self._tmp.name) / "macros")
        self.c = AppController(self.svc, self.library)
        self.addCleanup(self.c.shutdown)
        self.c.refresh()

    @staticmethod
    def said(status, language: str) -> str:
        from types import SimpleNamespace

        from app.gui.app import App
        from app.gui.i18n import Translator
        return App.status_text(SimpleNamespace(t=Translator(language)), status)

    def test_a_refused_choice_offers_it_once_and_yes_turns_it_on_and_chooses_it(self):
        from app.gui.controller import TargetStatus
        self.assertFalse(self.c.select("ld1"))
        offer = self.c.take_remedy_offer()
        self.assertIsNotNone(offer)
        self.assertIsNone(self.c.take_remedy_offer())                    # asked once
        job = self.c.begin_remedy(offer)
        self.assertTrue(self.c.busy)
        self.assertIn("Включаю отладку ADB", self.said(self.c.status, "ru"))
        self.assertFalse(self.c.refresh())                               # nothing else while it runs
        self.assertTrue(self.c.finish_remedy(job(), offer))
        self.assertFalse(self.c.busy)
        self.assertEqual((self.c.target_status, self.c.target.adb_serial), (TargetStatus.VERIFIED, "emulator-5554"))
        self.assertIn("Отладка ADB для LDPlayer-1 включена", self.said(self.c.status, "ru"))
        self.assertIn("Chosen and verified", self.said(self.c.status, "en"))

    def test_another_choice_drops_the_offer(self):
        self.assertFalse(self.c.select("ld1"))
        self.assertTrue(self.c.select("ld2"))
        self.assertIsNone(self.c.take_remedy_offer())

    def test_a_stop_while_it_runs_is_said_with_what_was_done(self):
        self.c.select("ld1")
        offer = self.c.take_remedy_offer()
        job = self.c.begin_remedy(offer)
        self.stop.trigger("test")
        try:
            outcome = job()
        except BaseException as exc:                                  # as App.run_job hands it on
            outcome = exc
        self.assertFalse(self.c.finish_remedy(outcome, offer))
        self.assertEqual(self.c.status.category, "emergency_stop")
        self.assertIn("Ничего не изменено", self.said(self.c.status, "ru"))
        self.assertIsNone(self.c.begin_remedy(offer))                   # not again while the stop is set

    def test_a_refused_run_offers_it_once_the_run_has_ended(self):
        from app.playback.macro import Macro
        from app.providers.recording import Recording
        from tests.test_playback import down, up
        target = self.svc.verify(next(e for e in self.c.entries if e.ref == "ld2"))
        self.library.save(Macro(Recording(target.provider_id, target.target_id, (down(0, 5, 6), up(10, 5, 6))),
                                "Tap"))
        self.c.refresh_macros()
        self.c.select_macro(next(i.key for i in self.c.macros if i.name == "Tap"))
        self.c.set_run_refs(["ld1"])
        self.assertTrue(self.c.start_run())
        self.assertFalse(self.c.run_active)                              # refused before anything was sent
        offer = self.c.take_remedy_offer()
        self.assertFalse(offer.reselect)
        self.assertTrue(self.c.finish_remedy(self.c.begin_remedy(offer)(), offer))
        self.assertIn("Запустите снова", self.said(self.c.status, "ru"))
        self.assertEqual(self.adb.taps, [])

    def test_the_offer_waits_while_the_emergency_stop_is_set(self):
        self.c.select("ld1")
        self.stop.trigger("test")
        self.assertIsNone(self.c.take_remedy_offer())                   # not asked while stopped...
        self.stop.reset()
        self.assertIsNotNone(self.c.take_remedy_offer())                # ...kept until the stop is reset

    def test_nothing_is_offered_or_started_while_something_else_runs(self):
        self.c.select("ld1")
        offer = self.c.remedy_offer
        self.c.recorder = object()                                     # a recording under way: busy
        self.assertIsNone(self.c.take_remedy_offer())
        self.assertIsNone(self.c.begin_remedy(offer))
        self.assertFalse(self.c.remedy_active)
        self.c.recorder = None
        self.assertIs(self.c.take_remedy_offer(), offer)

    def test_a_row_that_names_another_target_after_the_fix_is_not_chosen(self):
        import dataclasses
        self.c.select("ld1")
        offer = dataclasses.replace(self.c.take_remedy_offer(), listed=("another", "target"))
        self.assertFalse(self.c.finish_remedy(None, offer))
        self.assertIsNone(self.c.target)
        self.assertIn("Выберите цель заново", self.said(self.c.status, "ru"))

    def test_done_with_the_stop_pressed_meanwhile_still_says_it_was_done(self):
        self.c.select("ld1")
        offer = self.c.take_remedy_offer()
        job = self.c.begin_remedy(offer)
        outcome = job()
        self.stop.trigger("test")                                     # pressed just as it finished
        self.assertFalse(self.c.finish_remedy(outcome, offer))
        self.assertIn("Отладка ADB для LDPlayer-1 включена", self.said(self.c.status, "ru"))
        self.assertIn("Сбросьте экстренную остановку", self.said(self.c.status, "ru"))

    def test_closing_the_window_while_it_runs_is_asked_first(self):
        from types import SimpleNamespace

        from app.gui.app import App
        from app.gui.i18n import Translator
        closed, asked = [], []
        for answer in (False, True):
            window = SimpleNamespace(controller=SimpleNamespace(remedy_active=True, shutdown=lambda: closed.append(1)),
                                     t=Translator("ru"), _after=None, root=SimpleNamespace(destroy=lambda: None),
                                     _confirm_careful=lambda title, text: asked.append(title) or answer)
            App.close(window)
            self.assertEqual(len(closed), 1 if answer else 0)
        self.assertEqual(asked, ["Закрыть Olive сейчас?"] * 2)

    def test_the_window_asks_once_in_the_persons_language_and_only_yes_starts_it(self):
        from types import SimpleNamespace

        from app.gui.app import App
        from app.gui.i18n import Translator
        for answer in (False, True):
            with self.subTest(answer=answer):
                asked, jobs = [], []
                window = SimpleNamespace(controller=self.c, t=Translator("ru"), render=lambda: None,
                                         _confirm_careful=lambda title, text: asked.append((title, text)) or answer,
                                         run_job=lambda job, done: jobs.append((job, done)))
                self.c.select("ld1")
                App._offer_remedy(window)
                App._offer_remedy(window)                                # asked once, not again
                self.assertEqual(len(asked), 1)
                self.assertEqual(asked[0][0], "Включить отладку ADB?")
                self.assertEqual(len(jobs), 1 if answer else 0)
                self.assertEqual(self.c.busy, answer)
                self.assertEqual((self.console.calls, self.written), ([], []))   # nothing done by asking
        job, done = jobs[0]
        done(job())
        self.assertFalse(self.c.busy)
        self.assertEqual(self.console.calls, [("quit", 1), ("launch", 1)])


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
