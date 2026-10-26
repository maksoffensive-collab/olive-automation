"""`AutomationServices` for the LDPlayer/ADB provider: an adapter over the existing `app.instances`/`app.adb`/
`app.ldplayer` objects. No new logic; mirrors `app.bootstrap.windows_services.WindowsServices`.

    discover        LDConsole.list_instances()  (a fresh InstanceRegistry + MappingService is built each call,
                    exactly as WindowsServices.discover() rebuilds its own ref table each call)
    verify/recheck  MappingService.verify / revalidate
    capture         LDPlayerScreenshotProvider (existing; not duplicated here)
    create_recorder always refused: LDPlayer/ADB has no user-input observation mechanism, so no LDPlayer target
                    ever declares Capability.RECORD (see app.ldplayer.ids.CAPABILITIES)
    create_playback create_ldplayer_playback (InputSession: serial-addressed tap only)

Identity stays explicit end to end, on purpose: a registered profile (InstanceProfile), the current LDPlayer
process/device state (LDInstance, from a fresh `list_instances()`), and a verified ADB serial (VerifiedTarget, only
from MappingService.verify()) are three different things and are never conflated. `discover()` may list an
instance that turns out not to be currently verifiable (not running, no ADB device, identity mismatch); it is
listed with its best-known status as a hint for the human, never as a claim that it is already verified. Only
`verify()` -- and, on every real action, `AdbSession`'s own freshness gate inside `InputSession` -- decides that.
"""
from __future__ import annotations

import contextlib
from typing import Callable

from app.adb.client import AdbClient
from app.core.cancel import EmergencyStop
from app.core.errors import CapabilityNotAvailable, SafetyViolation, TargetNotVerified
from app.gui.services import TargetEntry
from app.instances.mapping import MappingService, VerifiedTarget
from app.instances.profile import InstanceProfile, InstanceRegistry
from app.ldplayer.console import LDConsole
from app.ldplayer.ids import PROVIDER_ID
from app.ldplayer.playback import create_ldplayer_playback
from app.ldplayer.provider import LDPlayerScreenshotProvider
from app.logging.structured import Logger
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.providers.base import Screenshot
from app.providers.recording import Recorder
from app.providers.target import Capability, Target


def _entry(profile: InstanceProfile, running: bool) -> TargetEntry:
    status = "running" if running else "offline"
    # background=True: input and screenshots go to the device over its own connection, so the instance's window
    # does not have to be visible, in front, or even restored (verified end to end on a live instance).
    # concurrent=True: two instances are independent end to end -- each run builds its own InputSession over its
    # own AdbSession and addresses its own verified device serial, and nothing between them is serialised (the
    # client and the mapping service hold no mutable state). Declared separately from `background` on purpose:
    # working without a window in front says nothing on its own about two runs being able to overlap.
    return TargetEntry(profile.logical_id, profile.label(), "LDPlayer", "instance",
                       f"index {profile.ld_index}, {status}", PROVIDER_ID, "LDPlayer", background=True,
                       concurrent=True)


