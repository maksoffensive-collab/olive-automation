"""READ-ONLY identity-mapping test for ONE manually started LDPlayer instance.

Usage (from the project root), after the user has started the instance by hand:
    .venv\\Scripts\\python.exe tools\\verify_mapping.py --index 9

It never starts, stops or controls anything. It runs: `ldconsole list2` / `runninglist`,
`adb devices -l` (server is not started), allowlisted read-only shell queries
(Android ID, wm size/density, package list), and `netstat -ano -p tcp`. Android IDs and other
identifiers are compared internally and NEVER printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient, DeviceState  # noqa: E402
from tool_config import load_ldplayer_settings  # noqa: E402
from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.errors import PlatformError, TargetNotVerified  # noqa: E402
from app.core.net import listening_port_owners  # noqa: E402
from app.core.process import CommandRunner  # noqa: E402
from app.instances.mapping import MappingService, adb_serial_ports  # noqa: E402
from app.instances.profile import InstanceRegistry  # noqa: E402
from app.ldplayer.config_reader import read_instance_config  # noqa: E402
from app.ldplayer.console import LDConsole  # noqa: E402
from app.logging.structured import JsonlFileSink, LogHub  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=int, required=True, help="LDPlayer index of the instance you started")
    args = ap.parse_args()

    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "verify_mapping.jsonl")], "DEBUG")
    stop = EmergencyStop()
    runner = CommandRunner(stop)
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        instances = console.list_instances()
        running = console.running_names()
    except PlatformError as exc:
        print(f"LDPlayer query failed: {exc}")
        return 1
    print(f"Running instances (ldconsole runninglist): {running or 'none'}")
    if len(running) != 1:
        print("EXPECTED exactly one running instance for this test. Stopping without doing anything.")
        return 2
    live = {i.index: i for i in instances}
    inst = live.get(args.index)
    if inst is None or inst.name not in running:
        print(f"Instance {args.index} is not the running instance. Stopping without doing anything.")
        return 2

    print(f"\nldconsole list2 line for index {inst.index}: {inst.raw}")
    print(f"  name={inst.name} pid={inst.pid} vbox_pid={inst.vbox_pid} top_hwnd={inst.top_hwnd} "
          f"bind_hwnd={inst.bind_hwnd} android_started={inst.android_started} size={inst.width}x{inst.height} dpi={inst.dpi}")

    snap = adb.snapshot()
    print(f"\nADB: {snap.status.value}; devices: {[(d.serial, d.state.value) for d in snap.devices]}")
    owners = listening_port_owners(runner)
    for d in snap.devices:
        ports = adb_serial_ports(d.serial)
        print(f"  {d.serial}: ports {ports} -> owner pids {[sorted(owners.get(p, [])) for p in ports]}")
        if d.state is DeviceState.DEVICE:
            for label, cmd in (("wm size", ("wm", "size")), ("wm density", ("wm", "density"))):
                try:
                    print(f"    {label}: {adb.shell_query(d.serial, *cmd)!r}")
                except PlatformError as exc:
                    print(f"    {label}: unavailable ({exc})")
            try:
                pkgs = [p for p in adb.shell_query(d.serial, "pm", "list", "packages").splitlines() if "viking" in p.lower()]
                print(f"    packages containing 'viking': {pkgs or 'none'}")
            except PlatformError as exc:
                print(f"    package list unavailable ({exc})")

    registry = InstanceRegistry.from_discovery(instances)
    svc = MappingService(registry, console, adb, stop, lambda i: read_instance_config(settings.ldplayer_dir, i),
                         lambda: listening_port_owners(runner), hub.logger("instances"))
    logical_id = f"ld{inst.index}"
    print(f"\n== Evidence for {logical_id} (identifier values are never printed)")
    try:
        _, per_device = svc.probe(logical_id)
        for serial, evidence in per_device:
            print(f"  candidate {serial}")
            for e in evidence:
                print(f"    - {e.kind:<16} {e.verdict.value:<11} {e.detail}")
    except TargetNotVerified as exc:
        print(f"  probe refused [{exc.reason_code}]: {exc}")
    print("\n== Verification decision")
    try:
        target = svc.verify(logical_id)
        print("VERIFIED:", target.describe())
        return 0
    except TargetNotVerified as exc:
        print(f"NOT VERIFIED [{exc.reason_code}]: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
