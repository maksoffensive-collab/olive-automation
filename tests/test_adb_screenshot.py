"""Tests for the read-only screenshot path (fakes and synthetic PNG data; no device touched)."""
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from app.adb.client import AdbClient, AdbError, CaptureGrant, DeviceState, InputGrant
from app.adb.png import png_size
from app.adb.session import AdbSession
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, TargetNotVerified
from tests.fakes import FakeRunner, result
from tests.test_adb_session import SessionAdb
from tests.test_mapping import make_service, two_instance_world


def make_png(w: int, h: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x10\x20\x30\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class ShotAdb(SessionAdb):
    def __init__(self, w, png):
        super().__init__(w)
        self.png = png
        self.shots = []

    def screencap_png(self, serial, *, grant):
        self.shots.append((serial, grant))
        return self.png


def build(png=None, w=None):
    w = w or two_instance_world()
    svc = make_service(w)
    adb = ShotAdb(w, png if png is not None else make_png(1280, 720))
    svc._adb = adb
    stop = EmergencyStop()
    return w, adb, stop, AdbSession(svc.verify("ld1"), adb, svc, stop)


class PngTests(unittest.TestCase):
    def test_reads_size(self):
        self.assertEqual(png_size(make_png(1280, 720)), (1280, 720))

    def test_rejects_garbage_truncation_corruption(self):
        good = make_png(20, 10)
        for bad in (b"", b"not a png" * 10, good[:-5], good.replace(b"\x00\x00\x00\x14", b"\x00\x00\x00\x15", 1),
                    good.replace(b"\n", b"\r\n")):
            with self.assertRaises(AdbError):
                png_size(bad)
        corrupt = bytearray(good)
        corrupt[20] ^= 0xFF  # inside IHDR: CRC must fail
        with self.assertRaises(AdbError):
            png_size(bytes(corrupt))


class ScreenshotSessionTests(unittest.TestCase):
    def test_capture_uses_verified_serial_and_checks_size(self):
        _, adb, _, s = build()
        shot = s.screenshot()
        self.assertEqual((shot.width, shot.height, shot.serial, shot.logical_id), (1280, 720, "emulator-5554", "ld1"))
        self.assertEqual([x[0] for x in adb.shots], ["emulator-5554"])
        self.assertIsInstance(adb.shots[0][1], CaptureGrant)

    def test_dimension_mismatch_discards(self):
        _, _, _, s = build(png=make_png(960, 540))
        with self.assertRaises(AdbError):
            s.screenshot()

    def test_invalid_png_discarded(self):
        _, _, _, s = build(png=b"garbage")
        with self.assertRaises(AdbError):
            s.screenshot()

    def test_requires_verified_target(self):
        w, adb, stop, s = build()
        with self.assertRaises(TargetNotVerified):
            AdbSession("emulator-5554", adb, make_service(w), stop)

    def test_offline_device_refused_no_capture(self):
        w, adb, _, s = build()
        w.devices[0].state = DeviceState.OFFLINE
        with self.assertRaises(TargetNotVerified):
            s.screenshot()
        self.assertEqual(adb.shots, [])

    def test_vanished_device_refused(self):
        w, adb, _, s = build()
        w.devices.pop(0)
        with self.assertRaises(TargetNotVerified):
            s.screenshot()
        self.assertEqual(adb.shots, [])

    def test_emergency_stop_blocks_capture(self):
        _, adb, stop, s = build()
        stop.trigger()
        with self.assertRaises(EmergencyStopped):
            s.screenshot()
        self.assertEqual(adb.shots, [])

    def test_serial_change_after_restart_voids_session(self):
        w, adb, _, s = build()
        s._max_age = 1e-9  # force re-verification
        w.devices[0].serial = "emulator-5560"
        with self.assertRaises(TargetNotVerified):
            s.screenshot()
        self.assertEqual(adb.shots, [])

    def test_other_instance_never_captured(self):
        _, adb, _, s = build()
        s.screenshot(); s.screenshot()
        self.assertNotIn("emulator-5556", {x[0] for x in adb.shots})

    def test_save_writes_inside_folder_and_never_overwrites(self):
        _, _, _, s = build()
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = s.save_screenshot(Path(tmp)), s.save_screenshot(Path(tmp))
            self.assertNotEqual(p1, p2)
            self.assertEqual(p1.parent, (Path(tmp) / "ld1").resolve())
            self.assertEqual(png_size(p1.read_bytes()), (1280, 720))

    def test_failed_capture_writes_no_file(self):
        _, _, _, s = build(png=b"garbage")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AdbError):
                s.save_screenshot(Path(tmp))
            self.assertEqual(list(Path(tmp).rglob("*")), [])


class ClientScreencapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adb = Path(self.tmp.name) / "adb.exe"
        self.adb.write_bytes(b"")
        self.stop = EmergencyStop()

    def client(self, handler):
        self.runner = FakeRunner(handler)
        return AdbClient(self.adb, self.runner, self.stop, retries=0, backoff_s=0)

    def grant(self, serial="emulator-5554"):
        from app.adb import client as c
        return CaptureGrant(serial, c._INPUT_MINT)

    def test_grant_unforgeable_and_serial_bound(self):
        with self.assertRaises(SafetyViolation):
            CaptureGrant("emulator-5554")
        c = self.client(lambda a: result(""))
        for g in (None, self.grant("emulator-5556"), InputGrant("emulator-5554", __import__("app.adb.client", fromlist=["x"])._INPUT_MINT)):
            with self.assertRaises(SafetyViolation):
                c.screencap_png("emulator-5554", grant=g)
        self.assertEqual(self.runner.calls, [])

    def test_exact_command_and_binary_passthrough(self):
        png = make_png(4, 4)
        c = self.client(lambda a: type(result(""))(a, 0, png, b"", 1))
        self.assertEqual(c.screencap_png("emulator-5554", grant=self.grant()), png)
        (argv, _), = self.runner.calls
        self.assertEqual(argv[1:], ("-s", "emulator-5554", "exec-out", "screencap", "-p"))

    def test_failure_and_timeout(self):
        for handler in (lambda a: result("", "boom", rc=1), lambda a: result("", timed_out=True)):
            c = self.client(handler)
            with self.assertRaises(AdbError):
                c.screencap_png("emulator-5554", grant=self.grant())

    def test_stop_runs_nothing(self):
        c = self.client(lambda a: result(""))
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            c.screencap_png("emulator-5554", grant=self.grant())
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