class LDPlayerServices:
    def __init__(self, console: LDConsole, adb: AdbClient, read_config: Callable, port_owners: Callable,
                stop: EmergencyStop | None = None, *, logger: Logger | None = None) -> None:
        self._console = console
        self._adb = adb
        self._read_config = read_config
        self._port_owners = port_owners
        self._stop = stop or EmergencyStop()
        self._log = logger
        self._registry = InstanceRegistry()
        self._mapping: MappingService | None = None

    @classmethod
    def create_default(cls, stop: EmergencyStop | None = None) -> "LDPlayerServices":
        from app.config.settings import load_settings
        from app.core.errors import ConfigError
        from app.core.net import listening_port_owners
        from app.core.process import CommandRunner
        from app.ldplayer.config_reader import read_instance_config

        settings = load_settings()
        if settings.ldplayer_dir is None:
            raise ConfigError("LDPlayer was not found (no folder with ldconsole.exe and adb.exe). "
                              "Set LDPLAYER_DIR or 'ldplayer_dir' in the configuration file.")
        stop = stop or EmergencyStop()
        runner = CommandRunner(stop)
        console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s)
        # The application may start LDPlayer's own ADB server (the person's decision of 2026-10-21: LDPlayer is
        # controlled over ADB, and without the server no instance can even be verified); the tools never do.
        adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                        retries=settings.retry_count, backoff_s=settings.retry_backoff_s, allow_server_start=True)
        ldplayer_dir = settings.ldplayer_dir
        return cls(console, adb, lambda index: read_instance_config(ldplayer_dir, index),
                   lambda: listening_port_owners(runner), stop)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def stop(self) -> EmergencyStop:
        return self._stop

    # ---- discovery / identity --------------------------------------------------------------------
    def discover(self) -> list[TargetEntry]:
        """Every registered instance, each labelled with its best-known running/offline status. Listing an
        instance here is NOT a claim that it can currently be verified -- only verify() decides that."""
        instances = self._console.list_instances()
        registry = InstanceRegistry.from_discovery(instances)
        self._registry = registry
        self._mapping = MappingService(registry, self._console, self._adb, self._stop, self._read_config,
                                       self._port_owners, self._log)
        running = {i.index for i in instances if i.pid != -1}
        if running and getattr(self._adb, "may_start_server", False):
            # so that the server is up by the time an instance is chosen; in the background, never waited for here
            self._adb.start_server_soon()
        return [_entry(p, p.ld_index in running) for p in registry.all()]

    def verify(self, entry: TargetEntry) -> Target:
        if self._mapping is None or self._registry.get(entry.ref) is None:
            raise TargetNotVerified("That instance is not part of the current list. Refresh and select it again.",
                                    reason_code="stale_entry")
        return self._mapping.verify(entry.ref)

    def _ld_target(self, target: object) -> VerifiedTarget:
        if not isinstance(target, VerifiedTarget):
            raise SafetyViolation("The LDPlayer services can only act on a verified LDPlayer target.")
        return target

    def recheck(self, target: Target) -> TargetEntry:
        fresh = self._require_mapping().revalidate(self._ld_target(target))
        return _entry(InstanceProfile(fresh.logical_id, fresh.ld_index, fresh.ld_name), running=True)

    def _require_mapping(self) -> MappingService:
        if self._mapping is None:
            raise TargetNotVerified("No instance has been discovered yet. Refresh first.", reason_code="stale_entry")
        return self._mapping

    # ---- screenshot / recorder / playback ---------------------------------------------------------
    def capture(self, target: Target) -> Screenshot:
        return LDPlayerScreenshotProvider(self._ld_target(target), self._adb, self._require_mapping(),
                                          self._stop).capture()

    def create_recorder(self, target: Target) -> Recorder:
        """LDPlayer/ADB has no user-input observation mechanism (unlike the Windows low-level hooks), so no
        LDPlayer target can ever be recorded: app.ldplayer.ids.CAPABILITIES never declares Capability.RECORD. The
        GUI's own capability gate (require_capability, called before create_recorder) already refuses this before
        reaching here; this raises the identical typed error directly, so a caller that reaches this method some
        other way -- bypassing the controller -- is refused exactly as cleanly, never with a bare
        NotImplementedError and never by silently returning None."""
        target = self._ld_target(target)
        raise CapabilityNotAvailable(f"Target {target.describe()} does not declare the 'record' capability.")

    def create_playback(self, macro: Macro, target: Target, *, dry_run: bool,
                        allow_different_target: bool) -> PlaybackEngine:
        return create_ldplayer_playback(macro, self._ld_target(target), self._adb, self._require_mapping(),
                                        self._stop, dry_run=dry_run, allow_different_target=allow_different_target)

    def guard_run(self, target: Target):
        """Nothing to watch: input goes to the device over its own connection, not to this computer's screen."""
        return contextlib.nullcontext()

    def bring_to_front(self, target: Target, *, activate: bool = False) -> bool:
        """Nothing to do: the device's screen is read and touched over its own connection, covered or not."""
        return False

    def screen_rect(self, target: Target):
        """None: the device's screen is not a place on this computer's screen (a picker shows its picture)."""
        return None

    def escape_watch(self, entries):
        """None of its own: this provider watches no keyboard (a combined application gets the Windows one)."""
        return None
