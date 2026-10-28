"""ADB client (the ONLY module that runs adb.exe).

Phase 4 scope: discovery and READ-ONLY identity queries. There is deliberately no method
here that sends input or changes device state; those will live on a session object that can
only be built from a VerifiedTarget (see app/instances). Nothing here starts the ADB server
unless the client was created with allow_server_start=True.

Every command has a timeout, bounded retries (only for transient failures), and is logged.
Statuses are distinct on purpose (not found / server not running / no devices / offline /
unauthorized / connected), never collapsed into one generic error.
"""
from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError, SafetyViolation
from app.core.process import CommandResult, Runner
from app.logging.structured import Logger

_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,80}$")
QUICK_LIST_TIMEOUT_S = 3.0      # `adb devices` answers in well under a second; a hint waits no longer than this
_TRANSIENT = ("device offline", "closed", "protocol fault", "cannot connect", "device not found",
              "connection reset", "daemon not running")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")

# The allowlisted character set `input_text` accepts (1..256 characters): ASCII letters, digits, space, and a
# handful of clearly inert punctuation marks. Deliberately narrow rather than an attempt to escape everything --
# none of these characters have any special meaning to the remote Android shell or to `input text`'s own decoding,
# so no quoting scheme has to be trusted to be perfect. See AdbClient.input_text for how the string is transported.
SAFE_TEXT_PATTERN = re.compile(r"\A[A-Za-z0-9 .,_\-]{1,256}\Z")   # \A/\Z, not ^/$: never let a trailing "\n" hide


def is_read_only_query(command: tuple[str, ...]) -> bool:
    """True only for the exact, side-effect-free shell queries used for identity checks.

    The whole command shape is matched (not just the first words), so for example
    `wm size reset` or `settings put ...` are refused.
    """
    if not command or not all(isinstance(t, str) and _TOKEN_RE.match(t) for t in command):
        return False
    head = command[0]
    if head == "getprop":
        return len(command) <= 2
    if head == "settings":
        return len(command) == 4 and command[1] == "get"
    if head == "pm":
        return command[1:3] == ("list", "packages") and len(command) <= 4
    if head == "wm":
        return command in (("wm", "size"), ("wm", "density"))
    return False


class AdbError(PlatformError):
    pass


_INPUT_MINT = object()  # only app/adb/input.py holds this; see InputGrant


class CaptureGrant:
    """Permission slip for AdbClient.screencap_png. Only AdbSession (built from a VerifiedTarget) makes one."""
    def __init__(self, serial: str, _mint: object = None) -> None:
        if _mint is not _INPUT_MINT:
            raise SafetyViolation("A CaptureGrant can only be created by an AdbSession.")
        self.serial = serial


class InputGrant:
    """Permission slip for AdbClient.input_tap. Only InputSession (built from a VerifiedTarget) makes one."""
    def __init__(self, serial: str, _mint: object = None) -> None:
        if _mint is not _INPUT_MINT:
            raise SafetyViolation("An InputGrant can only be created by an InputSession.")
        self.serial = serial


class AdbNotFound(AdbError):
    pass


class AdbServerNotRunning(AdbError):
    pass


class AdbStatus(Enum):
    NOT_FOUND = "adb.exe not found"
    SERVER_NOT_RUNNING = "ADB server is not running"
    NO_DEVICES = "ADB is running, no devices attached"
    DEVICES_PRESENT = "ADB is running, devices attached"


class DeviceState(Enum):
    DEVICE = "device"              # connected and authorised
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    OTHER = "other"


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: DeviceState
    raw_state: str
    props: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdbSnapshot:
    status: AdbStatus
    version: str | None
    devices: tuple[AdbDevice, ...]


