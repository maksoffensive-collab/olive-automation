r"""Read-only check of the ADB session layer against ONE running LDPlayer instance.

The target is resolved through the verified mapping (logical id 'ld<index>'), never from a
hard-coded serial. Sends no input, launches nothing, changes nothing.

    .venv\\Scripts\\python.exe tools\\adb_session_check.py --index 9
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient  # noqa: E402
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
    args = ap.parse_args()
    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "adb_session_check.jsonl")], "DEBUG")
    stop = EmergencyStop()
    runner = CommandRunner(stop)
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        registry = InstanceRegistry.from_discovery(console.list_instances())
        svc = MappingService(registry, console, adb, stop, lambda i: read_instance_config(settings.ldplayer_dir, i),
                             lambda: listening_port_owners(runner), hub.logger("instances"))
        target = svc.verify(f"ld{args.index}")
        print("VERIFIED:", target.describe())
        session = AdbSession(target, adb, svc, stop)
        print("device state     :", session.state().value)
        print("android version  :", session.android_version())
        d = session.display()
        print(f"display          : {d.width}x{d.height} @ {d.dpi} dpi")
        print("manufacturer     :", session.getprop("ro.product.manufacturer"))
        print("settings package (com.android.settings) installed:", session.is_package_installed("com.android.settings"))
        print("re-verified serial unchanged:", svc.revalidate(target).adb_serial == target.adb_serial)
        return 0
    except PlatformError as exc:
        print(f"REFUSED/FAILED: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
