"""Application entry point: `python -m app` (default: every provider this machine currently supports, combined
-- see below) or `python -m app --provider windows|ldplayer` (an explicit override that builds exactly that one
provider and no other; `pythonw` instead of `python` for either, for no console window).

This is the ONE place that decides which provider(s) back the GUI; `app.gui` never learns which one (or how many)
were chosen -- `WindowsServices`, `LDPlayerServices` and `CompositeAutomationServices` all satisfy the same
`AutomationServices` contract, so `App`/`AppController` run unmodified regardless.

Without `--provider`, this builds a `CompositeAutomationServices` (app/bootstrap/composite_services.py) over
every provider that is actually usable right now: the Windows provider is always available, and the LDPlayer
provider is added only if LDPlayer is actually installed and configured -- its absence degrades the platform to
Windows-only rather than refusing to start, since "no LDPlayer installs found" is an expected, ordinary
environment, not a startup failure. `--provider` remains for a deliberately single-provider run (debugging, a
test rig, or a machine where only one provider should ever be touched): given explicitly, an unavailable provider
is still a hard, reported failure exactly as before -- only the *default* path tolerates its absence."""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from app import __version__
from app.bootstrap.windows_services import WindowsServices
from app.core.cancel import EmergencyStop
from app.core.errors import PlatformError
from app.gui.history import RunHistory
from app.gui.library import MacroLibrary
from app.gui.meta import MacroMeta
from app.gui.prefs import PreferencesStore
from app.gui.services import AutomationServices

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_data_dir(frozen: bool | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Where the person's macros, settings and history live. Run from the source tree: `data/` in the project, as
    always. The packaged application (a frozen executable, docs/RELEASE.md): `%LOCALAPPDATA%\\Olive\\data`, outside
    the program's folder, so installing a newer version replaces the program and never the person's data."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not frozen:
        return PROJECT_ROOT / "data"
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Olive" / "data"


DATA_DIR = default_data_dir()
MACRO_DIR = DATA_DIR / "macros"
PREFS_PATH = DATA_DIR / "gui_settings.json"
META_PATH = DATA_DIR / "macro_meta.json"
HISTORY_PATH = DATA_DIR / "run_history.json"
PROVIDERS = ("windows", "ldplayer")


def _reveal(path: object) -> bool:
    """Show a macro file in the file manager. This lives in the composition root, not in `app.gui`: the GUI
    layer deliberately touches no operating system, so it asks for this through an injected callable and simply
    reports it if the request could not be carried out."""
    try:
        target = Path(str(path))
        folder = target.parent if target.exists() else target
        if not folder.is_dir():
            return False
        os.startfile(str(folder))            # noqa: S606 - opening a folder the user already owns
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _explicit_provider_from(argv: list[str]) -> str | None:
    """`--provider windows|ldplayer`, or None if the flag was not given at all (the normal, combined launch).
    Any other value is rejected up front, not guessed."""
    for i, arg in enumerate(argv):
        if arg == "--provider" and i + 1 < len(argv):
            value = argv[i + 1]
            if value not in PROVIDERS:
                raise ValueError(f"--provider must be one of {PROVIDERS}, got {value!r}.")
            return value
    return None


def _build_services(explicit: str | None, stop: EmergencyStop) -> AutomationServices | None:
    """None only on a reported, hard failure (an explicitly requested provider that is not usable)."""
    from app.core.errors import ConfigError

    if explicit == "windows":
        return WindowsServices.create_default(stop)
    if explicit == "ldplayer":
        from app.bootstrap.ldplayer_services import LDPlayerServices
        try:
            return LDPlayerServices.create_default(stop)
        except ConfigError as exc:
            print(str(exc))
            return None

    from app.bootstrap.composite_services import CompositeAutomationServices
    from app.bootstrap.ldplayer_services import LDPlayerServices
    available: list[AutomationServices] = [WindowsServices.create_default(stop)]
    try:
        available.append(LDPlayerServices.create_default(stop))
    except ConfigError:
        pass          # not installed/configured here: run Windows-only rather than refuse to start
    return available[0] if len(available) == 1 else CompositeAutomationServices(available)


def _build_ocr(stop: EmergencyStop):
    """The OCR engine text conditions are read with, or None.

    Chosen here, the one composition root, and nowhere else: the GUI receives it as an opaque object and hands it to
    the observation layer, so no other package names OCR (`tests.test_ai_ocr.CoreWorksWithoutAiOrOcr`). The engine is
    Windows' own OCR (`app.ocr.winrt`): nothing to install, offline, run through the platform's `CommandRunner`, so the
    emergency stop kills it and every read has a hard timeout.

    Optional by construction. The import is lazy and building the engine touches nothing on disk, so the only
    failures possible here are a missing or broken engine module: it is reported (stderr, like the configuration
    errors above) and the application starts without OCR -- everything else works, and a run that asks for text ends
    as `ocr_unavailable`. Problems that come and go (the temp folder, the script, PowerShell, a language pack) are
    reported by the read that meets them and heal by themselves; they are not decided once, here."""
    try:
        from app.core.process import CommandRunner
        from app.ocr.winrt import WindowsOcrProvider
        return WindowsOcrProvider(CommandRunner(stop))
    except (ImportError, OSError, PlatformError) as exc:
        print(f"Text recognition (OCR) is unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _enable_dpi_awareness() -> None:
    """Per-monitor v2, like the tools: window and hook coordinates are then real pixels for everyone."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _self_check_path_from(argv: list[str]) -> str | None:
    """`--self-check PATH`, or None if not given (a flag without its PATH is not a self-check)."""
    for i, arg in enumerate(argv):
        if arg == "--self-check" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def self_check(path: str) -> int:
    """`--self-check PATH`: write what this build can load to PATH as JSON and end -- 0 when the window, the
    application and text recognition can all be loaded, else 1. No window is shown, no target is looked for and
    nothing is sent; it is how a packaged release is checked before it is published (scripts/build_release.py).
    The fast picture search is reported, never required: the application works without it."""
    checks: dict[str, str] = {}

    def attempt(name: str, load) -> None:
        try:
            load()
            checks[name] = "ok"
        except Exception as exc:            # reported, not raised: the report is the result
            checks[name] = f"{type(exc).__name__}: {exc}"

    def window() -> None:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()                     # before it is ever drawn: nothing appears on the screen
        root.destroy()

    def application() -> None:
        import app.bootstrap.composite_services  # noqa: F401
        import app.bootstrap.ldplayer_services  # noqa: F401
        import app.gui.app  # noqa: F401

    def text_recognition() -> None:
        """Built exactly as a start builds it (`_build_ocr`, the one place that may name the engine); building it
        reads nothing and runs nothing."""
        if _build_ocr(EmergencyStop()) is None:
            raise ImportError("the text recognition engine could not be loaded")

    def picture_search() -> None:
        """One real search: a 2 x 2 picture must be found exactly where it is in a 4 x 4 one, and nowhere else."""
        from app.vision.roi import Roi
        corner = bytes(range(10, 130, 10))                  # four distinct colours
        rgb = bytearray(4 * 4 * 3)
        for row in (2, 3):
            start = (row * 4 + 2) * 3
            rgb[start:start + 6] = corner[(row - 2) * 6:(row - 1) * 6]
        found = fast(bytes(rgb), 4, 4, Roi(0, 0, 4, 4), corner, 2, 2, 0.0, 10)
        if found != [(0, 2, 2)]:
            raise ValueError(f"wrong answer {found!r}")

    attempt("window", window)
    attempt("application", application)
    attempt("text_recognition", text_recognition)
    from app.vision.template import fast_matcher
    fast = fast_matcher()
    if fast is None:
        checks["fast_picture_search"] = "unavailable"
    else:
        attempt("fast_picture_search", picture_search)
    required = ("window", "application", "text_recognition")
    report = {"version": __version__, "python": sys.version.split()[0],
              "frozen": bool(getattr(sys, "frozen", False)), "data_dir": str(DATA_DIR), "checks": checks,
              "ok": all(checks[name] == "ok" for name in required)}
    Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        print("This application currently supports Windows only.")
        return 2
    args = sys.argv[1:] if argv is None else argv
    check_path = _self_check_path_from(args)
    if check_path is not None:
        return self_check(check_path)
    try:
        explicit = _explicit_provider_from(args)
    except ValueError as exc:
        print(str(exc))
        return 2
    _enable_dpi_awareness()
    from app.gui.app import App          # imported late: Tk is only needed when the GUI actually starts
    services = _build_services(explicit, EmergencyStop())
    if services is None:
        return 1
    App(services, MacroLibrary(MACRO_DIR), prefs=PreferencesStore(PREFS_PATH),
        meta=MacroMeta(META_PATH), history=RunHistory(HISTORY_PATH), open_folder=_reveal,
        ocr=_build_ocr(services.stop)).run()
    return 0
