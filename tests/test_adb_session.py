"""Tests for the read-only AdbSession (fakes only; live behaviour is checked by tools/adb_session_check.py)."""
import unittest

from app.adb.client import AdbError, DeviceState
from app.adb.session import AdbSession
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, TargetNotVerified
from tests.test_mapping import Dev, FakeAdb, Inst, World, make_service, two_instance_world


class SessionAdb(FakeAdb):
    """FakeAdb plus the extra read-only queries the session uses. Records every call."""
    def __init__(self, w):
        super().__init__(w)
        self.calls = []

    def list_devices(self):
        self.calls.append(("list_devices",))
        return super().list_devices()

    def getprop(self, serial, name):
        self.calls.append(("getprop", serial, name))
        if name == "ro.build.version.release":
            return "9"
        return super().getprop(serial, name)

    def shell_query(self, serial, *cmd):
        self.calls.append(("shell", serial, cmd))
        if cmd == ("wm", "size"):
            return "Physical size: 1280x720"
        if cmd == ("wm", "density"):
            return "Physical density: 240"
        if cmd[:3] == ("pm", "list", "packages"):
            return "package:com.example.one\npackage:com.example.onemore"
        raise AssertionError(cmd)


def build(w=None, max_age=60.0, clock=None):
    w = w or two_instance_world()
    svc = make_service(w)
    adb = SessionAdb(w)
    svc._adb = adb  # same fake world, with call recording
    stop = EmergencyStop()
    target = svc.verify("ld1")
    kw = {"clock": clock} if clock else {}
    return w, svc, adb, stop, AdbSession(target, adb, svc, stop, max_age_s=max_age, **kw)


class SessionTests(unittest.TestCase):
    def test_requires_verified_target(self):
        w, svc, adb, stop, _ = build()
        with self.assertRaises(TargetNotVerified):
            AdbSession("emulator-5554", adb, svc, stop)

    def test_queries_use_the_targets_serial_only(self):
        w, svc, adb, stop, s = build()
        adb.calls.clear()
        self.assertEqual(s.android_version(), "9")
        d = s.display()
        self.assertEqual((d.width, d.height, d.dpi), (1280, 720, 240))
        serials = {c[1] for c in adb.calls if c[0] != "list_devices"}
        self.assertEqual(serials, {"emulator-5554"})

    def test_package_check_exact_match_and_validation(self):
        _, _, _, _, s = build()
        self.assertTrue(s.is_package_installed("com.example.one"))
        self.assertFalse(s.is_package_installed("com.example"))  # prefix must not count
        for bad in ("x; rm -rf /", "com.example one", ""):
            with self.assertRaises(AdbError):
                s.is_package_installed(bad)

    def test_only_read_only_commands_are_issued(self):
        _, _, adb, _, s = build()
        s.display(); s.android_version(); s.is_package_installed("com.example.one")
        shells = [c[2] for c in adb.calls if c[0] == "shell"]
        self.assertTrue(all(c[0] in ("wm", "pm") and c not in (("wm", "size", "reset"),) for c in shells))

    def test_device_going_offline_is_refused(self):
        w, _, _, _, s = build()
        w.devices[0].state = DeviceState.OFFLINE
        with self.assertRaises(TargetNotVerified) as cm:
            s.display()
        self.assertEqual(cm.exception.reason_code, "device_not_online")

    def test_device_disappearing_is_refused(self):
        w, _, _, _, s = build()
        w.devices.pop(0)
        with self.assertRaises(TargetNotVerified) as cm:
            s.android_version()
        self.assertEqual(cm.exception.reason_code, "device_gone")

    def test_stale_target_is_reverified_and_serial_change_voids_session(self):
        now = [0.0]
        w, svc, adb, stop, s = build(max_age=10, clock=lambda: now[0] + s_start())
        # simulate: target was minted at real monotonic time; move the clock far ahead
        now[0] = 1e9
        # same world: re-verification succeeds and the session keeps working
        self.assertEqual(s.android_version(), "9")
        # emulator restarted on a new serial: identity follows the evidence, serial differs -> void
        w.devices[0].serial = "emulator-5560"
        now[0] = 2e9
        with self.assertRaises(TargetNotVerified) as cm:
            s.android_version()
        self.assertIn(cm.exception.reason_code, ("serial_changed", "device_gone"))

    def test_stale_target_that_no_longer_verifies_is_refused(self):
        now = [0.0]
        w, svc, adb, stop, s = build(max_age=10, clock=lambda: now[0] + s_start())
        w.devices[0].android_id = "different"
        now[0] = 1e9
        with self.assertRaises(TargetNotVerified):
            s.display()

    def test_emergency_stop_blocks_queries(self):
        _, _, adb, stop, s = build()
        adb.calls.clear()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            s.getprop("ro.serialno")
        self.assertEqual(adb.calls, [])

    def test_session_has_no_input_capability(self):
        _, _, _, _, s = build()
        for name in ("tap", "swipe", "input", "key", "text", "shell", "launch", "start_app"):
            self.assertFalse(hasattr(s, name), name)


def s_start():
    import time
    return time.monotonic()


if __name__ == "__main__":
    unittest.main()
