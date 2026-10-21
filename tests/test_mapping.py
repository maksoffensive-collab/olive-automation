"""Tests for verified identity mapping and explicit selection (fakes only).

These prove the LOGIC refuses unsafe cases. They do NOT prove that the evidence sources
hold on a real LDPlayer 14 instance: that is [NEEDS TESTING] with one manually started instance.
"""
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from app.adb.client import AdbDevice, AdbError, AdbServerNotRunning, DeviceState
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, NoTargetSelected, TargetNotVerified
from app.core.net import parse_netstat_listeners
from app.instances.mapping import MappingService, Verdict, VerifiedTarget, adb_serial_ports
from app.instances.profile import InstanceProfile, InstanceRegistry, RegistryError
from app.instances.selection import Selection, resolve_selection
from app.ldplayer.config_reader import parse_instance_config
from app.ldplayer.console import LDInstance
from app.logging.structured import LogHub, MemorySink


@dataclass
class Inst:
    index: int
    name: str
    running: bool = True
    pid: int = 1000
    vbox: int = 2000
    android_id: str | None = None


@dataclass
class Dev:
    serial: str
    android_id: str | None
    owner_pid: int | None
    state: DeviceState = DeviceState.DEVICE
    query_fails: bool = False
    imei: str | None = None  # injected phone.imei; None = not reported


@dataclass
class World:
    instances: list[Inst] = field(default_factory=list)
    devices: list[Dev] = field(default_factory=list)
    server_up: bool = True

    def ld_instances(self):
        return [LDInstance(i.index, i.name, 0, 0, 1 if i.running else 0, i.pid if i.running else -1,
                           i.vbox if i.running else -1, 960, 540, 160, "") for i in self.instances]


class FakeConsole:
    def __init__(self, w): self.w = w
    def list_instances(self): return self.w.ld_instances()
    def running_names(self): return [i.name for i in self.w.instances if i.running]


class FakeAdb:
    def __init__(self, w): self.w = w
    def list_devices(self):
        if not self.w.server_up:
            raise AdbServerNotRunning("down")
        return [AdbDevice(d.serial, d.state, d.state.value) for d in self.w.devices]
    def getprop(self, serial, name):
        d = next(d for d in self.w.devices if d.serial == serial)
        if d.query_fails:
            raise AdbError("boom")
        if name == "phone.androidid":
            return d.android_id or ""
        if name == "phone.imei":
            return d.imei or ""
        raise AssertionError(f"unexpected property {name}")


def make_service(w: World, registry=None, stop=None, logger=None):
    def read_config(index):
        inst = next(i for i in w.instances if i.index == index)
        data = {"propertySettings.phoneAndroidId": inst.android_id,
                "propertySettings.phoneIMEI": f"imei{index}", "propertySettings.macAddress": f"mac{index}"}
        return parse_instance_config(index, Path("x"), data)

    def owners():
        out = {}
        for d in w.devices:
            if d.owner_pid is None:
                continue
            for p in adb_serial_ports(d.serial):
                out.setdefault(p, set()).add(d.owner_pid)
        return out
    registry = registry or InstanceRegistry.from_discovery(w.ld_instances())
    return MappingService(registry, FakeConsole(w), FakeAdb(w), stop or EmergencyStop(), read_config, owners, logger)


def two_instance_world() -> World:
    return World(
        instances=[Inst(1, "LDPlayer-1", pid=1100, vbox=1200, android_id="aaaa1111"),
                   Inst(2, "LDPlayer-2", pid=2100, vbox=2200, android_id="bbbb2222")],
        devices=[Dev("emulator-5554", "aaaa1111", 1200), Dev("emulator-5556", "bbbb2222", 2200)],
    )


