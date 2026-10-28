"""`AutomationServices` for the LDPlayer/ADB provider: an adapter over the existing `app.instances`/`app.adb`/
`app.ldplayer` objects. No new logic; mirrors `app.bootstrap.windows_services.WindowsServices`.

    discover        LDConsole.list_instances()  (a fresh InstanceRegistry + MappingService is built each call,
                    exactly as WindowsServices.discover() rebuilds its own ref table each call); when instances run,
                    one brief `adb devices` (and the port owners when two devices or more are listed) to list an
                    instance reached several ways once per way -- a hint, never evidence
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
import time
from typing import Callable

from app.adb.client import AdbClient, DeviceState
from app.core.cancel import EmergencyStop
from app.core.errors import (CapabilityNotAvailable, EmergencyStopped, PlatformError, SafetyViolation,
                             TargetNotVerified, said_in)
from app.core.procs import hold_processes
from app.gui.services import TargetEntry
from app.instances.mapping import MappingService, VerifiedTarget, ways_by_instance
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
from app.providers.remedy import Remedy
from app.providers.target import Capability, Target


def _entry(profile: InstanceProfile, running: bool, way: str | None = None) -> TargetEntry:
    """One row of the list: the instance, or -- when it can be reached several ways -- the instance over ONE of
    them (`way`, an ADB serial). Rows of one instance differ only in their ref and the way they name, so they look
    the same to a remembered target and the person chooses which (the person's rule of 2026-10-27)."""
    status = "running" if running else "offline"
    # background=True: input and screenshots go to the device over its own connection, so the instance's window
    # does not have to be visible, in front, or even restored (verified end to end on a live instance).
    # concurrent=True: two instances are independent end to end -- each run builds its own InputSession over its
    # own AdbSession and addresses its own verified device serial, and nothing between them is serialised (the
    # client and the mapping service hold no mutable state). Declared separately from `background` on purpose:
    # working without a window in front says nothing on its own about two runs being able to overlap.
    over = f", ADB {way}" if way else ""
    ru = f"№ {profile.ld_index}, {'запущен' if running else 'не запущен'}{over}"
    return TargetEntry(_ref(profile.logical_id, way), profile.label(), "LDPlayer", "instance",
                       f"index {profile.ld_index}, {status}{over}", PROVIDER_ID, "LDPlayer", background=True,
                       concurrent=True, size_in=(("ru", ru),))


def _ref(logical_id: str, way: str | None) -> str:
    return f"{logical_id}@{way}" if way else logical_id


# Turning an instance's ADB debugging on (the person's decision of 2026-10-27): every wait bounded by the clock (a
# check that itself takes long -- a hung adb -- counts too; at most one check runs past the bound).
CLOSE_WAIT_S = 60.0           # `ldconsole quit` to the instance listed as stopped AND its processes gone
START_WAIT_S = 180.0          # `ldconsole launch` to Android started (26 s seen for a new instance)
REACH_WAIT_S = 60.0           # Android started to the instance proven over ADB
POLL_S = 1.0
_NOT_YET = frozenset({"adb_starting", "adb_server", "no_adb_device", "no_verified_serial", "adb_error",
                      "ports_unreadable", "config_unreadable"})


def _so_far(done: list[str]) -> tuple[str, str]:
    """What turning debugging on had done when it stopped, in English and Russian."""
    if "started" in done:
        return ("ADB debugging was turned on and the instance was started again.",
                "Отладка ADB включена, экземпляр запущен снова.")
    if "set" in done and "launching" in done:
        return ("ADB debugging was turned on in its settings and the instance was asked to start again; if it does "
                "not open, start it in LDPlayer.", "Отладка ADB включена в его настройках, экземпляру дана команда "
                "запуститься; если он не откроется, запустите его в LDPlayer.")
    if "set" in done:
        return ("ADB debugging was turned on in its settings, but the instance was not started again: start it in "
                "LDPlayer.", "Отладка ADB включена в его настройках, но экземпляр не запущен снова: запустите его "
                "в LDPlayer.")
    again_en = ("started again." if "restarted" in done else
                "asked to start again (if it does not open, start it in LDPlayer)." if "launching" in done else
                "not started again.")
    again_ru = ("запущен снова." if "restarted" in done else
                "получил команду запуститься (если не откроется, запустите его в LDPlayer)." if "launching" in done
                else "не запущен снова.")
    if "written" in done:
        return ("The setting was written but did not read back as written (Olive kept the file as it was in its "
                "backup folder); the instance was " + again_en,
                "Настройка записана, но не читается так, как записана (файл в прежнем виде сохранён в папке "
                "резервных копий Olive); экземпляр " + again_ru)
    if "launching" in done:
        return "Its settings were not changed; the instance was " + again_en, \
               "Его настройки не менялись; экземпляр " + again_ru
    if "closed" in done:
        return ("The instance was closed; its settings were not changed.",
                "Экземпляр закрыт; его настройки не менялись.")
    if "closing" in done:
        return ("The instance was asked to close; its settings were not changed.",
                "Экземпляру дана команда закрыться; его настройки не менялись.")
    return "Nothing was changed.", "Ничего не изменено."


class LDPlayerServices:
    def __init__(self, console: LDConsole, adb: AdbClient, read_config: Callable, port_owners: Callable,
                stop: EmergencyStop | None = None, *, logger: Logger | None = None,
                enable_debugging: Callable[..., object] | None = None,
                hold_processes: Callable[..., object] = hold_processes,
                clock: Callable[[], float] = time.monotonic) -> None:
        """`enable_debugging(index, name, check_only=False)` turns an instance's ADB debugging on in its LDPlayer
        file (the instance closed), or with `check_only` only checks that it could, writing nothing; given, a
        refusal for debugging turned off carries a `Remedy` the person may say yes to."""
        self._console = console
        self._adb = adb
        self._read_config = read_config
        self._port_owners = port_owners
        self._stop = stop or EmergencyStop()
        self._log = logger
        self._enable_debugging = enable_debugging
        self._hold_processes = hold_processes
        self._clock = clock
        self._registry = InstanceRegistry()
        self._mapping: MappingService | None = None
        self._rows: dict[str, tuple[str, str | None]] = {}     # row ref -> (logical id, the way it names or None)

    @classmethod
    def create_default(cls, stop: EmergencyStop | None = None) -> "LDPlayerServices":
        from app.config.settings import load_settings
        from app.core.errors import ConfigError
        from app.core.net import listening_port_owners
        from app.core.process import CommandRunner
        from app.ldplayer.config_reader import read_instance_config
        from app.ldplayer.config_writer import enable_adb_debugging

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
        backups = settings.data_dir / "ldplayer-backups"        # each file as it was, before Olive changed it
        return cls(console, adb, lambda index: read_instance_config(ldplayer_dir, index),
                   lambda: listening_port_owners(runner), stop,
                   enable_debugging=lambda index, name, check_only=False: enable_adb_debugging(
                       ldplayer_dir, index, name, backups, check_only=check_only))

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
        running = {i.index for i in instances if i.pid != -1}
        if running and getattr(self._adb, "may_start_server", False):
            # so that the server is up by the time an instance is chosen; in the background, never waited for here
            self._adb.start_server_soon()
        ways = self._ways(instances, running)       # may meet the stop: nothing of this listing is kept then
        self._registry = registry
        self._mapping = MappingService(registry, self._console, self._adb, self._stop, self._read_config,
                                       self._port_owners, self._log)
        entries: list[TargetEntry] = []
        for profile in registry.all():
            several = ways.get(profile.ld_index, [])
            for way in (several if len(several) > 1 else [None]):
                entries.append(_entry(profile, profile.ld_index in running, way))
        self._rows = {}
        for entry in entries:
            logical_id, _, way = entry.ref.partition("@")
            self._rows[entry.ref] = (logical_id, way or None)
        return entries

    def _ways(self, instances, running: set[int]) -> dict[int, list[str]]:
        """Which ADB serials lead to which running instance, for the list only (an instance reached several ways is
        listed once per way). Asked only when an instance runs, the server is up and two devices or more are
        listed -- else nothing can have two ways; anything that fails here leaves one row per instance (choosing it
        then proves it, and refuses two ways it could not tell apart). The window waits for it, so adb is asked once,
        briefly (`list_devices_once`); the emergency stop is the stop here too, never "no ways"."""
        if not running:
            return {}
        self._stop.check()
        try:
            listing = getattr(self._adb, "list_devices_once", self._adb.list_devices)
            devices = [d.serial for d in listing() if d.state is DeviceState.DEVICE]
            if len(devices) < 2:
                return {}
            return ways_by_instance([i for i in instances if i.index in running], devices, self._port_owners())
        except EmergencyStopped:
            raise
        except PlatformError:
            self._stop.check()                      # a stop that ended adb or the port listing is the stop
            return {}

    def verify(self, entry: TargetEntry) -> Target:
        row = self._rows.get(entry.ref)
        if self._mapping is None or row is None or self._registry.get(row[0]) is None:
            raise TargetNotVerified("That instance is not part of the current list. Refresh and select it again.",
                                    reason_code="stale_entry").saying(
                ru="Этого экземпляра нет в текущем списке. Обновите список и выберите его снова.")
        try:
            return self._mapping.verify(row[0], serial=row[1])
        except TargetNotVerified as exc:
            if exc.reason_code == "adb_debug_off" and self._enable_debugging is not None:
                exc.remedy = self._debugging_remedy(self._registry.get(row[0]))
            raise

    # ---- turning ADB debugging on (the person's decision of 2026-10-27; only on their yes) ---------------------
    def _debugging_remedy(self, profile: InstanceProfile) -> Remedy:
        label = profile.label()
        return Remedy(
            title={"en": "Turn on ADB debugging?", "ru": "Включить отладку ADB?"},
            question={
                "en": f"{label} has ADB debugging turned off, so Olive cannot control it. Olive can turn it on: it "
                      f"closes {label}, sets “ADB debugging: Open local connection” in its LDPlayer settings and "
                      f"starts it again (about a minute; its window opens again). Whatever is open in {label} -- a "
                      "game, say -- is closed. Turn it on?",
                "ru": f"У экземпляра {label} выключена отладка ADB, поэтому Olive не может им управлять. Olive может "
                      f"включить её сам: закроет {label}, включит в его настройках LDPlayer «Отладка ADB: открыть "
                      "локальное подключение» и запустит его снова (около минуты; его окно откроется заново). Всё, "
                      f"что открыто в {label} (например, игра), закроется. Включить?"},
            run=lambda: self._turn_on_debugging(profile),
            doing={"en": f"Turning ADB debugging on for {label}: closing it, changing the setting, starting it again "
                         "(about a minute)…",
                   "ru": f"Включаю отладку ADB для {label}: закрываю, меняю настройку, запускаю снова (около "
                         "минуты)…"},
            done={"en": f"ADB debugging is on for {label}, and it was started again.",
                  "ru": f"Отладка ADB для {label} включена, экземпляр запущен снова."})

    def _turn_on_debugging(self, profile: InstanceProfile) -> None:
        """Close the instance (if it runs), turn the setting on in its file, start it, and wait until it is proven
        over ADB. Runs on a worker thread after the person's yes. Every wait is bounded and hears the emergency
        stop; a stop or a failure part-way says what was done by then."""
        label, done = profile.label(), []

        def stopped() -> EmergencyStopped:
            then_en, then_ru = _so_far(done)
            return EmergencyStopped(f"Emergency stop while turning ADB debugging on for {label}. {then_en}").saying(
                ru=f"Экстренная остановка во время включения отладки ADB для {label}. {then_ru}")

        def failed(en: str, ru: str) -> PlatformError:
            if self._stop.is_set():
                return stopped()                    # a stop met on the way is the stop, whatever else failed with it
            then_en, then_ru = _so_far(done)
            return TargetNotVerified(f"{en} {then_en}", reason_code="debugging_not_turned_on").saying(
                ru=f"{ru} {then_ru}")

        def instance():
            try:
                found = {i.index: i for i in self._console.list_instances()}.get(profile.ld_index)
            except EmergencyStopped:
                raise stopped() from None
            except PlatformError as exc:
                if self._stop.is_set():
                    raise stopped() from None
                raise failed(f"LDPlayer could not be asked about {label}: {exc}",
                             f"Не удалось спросить LDPlayer о {label}: {said_in(exc, 'ru')}") from None
            if found is None or found.name != profile.ld_name:
                raise failed(f"LDPlayer no longer lists {label} under number {profile.ld_index}.",
                             f"LDPlayer больше не показывает {label} под номером {profile.ld_index}.")
            return found

        def wait_until(holds: Callable[[], bool], limit_s: float, words: Callable[[], tuple[str, str]]) -> None:
            """Until `holds()`, at most `limit_s` by the clock -- the time the checks take counts too; `words()`: why
            it did not, in English and Russian."""
            deadline = self._clock() + limit_s
            while not holds():
                left = deadline - self._clock()
                if left <= 0:
                    raise failed(*words())
                if self._stop.wait(min(POLL_S, left)):
                    raise stopped()

        def console(step: Callable[[], None]) -> None:
            try:
                step()
            except EmergencyStopped:
                raise stopped() from None
            except PlatformError as exc:
                raise failed(f"LDPlayer refused: {exc}", f"LDPlayer отказал: {said_in(exc, 'ru')}") from None

        def the_file(check_only: bool) -> BaseException | None:
            """The file changed (or, `check_only`, only checked): None, else what stood in the way."""
            try:
                self._enable_debugging(profile.ld_index, profile.ld_name, check_only=check_only)
            except EmergencyStopped:
                raise stopped() from None
            except Exception as exc:                # whatever it was: the instance is not left closed for it
                return exc
            return None

        if self._stop.is_set():
            raise stopped()
        current = instance()
        problem = the_file(check_only=True)         # before anything is closed: a file Olive cannot change is said now
        if problem is not None:
            raise failed(str(problem), said_in(problem, "ru"))
        if current.pid != -1:
            # LDPlayer writes the file itself while an instance closes -- before or after its list says stopped,
            # while its processes still run ([VERIFIED] 2026-10-27): the file is touched only once the instance's
            # own processes are gone, held from before `quit` (an id given to another program cannot look like them).
            held = self._hold_processes(tuple(p for p in (current.pid, current.vbox_pid) if p and p > 0))
            listed = [False]

            def closed() -> bool:
                listed[0] = instance().pid == -1
                return listed[0] and not held.running()
            try:
                done.append("closing")              # said even if the call fails: it may have reached LDPlayer
                console(lambda: self._console.quit_instance(profile.ld_index))
                wait_until(closed, CLOSE_WAIT_S, lambda: (
                    (f"{label} is listed as stopped, but its processes did not end within {CLOSE_WAIT_S:g} s.",
                     f"{label} указан как остановленный, но его процессы не завершились за {CLOSE_WAIT_S:g} с.")
                    if listed[0] else (f"{label} did not close within {CLOSE_WAIT_S:g} s.",
                                       f"{label} не закрылся за {CLOSE_WAIT_S:g} с.")))
            finally:
                held.close()
            done.append("closed")
        if self._stop.is_set():
            raise stopped()
        problem = the_file(check_only=False)
        if problem is not None:
            if getattr(problem, "written", False):
                done.append("written")
            if "closed" in done:                    # not left closed for nothing: started again, as it was
                done.append("launching")
                console(lambda: self._console.launch_instance(profile.ld_index))
                done.append("restarted")
            raise failed(str(problem), said_in(problem, "ru"))
        done.append("set")
        done.append("launching")                    # said even if the call fails: it may have reached LDPlayer
        console(lambda: self._console.launch_instance(profile.ld_index))
        wait_until(lambda: instance().android_started == 1, START_WAIT_S,
                   lambda: (f"{label} did not start within {START_WAIT_S:g} s.",
                            f"{label} не запустился за {START_WAIT_S:g} с."))
        done.append("started")

        def reached() -> bool:
            try:
                self._require_mapping().verify(profile.logical_id)
                return True
            except EmergencyStopped:
                raise stopped() from None
            except TargetNotVerified as exc:
                if exc.reason_code == "ambiguous":
                    return True                     # reached, several ways: the person chooses which in the list
                if exc.reason_code == "adb_debug_off":
                    raise failed(f"LDPlayer did not keep the setting: {label} still has ADB debugging off.",
                                 f"LDPlayer не сохранил настройку: у {label} отладка ADB по-прежнему выключена."
                                 ) from None
                if exc.reason_code in _NOT_YET:
                    return False
                raise failed(str(exc), said_in(exc, "ru")) from None
        wait_until(reached, REACH_WAIT_S, lambda: (f"{label} could not be reached over ADB within {REACH_WAIT_S:g} s.",
                                                   f"До {label} не удалось достучаться по ADB за {REACH_WAIT_S:g} с."))

    def _ld_target(self, target: object) -> VerifiedTarget:
        if not isinstance(target, VerifiedTarget):
            raise SafetyViolation("The LDPlayer services can only act on a verified LDPlayer target.")
        return target

    def recheck(self, target: Target) -> TargetEntry:
        """Prove it again over the same way, and give back the row of the current list it is: the row of its way
        when the instance is listed once per way, else the instance's one row."""
        fresh = self._require_mapping().revalidate(self._ld_target(target))
        way = fresh.adb_serial if _ref(fresh.logical_id, fresh.adb_serial) in self._rows else None
        return _entry(InstanceProfile(fresh.logical_id, fresh.ld_index, fresh.ld_name), True, way)

    def _require_mapping(self) -> MappingService:
        if self._mapping is None:
            raise TargetNotVerified("No instance has been discovered yet. Refresh first.", reason_code="stale_entry"
                                    ).saying(ru="Экземпляры ещё не найдены. Сначала обновите список.")
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
