r"""Read-only Windows provider check: list windows, or verify ONE window and save one screenshot of it.

Sends no input, focuses/moves/closes nothing, launches nothing, and sends nothing anywhere. Screenshots go to
data/screenshots/windows/<target id>/ (git-ignored). Titles are hidden unless --titles is given.

    .venv\Scripts\python.exe tools\windows_check.py --list
    .venv\Scripts\python.exe tools\windows_check.py --hwnd 197234 [--process-name explorer.exe]
    .venv\Scripts\python.exe tools\windows_check.py --process-name notepad.exe      (first matching window)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import load_settings  # noqa: E402
from app.core.cancel import EmergencyStop  # noqa: E402
from app.core.errors import PlatformError  # noqa: E402
from app.vision.frame import Frame  # noqa: E402
from app.windows.provider import WindowScreenshotProvider  # noqa: E402
from app.windows.target import WindowVerifier  # noqa: E402
from app.windows.win32 import Win32Backend  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list visible top-level windows and exit")
    ap.add_argument("--hwnd", type=int)
    ap.add_argument("--process-name", help="executable name to require / to select the first matching window")
    ap.add_argument("--titles", action="store_true", help="show window titles")
    args = ap.parse_args()
    backend, stop = Win32Backend(), EmergencyStop()
    windows = backend.enumerate()
    if args.list:
        for w in windows:
            title = f"  {w.title!r}" if args.titles else ""
            print(f"hwnd={w.hwnd:<10} {w.process_name or '?':<24} {w.width}x{w.height}"
                  f"{' minimized' if w.minimized else ''}{title}")
        print(f"{len(windows)} windows")
        return 0
    hwnd = args.hwnd
    if hwnd is None:
        wanted = (args.process_name or "").lower()
        pick = [w for w in windows if wanted and w.process_name.lower() == wanted and not w.minimized]
        if not pick:
            print("No window selected: give --hwnd, or --process-name matching a visible, non-minimized window.")
            return 2
        hwnd = pick[0].hwnd
    verifier = WindowVerifier(backend, stop)
    try:
        target = verifier.verify(hwnd, expected_process_name=args.process_name)
        print(f"VERIFIED: {target.describe() if args.titles else target.provider_id + ':' + target.target_id}")
        print(f"  checks: {', '.join(target.verification.methods)}; capabilities: "
              f"{sorted(c.value for c in target.capabilities)}")
        shot = WindowScreenshotProvider(target, verifier, stop).capture()
    except PlatformError as exc:
        print(f"REFUSED: {exc}")
        return 2
    frame = Frame.from_screenshot(shot)  # decodes with the generic vision decoder: proves the PNG is valid
    folder = load_settings().data_dir / "screenshots" / "windows" / target.target_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.png"
    with open(path, "xb") as fh:
        fh.write(shot.png)
    distinct = len({frame.bitmap.rgb[i:i + 3] for i in range(0, len(frame.bitmap.rgb), 3 * 251)})
    print(f"Screenshot: {shot.width}x{shot.height}, {len(shot.png)} bytes, decoded OK by vision "
          f"({frame.width}x{frame.height}), ~{distinct} distinct sampled colours, provenance "
          f"{shot.source.provider_id}:{shot.source.target_id}")
    print(f"Saved: {path}  (content not viewed or analysed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