class VerifyHappyPath(unittest.TestCase):
    def test_each_instance_maps_to_its_own_serial(self):
        svc = make_service(two_instance_world())
        t1, t2 = svc.verify("ld1"), svc.verify("ld2")
        self.assertEqual((t1.ld_index, t1.ld_name, t1.adb_serial), (1, "LDPlayer-1", "emulator-5554"))
        self.assertEqual((t2.ld_index, t2.ld_name, t2.adb_serial), (2, "LDPlayer-2", "emulator-5556"))
        self.assertTrue(all(e.verdict is Verdict.MATCH for e in t1.evidence))
        self.assertEqual({e.kind for e in t1.evidence}, {"config_identity", "process_owner"})
        self.assertIn("LDPlayer-1", t1.describe())
        self.assertIn("emulator-5554", t1.describe())

    def test_result_is_independent_of_adb_device_order_and_numbering(self):
        w = two_instance_world()
        w.devices.reverse()
        svc = make_service(w)
        self.assertEqual(svc.verify("ld1").adb_serial, "emulator-5554")
        # Swap which serial belongs to which instance: mapping must follow the evidence, not the numbers.
        w2 = World(instances=two_instance_world().instances,
                   devices=[Dev("emulator-5554", "bbbb2222", 2200), Dev("emulator-5556", "aaaa1111", 1200)])
        svc2 = make_service(w2)
        self.assertEqual(svc2.verify("ld1").adb_serial, "emulator-5556")
        self.assertEqual(svc2.verify("ld2").adb_serial, "emulator-5554")

    def test_other_devices_are_ignored(self):
        w = two_instance_world()
        w.devices.append(Dev("emulator-5558", "cccc3333", 9999))
        self.assertEqual(make_service(w).verify("ld1").adb_serial, "emulator-5554")

    def test_offline_device_never_counts(self):
        w = two_instance_world()
        w.devices[0].state = DeviceState.OFFLINE  # the only device that matches ld1 is offline
        with self.assertRaises(TargetNotVerified) as cm:
            make_service(w).verify("ld1")
        self.assertEqual(cm.exception.reason_code, "no_verified_serial")  # ld2's device is online but is not ld1

    def test_tcp_style_serial(self):
        w = World(instances=[Inst(3, "LDPlayer-3", pid=31, vbox=32, android_id="dddd")],
                  devices=[Dev("127.0.0.1:5565", "dddd", 32)])
        self.assertEqual(make_service(w).verify("ld3").adb_serial, "127.0.0.1:5565")


class VerifyRefusals(unittest.TestCase):
    def assertRefused(self, w, code, logical_id="ld1", **kw):
        with self.assertRaises(TargetNotVerified) as cm:
            make_service(w, **kw).verify(logical_id)
        self.assertEqual(cm.exception.reason_code, code)
        return cm.exception

    def test_unknown_logical_id(self):
        self.assertRefused(two_instance_world(), "unknown_instance", "nope")

    def test_instance_missing_from_ldplayer(self):
        w = two_instance_world()
        reg = InstanceRegistry([InstanceProfile("x", 9, "LDPlayer-9")])
        self.assertRefused(w, "not_in_ldplayer", "x", registry=reg)

    def test_name_changed_refused(self):
        w = two_instance_world()
        reg = InstanceRegistry.from_discovery(w.ld_instances())
        w.instances[0].name = "Renamed"
        self.assertRefused(w, "name_mismatch", registry=reg)

    def test_not_running(self):
        w = two_instance_world()
        w.instances[0].running = False
        self.assertRefused(w, "not_running")

    def test_adb_server_down(self):
        w = two_instance_world()
        w.server_up = False
        self.assertRefused(w, "adb_server")

    def test_no_devices(self):
        w = two_instance_world()
        w.devices.clear()
        self.assertRefused(w, "no_adb_device")

    def test_only_offline_devices(self):
        w = two_instance_world()
        for d in w.devices:
            d.state = DeviceState.UNAUTHORIZED
        self.assertRefused(w, "no_adb_device")

    def test_android_id_mismatch(self):
        w = two_instance_world()
        w.devices[0].android_id = "zzzz"
        self.assertRefused(w, "no_verified_serial")

    def test_android_id_unreadable(self):
        w = two_instance_world()
        w.devices[0].query_fails = True
        self.assertRefused(w, "no_verified_serial")

    def test_imei_mismatch_refused_even_if_android_id_matches(self):
        w = two_instance_world()
        w.devices[0].imei = "imei999"  # config of instance 1 holds imei1
        self.assertRefused(w, "no_verified_serial")

    def test_imei_match_accepted(self):
        w = two_instance_world()
        w.devices[0].imei = "imei1"
        self.assertEqual(make_service(w).verify("ld1").adb_serial, "emulator-5554")

    def test_android_id_null(self):
        w = two_instance_world()
        w.devices[0].android_id = None
        self.assertRefused(w, "no_verified_serial")

    def test_config_has_no_android_id(self):
        w = two_instance_world()
        w.instances[0].android_id = None
        self.assertRefused(w, "no_verified_serial")

    def test_process_owner_wrong(self):
        w = two_instance_world()
        w.devices[0].owner_pid = 4242  # identity matches but a different process owns the port
        self.assertRefused(w, "no_verified_serial")

    def test_process_owner_unknown(self):
        w = two_instance_world()
        w.devices[0].owner_pid = None
        self.assertRefused(w, "no_verified_serial")

    def test_ldplayer_reports_no_process_ids(self):
        w = two_instance_world()
        w.instances[0].pid = -1
        w.instances[0].vbox = -1
        self.assertRefused(w, "no_verified_serial")

    def test_two_devices_claim_same_instance_is_ambiguous(self):
        w = two_instance_world()
        w.devices.append(Dev("emulator-5560", "aaaa1111", 1200))
        self.assertRefused(w, "ambiguous")

    def test_shared_android_id_between_instances_refused(self):
        w = two_instance_world()
        w.instances[1].android_id = "aaaa1111"
        self.assertRefused(w, "identity_not_unique")

    def test_serial_that_encodes_no_port_is_not_verifiable(self):
        w = World(instances=[Inst(1, "LDPlayer-1", pid=1, vbox=2, android_id="a")],
                  devices=[Dev("weirdserial", "a", 2)])
        self.assertRefused(w, "no_verified_serial")

    def test_error_message_does_not_leak_identifiers(self):
        w = two_instance_world()
        w.devices[0].android_id = "zzzz"
        exc = self.assertRefused(w, "no_verified_serial")
        for secret in ("aaaa1111", "bbbb2222", "zzzz"):
            self.assertNotIn(secret, str(exc))


