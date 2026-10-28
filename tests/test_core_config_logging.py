"""Tests for the emergency stop, command runner, configuration and structured logging."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.config.settings import Settings, discover_ldplayer_dir, is_ldplayer_dir, load_settings
from app.core.cancel import EmergencyStop
from app.core.errors import ConfigError, EmergencyStopped
from app.core.process import CommandRunner
from app.logging.structured import ConsoleSink, JsonlFileSink, LogHub, MemorySink, redact

PY = sys.executable


class EmergencyStopTests(unittest.TestCase):
    def test_trigger_check_reset(self):
        stop = EmergencyStop()
        stop.check()
        stop.trigger("test")
        self.assertTrue(stop.is_set())
        self.assertEqual(stop.reason, "test")
        with self.assertRaises(EmergencyStopped):
            stop.check()
        stop.reset()
        stop.check()
        self.assertIsNone(stop.reason)

    def test_first_reason_is_kept(self):
        stop = EmergencyStop()
        stop.trigger("first")
        stop.trigger("second")
        self.assertEqual(stop.reason, "first")

    def test_wait_returns_early_when_triggered(self):
        stop = EmergencyStop()
        threading.Timer(0.1, stop.trigger).start()
        started = time.monotonic()
        self.assertTrue(stop.wait(5))
        self.assertLess(time.monotonic() - started, 2)


class CommandRunnerTests(unittest.TestCase):
    def setUp(self):
        self.stop = EmergencyStop()
        self.runner = CommandRunner(self.stop)

    def test_captures_output_and_exit_code(self):
        r = self.runner.run([PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                            timeout=20)
        self.assertEqual(r.returncode, 3)
        self.assertIn("out", r.stdout_text)
        self.assertIn("err", r.stderr_text)
        self.assertFalse(r.ok)
        self.assertFalse(r.timed_out)

    def test_success(self):
        r = self.runner.run([PY, "-c", "print('hi')"], timeout=20)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout_text.strip(), "hi")

    def test_timeout_kills_process(self):
        started = time.monotonic()
        r = self.runner.run([PY, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertLess(time.monotonic() - started, 10)

    def test_cancel_event_kills_process(self):
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        r = self.runner.run([PY, "-c", "import time; time.sleep(30)"], timeout=30, cancel=cancel)
        self.assertTrue(r.cancelled)
        self.assertLess(time.monotonic() - started, 10)

    def test_emergency_stop_kills_running_process(self):
        threading.Timer(0.4, self.stop.trigger).start()
        started = time.monotonic()
        r = self.runner.run([PY, "-c", "import time; time.sleep(30)"], timeout=30)
        self.assertTrue(r.cancelled)
        self.assertLess(time.monotonic() - started, 10)

    def test_emergency_stop_blocks_new_commands(self):
        self.stop.trigger()
        with self.assertRaises(EmergencyStopped):
            self.runner.run([PY, "-c", "print(1)"], timeout=5)

    def test_timeout_is_required(self):
        for bad in (None, 0, -1):
            with self.assertRaises(ValueError):
                self.runner.run([PY, "-c", "pass"], timeout=bad)

    def test_shell_strings_refused(self):
        with self.assertRaises(TypeError):
            self.runner.run("echo hi", timeout=5)

    def test_empty_command_is_refused(self):
        with self.assertRaises(ValueError):
            self.runner.run([], timeout=5)

    def test_missing_program_reported_not_raised(self):
        r = self.runner.run([r"C:\definitely\not\here.exe"], timeout=5)
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.error)


class ProcessRunningTests(unittest.TestCase):
    """app.core.procs: whether a process still runs -- read-only, starts nothing; unknown means "still runs"."""

    def test_a_running_process_an_ended_one_and_no_process(self):
        import os
        import subprocess
        from app.core.procs import process_running
        self.assertTrue(process_running(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])          # a process of this test's own
        child.wait(timeout=30)
        self.assertFalse(process_running(child.pid))                     # ended (its exit code is known)
        for nothing in (0, -1, True, "12", 4_000_000):
            self.assertFalse(process_running(nothing), nothing)

    def test_a_process_it_may_not_ask_about_counts_as_running(self):
        from unittest import mock

        import app.core.procs as procs
        for error, running in ((5, True), (87, False)):               # no right to ask / no such process
            with self.subTest(error=error), \
                    mock.patch.object(procs, "_kernel32", return_value=mock.Mock(OpenProcess=lambda *a: None)), \
                    mock.patch.object(procs.ctypes, "get_last_error", return_value=error):
                self.assertEqual(procs.process_running(1234), running)

    def test_held_processes_are_asked_by_their_handles(self):
        """Held before they are asked to end, so an id given to another program afterwards cannot look like them."""
        import subprocess
        from app.core.procs import hold_processes
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with hold_processes((child.pid, 0, -1, 4_000_000)) as held:
                self.assertTrue(held.running())
                child.kill()
                child.wait(timeout=30)
                self.assertFalse(held.running())
        finally:
            if child.poll() is None:
                child.kill()
        with hold_processes((4_000_000,)) as gone:                      # gone already: nothing to wait for
            self.assertFalse(gone.running())


class SettingsTests(unittest.TestCase):
    def _fake_ld(self, root: Path) -> Path:
        d = root / "LDPlayer14"
        d.mkdir()
        (d / "ldconsole.exe").write_bytes(b"")
        (d / "adb.exe").write_bytes(b"")
        return d

    def test_defaults_have_bounded_values(self):
        s = Settings()
        self.assertGreater(s.default_timeout_s, 0)
        self.assertLessEqual(s.retry_count, 5)

    def test_env_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = load_settings(env={"LDAP_RETRY_COUNT": "3", "LDAP_LOG_LEVEL": "debug", "SystemDrive": tmp})
            self.assertEqual((s.retry_count, s.log_level), (3, "DEBUG"))

    def test_invalid_values_rejected(self):
        for env in ({"LDAP_RETRY_COUNT": "99"}, {"LDAP_RETRY_COUNT": "x"}, {"LDAP_ADB_TIMEOUT_S": "0"},
                    {"LDAP_LOG_LEVEL": "LOUD"}, {"LDAP_PERFORMANCE_PROFILE": "turbo"},
                    {"LDAP_WORKER_LIMIT": "0"}):
            with self.subTest(env=env), self.assertRaises(ConfigError):
                load_settings(env=env)

    def test_toml_file_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.toml"
            cfg.write_text('retry_count = 1\nlog_level = "WARNING"\nmystery = 5\n', encoding="utf-8")
            s = load_settings(cfg, env={"LDAP_LOG_LEVEL": "ERROR", "SystemDrive": tmp})
            self.assertEqual((s.retry_count, s.log_level), (1, "ERROR"))
            self.assertEqual(s.unknown_keys, ("mystery",))

    def test_bad_toml_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.toml"
            bad.write_text("this is = = not toml", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_settings(bad, env={})
            with self.assertRaises(ConfigError):
                load_settings(Path(tmp) / "missing.toml", env={})

    def test_ldplayer_dir_must_contain_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                load_settings(env={"LDPLAYER_DIR": tmp})
            good = self._fake_ld(Path(tmp))
            s = load_settings(env={"LDPLAYER_DIR": str(good)})
            self.assertEqual(s.ldplayer_dir, good)
            self.assertEqual(s.ldconsole_path, good / "ldconsole.exe")
            self.assertEqual(s.adb_path, good / "adb.exe")

    def test_discovery_accepts_only_real_installs(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self._fake_ld(Path(tmp))
            empty = Path(tmp) / "nothing"
            empty.mkdir()
            self.assertFalse(is_ldplayer_dir(empty))
            self.assertEqual(discover_ldplayer_dir([Path(tmp) / "missing", empty, good]), good)
            self.assertIsNone(discover_ldplayer_dir([empty]))


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_and_long_digits(self):
        out = redact({"imei": "350000000000001", "api_key": "abc", "note": "id 350000000000001 here",
                      "nested": {"Password": "x", "ok": 1}, "list": ["350000000000001", "fine"]})
        self.assertEqual(out["imei"], "[REDACTED]")
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertNotIn("350000000000001", out["note"])
        self.assertEqual(out["nested"], {"Password": "[REDACTED]", "ok": 1})
        self.assertEqual(out["list"], ["[REDACTED]", "fine"])

    def test_short_numbers_untouched(self):
        self.assertEqual(redact({"port": 5555, "text": "emulator-5554"}), {"port": 5555, "text": "emulator-5554"})


class LoggingTests(unittest.TestCase):
    def test_event_fields_and_query(self):
        mem = MemorySink()
        log = LogHub([mem], "DEBUG").logger("adb").bind(instance="Farm-01", target="emulator-5554")
        log.info("FIND_IMAGE", result="FOUND", duration_ms=421, confidence=0.93)
        log.error("TAP", result="FAILED", error="device offline", recovery_action="reconnect")
        other = LogHub([mem], "DEBUG").logger("vision")
        other.debug("SCAN")
        events = mem.query()
        self.assertEqual(len(events), 3)
        e = events[0]
        self.assertEqual((e.level, e.component, e.instance, e.target, e.operation, e.result, e.duration_ms),
                         ("INFO", "adb", "Farm-01", "emulator-5554", "FIND_IMAGE", "FOUND", 421))
        self.assertEqual(e.details, {"confidence": 0.93})
        self.assertEqual(len(mem.query(min_level="ERROR")), 1)
        self.assertEqual(len(mem.query(component="vision")), 1)
        self.assertEqual(len(mem.query(instance="Farm-01")), 2)
        self.assertEqual(len(mem.query(operation="TAP")), 1)
        self.assertEqual(len(mem.query(limit=1)), 1)

    def test_min_level_filters(self):
        mem = MemorySink()
        log = LogHub([mem], "WARNING").logger("x")
        log.info("A")
        log.warning("B")
        self.assertEqual([e.operation for e in mem.query()], ["B"])

    def test_secrets_never_reach_sinks(self):
        mem = MemorySink()
        log = LogHub([mem]).logger("x")
        log.info("PROBE", imei="350000000000001", message="serial 350000000000001", error="tok 350000000000001")
        e = mem.query()[0]
        blob = e.to_json()
        self.assertNotIn("350000000000001", blob)

    def test_memory_sink_is_bounded(self):
        mem = MemorySink(max_events=10)
        log = LogHub([mem]).logger("x")
        for i in range(50):
            log.info(f"OP{i}")
        events = mem.query()
        self.assertEqual(len(events), 10)
        self.assertEqual(events[-1].operation, "OP49")

    def test_broken_sink_does_not_break_logging(self):
        class Broken:
            def emit(self, event):
                raise RuntimeError("boom")
        mem = MemorySink()
        LogHub([Broken(), mem]).logger("x").info("A")
        self.assertEqual(len(mem.query()), 1)

    def test_timed_success_and_failure(self):
        mem = MemorySink()
        log = LogHub([mem]).logger("x")
        with log.timed("WORK"):
            pass
        with self.assertRaises(ValueError):
            with log.timed("BAD"):
                raise ValueError("nope")
        ok, bad = mem.query()
        self.assertEqual((ok.result, ok.level), ("OK", "INFO"))
        self.assertEqual((bad.result, bad.level), ("FAILED", "ERROR"))
        self.assertIn("nope", bad.error)
        self.assertIsNotNone(ok.duration_ms)

    def test_jsonl_file_sink_writes_valid_lines_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "app.jsonl"
            sink = JsonlFileSink(path, max_bytes=600, backups=2)
            log = LogHub([sink]).logger("x")
            for i in range(40):
                log.info("OP", index=i, message="hello Привет")
            files = sorted(p.name for p in path.parent.iterdir())
            self.assertIn("app.jsonl", files)
            self.assertIn("app.jsonl.1", files)
            self.assertLessEqual(len(files), 3)  # current + 2 backups
            for p in path.parent.iterdir():
                for line in p.read_text(encoding="utf-8").splitlines():
                    self.assertEqual(json.loads(line)["component"], "x")

    def test_console_sink_format(self):
        import io
        buf = io.StringIO()
        log = LogHub([ConsoleSink(buf)]).logger("adb").bind(instance="Farm-03", target="emulator-5558")
        log.info("FIND_IMAGE", result="FOUND", duration_ms=421)
        line = buf.getvalue()
        for part in ("[INFO]", "adb", "instance=Farm-03", "target=emulator-5558", "op=FIND_IMAGE",
                     "result=FOUND", "421ms"):
            self.assertIn(part, line)

    def test_console_sink_does_not_interleave_concurrent_writes(self):
        """Reliability: `run_on_targets` (SIMULTANEOUS/STAGGERED) and AdbClient's own retry logging can call a
        shared Logger from several worker threads at once. MemorySink and JsonlFileSink already guard emit() with
        their own lock; ConsoleSink must too, or two concurrent events can be written half-interleaved into one
        garbled line. The stream below deliberately splits each write and pauses mid-write, which reproduces the
        interleaving deterministically without a lock and must never do so with one."""
        class RacyStream:
            def __init__(self, gate):
                self.chunks: list[str] = []
                self.gate = gate

            def write(self, text):
                half = len(text) // 2
                self.chunks.append(text[:half])
                self.gate.set()                     # tell the other thread "I am mid-write, go now"
                time.sleep(0.05)
                self.chunks.append(text[half:])

        gate = threading.Event()
        sink = ConsoleSink(RacyStream(gate))
        ev_a = self._event("AAAA", "opA")
        ev_b = self._event("BBBB", "opB")

        def emit_a():
            sink.emit(ev_a)

        def emit_b():
            self.assertTrue(gate.wait(2), "the first emit never reached its write()")
            sink.emit(ev_b)

        ta, tb = threading.Thread(target=emit_a), threading.Thread(target=emit_b)
        ta.start(), tb.start()
        ta.join(5), tb.join(5)

        lines = [l for l in "".join(sink._stream.chunks).split("\n") if l]
        self.assertEqual(len(lines), 2, f"a write from one event leaked into the other: {lines!r}")
        self.assertTrue(any("AAAA" in l and "BBBB" not in l for l in lines))
        self.assertTrue(any("BBBB" in l and "AAAA" not in l for l in lines))

    @staticmethod
    def _event(component: str, operation: str) -> "LogEvent":
        from app.logging.structured import LogEvent
        return LogEvent(timestamp="t", level="INFO", component=component, operation=operation)

    def test_thread_safety_smoke(self):
        mem = MemorySink(max_events=100000)
        hub = LogHub([mem])

        def work(n):
            log = hub.logger(f"t{n}")
            for i in range(200):
                log.info("OP", i=i)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(mem.query()), 1600)


if __name__ == "__main__":
    unittest.main()
