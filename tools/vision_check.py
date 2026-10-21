r"""Live smoke test of the vision primitives on real screenshots of ONE verified instance.

Read-only: two screenshots go through the verified pipeline (AdbSession.screenshot), then colour
detection, template matching (a crop of the frame itself) and change detection run locally.
Only numbers are printed; the image content is not interpreted. No input is sent.

    .venv\Scripts\python.exe tools\vision_check.py --index 9
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adb.client import AdbClient  # noqa: E402
from app.ldplayer.provider import LDPlayerScreenshotProvider  # noqa: E402
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
from app.vision.change import compare_frames  # noqa: E402
from app.vision.color import find_color  # noqa: E402
from app.vision.frame import Frame  # noqa: E402
from app.vision.roi import Roi  # noqa: E402
from app.vision.template import match_template  # noqa: E402


def timed(label, fn):
    t0 = time.perf_counter()
    out = fn()
    print(f"  {label:<28} {time.perf_counter() - t0:7.2f} s")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", type=int, required=True)
    args = ap.parse_args()
    settings = load_ldplayer_settings()
    if settings is None:
        return 1
    hub = LogHub([JsonlFileSink(settings.log_dir / "vision_check.jsonl")], "DEBUG")
    stop = EmergencyStop()
    runner = CommandRunner(stop)
    console = LDConsole(settings.ldconsole_path, runner, settings.ldconsole_timeout_s, hub.logger("ldplayer"))
    adb = AdbClient(settings.adb_path, runner, stop, timeout_s=settings.adb_timeout_s,
                    retries=settings.retry_count, backoff_s=settings.retry_backoff_s, logger=hub.logger("adb"))
    try:
        if len(console.running_names()) != 1:
            print("Expected exactly one running instance. Nothing captured.")
            return 2
        registry = InstanceRegistry.from_discovery(console.list_instances())
        svc = MappingService(registry, console, adb, stop, lambda i: read_instance_config(settings.ldplayer_dir, i),
                             lambda: listening_port_owners(runner), hub.logger("instances"))
        target = svc.verify(f"ld{args.index}")
        print("VERIFIED:", target.describe())
        provider = LDPlayerScreenshotProvider(target, adb, svc, stop)
        shot1 = timed("screenshot 1", provider.capture)
        shot2 = timed("screenshot 2", provider.capture)
        f1 = timed("decode PNG -> Frame 1", lambda: Frame.from_screenshot(shot1))
        f2 = Frame.from_screenshot(shot2)
        print(f"  frame: {f1.width}x{f1.height}, target {f1.target_id} via {f1.source.provider_id}")

        colour = f1.bitmap.pixel(f1.width // 2, f1.height // 2)
        m = timed("find_color (full frame)", lambda: find_color(f1, colour, tolerance=0))
        print(f"    colour found={m.found}, matching pixels={m.count}")

        change = timed("compare_frames (full)", lambda: compare_frames(f1, f2))
        print(f"    changed={change.changed}, changed pixels={change.changed_pixels}/{change.total_pixels}")

        # template = a crop of the frame itself, so an exact hit must exist; try a few spots for a distinctive one
        found = None
        for (x, y) in ((600, 300), (100, 100), (900, 500), (300, 400), (1000, 100), (50, 600)):
            tpl = f1.bitmap.crop(x, y, 48, 32)
            try:
                ms = timed(f"match_template crop@{x},{y}", lambda: match_template(f1, tpl, min_confidence=0.99))
            except PlatformError as exc:
                print(f"    crop@{x},{y}: {exc}")
                continue
            found = (x, y, ms)
            break
        if found:
            x, y, ms = found
            ok = bool(ms) and (ms[0].x, ms[0].y) == (x, y) and ms[0].confidence == 1.0
            print(f"    best match at ({ms[0].x}, {ms[0].y}) confidence {ms[0].confidence:.4f}; expected ({x}, {y}): {'OK' if ok else 'MISMATCH'}")
            roi_ms = timed("match_template in ROI", lambda: match_template(f1, f1.bitmap.crop(x, y, 48, 32),
                                                                          min_confidence=0.99, roi=Roi(max(0, x - 100), max(0, y - 80), 250, 200)))
            print(f"    ROI match: {(roi_ms[0].x, roi_ms[0].y) if roi_ms else None}")
        else:
            print("  no distinctive crop found (flat screen); template test skipped")
        return 0
    except PlatformError as exc:
        print(f"REFUSED/FAILED: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
