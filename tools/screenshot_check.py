r"""Read-only screenshot of ONE running, verified LDPlayer instance, saved under data/screenshots.

The target comes from the verified mapping ('ld<index>'), never from a serial. Sends no input.

    .venv\Scripts\python.exe tools\screenshot_check.py --index 9 --width 1280 --height 720
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient  # noqa: E402
from app.adb.png import png_size  # noqa: E402
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
    ap.add_argument("--width", type=int, required=True, help="expected width")
    ap.add_argument("--height", type=int, required=True, help="expected height")
    args = ap.parse_args()
    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "screenshot_check.jsonl")], "DEBUG")
    stop = EmergencyStop()
    runner = CommandRunner(stop)
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        running = console.running_names()
        if len(running) != 1:
            print(f"Expected exactly one running instance, found {running}. Nothing captured.")
            return 2
        registry = InstanceRegistry.from_discovery(console.list_instances())
        svc = MappingService(registry, console, adb, stop, lambda i: read_instance_config(settings.ldplayer_dir, i),
                             lambda: listening_port_owners(runner), hub.logger("instances"))
        target = svc.verify(f"ld{args.index}")
        print("VERIFIED:", target.describe())
        path = AdbSession(target, adb, svc, stop).save_screenshot(settings.data_dir / "screenshots")
        data = path.read_bytes()
        w, h = png_size(data)
        print(f"saved: {path}")
        print(f"size on disk: {len(data)} bytes; PNG dimensions: {w}x{h}; expected: {args.width}x{args.height}")
        ok = (w, h) == (args.width, args.height)
        print("DIMENSIONS OK" if ok else "DIMENSION MISMATCH")
        return 0 if ok else 4
    except PlatformError as exc:
        print(f"REFUSED/FAILED: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
