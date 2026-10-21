"""Tests for LDPlayer discovery, the config reader and the ADB client (fakes only)."""
import socket
import tempfile
import unittest
from pathlib import Path

from app.adb.client import (
    AdbClient, AdbError, AdbNotFound, AdbServerNotRunning, AdbStatus, DeviceState, parse_devices, parse_version,
)
from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation
from app.ldplayer.config_reader import (
    InstanceConfigError, find_identity_conflicts, parse_instance_config, read_instance_config,
)
from app.ldplayer.console import LDConsole, LDConsoleError, parse_list2, parse_runninglist
from app.logging.structured import LogHub, MemorySink
from tests.fakes import FakeRunner, result

LIST2 = (
    "0,LDPlayer,0,0,0,-1,-1,960,540,160\r\n"
    "9,LDPlayer-9,0,0,0,-1,-1,1280,720,240\r\n"
    "17,LDPlayer-17,0,0,0,-1,-1,960,540,160\r\n"
)


class ParseList2Tests(unittest.TestCase):
    def test_real_shape(self):
        items = parse_list2(LIST2)
        self.assertEqual([i.index for i in items], [0, 9, 17])
        self.assertEqual((items[1].name, items[1].width, items[1].height, items[1].dpi), ("LDPlayer-9", 1280, 720, 240))
        self.assertEqual((items[0].pid, items[0].vbox_pid), (-1, -1))

    def test_running_shape_values_parsed(self):
        item = parse_list2("3,Farm-03,199234,199236,1,4520,6100,960,540,160")[0]
        self.assertEqual((item.top_hwnd, item.android_started, item.pid, item.vbox_pid), (199234, 1, 4520, 6100))

    def test_comma_in_name(self):
        item = parse_list2("2,My, odd name,0,0,0,-1,-1,960,540,160")[0]
        self.assertEqual((item.index, item.name), (2, "My, odd name"))

    def test_malformed_refused(self):
        for bad in ("nonsense", "x,LDPlayer,0,0,0,-1,-1,960,540,160", "1,LDPlayer,0,0,0,-1,-1,960,540", "1,N,a,0,0,-1,-1,9,5,1"):
            with self.subTest(bad=bad), self.assertRaises(LDConsoleError):
                parse_list2(bad)

    def test_duplicate_index_refused(self):
        with self.assertRaises(LDConsoleError):
            parse_list2("1,A,0,0,0,-1,-1,1,1,1\n1,B,0,0,0,-1,-1,1,1,1")

    def test_empty_output_is_no_instances(self):
        self.assertEqual(parse_list2(""), [])
        self.assertEqual(parse_runninglist(""), [])
        self.assertEqual(parse_runninglist("A\r\n\r\nB\r\n"), ["A", "B"])


class LDConsoleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ldconsole.exe"
        self.path.write_bytes(b"")

    def test_lists_instances_with_fake_runner(self):
        runner = FakeRunner(lambda a: result(LIST2))
        console = LDConsole(self.path, runner, 5)
        self.assertEqual(len(console.list_instances()), 3)
        self.assertEqual(runner.calls[0][0][1:], ("list2",))
        self.assertEqual(runner.calls[0][1], 5)

    def test_only_read_only_commands_allowed(self):
        runner = FakeRunner(lambda a: result(""))
        console = LDConsole(self.path, runner, 5)
        for cmd in ("launch", "quit", "quitall", "remove", "copy", "reboot", "modify", "adb", "runapp", "killapp", ""):
            with self.subTest(cmd=cmd), self.assertRaises(SafetyViolation):
                console.run_readonly(cmd, "--index", "0")
        self.assertEqual(runner.calls, [])  # nothing was executed

    def test_failure_modes_have_clear_errors(self):
        console = LDConsole(self.path, FakeRunner(lambda a: result("", "boom", 1)), 5)
        with self.assertRaises(LDConsoleError):
            console.list_instances()
        console = LDConsole(self.path, FakeRunner(lambda a: result(timed_out=True)), 5)
        with self.assertRaisesRegex(LDConsoleError, "did not answer"):
            console.list_instances()
        console = LDConsole(Path(self._tmp.name) / "missing.exe", FakeRunner(lambda a: result("")), 5)
        with self.assertRaisesRegex(LDConsoleError, "not found"):
            console.list_instances()

    def test_logs_operation(self):
        mem = MemorySink()
        console = LDConsole(self.path, FakeRunner(lambda a: result(LIST2)), 5, LogHub([mem]).logger("ldplayer"))
        console.list_instances()
        self.assertEqual(mem.query()[0].operation, "LDCONSOLE_LIST2")