class VerifiedTargetIntegrity(unittest.TestCase):
    def test_cannot_be_constructed_directly(self):
        with self.assertRaises(TargetNotVerified) as cm:
            VerifiedTarget("x", 1, "n", "emulator-5554", 1, 2, 0, (), 0.0)
        self.assertEqual(cm.exception.reason_code, "forged")
        with self.assertRaises(TargetNotVerified):
            VerifiedTarget("x", 1, "n", "emulator-5554", 1, 2, 0, (), 0.0, _mint=object())

    def test_is_immutable_and_reports_age(self):
        t = make_service(two_instance_world()).verify("ld1")
        with self.assertRaises(Exception):
            t.adb_serial = "emulator-9999"
        self.assertGreaterEqual(t.age_s(), 0)
        self.assertAlmostEqual(t.age_s(now=t.verified_at + 30), 30)

    def test_revalidate_detects_serial_change_after_restart(self):
        w = two_instance_world()
        svc = make_service(w)
        t = svc.verify("ld1")
        w.devices[0].serial = "emulator-5570"  # instance restarted and got another serial
        self.assertEqual(svc.revalidate(t).adb_serial, "emulator-5570")

    def test_revalidate_refuses_when_instance_stopped(self):
        w = two_instance_world()
        svc = make_service(w)
        t = svc.verify("ld1")
        w.instances[0].running = False
        with self.assertRaises(TargetNotVerified):
            svc.revalidate(t)


class VerifyMany(unittest.TestCase):
    def test_distinct_serials(self):
        ts = make_service(two_instance_world()).verify_many(["ld1", "ld2"])
        self.assertEqual([t.adb_serial for t in ts], ["emulator-5554", "emulator-5556"])

    def test_serial_collision_refused_as_defence_in_depth(self):
        # Normal evidence cannot produce this (unique ids, one owner per port); force it anyway.
        svc = make_service(two_instance_world())
        first = svc.verify("ld1")
        svc.verify = lambda _id: first
        with self.assertRaises(TargetNotVerified) as cm:
            svc.verify_many(["ld1", "ld2"])
        self.assertEqual(cm.exception.reason_code, "serial_collision")

    def test_selection_collision_refused(self):
        w = two_instance_world()
        svc = make_service(w)
        first = svc.verify("ld1")
        svc.verify = lambda _id: first
        with self.assertRaises(TargetNotVerified) as cm:
            resolve_selection(Selection.of("ld1", "ld2"), InstanceRegistry.from_discovery(w.ld_instances()), svc)
        self.assertEqual(cm.exception.reason_code, "serial_collision")

    def test_registry_refuses_two_ids_for_one_instance(self):
        with self.assertRaises(RegistryError):
            InstanceRegistry([InstanceProfile("a", 1, "LDPlayer-1"), InstanceProfile("b", 1, "LDPlayer-1")])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.w = two_instance_world()
        self.svc = make_service(self.w)
        self.reg = InstanceRegistry.from_discovery(self.w.ld_instances())

    def test_empty_is_none_never_all(self):
        for sel in (Selection.none(), Selection.of(), Selection.of(""), Selection.of("  ", ""), Selection()):
            with self.subTest(sel=sel):
                self.assertTrue(sel.is_empty)
                with self.assertRaises(NoTargetSelected):
                    resolve_selection(sel, self.reg, self.svc)

    def test_empty_selection_verifies_nothing(self):
        calls = []
        orig = self.svc.verify
        self.svc.verify = lambda i: calls.append(i) or orig(i)
        with self.assertRaises(NoTargetSelected):
            resolve_selection(Selection.of(), self.reg, self.svc)
        self.assertEqual(calls, [])

    def test_explicit_only_those(self):
        r = resolve_selection(Selection.of("ld2"), self.reg, self.svc)
        self.assertEqual([t.logical_id for t in r.require_all()], ["ld2"])

    def test_duplicates_collapsed_order_kept(self):
        sel = Selection.of("ld2", "ld1", "ld2")
        self.assertEqual(sel.ids, ("ld2", "ld1"))

    def test_all_is_explicit_and_verifies_each(self):
        r = resolve_selection(Selection.all_verified(), self.reg, self.svc)
        self.assertEqual(sorted(t.logical_id for t in r.targets), ["ld1", "ld2"])

    def test_all_reports_unverifiable_instances_without_executing_them(self):
        self.w.instances.append(Inst(3, "LDPlayer-3", running=False, android_id="cccc"))
        reg = InstanceRegistry.from_discovery(self.w.ld_instances())
        r = resolve_selection(Selection.all_verified(), reg, make_service(self.w, registry=reg))
        self.assertEqual(sorted(t.logical_id for t in r.targets), ["ld1", "ld2"])
        self.assertIn("ld3", r.failures)
        with self.assertRaises(TargetNotVerified):
            r.require_all()
        self.assertEqual(len(r.require_any()), 2)

    def test_unknown_id_in_explicit_selection_is_reported_not_ignored(self):
        r = resolve_selection(Selection.of("ld1", "ghost"), self.reg, self.svc)
        self.assertIn("ghost", r.failures)
        self.assertEqual([t.logical_id for t in r.targets], ["ld1"])
        with self.assertRaises(TargetNotVerified):
            r.require_all()

    def test_nothing_verified_raises_no_target(self):
        self.w.devices.clear()
        r = resolve_selection(Selection.of("ld1"), self.reg, self.svc)
        with self.assertRaises(NoTargetSelected):
            r.require_any()


