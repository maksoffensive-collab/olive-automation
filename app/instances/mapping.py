"""Verified identity mapping.

    logical id <-> LDPlayer index + name <-> process/window <-> ADB serial

An action may only target a `VerifiedTarget`. One can only be produced by
`MappingService.verify`, which requires TWO independent pieces of evidence and refuses on
any contradiction, ambiguity or missing evidence (STOP, never guess):

  1. config identity : the identity LDPlayer injects into the device (`getprop phone.androidid`,
                       `phone.imei`) equals the one stored in that instance's own LDPlayer config file;
  2. process owner   : the TCP port behind the ADB serial is owned by that instance's
                       LDPlayer / VirtualBox process ids from `ldconsole list2`.

STATUS: logic tested with fakes only. Whether these two evidence sources actually hold on
real LDPlayer 14 is [NEEDS TESTING] and is decided by the manual one-instance test.
Serial numbers and ADB ordering are never used as evidence.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from app.adb.client import AdbClient, AdbError, AdbServerNotRunning, DeviceState
from app.core.cancel import EmergencyStop
from app.core.errors import PlatformError, TargetNotVerified
from app.instances.profile import InstanceProfile, InstanceRegistry
from app.ldplayer.config_reader import InstanceConfig, find_identity_conflicts
from app.ldplayer.console import LDConsole, LDConsoleError, LDInstance
from app.ldplayer.ids import CAPABILITIES, PROVIDER_ID
from app.logging.structured import Logger
from app.providers.base import TargetIdentity
from app.providers.target import Capability, Target, TargetVerification, VerificationState

_MINT = object()


def _debug_off(label: str) -> str:
    return (f"{label} has ADB debugging turned off in its LDPlayer settings, so nothing can control it over ADB. In "
            "LDPlayer open Settings > Other settings > ADB debugging, choose \"Open local connection\", save, and "
            "restart the instance.")


class Verdict(Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Evidence:
    kind: str
    verdict: Verdict
    detail: str  # never contains device identifiers


@dataclass(frozen=True)
class VerifiedTarget(Target):
    """Proof that `adb_serial` belongs to LDPlayer instance `ld_index`/`ld_name`. Not forgeable by accident.

    Also the LDPlayer implementation of the provider-neutral `Target` contract: generic code sees only
    `identity` (provider id + logical id + name), the explicitly declared `capabilities` and the `verification`
    provenance, never the serial, pids or window handle."""
    logical_id: str
    ld_index: int
    ld_name: str
    adb_serial: str
    pid: int
    vbox_pid: int
    top_hwnd: int
    evidence: tuple[Evidence, ...]
    verified_at: float
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TargetNotVerified("A VerifiedTarget can only be created by MappingService.verify().",
                                    reason_code="forged")

    @property
    def identity(self) -> TargetIdentity:
        return TargetIdentity(PROVIDER_ID, self.logical_id, self.ld_name)

    @property
    def capabilities(self) -> frozenset[Capability]:
        return CAPABILITIES

    @property
    def verification(self) -> TargetVerification:
        # Only MappingService.verify() can mint a VerifiedTarget, and it does so only when every check matched.
        return TargetVerification(VerificationState.VERIFIED, tuple(e.kind for e in self.evidence), self.verified_at)

    def describe(self) -> str:
        return (f"Target: {self.ld_name} | LDPlayer index {self.ld_index} | ADB {self.adb_serial} | "
                f"status running (pid {self.pid})")

    def age_s(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.verified_at


def adb_serial_ports(serial: str) -> list[int]:
    """Ports that can belong to a serial: emulator-N -> N and N+1 (console, adb); host:N -> N."""
    m = re.fullmatch(r"emulator-(\d+)", serial)
    if m:
        return [int(m.group(1)), int(m.group(1)) + 1]
    m = re.fullmatch(r"[\w.\-]+:(\d+)", serial)
    if m:
        return [int(m.group(1))]
    return []


class MappingService:
    def __init__(self, registry: InstanceRegistry, console: LDConsole, adb: AdbClient, stop: EmergencyStop,
                 read_config: Callable[[int], InstanceConfig], port_owners: Callable[[], dict[int, set[int]]],
                 logger: Logger | None = None) -> None:
        self._registry = registry
        self._console = console
        self._adb = adb
        self._stop = stop
        self._read_config = read_config
        self._port_owners = port_owners
        self._log = logger

    # ---- evidence ------------------------------------------------------
    def _read_prop(self, serial: str, name: str) -> str | None:
        value = self._adb.getprop(serial, name).strip()
        return None if not value or value.lower() == "null" else value

    def _config_identity(self, serial: str, cfg: InstanceConfig) -> Evidence:
        """Compare the identity LDPlayer injects into the device (`getprop phone.androidid`, verified on
        LDPlayer 14 to equal the config's phoneAndroidId) with the instance's own config. `settings get
        secure android_id` is NOT used: on a real instance it differs from the config value."""
        expected = cfg.identity.android_id
        if not expected:
            return Evidence("config_identity", Verdict.UNAVAILABLE, "the instance config stores no Android ID")
        try:
            actual = self._read_prop(serial, "phone.androidid")
            actual_imei = self._read_prop(serial, "phone.imei") if cfg.identity.imei else None
        except (AdbError, PlatformError) as exc:
            return Evidence("config_identity", Verdict.UNAVAILABLE, f"could not read device identity: {type(exc).__name__}")
        if actual is None:
            return Evidence("config_identity", Verdict.UNAVAILABLE, "device reported no injected Android ID")
        if actual.lower() != expected.strip().lower():
            return Evidence("config_identity", Verdict.MISMATCH, "device Android ID differs from the instance config")
        if actual_imei is not None and actual_imei.lower() != cfg.identity.imei.strip().lower():
            return Evidence("config_identity", Verdict.MISMATCH, "device IMEI differs from the instance config")
        return Evidence("config_identity", Verdict.MATCH, "device identity (Android ID, IMEI if present) equals the instance config")

    def _process_owner(self, serial: str, inst: LDInstance, owners: dict[int, set[int]]) -> Evidence:
        ports = adb_serial_ports(serial)
        if not ports:
            return Evidence("process_owner", Verdict.UNAVAILABLE, "serial does not encode a port")
        valid = {p for p in (inst.pid, inst.vbox_pid) if p and p > 0}
        if not valid:
            return Evidence("process_owner", Verdict.UNAVAILABLE, "ldconsole reported no process ids for the instance")
        seen = set().union(*(owners.get(p, set()) for p in ports))
        if not seen:
            return Evidence("process_owner", Verdict.UNAVAILABLE, "no process is listening on the serial's ports")
        if seen & valid:
            return Evidence("process_owner", Verdict.MATCH, "serial's port is owned by the instance's process")
        return Evidence("process_owner", Verdict.MISMATCH, "serial's port is owned by a different process")

    # ---- verification -----------------------------------------------------
    def _fail(self, profile: InstanceProfile, code: str, message: str) -> TargetNotVerified:
        if self._log:
            self._log.bind(instance=profile.logical_id).warning("VERIFY_TARGET", result="REFUSED", error=message,
                                                                 reason_code=code)
        return TargetNotVerified(message, reason_code=code)

    def _console_call(self, profile: InstanceProfile, query: Callable):
        """Run an ldconsole query; a console failure is a typed refusal (never an untyped error, never a guess)."""
        try:
            return query()
        except LDConsoleError as exc:
            self._stop.check()                      # a stop that killed the command is a stop, not a refusal
            raise self._fail(profile, "ldconsole_error", f"ldconsole could not be queried: {exc}") from None

    def _devices(self, profile: InstanceProfile) -> list:
        """The connected, authorised ADB devices. A server that is not running is started in the background when
        this client may start it (the application's may) -- never waited for here: verifying runs on the window's
        thread, which also carries the emergency stop -- and the person is asked to choose again in a moment; a start
        that failed is said, with its reason, at the next choice. An ADB command ended by the emergency stop is the
        stop, never a refusal."""
        try:
            listed = self._adb.list_devices()
        except AdbServerNotRunning:
            self._stop.check()                      # a stop at this moment is the stop, never a refusal
            if getattr(self._adb, "may_start_server", False):
                failure = getattr(self._adb, "take_start_failure", lambda: None)()
                if failure is not None:             # said once; the next choice starts it again
                    raise self._fail(profile, "adb_server", f"{failure} Choose the instance again to try once "
                                                            "more.") from None
                self._adb.start_server_soon()
                raise self._fail(profile, "adb_starting",
                                 "The ADB server was not running; it is being started now. Choose the instance again "
                                 "in a few seconds.") from None
            raise self._fail(profile, "adb_server",
                             "The ADB server is not running, so the instance cannot be verified.") from None
        except AdbError as exc:
            self._stop.check()                      # a stop that killed the command is a stop, not a refusal
            raise self._fail(profile, "adb_error", f"ADB could not be queried: {exc}") from None
        return [d for d in listed if d.state is DeviceState.DEVICE]

    def _adb_debug_off(self, index: int) -> bool:
        """Whether the instance's own settings say its ADB debugging is off (unknown or unreadable: not said)."""
        try:
            return self._read_config(index).adb_debug == 0
        except PlatformError:
            return False

    def probe(self, logical_id: str) -> tuple[LDInstance, list[tuple[str, tuple[Evidence, ...]]]]:
        """Collect evidence for every connected ADB device WITHOUT deciding. For diagnostics.

        Applies the same preconditions as verify() (name match, running, ADB reachable, unique config
        identity) and raises TargetNotVerified if one fails.
        """
        _, inst, evidence_by_device, _ = self._gather(logical_id)
        return inst, evidence_by_device

    def verify(self, logical_id: str) -> VerifiedTarget:
        profile, inst, per_device, devices = self._gather(logical_id)
        label = profile.label()
        matches = [(serial, evidence) for serial, evidence in per_device
                   if all(e.verdict is Verdict.MATCH for e in evidence)]
        if not matches and self._adb_debug_off(inst.index):
            raise self._fail(profile, "adb_debug_off", _debug_off(label))
        if not matches:
            raise self._fail(profile, "no_verified_serial",
                             f"None of the {len(devices)} connected ADB device(s) could be proven to be {label}. "
                             "Not sending anything.")
        if len(matches) > 1:
            raise self._fail(profile, "ambiguous", f"{len(matches)} ADB devices all claim to be {label}. Refusing to pick one.")

        serial, evidence = matches[0]
        target = VerifiedTarget(profile.logical_id, inst.index, inst.name, serial, inst.pid, inst.vbox_pid,
                                inst.top_hwnd, evidence, time.monotonic(), _mint=_MINT)
        if self._log:
            self._log.bind(instance=profile.logical_id, target=serial).info(
                "VERIFY_TARGET", result="VERIFIED", message=target.describe(),
                evidence=[e.kind for e in evidence])
        return target

    def _gather(self, logical_id: str):
        self._stop.check()
        profile = self._registry.get(logical_id)
        if profile is None:
            raise TargetNotVerified(f"'{logical_id}' is not a known instance.", reason_code="unknown_instance")
        label = profile.label()

        live = {i.index: i for i in self._console_call(profile, self._console.list_instances)}
        inst = live.get(profile.ld_index)
        if inst is None:
            raise self._fail(profile, "not_in_ldplayer", f"LDPlayer has no instance with index {profile.ld_index} ({label}).")
        if inst.name != profile.ld_name:
            raise self._fail(profile, "name_mismatch",
                             f"LDPlayer instance {profile.ld_index} is now named '{inst.name}', expected '{profile.ld_name}'. "
                             "Refusing to guess which instance is meant.")
        if inst.name not in self._console_call(profile, self._console.running_names):
            raise self._fail(profile, "not_running", f"{label} (LDPlayer instance {inst.index}) is not running. Start it first.")

        self._stop.check()
        devices = self._devices(profile)
        if not devices:
            if self._adb_debug_off(inst.index):
                raise self._fail(profile, "adb_debug_off", _debug_off(label))
            raise self._fail(profile, "no_adb_device", f"{label} is running but no ADB device is connected and authorised.")

        try:
            cfg = self._read_config(inst.index)
            configs = [self._read_config(i.index) for i in live.values()]
        except PlatformError as exc:
            raise self._fail(profile, "config_unreadable", f"Instance configuration could not be read: {exc}") from None
        own = cfg.identity.android_id
        if own and inst.index in find_identity_conflicts(configs).get("android_id", []):
            raise self._fail(profile, "identity_not_unique",
                             f"{label}'s Android ID is shared with another instance, so it cannot identify it.")
        try:
            owners = self._port_owners()
        except PlatformError as exc:
            raise self._fail(profile, "ports_unreadable", str(exc)) from None

        per_device: list[tuple[str, tuple[Evidence, ...]]] = []
        for dev in devices:
            self._stop.check()
            per_device.append((dev.serial, (self._config_identity(dev.serial, cfg),
                                            self._process_owner(dev.serial, inst, owners))))
        return profile, inst, per_device, devices

    def verify_many(self, logical_ids: list[str]) -> list[VerifiedTarget]:
        """Verify several instances; refuses if two of them resolve to the same ADB serial."""
        targets = [self.verify(i) for i in logical_ids]
        serials = [t.adb_serial for t in targets]
        if len(set(serials)) != len(serials):
            raise TargetNotVerified("Two instances resolved to the same ADB serial; refusing to continue.",
                                    reason_code="serial_collision")
        return targets

    def revalidate(self, target: VerifiedTarget) -> VerifiedTarget:
        """Prove the target again (full check). Use after restarts or before long operations."""
        return self.verify(target.logical_id)