CONFIG = {
    "propertySettings.phoneIMEI": "350000000000001", "propertySettings.phoneIMSI": "460000000000002",
    "propertySettings.phoneSimSerial": "89000000000000000003", "propertySettings.phoneAndroidId": "0123456789abcdef",
    "propertySettings.macAddress": "001122334455", "propertySettings.phoneModel": "SM-S9110",
    "propertySettings.phoneManufacturer": "samsung", "advancedSettings.resolution": {"width": 960, "height": 540},
    "advancedSettings.resolutionDpi": 160, "advancedSettings.cpuCount": 3, "advancedSettings.memorySize": 3072,
}


class ConfigReaderTests(unittest.TestCase):
    def test_parse_and_redaction(self):
        cfg = parse_instance_config(4, Path("x"), CONFIG)
        self.assertEqual((cfg.width, cfg.height, cfg.dpi, cfg.cpu_count, cfg.memory_mb), (960, 540, 160, 3, 3072))
        self.assertEqual(cfg.identity.model, "SM-S9110")
        for text in (repr(cfg.identity), str(cfg.identity)):
            for secret in ("350000000000001", "0123456789abcdef", "001122334455", "460000000000002"):
                self.assertNotIn(secret, text)
        self.assertEqual(cfg.identity.values()["android_id"], "0123456789abcdef")

    def test_reads_file_read_only_and_reports_errors(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "vms" / "config"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "leidian5.config").write_text(json.dumps(CONFIG), encoding="utf-8")
            (cfg_dir / "leidian6.config").write_text("{oops", encoding="utf-8")
            (cfg_dir / "leidian7.config").write_text("[1]", encoding="utf-8")
            before = (cfg_dir / "leidian5.config").read_bytes()
            self.assertEqual(read_instance_config(Path(tmp), 5).index, 5)
            self.assertEqual((cfg_dir / "leidian5.config").read_bytes(), before)
            for idx in (6, 7, 8):
                with self.subTest(idx=idx), self.assertRaises(InstanceConfigError):
                    read_instance_config(Path(tmp), idx)

    def test_identity_conflicts_expose_kinds_not_values(self):
        a = parse_instance_config(1, Path("a"), CONFIG)
        b = parse_instance_config(2, Path("b"), CONFIG)
        c = parse_instance_config(3, Path("c"), {**CONFIG, "propertySettings.phoneAndroidId": "zzz",
                                                 "propertySettings.phoneIMEI": "1", "propertySettings.phoneIMSI": "2",
                                                 "propertySettings.phoneSimSerial": "3", "propertySettings.macAddress": "4"})
        conflicts = find_identity_conflicts([a, b, c])
        self.assertEqual(conflicts["android_id"], [1, 2])
        self.assertNotIn(3, conflicts["imei"])
        self.assertNotIn("0123456789abcdef", str(conflicts))
        self.assertEqual(find_identity_conflicts([a, c]), {})


DEVICES = (
    "List of devices attached\r\n"
    "emulator-5554          device product:sdk model:SM_S9110 device:star2 transport_id:1\r\n"
    "emulator-5556          offline transport_id:2\r\n"
    "127.0.0.1:5557         unauthorized\r\n"
    "weird-1                sideload\r\n"
    "\r\n"
)


