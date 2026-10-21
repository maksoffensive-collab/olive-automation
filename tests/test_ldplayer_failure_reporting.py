"""LDPlayer console / configuration failures are typed, clean refusals (fifth bug-fix stage).

  C-15  an ldconsole failure (`LDConsoleError`) escaped `MappingService.verify` / `probe` / `resolve_selection`
        untyped; the contract is `TargetNotVerified` with a reason code, per instance;
  C-13  the LDPlayer command-line tools died with a traceback on an invalid configuration (`ConfigError`) or when
        no LDPlayer folder was found (`Path(None)` TypeError).
Deterministic: fakes and subprocesses that stop before any external command; no real LDPlayer is touched.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, TargetNotVerified
from app.instances.profile import InstanceRegistry
from app.instances.selection import Selection, resolve_selection
from app.ldplayer.console import LDConsoleError
from tests.test_mapping import FakeConsole, make_service, two_instance_world

ROOT = Path(__file__).resolve().parents[1]


class BrokenConsole(FakeConsole):
    """A console whose `list2` and/or `runninglist` fail the way `LDConsole` reports it."""

    def __init__(self, world, *, list_fails=False, running_fails=False, message="ldconsole 'list2' did not answer within 20 s."):
        super().__init__(world)
        self.list_fails, self.running_fails, self.message = list_fails, running_fails, message

    def list_instances(self):
        if self.list_fails:
            raise LDConsoleError(self.message)
        return super().list_instances()

    def running_names(self):
        if self.running_fails:
            raise LDConsoleError(self.message)
        return super().running_names()


class CountingAdb:
    """Fails the test if the ADB layer is reached at all."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def list_devices(self):
        self.calls += 1
        return self.inner.list_devices()

    def getprop(self, *a):
        self.calls += 1
        return self.inner.getprop(*a)


class ConsoleFailureIsATypedRefusal(unittest.TestCase):
    """C-15."""

    def service(self, **console_kw):
        world = two_instance_world()
        svc = make_service(world)
        svc._console = BrokenConsole(world, **console_kw)
        svc._adb = CountingAdb(svc._adb)
        return svc, world

    def assertRefused(self, fn, code="ldconsole_error"):
        with self.assertRaises(TargetNotVerified) as cm:
            fn()
        self.assertEqual(cm.exception.reason_code, code)
        return cm.exception

    def test_list2_failure_is_target_not_verified(self):
        svc, _ = self.service(list_fails=True)
        exc = self.assertRefused(lambda: svc.verify("ld1"))
        self.assertIn("did not answer", str(exc))
        self.assertEqual(svc._adb.calls, 0)                                   # nothing was sent to any device

    def test_runninglist_failure_is_target_not_verified(self):
        svc, _ = self.service(running_fails=True)
        self.assertRefused(lambda: svc.verify("ld1"))
        self.assertEqual(svc._adb.calls, 0)

    def test_probe_and_revalidate_and_verify_many_follow_the_same_contract(self):
        svc, _ = self.service(list_fails=True)
        self.assertRefused(lambda: svc.probe("ld1"))
        self.assertRefused(lambda: svc.verify_many(["ld1", "ld2"]))

    def test_selection_reports_the_failure_per_instance_instead_of_aborting(self):
        svc, world = self.service(list_fails=True)
        registry = InstanceRegistry.from_discovery(world.ld_instances())
        result = resolve_selection(Selection.of("ld1", "ld2"), registry, svc)
        self.assertEqual(result.targets, [])
        self.assertEqual(sorted(result.failures), ["ld1", "ld2"])
        self.assertIn("did not answer", result.failures["ld1"])

    def test_a_console_that_recovers_verifies_normally(self):
        svc, _ = self.service()
        self.assertEqual(svc.verify("ld1").adb_serial, "emulator-5554")

    def test_the_emergency_stop_is_never_turned_into_a_refusal(self):
        world = two_instance_world()
        stop = EmergencyStop()
        svc = make_service(world, stop=stop)

        class StopsWhileRunning(FakeConsole):
            def list_instances(self_inner):
                stop.trigger("panic")
                raise LDConsoleError("ldconsole 'list2' was cancelled.")       # what LDConsole reports for a killed command

        svc._console = StopsWhileRunning(world)
        with self.assertRaises(EmergencyStopped):
            svc.verify("ld1")

    def test_the_message_never_contains_identifiers(self):
        svc, _ = self.service(list_fails=True)
        text = str(self.assertRefused(lambda: svc.verify("ld1")))
        for secret in ("aaaa1111", "bbbb2222"):
            self.assertNotIn(secret, text)

    def test_unknown_id_and_other_refusals_are_unchanged(self):
        svc, world = self.service()
        self.assertRefused(lambda: svc.verify("nope"), "unknown_instance")
        world.instances[0].running = False
        self.assertRefused(lambda: svc.verify("ld1"), "not_running")


def run_tool(name: str, args: list[str], env_extra: dict[str, str], drop: tuple[str, ...] = ()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(ROOT / "tools" / f"{name}.py"), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


TOOLS = (("adb_session_check", ["--index", "9"]), ("input_tap_test", ["--index", "9", "--x", "1", "--y", "1"]),
         ("screenshot_check", ["--index", "9", "--width", "1", "--height", "1"]), ("vision_check", ["--index", "9"]),
         ("verify_mapping", ["--index", "9"]), ("discover", []))


class ToolsReportBadConfigurationCleanly(unittest.TestCase):
    """C-13."""

    def assert_clean_exit(self, done, *needles):
        self.assertNotIn("Traceback", done.stderr, done.stderr)
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        for needle in needles:
            self.assertIn(needle, done.stdout)

    def test_an_invalid_configured_folder_is_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, args in TOOLS:
                with self.subTest(tool=name):
                    done = run_tool(name, args, {"LDPLAYER_DIR": str(Path(tmp) / "no-such-folder")})
                    self.assert_clean_exit(done, "LDPlayer")
                    self.assertIn("ldconsole.exe", done.stdout)

    def test_an_invalid_numeric_setting_is_a_message_not_a_traceback(self):
        for name, args in TOOLS:
            with self.subTest(tool=name):
                done = run_tool(name, args, {"LDAP_RETRY_COUNT": "99"})
                self.assert_clean_exit(done, "retry_count")

    def test_no_ldplayer_installation_is_a_message_not_a_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SystemDrive": tmp, "ProgramFiles": "", "ProgramFiles(x86)": ""}
            for name, args in TOOLS:
                with self.subTest(tool=name):
                    done = run_tool(name, args, env, drop=("LDPLAYER_DIR",))
                    self.assert_clean_exit(done, "LDPlayer")

    def test_a_valid_configuration_still_gets_past_the_check(self):
        # the tools must not have become unreachable: with a valid folder they proceed (and stop at the first
        # external command, which does not exist in this fake folder)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "LDPlayer14"
            fake.mkdir()
            (fake / "ldconsole.exe").write_bytes(b"")
            (fake / "adb.exe").write_bytes(b"")
            done = run_tool("discover", [], {"LDPLAYER_DIR": str(fake), "LDAP_DATA_DIR": str(Path(tmp) / "data")})
            self.assertNotIn("configuration", done.stdout.lower())
            self.assertIn(str(fake), done.stdout)


if __name__ == "__main__":
    unittest.main()