class SafetyIntegration(unittest.TestCase):
    def test_emergency_stop_blocks_verification(self):
        stop = EmergencyStop()
        svc = make_service(two_instance_world(), stop=stop)
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            svc.verify("ld1")

    def test_emergency_stop_propagates_through_selection(self):
        stop = EmergencyStop()
        w = two_instance_world()
        svc = make_service(w, stop=stop)
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            resolve_selection(Selection.all_verified(), InstanceRegistry.from_discovery(w.ld_instances()), svc)

    def test_logging_records_target_and_never_identifiers(self):
        mem = MemorySink()
        w = two_instance_world()
        svc = make_service(w, logger=LogHub([mem]).logger("instances"))
        svc.verify("ld1")
        w.devices[1].android_id = "zzzz"
        with self.assertRaises(TargetNotVerified):
            svc.verify("ld2")
        ev = mem.query(operation="VERIFY_TARGET")
        self.assertEqual([(e.result, e.instance) for e in ev], [("VERIFIED", "ld1"), ("REFUSED", "ld2")])
        self.assertEqual(ev[0].target, "emulator-5554")
        blob = " ".join(e.to_json() for e in ev)
        for secret in ("aaaa1111", "bbbb2222", "zzzz"):
            self.assertNotIn(secret, blob)


class HelperTests(unittest.TestCase):
    def test_serial_ports(self):
        self.assertEqual(adb_serial_ports("emulator-5554"), [5554, 5555])
        self.assertEqual(adb_serial_ports("127.0.0.1:5565"), [5565])
        self.assertEqual(adb_serial_ports("R58M123ABC"), [])

    def test_netstat_parser(self):
        text = ("  Proto  Local Address          Foreign Address        State           PID\r\n"
                "  TCP    0.0.0.0:5555           0.0.0.0:0              LISTENING       1200\r\n"
                "  TCP    127.0.0.1:5037         0.0.0.0:0              LISTENING       20324\r\n"
                "  TCP    127.0.0.1:5555         127.0.0.1:50000        ESTABLISHED     1200\r\n"
                "  TCP    [::]:135               [::]:0                 LISTENING       900\r\n")
        # ESTABLISHED lines are ignored; IPv4 and IPv6 listeners are both recorded.
        self.assertEqual(parse_netstat_listeners(text), {5555: {1200}, 5037: {20324}, 135: {900}})

    def test_registry_rules(self):
        reg = InstanceRegistry([InstanceProfile("a", 1, "n1")])
        with self.assertRaises(RegistryError):
            reg.add(InstanceProfile("a", 2, "n2"))
        with self.assertRaises(RegistryError):
            reg.add(InstanceProfile("b", 1, "n1"))
        with self.assertRaises(RegistryError):
            reg.add(InstanceProfile(" ", 3, "n3"))


if __name__ == "__main__":
    unittest.main()