class ParseAdbTests(unittest.TestCase):
    def test_devices_and_states(self):
        devs = parse_devices(DEVICES)
        self.assertEqual([d.serial for d in devs], ["emulator-5554", "emulator-5556", "127.0.0.1:5557", "weird-1"])
        self.assertEqual([d.state for d in devs],
                         [DeviceState.DEVICE, DeviceState.OFFLINE, DeviceState.UNAUTHORIZED, DeviceState.OTHER])
        self.assertEqual(devs[0].props["model"], "SM_S9110")
        self.assertEqual(devs[3].raw_state, "sideload")

    def test_daemon_chatter_and_empty(self):
        self.assertEqual(parse_devices("* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
                                       "List of devices attached\n\n"), [])
        self.assertEqual(parse_devices(""), [])

    def test_duplicate_and_malformed_refused(self):
        with self.assertRaises(AdbError):
            parse_devices("List of devices attached\na device\na device\n")
        with self.assertRaises(AdbError):
            parse_devices("List of devices attached\nlonely\n")

    def test_version(self):
        text = "Android Debug Bridge version 1.0.41\nVersion 34.0.4-10411341\nInstalled as x\n"
        self.assertEqual(parse_version(text), "1.0.41 (34.0.4-10411341)")
        self.assertIsNone(parse_version("nope"))


class StubClient(AdbClient):
    running = True

    def server_running(self):
        return self.running


class AdbClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.adb = Path(self._tmp.name) / "adb.exe"
        self.adb.write_bytes(b"")
        self.stop = EmergencyStop()

    def client(self, handler, **kw):
        runner = FakeRunner(handler)
        kw.setdefault("backoff_s", 0.0)
        return StubClient(self.adb, runner, self.stop, timeout_s=7, **kw), runner

    def test_snapshot_states(self):
        c, _ = self.client(lambda a: result(DEVICES if "devices" in a else "Android Debug Bridge version 1.0.41\n"))
        snap = c.snapshot()
        self.assertEqual((snap.status, len(snap.devices), snap.version), (AdbStatus.DEVICES_PRESENT, 4, "1.0.41"))
        c, _ = self.client(lambda a: result("List of devices attached\n\n" if "devices" in a else "Android Debug Bridge version 1.0.41\n"))
        self.assertEqual(c.snapshot().status, AdbStatus.NO_DEVICES)
        c.running = False
        self.assertEqual(c.snapshot().status, AdbStatus.SERVER_NOT_RUNNING)
        missing = StubClient(Path(self._tmp.name) / "none.exe", FakeRunner(lambda a: result("")), self.stop)
        self.assertEqual(missing.snapshot().status, AdbStatus.NOT_FOUND)

    def test_snapshot_never_starts_server(self):
        c, runner = self.client(lambda a: result("Android Debug Bridge version 1.0.41\n"))
        c.running = False
        c.snapshot()
        flat = [x for call, _ in runner.calls for x in call]
        self.assertNotIn("start-server", flat)
        self.assertNotIn("devices", flat)  # not even queried when the server is down

    def test_list_devices_requires_running_server_and_binary(self):
        c, _ = self.client(lambda a: result(""))
        c.running = False
        with self.assertRaises(AdbServerNotRunning):
            c.list_devices()
        with self.assertRaises(AdbNotFound):
            StubClient(None, FakeRunner(lambda a: result("")), self.stop).version()

    def test_start_server_gated(self):
        c, runner = self.client(lambda a: result(""))
        with self.assertRaises(SafetyViolation):
            c.start_server()
        self.assertEqual(runner.calls, [])
        c2, runner2 = self.client(lambda a: result(""), allow_server_start=True)
        c2.start_server()
        self.assertEqual(runner2.calls[0][0][1:], ("start-server",))

    def test_every_command_gets_a_timeout(self):
        c, runner = self.client(lambda a: result("List of devices attached\n\n"))
        c.list_devices()
        self.assertTrue(all(t == 7 for _, t in runner.calls))

    def test_retries_are_bounded_for_transient_errors(self):
        c, runner = self.client(lambda a: result("", "error: device offline", 1), retries=2)
        with self.assertRaises(AdbError):
            c.getprop("emulator-5554", "ro.build.version.sdk")
        self.assertEqual(len(runner.calls), 3)  # 1 try + 2 retries, then stop

    def test_no_retry_for_permanent_errors(self):
        c, runner = self.client(lambda a: result("", "error: unknown thing", 1), retries=3)
        with self.assertRaises(AdbError):
            c.getprop("emulator-5554", "x")
        self.assertEqual(len(runner.calls), 1)

    def test_transient_then_success(self):
        answers = iter([result("", "error: device offline", 1), result("33\r\n")])
        c, runner = self.client(lambda a: next(answers))
        self.assertEqual(c.getprop("emulator-5554", "ro.build.version.sdk"), "33")
        self.assertEqual(len(runner.calls), 2)

    def test_timeout_is_reported_after_bounded_retries(self):
        c, runner = self.client(lambda a: result(timed_out=True), retries=1)
        with self.assertRaisesRegex(AdbError, "did not answer"):
            c.getprop("emulator-5554", "x")
        self.assertEqual(len(runner.calls), 2)

    def test_emergency_stop_blocks_commands(self):
        c, runner = self.client(lambda a: result(""))
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            c.getprop("emulator-5554", "x")
        self.assertEqual(runner.calls, [])

    def test_stop_during_backoff_ends_retries(self):
        def handler(args):
            self.stop.trigger()
            return result("", "device offline", 1)
        c, runner = self.client(handler, retries=5, backoff_s=0.01)
        with self.assertRaises(EmergencyStopped):
            c.getprop("emulator-5554", "x")
        self.assertEqual(len(runner.calls), 1)

    def test_shell_query_allowlist(self):
        c, runner = self.client(lambda a: result("ok\r\n"))
        for allowed in (("getprop", "ro.x"), ("settings", "get", "secure", "android_id"),
                        ("pm", "list", "packages"), ("wm", "size"), ("wm", "density")):
            self.assertEqual(c.shell_query("emulator-5554", *allowed), "ok")
        before = len(runner.calls)
        for forbidden in (("input", "tap", "1", "1"), ("rm", "-rf", "/sdcard"), ("settings", "put", "a", "b"),
                          ("am", "start", "x"), ("pm", "uninstall", "x"), ("wm", "size", "reset"), ("reboot",), ("su",), ()):
            with self.subTest(cmd=forbidden), self.assertRaises(SafetyViolation):
                c.shell_query("emulator-5554", *forbidden)
        self.assertEqual(len(runner.calls), before)

    def test_serial_and_property_validation(self):
        c, runner = self.client(lambda a: result("x"))
        for forbidden in (("wm", "density", "100"), ("wm", "overscan", "0,0,0,0"), ("pm", "list", "packages", "a;b"),
                          ("pm", "list", "packages", "x", "y"), ("settings", "get", "secure"),
                          ("getprop", "a", "b"), ("getprop", "a;reboot"), ("settings", "get", "secure", "$(id)")):
            with self.subTest(cmd=forbidden), self.assertRaises(SafetyViolation):
                c.shell_query("emulator-5554", *forbidden)
        self.assertEqual(runner.calls, [])
        for bad in ("", "a b", "a;b", "$(x)", "x" * 200, "emulator-5554 && rm"):
            with self.subTest(serial=bad), self.assertRaises(AdbError):
                c.getprop(bad, "ro.x")
        with self.assertRaises(AdbError):
            c.getprop("emulator-5554", "ro.x; reboot")
        self.assertEqual(runner.calls, [])

    def test_retries_argument_bounded(self):
        with self.assertRaises(ValueError):
            AdbClient(self.adb, FakeRunner(lambda a: result("")), self.stop, retries=99)

    def test_logging_marks_attempts_and_hides_ids(self):
        mem = MemorySink()
        answers = iter([result("", "device offline", 1), result("350000000000001\n")])
        c = StubClient(self.adb, FakeRunner(lambda a: next(answers)), self.stop, timeout_s=3, backoff_s=0.0,
                       logger=LogHub([mem]).logger("adb"))
        c.getprop("emulator-5554", "ro.serialno")
        events = mem.query(operation="ADB_SHELL_QUERY")
        self.assertEqual([e.details["attempt"] for e in events], [1, 2])
        self.assertEqual(events[0].target, "emulator-5554")
        self.assertNotIn("350000000000001", " ".join(e.to_json() for e in mem.query()))


class ServerRunningTests(unittest.TestCase):
    def test_real_socket_probe(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            c = AdbClient(None, FakeRunner(lambda a: result("")), EmergencyStop(), server_port=port)
            self.assertTrue(c.server_running())
        finally:
            listener.close()
        self.assertFalse(c.server_running())


if __name__ == "__main__":
    unittest.main()
