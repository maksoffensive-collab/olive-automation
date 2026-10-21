"""Build the Windows release of Olive Automation (docs/RELEASE.md): one folder with Olive.exe -- Python, tkinter and the
fast picture search inside, nothing to install -- zipped, with its SHA-256 and the installer next to it. These are
the files uploaded to a GitHub release.

    .venv\\Scripts\\python.exe scripts\\build_release.py                # from a committed tree (the normal case)
    .venv\\Scripts\\python.exe scripts\\build_release.py --allow-dirty  # a trial build of uncommitted work

Output in dist\\ (ignored by git): Olive-<version>-win64.zip, Olive-<version>-win64.zip.sha256 and install.ps1.
A build only counts once it has checked itself: `Olive.exe --self-check` must find the window, the application and
text recognition loadable, the fast picture search present, and the person's data outside the program's folder.
Nothing is shown on the screen, no target is looked for and nothing is sent while it does.

Needs PyInstaller (requirements-dev.txt) and OpenCV (requirements-vision.txt) in the project's .venv."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "release"
DIST = ROOT / "dist"
NAME = "Olive"
BUILD_TIMEOUT_S = 1200
CHECK_TIMEOUT_S = 120
# Left out of the release: OpenCV's video reader (29 MB) -- Olive only ever searches pictures. The self-check runs
# after this, so a build that needed it would fail there.
UNNEEDED = ("_internal/cv2/opencv_videoio_ffmpeg*.dll",)

sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402

LAUNCHER = "from app.bootstrap.main import main\n\nraise SystemExit(main())\n"


def fail(message: str) -> int:
    print(f"BUILD FAILED: {message}")
    return 1


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()


def uncommitted() -> list[str]:
    """Changed tracked files anywhere, and new files in the code that goes into the build."""
    tracked = git("status", "--porcelain", "--untracked-files=no").splitlines()
    new_code = git("status", "--porcelain", "--untracked-files=all", "--", "app").splitlines()
    return sorted(set(tracked) | {line for line in new_code if line.startswith("??")})


def remove_inside(path: Path, owner: Path) -> None:
    """Delete a previous build output -- only ever a folder or file inside build\\ or dist\\."""
    if owner not in path.resolve().parents:
        raise RuntimeError(f"refusing to delete {path}: not inside {owner}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def run_pyinstaller() -> Path:
    BUILD.mkdir(parents=True, exist_ok=True)
    launcher = BUILD / "olive_launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    remove_inside(DIST / NAME, DIST)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
               "--distpath", str(DIST), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
               "--paths", str(ROOT), "--collect-submodules", "app", str(launcher)]
    print("Running PyInstaller (a few minutes) ...", flush=True)
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    if done.returncode != 0:
        print(done.stdout[-4000:], done.stderr[-4000:], sep="\n")
        raise RuntimeError(f"PyInstaller ended with {done.returncode}")
    exe = DIST / NAME / f"{NAME}.exe"
    if not exe.is_file():
        raise RuntimeError(f"PyInstaller reported success but {exe} is missing")
    for pattern in UNNEEDED:
        for path in (DIST / NAME).glob(pattern):
            path.unlink()
    return exe


def self_check(exe: Path) -> dict:
    report_path = BUILD / "self-check.json"
    if report_path.exists():
        report_path.unlink()
    done = subprocess.run([str(exe), "--self-check", str(report_path)], cwd=BUILD, capture_output=True,
                          timeout=CHECK_TIMEOUT_S)
    if not report_path.is_file():
        raise RuntimeError(f"Olive.exe --self-check wrote no report (exit code {done.returncode})")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    problems = [f"{name}: {value}" for name, value in report["checks"].items() if value != "ok"]
    if done.returncode != 0 or not report["ok"] or problems:
        raise RuntimeError("the build's self-check failed: " + "; ".join(problems or [f"exit {done.returncode}"]))
    if not report["frozen"] or report["version"] != __version__:
        raise RuntimeError(f"the self-check ran something else: {report}")
    data = Path(report["data_dir"])
    if data.parts[-2:] != ("Olive", "data") or (DIST / NAME).resolve() in data.parents:
        raise RuntimeError(f"the packaged data folder is wrong: {data}")
    return report


def package(commit: str) -> tuple[Path, str]:
    folder = DIST / NAME
    built = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (folder / "VERSION.txt").write_text(f"Olive Automation {__version__} (beta)\ncommit {commit}\nbuilt {built}\n",
                                        encoding="utf-8")
    shutil.copy2(ROOT / "scripts" / "uninstall.ps1", folder / "Uninstall.ps1")
    archive = DIST / f"{NAME}-{__version__}-win64.zip"
    remove_inside(archive, DIST)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, Path(NAME) / path.relative_to(folder))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (DIST / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    shutil.copy2(ROOT / "scripts" / "install.ps1", DIST / "install.ps1")
    return archive, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows release of Olive Automation.")
    parser.add_argument("--allow-dirty", action="store_true", help="build uncommitted work (a trial build)")
    args = parser.parse_args(argv)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        return fail("PyInstaller is not installed: .venv\\Scripts\\python.exe -m pip install -r requirements-dev.txt")
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return fail("OpenCV is not installed: .venv\\Scripts\\python.exe -m pip install -r requirements-vision.txt")
    changes = uncommitted()
    if changes and not args.allow_dirty:
        return fail("uncommitted changes (commit first, or --allow-dirty for a trial build):\n  "
                    + "\n  ".join(changes))
    commit = git("rev-parse", "--short", "HEAD") + ("-dirty" if changes else "")
    try:
        exe = run_pyinstaller()
        report = self_check(exe)
        archive, digest = package(commit)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return fail(str(exc))
    size = archive.stat().st_size / 2**20
    print(f"Self-check: {report['checks']}")
    print(f"Built Olive Automation {__version__} from {commit}:")
    print(f"  {archive.relative_to(ROOT)}  ({size:.0f} MB)\n  sha256 {digest}")
    print(f"  {(DIST / 'install.ps1').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
