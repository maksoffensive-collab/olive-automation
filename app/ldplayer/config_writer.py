"""The one change Olive makes to an LDPlayer instance's file (`vms/config/leidianN.config`): its "ADB debugging"
setting turned to "Open local connection" (`basicSettings.adbDebug` = 1) -- the person's decision of 2026-10-27, on
their explicit yes each time, with the instance closed (LDPlayer reads the file when an instance starts).

Written only when the file is exactly in the form Olive writes back (LDPlayer's own: JSON with four spaces, the
same line ends, no newline at the end -- [VERIFIED] 2026-10-27 on a file LDPlayer wrote), so the one setting is the
only change; anything else is refused and nothing is written. The file as it was is kept first in Olive's own
backup folder; the new one replaces it in one step (a temporary file beside it, then `os.replace`).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.core.errors import PlatformError
from app.ldplayer.config_reader import ADB_DEBUG_KEY

OPEN_LOCAL_CONNECTION = 1
_BOM = b"\xef\xbb\xbf"


class ConfigWriteRefused(PlatformError):
    """The file was not changed as asked: not in the form Olive can write back unchanged, another instance's,
    unreadable -- or (`written`) replaced but not reading back as written."""

    written = False


def config_path(ldplayer_dir: Path, index: int) -> Path:
    return Path(ldplayer_dir) / "vms" / "config" / f"leidian{index}.config"


def _dumps(data: dict, *, ascii_only: bool, crlf: bool, newline_at_end: bool) -> str:
    text = json.dumps(data, indent=4, ensure_ascii=ascii_only)
    text += "\n" if newline_at_end else ""
    return text.replace("\n", "\r\n") if crlf else text


def enable_adb_debugging(ldplayer_dir: Path, index: int, name: str, backup_dir: Path, *,
                         check_only: bool = False) -> Path | None:
    """Turn instance `index`'s ADB debugging on. `name` is the instance's name as LDPlayer lists it: a file that
    names another instance is refused. Returns the backup of the file as it was, or None when the setting was on
    already (nothing written) or `check_only` (every check made, nothing written: asked before the instance is
    closed). Raises ConfigWriteRefused, the file untouched, for anything it cannot do exactly -- `written` set when
    the file was replaced but does not read back as written."""
    path = config_path(ldplayer_dir, index)

    def refused(en: str, ru: str, *, written: bool = False) -> ConfigWriteRefused:
        error = ConfigWriteRefused(en).saying(ru=ru)
        error.written = written
        return error
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise refused(f"The settings file of instance {index} could not be read ({exc}).",
                      f"Не удалось прочитать файл настроек экземпляра {index} ({exc}).") from None
    bom = raw.startswith(_BOM)
    try:
        text = raw[len(_BOM) if bom else 0:].decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        raise refused(f"The settings file of instance {index} is not in the form LDPlayer writes; nothing was changed.",
                      f"Файл настроек экземпляра {index} не в том виде, в каком его пишет LDPlayer; ничего не "
                      "изменено.")
    own = data.get("statusSettings.playerName")
    if isinstance(own, str) and own and own != name:
        raise refused(f"The settings file of instance {index} names “{own}”, not “{name}”; nothing was changed.",
                      f"Файл настроек экземпляра {index} называет «{own}», а не «{name}»; ничего не изменено.")
    crlf, at_end = "\r\n" in text, text.endswith("\n")
    form = next((ascii_only for ascii_only in (False, True)
                 if _dumps(data, ascii_only=ascii_only, crlf=crlf, newline_at_end=at_end) == text), None)
    if form is None:
        raise refused(f"The settings file of instance {index} is not in the form Olive can write back unchanged; "
                      "nothing was changed. Turn ADB debugging on in LDPlayer's own settings.",
                      f"Файл настроек экземпляра {index} не в том виде, который Olive может записать без других "
                      "изменений; ничего не изменено. Включите отладку ADB в настройках самого LDPlayer.")
    if data.get(ADB_DEBUG_KEY) == OPEN_LOCAL_CONNECTION or check_only:
        return None
    data[ADB_DEBUG_KEY] = OPEN_LOCAL_CONNECTION
    new = (_BOM if bom else b"") + _dumps(data, ascii_only=form, crlf=crlf, newline_at_end=at_end).encode("utf-8")
    temporary = path.with_name(path.name + ".olive-new")
    try:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"leidian{index}-{time.strftime('%Y%m%d-%H%M%S')}.config"
        backup.write_bytes(raw)
        temporary.write_bytes(new)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)          # Olive's own half-written file, never LDPlayer's
        except OSError:
            pass
        raise refused(f"The settings file of instance {index} could not be written ({exc}).",
                      f"Не удалось записать файл настроек экземпляра {index} ({exc}).") from None
    try:
        same = path.read_bytes() == new
    except OSError:
        same = False
    if not same:
        raise refused(f"The settings file of instance {index} does not read back as written.",
                      f"Файл настроек экземпляра {index} после записи читается не так, как записан.", written=True)
    return backup
