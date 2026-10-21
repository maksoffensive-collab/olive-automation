r"""Controlled single-tap test against ONE verified LDPlayer instance.

Default is a DRY RUN: it verifies the target, reads the display and checks the coordinates,
and sends nothing. Add --send to send exactly one tap (session budget: max_actions=1).

    .venv\Scripts\python.exe tools\input_tap_test.py --index 9 --x 1240 --y 360
    .venv\Scripts\python.exe tools\input_tap_test.py --index 9 --x 1240 --y 360 --send
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient  # noqa: E402
from app.adb.input import InputSession  # noqa: E402
from app.adb.session import AdbSession  # noqa: E402
from tool_config import load_ldplayer_settings  # noqa: E402
from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.errors import PlatformError  # noqa: E402
from app.core.net import listening_port_owners  # noqa: E402
from app.core.process import CommandRunner  # noqa: E402
from app.instances.mapping import MappingService  # noqa: E402
from app.instances.profile import InstanceRegistry  # noqa: E402
from app.ldplayer.config_reader import read_instance_config  # noqa: E402
from app.ldplayer.console import LDConsole  # noqa: E402
from app.logging.structured import JsonlFileSink, LogHub  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--x", type=int, required=True)
    ap.add_argument("--y", type=int, required=True)
    ap.add_argument("--send", action="store_true", help="really send the single tap")
    args = ap.parse_args()
    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "input_tap_test.jsonl")], "DEBUG")
    stop = EmergencyStop()
    runner = CommandRunner(stop)
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        running = console.running_names()
        if len(running) != 1:
            print(f"Expected exactly one running instance, found {running}. Nothing sent.")
            return 2
        registry = InstanceRegistry.from_discovery(console.list_instances())
        svc = MappingService(registry, console, adb, stop, lambda i: read_instance_config(settings.ldplayer_dir, i),
                             lambda: listening_port_owners(runner), hub.logger("instances"))
        target = svc.verify(f"ld{args.index}")
        print("VERIFIED:", target.describe())
        d = AdbSession(target, adb, svc, stop).display()
        inside = 0 <= args.x < d.width and 0 <= args.y < d.height
        print(f"display: {d.width}x{d.height}; intended tap: ({args.x}, {args.y}); inside bounds: {inside}")
        if not inside:
            print("Outside the display. Nothing sent.")
            return 2
        if not args.send:
            print("DRY RUN: nothing sent.")
            return 0
        session = InputSession(target, adb, svc, stop, max_actions=1)
        session.tap(args.x, args.y)
        print(f"TAP SENT via InputSession to {target.ld_name} ({target.adb_serial}); "
              f"actions sent: {session.actions_sent}")
        return 0
    except PlatformError as exc:
        print(f"REFUSED/FAILED: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
