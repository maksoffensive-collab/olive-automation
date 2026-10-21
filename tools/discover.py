"""Read-only discovery report: LDPlayer instances and ADB devices.

Usage (from the project root):
    .venv\\Scripts\\python.exe tools\\discover.py

Runs only `ldconsole list2` / `runninglist`, `adb version` and `adb devices -l` (the ADB
server is never started), and reads the per-instance config files. Prints identifiers only
as counts or kinds; IMEI / Android ID / MAC values are never printed. It does not map
instances to ADB serials: that needs the verified mapping described in docs/ADB.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient, AdbStatus  # noqa: E402
from tool_config import load_ldplayer_settings  # noqa: E402
from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.errors import PlatformError  # noqa: E402
from app.core.process import CommandRunner  # noqa: E402
from app.ldplayer.config_reader import find_identity_conflicts, read_instance_config  # noqa: E402
from app.ldplayer.console import LDConsole  # noqa: E402
from app.logging.structured import JsonlFileSink, LogHub  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "discover.jsonl")], settings.log_level)
    stop = EmergencyStop()
    runner = CommandRunner(stop)

    print("== Environment")
    print(f"LDPlayer folder : {settings.ldplayer_dir}")

    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        instances = console.list_instances()
        running = console.running_names()
    except PlatformError as exc:
        print(f"Could not list LDPlayer instances: {exc}")
        return 1
    snap = adb.snapshot()
    print(f"ADB             : {settings.adb_path}  version={snap.version}")
    print(f"ADB status      : {snap.status.value}")
    print(f"Running (ldconsole runninglist): {running or 'none'}")

    configs = []
    config_errors = {}
    for inst in instances:
        try:
            configs.append(read_instance_config(settings.ldplayer_dir, inst.index))
        except PlatformError as exc:
            config_errors[inst.index] = str(exc)
    by_index = {c.index: c for c in configs}

    print(f"\n== LDPlayer instances ({len(instances)})")
    print(f"{'idx':>3}  {'name':<14} {'status':<8} {'size':<10} {'dpi':>4}  {'config file':<12} {'ADB serial':<22} Viking Rise")
    for inst in instances:
        status = "running" if inst.name in running else "stopped"
        cfg = by_index.get(inst.index)
        cfg_txt = f"leidian{inst.index}" if cfg else "MISSING"
        print(f"{inst.index:>3}  {inst.name:<14} {status:<8} {inst.width}x{inst.height:<5} {inst.dpi:>4}  "
              f"{cfg_txt:<12} {'unverified':<22} {'not detectable (stopped)' if status == 'stopped' else 'not checked'}")
    for idx, err in config_errors.items():
        print(f"  ! instance {idx}: {err}")

    print("\n== Consistency checks (identifier values are never printed)")
    mismatched = [c.index for c in configs
                  if next(i for i in instances if i.index == c.index).width != c.width
                  or next(i for i in instances if i.index == c.index).height != c.height]
    print(f"list2 size vs config size mismatches: {mismatched or 'none'}")
    conflicts = find_identity_conflicts(configs)
    print(f"identifier kinds shared between instances: {conflicts or 'none (each identifier is unique)'}")
    missing_id = [c.index for c in configs if not c.identity.values()]
    print(f"instances without any stored identifier: {missing_id or 'none'}")

    print("\n== ADB devices")
    if snap.status in (AdbStatus.NOT_FOUND, AdbStatus.SERVER_NOT_RUNNING):
        print(snap.status.value)
    elif not snap.devices:
        print("none attached")
    for dev in snap.devices:
        print(f"{dev.serial}  state={dev.state.value}  props={dev.props}")
    print("\nMapping instance <-> ADB serial: NOT PERFORMED (no verified evidence yet).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