def parse_devices(text: str) -> list[AdbDevice]:
    """Parse `adb devices -l`. Daemon chatter ('* daemon ...') and the header are skipped."""
    out: list[AdbDevice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            raise AdbError(f"Unexpected 'adb devices' line: {line!r}").saying(
                ru=f"Непонятная строка в ответе «adb devices»: {line!r}")
        serial, raw_state = parts[0], parts[1]
        try:
            state = DeviceState(raw_state)
        except ValueError:
            state = DeviceState.OTHER
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        out.append(AdbDevice(serial, state, raw_state, props))
    serials = [d.serial for d in out]
    if len(set(serials)) != len(serials):
        raise AdbError("'adb devices' listed the same serial twice; refusing to continue.").saying(
            ru="«adb devices» показал один и тот же серийный номер дважды; продолжать нельзя.")
    return out


def parse_version(text: str) -> str | None:
    match = re.search(r"Android Debug Bridge version ([\d.]+)", text)
    build = re.search(r"Version ([\w.\-]+)", text)
    if not match:
        return None
    return match.group(1) + (f" ({build.group(1)})" if build else "")


class AdbClient:
    def __init__(self, adb_path: Path | None, runner: Runner, stop: EmergencyStop, *, timeout_s: float = 15.0,
                 retries: int = 2, backoff_s: float = 0.5, logger: Logger | None = None,
                 server_port: int = 5037, allow_server_start: bool = False) -> None:
        if retries < 0 or retries > 5:
            raise ValueError("retries must be between 0 and 5")
        self._path = Path(adb_path) if adb_path else None
        self._runner = runner
        self._stop = stop
        self._timeout = timeout_s
        self._retries = retries
        self._backoff = backoff_s
        self._log = logger
        self._port = server_port
        self._allow_start = allow_server_start
        self._starting: threading.Thread | None = None      # a start in the background, while it runs
        self._starting_lock = threading.Lock()
        self._start_failure: PlatformError | None = None     # why the last background start failed, until said

    # ---- environment -------------------------------------------------
    @property
    def may_start_server(self) -> bool:
        """Whether this client was created to start the ADB server when it is not running (the application's is:
        controlling an instance needs it; the read-only tools' are not)."""
        return self._allow_start

    def binary_found(self) -> bool:
        return self._path is not None and self._path.is_file()

    def server_running(self) -> bool:
        """True if something accepts connections on the ADB server port. Never starts the server."""
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                return True
        except OSError:
            return False

    def _require_binary(self) -> Path:
        if not self.binary_found():
            raise AdbNotFound("adb.exe was not found. Check the LDPlayer folder in Settings.").saying(
                ru="adb.exe не найден. Проверьте папку LDPlayer в настройках.")
        return self._path  # type: ignore[return-value]

    # ---- command execution (timeout + bounded retry + logging) ------------
    def _run(self, args: list[str], *, retry: bool = True, serial: str | None = None,
             operation: str = "ADB", timeout_s: float | None = None) -> CommandResult:
        adb = self._require_binary()
        attempts = (self._retries if retry else 0) + 1
        result: CommandResult | None = None
        for attempt in range(1, attempts + 1):
            self._stop.check()
            result = self._runner.run([str(adb), *args], timeout=timeout_s or self._timeout)
            transient = result.timed_out or (not result.ok and not result.cancelled and any(
                t in (result.stderr_text + result.stdout_text).lower() for t in _TRANSIENT))
            if self._log:
                log = self._log.bind(target=serial) if serial else self._log
                log.log("INFO" if result.ok else "WARNING", operation, "OK" if result.ok else "FAILED",
                        duration_ms=result.duration_ms, attempt=attempt, of=attempts, exit_code=result.returncode,
                        timed_out=result.timed_out, error=None if result.ok else (
                            result.error or ("timeout" if result.timed_out else result.stderr_text.strip()[:200])))
            if result.ok or result.cancelled or not transient or attempt == attempts:
                break
            if self._stop.wait(self._backoff * (2 ** (attempt - 1))):
                self._stop.check()
        assert result is not None
        return result

    # ---- discovery ---------------------------------------------------
    def version(self) -> str | None:
        result = self._run(["version"], retry=False, operation="ADB_VERSION")
        return parse_version(result.stdout_text) if result.ok else None

    def snapshot(self) -> AdbSnapshot:
        """Current ADB state without changing anything (does not start the server)."""
        if not self.binary_found():
            return AdbSnapshot(AdbStatus.NOT_FOUND, None, ())
        version = self.version()
        if not self.server_running():
            return AdbSnapshot(AdbStatus.SERVER_NOT_RUNNING, version, ())
        devices = tuple(self.list_devices())
        return AdbSnapshot(AdbStatus.DEVICES_PRESENT if devices else AdbStatus.NO_DEVICES, version, devices)

    def list_devices(self) -> list[AdbDevice]:
        return self._list_devices(retry=True, timeout_s=self._timeout)

    def list_devices_once(self, timeout_s: float = QUICK_LIST_TIMEOUT_S) -> list[AdbDevice]:
        """`list_devices` for a hint that a person waits for (the list of targets): one try, never retried, at most
        `timeout_s` (never more than the client's own timeout)."""
        return self._list_devices(retry=False, timeout_s=min(timeout_s, self._timeout))

    def _list_devices(self, *, retry: bool, timeout_s: float) -> list[AdbDevice]:
        if not self.server_running():
            raise AdbServerNotRunning("The ADB server is not running, so no devices can be listed.").saying(
                ru="Сервер ADB не запущен, поэтому список устройств получить нельзя.")
        result = self._run(["devices", "-l"], retry=retry, operation="ADB_DEVICES", timeout_s=timeout_s)
        if result.timed_out:
            raise AdbError(f"ADB did not answer within {timeout_s:g} s while listing devices.").saying(
                ru=f"ADB не ответил за {timeout_s:g} с, когда запрашивался список устройств.")
        if not result.ok:
            raise AdbError(f"Listing ADB devices failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Не удалось получить список устройств ADB: {result.error or result.stderr_text.strip()}")
        return parse_devices(result.stdout_text)

    def start_server(self) -> None:
        """Starts the ADB server. This changes system state, so it must be enabled explicitly."""
        if not self._allow_start:
            raise SafetyViolation("Starting the ADB server is not enabled for this client.")
        result = self._run(["start-server"], retry=False, operation="ADB_START_SERVER")
        if not result.ok:
            raise AdbError(f"Could not start the ADB server: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Не удалось запустить сервер ADB: {result.error or result.stderr_text.strip()}")

    def start_server_soon(self) -> bool:
        """Start the server on a thread of its own and return at once: where a person waits -- the window's thread,
        which also carries the emergency stop -- a start (about 2 s) is never waited for. True when a start was begun
        now, False when one is under way already. Gated like `start_server`; on its thread, a server that is running
        already or an emergency stop means no start, and a failure is kept for `take_start_failure` (and logged when
        this client has a logger), never raised."""
        if not self._allow_start:
            raise SafetyViolation("Starting the ADB server is not enabled for this client.")
        with self._starting_lock:
            if self._starting is not None and self._starting.is_alive():
                return False
            self._starting = threading.Thread(target=self._start_quietly, name="adb-server-start", daemon=True)
            self._starting.start()
            return True

    def _start_quietly(self) -> None:
        if self._stop.is_set() or self.server_running():
            return
        try:
            self.start_server()
        except EmergencyStopped:
            return                                # the stop is no failure to report
        except Exception as exc:                 # adb's own failure (a port taken, adb.exe missing): kept, not raised
            if self._stop.is_set():
                return                            # a start the stop cancelled is no failure either
            # kept as the error itself, so its words in other languages go with it (`said_in`)
            self._start_failure = exc if isinstance(exc, PlatformError) else AdbError(
                str(exc)[:300] or type(exc).__name__)
            if self._log:
                self._log.warning("ADB_START_SERVER", result="FAILED", error=f"{type(exc).__name__}: {exc}"[:200])
        else:
            self._start_failure = None

    def take_start_failure(self) -> PlatformError | None:
        """Why the last background start failed -- said once, then forgotten, so the next choice tries again -- or
        None (also while a start is under way). `str()` of it is its English words."""
        with self._starting_lock:
            if self._starting is not None and self._starting.is_alive():
                return None
            failure, self._start_failure = self._start_failure, None
            return failure

    # ---- screenshot (read-only; requires a CaptureGrant) ---------------------
    def screencap_png(self, serial: str, *, grant: CaptureGrant) -> bytes:
        """Return the raw PNG bytes of the device screen (`exec-out screencap -p`). Reads only."""
        if not isinstance(grant, CaptureGrant) or grant.serial != serial:
            raise SafetyViolation("screencap_png needs a CaptureGrant issued for this exact serial.")
        if not _SERIAL_RE.match(serial or ""):
            raise AdbError(f"'{serial}' is not a valid ADB serial.")
        self._stop.check()
        result = self._run(["-s", serial, "exec-out", "screencap", "-p"], serial=serial, operation="ADB_SCREENCAP")
        if result.timed_out:
            raise AdbError(f"Device {serial} did not deliver a screenshot within {self._timeout:g} s.").saying(
                ru=f"Устройство {serial} не прислало снимок экрана за {self._timeout:g} с.")
        if not result.ok:
            raise AdbError(f"Screenshot from {serial} failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Не удалось снять экран устройства {serial}: {result.error or result.stderr_text.strip()}")
        return result.stdout

    # ---- input (state-changing; requires an InputGrant) --------------------
    def input_tap(self, serial: str, x: int, y: int, *, grant: InputGrant) -> None:
        """Send exactly one `input tap x y`. Never retried (a repeated tap would be a second action)."""
        if not isinstance(grant, InputGrant) or grant.serial != serial:
            raise SafetyViolation("input_tap needs an InputGrant issued for this exact serial.")
        if not _SERIAL_RE.match(serial or ""):
            raise AdbError(f"'{serial}' is not a valid ADB serial.")
        for v in (x, y):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100000:
                raise AdbError("Tap coordinates must be integers between 0 and 100000.").saying(
                    ru="Координаты нажатия должны быть целыми числами от 0 до 100000.")
        self._stop.check()
        result = self._run(["-s", serial, "shell", "input", "tap", str(x), str(y)], retry=False,
                           serial=serial, operation="ADB_INPUT_TAP")
        if result.timed_out:
            raise AdbError(f"Device {serial} did not answer within {self._timeout:g} s; the tap may or may not have happened.").saying(
                ru=f"Устройство {serial} не ответило за {self._timeout:g} с; нажатие могло пройти, а могло и нет.")
        if not result.ok:
            raise AdbError(f"Tap on {serial} failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Нажатие на устройстве {serial} не удалось: {result.error or result.stderr_text.strip()}")

    def input_keyevent(self, serial: str, keycode: int, *, grant: InputGrant) -> None:
        """Send exactly one `input keyevent <code>` -- a complete Android key press (down immediately followed by
        up; adb has no portable way to split the two). `keycode` is a plain Android keycode int; this module has
        no notion of a neutral key (see app.ldplayer.keymap.android_keycode, one layer up, for that translation).
        Never retried."""
        if not isinstance(grant, InputGrant) or grant.serial != serial:
            raise SafetyViolation("input_keyevent needs an InputGrant issued for this exact serial.")
        if not _SERIAL_RE.match(serial or ""):
            raise AdbError(f"'{serial}' is not a valid ADB serial.")
        if isinstance(keycode, bool) or not isinstance(keycode, int) or not 0 <= keycode <= 999:
            raise AdbError("Android keycode must be an integer between 0 and 999.").saying(
                ru="Код клавиши Android должен быть целым числом от 0 до 999.")
        self._stop.check()
        result = self._run(["-s", serial, "shell", "input", "keyevent", str(keycode)], retry=False,
                           serial=serial, operation="ADB_INPUT_KEYEVENT")
        if result.timed_out:
            raise AdbError(f"Device {serial} did not answer within {self._timeout:g} s; "
                           "the key press may or may not have happened.").saying(
                ru=f"Устройство {serial} не ответило за {self._timeout:g} с; клавиша могла нажаться, а могла и нет.")
        if not result.ok:
            raise AdbError(f"Key event on {serial} failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Нажатие клавиши на устройстве {serial} не удалось: {result.error or result.stderr_text.strip()}")

    def input_text(self, serial: str, text: str, *, grant: InputGrant) -> None:
        """Send exactly one `input text '<text>'`. `text` must already satisfy SAFE_TEXT_PATTERN (checked here
        again, authoritatively): only ASCII letters, digits, space and '.', ',', '_', '-'. It is always wrapped in
        single quotes before being handed to adb.exe: `adb shell <args>` joins the remaining arguments with spaces
        and runs the result through the device's shell, so an un-quoted argument containing a space would arrive
        as two separate arguments, and any shell-special character would be interpreted by that remote shell, not
        by us. Single-quoting is safe here specifically because the allowlist forbids the one character (`'`) that
        could break out of it, so the text can never become additional shell syntax and never depends on any
        Android-version-specific '%s'-encodes-a-space convention. Never retried."""
        if not isinstance(grant, InputGrant) or grant.serial != serial:
            raise SafetyViolation("input_text needs an InputGrant issued for this exact serial.")
        if not _SERIAL_RE.match(serial or ""):
            raise AdbError(f"'{serial}' is not a valid ADB serial.")
        if not isinstance(text, str) or not SAFE_TEXT_PATTERN.match(text):
            raise AdbError("Text may contain only ASCII letters, digits, space, '.', ',', '_' and '-' "
                           "(1..256 characters); nothing sent.").saying(
                ru="Текст может содержать только латинские буквы, цифры, пробел и символы . , _ - "
                   "(от 1 до 256 символов); ничего не отправлено.")
        self._stop.check()
        result = self._run(["-s", serial, "shell", "input", "text", f"'{text}'"], retry=False,
                           serial=serial, operation="ADB_INPUT_TEXT")
        if result.timed_out:
            raise AdbError(f"Device {serial} did not answer within {self._timeout:g} s; "
                           "the text may or may not have been typed.").saying(
                ru=f"Устройство {serial} не ответило за {self._timeout:g} с; текст мог быть набран, а мог и нет.")
        if not result.ok:
            raise AdbError(f"Typing text on {serial} failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Набор текста на устройстве {serial} не удался: {result.error or result.stderr_text.strip()}")

    # ---- read-only identity queries ------------------------------------
    def shell_query(self, serial: str, *command: str) -> str:
        """Run one allowlisted READ-ONLY shell query on a device and return stdout (stripped)."""
        if not _SERIAL_RE.match(serial or ""):
            raise AdbError(f"'{serial}' is not a valid ADB serial.")
        if not is_read_only_query(tuple(command)):
            raise SafetyViolation(f"Shell command {command[:3]!r} is not on the read-only allowlist.")
        result = self._run(["-s", serial, "shell", *command], serial=serial, operation="ADB_SHELL_QUERY")
        if result.timed_out:
            raise AdbError(f"Device {serial} did not answer within {self._timeout:g} s.").saying(
                ru=f"Устройство {serial} не ответило за {self._timeout:g} с.")
        if not result.ok:
            raise AdbError(f"Query on device {serial} failed: {result.error or result.stderr_text.strip()}").saying(
                ru=f"Запрос к устройству {serial} не удался: {result.error or result.stderr_text.strip()}")
        return result.stdout_text.replace("\r\n", "\n").strip()

    def getprop(self, serial: str, name: str) -> str:
        if not re.match(r"^[A-Za-z0-9._\-]{1,80}$", name):
            raise AdbError(f"'{name}' is not a valid Android property name.")
        return self.shell_query(serial, "getprop", name)
