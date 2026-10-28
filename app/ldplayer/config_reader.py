"""Read-only reader for LDPlayer per-instance config files (`vms/config/leidianN.config`).

The files hold device identifiers (IMEI, IMSI, Android ID, MAC). They are kept in an
`Identity` object whose repr and string form are redacted, so they cannot leak through
logs, tracebacks or diagnostics by accident. Files are opened in binary read mode only.

[ASSUMPTION] `leidianN.config` belongs to the instance with LDPlayer index N. It matches
in count and naming on this machine and is [NEEDS TESTING] against a running instance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import PlatformError

ADB_DEBUG_KEY = "basicSettings.adbDebug"


class InstanceConfigError(PlatformError):
    pass


@dataclass(frozen=True)
class Identity:
    android_id: str | None = None
    imei: str | None = None
    imsi: str | None = None
    sim_serial: str | None = None
    mac: str | None = None
    model: str | None = None
    manufacturer: str | None = None

    def __repr__(self) -> str:  # never show identifiers
        present = [name for name in ("android_id", "imei", "imsi", "sim_serial", "mac", "model", "manufacturer")
                   if getattr(self, name)]
        return f"Identity(<redacted>, fields={present})"

    __str__ = __repr__

    def values(self) -> dict[str, str]:
        """The raw identifiers, for matching only. Never log the result."""
        return {k: v for k, v in (("android_id", self.android_id), ("imei", self.imei), ("imsi", self.imsi),
                                  ("sim_serial", self.sim_serial), ("mac", self.mac)) if v}


@dataclass(frozen=True)
class InstanceConfig:
    index: int
    path: Path
    identity: Identity
    width: int | None
    height: int | None
    dpi: int | None
    cpu_count: int | None
    memory_mb: int | None
    player_name: str | None = None
    keys: tuple[str, ...] = field(default=(), compare=False)
    adb_debug: int | None = None      # LDPlayer's "ADB debugging" setting: 0 = off (no ADB connection at all)

    @property
    def adb_debug_off(self) -> bool:
        """Whether LDPlayer's "ADB debugging" is off: set to "Close" (0), or not in the file at all -- LDPlayer's
        default: a new instance's file has no such key, and the instance then opens no ADB port [VERIFIED 2026-10-27
        on a new instance; 2026-09-25 on LDPlayer-10]. A value that is not a whole number is not known: False."""
        return self.adb_debug == 0 or ADB_DEBUG_KEY not in self.keys


def _s(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return str(value) if value not in (None, "") else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_instance_config(index: int, path: Path, data: dict[str, Any]) -> InstanceConfig:
    res = data.get("advancedSettings.resolution")
    res = res if isinstance(res, dict) else {}
    return InstanceConfig(
        index=index, path=path,
        identity=Identity(
            android_id=_s(data, "propertySettings.phoneAndroidId"),
            imei=_s(data, "propertySettings.phoneIMEI"),
            imsi=_s(data, "propertySettings.phoneIMSI"),
            sim_serial=_s(data, "propertySettings.phoneSimSerial"),
            mac=_s(data, "propertySettings.macAddress"),
            model=_s(data, "propertySettings.phoneModel"),
            manufacturer=_s(data, "propertySettings.phoneManufacturer"),
        ),
        width=res.get("width"), height=res.get("height"),
        dpi=data.get("advancedSettings.resolutionDpi"),
        cpu_count=data.get("advancedSettings.cpuCount"),
        memory_mb=data.get("advancedSettings.memorySize"),
        player_name=_s(data, "statusSettings.playerName"),
        keys=tuple(data.keys()),
        adb_debug=_int(data.get(ADB_DEBUG_KEY)),
    )


def read_instance_config(ldplayer_dir: Path, index: int) -> InstanceConfig:
    path = Path(ldplayer_dir) / "vms" / "config" / f"leidian{index}.config"
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8-sig"))
    except FileNotFoundError:
        raise InstanceConfigError(f"No configuration file for LDPlayer instance {index} at '{path}'.").saying(
            ru=f"Нет файла настроек экземпляра LDPlayer {index}: «{path}».") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstanceConfigError(f"The configuration file for instance {index} is not valid JSON: {exc}").saying(
            ru=f"Файл настроек экземпляра {index} — неправильный JSON: {exc}") from None
    if not isinstance(data, dict):
        raise InstanceConfigError(f"The configuration file for instance {index} has an unexpected structure.").saying(
            ru=f"Файл настроек экземпляра {index} устроен не так, как ожидалось.")
    return parse_instance_config(index, path, data)


def find_identity_conflicts(configs: list[InstanceConfig]) -> dict[str, list[int]]:
    """Identifier kinds shared by more than one instance, as {kind: [instance indexes]}.

    A shared identifier means it cannot identify a single instance on its own.
    Only kinds and indexes are returned; identifier values are never exposed.
    """
    conflicts: dict[str, set[int]] = {}
    for kind in ("android_id", "imei", "imsi", "sim_serial", "mac"):
        seen: dict[str, list[int]] = {}
        for cfg in configs:
            value = cfg.identity.values().get(kind)
            if value:
                seen.setdefault(value, []).append(cfg.index)
        for indexes in seen.values():
            if len(indexes) > 1:
                conflicts.setdefault(kind, set()).update(indexes)
    return {k: sorted(v) for k, v in conflicts.items()}
