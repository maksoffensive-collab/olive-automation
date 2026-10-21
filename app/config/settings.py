"""Central configuration.

Precedence (lowest to highest): built-in defaults < optional TOML file < environment
variables (`LDAP_*`, plus `LDPLAYER_DIR`). Every value is validated; invalid values raise
ConfigError with a message the user can act on. Nothing here contacts a network or
writes a file. No user-specific path is hard-coded: the LDPlayer folder is discovered
from a list of candidates and accepted only if it really contains ldconsole.exe and adb.exe.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from app.core.errors import ConfigError

PROFILES = ("low_resource", "balanced", "high_reliability")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_RETRIES = 5
MAX_WORKERS = 16

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    ldplayer_dir: Path | None = None
    data_dir: Path = PROJECT_ROOT / "data"
    default_timeout_s: float = 30.0
    adb_timeout_s: float = 15.0
    ldconsole_timeout_s: float = 20.0
    retry_count: int = 2
    retry_backoff_s: float = 0.5
    poll_interval_s: float = 1.0
    log_level: str = "INFO"
    worker_limit: int = 4
    performance_profile: str = "balanced"
    emergency_stop_hotkey: str = "esc"
    unknown_keys: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ldconsole_path(self) -> Path | None:
        return self.ldplayer_dir / "ldconsole.exe" if self.ldplayer_dir else None

    @property
    def adb_path(self) -> Path | None:
        return self.ldplayer_dir / "adb.exe" if self.ldplayer_dir else None

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def is_ldplayer_dir(path: Path) -> bool:
    return path.is_dir() and (path / "ldconsole.exe").is_file() and (path / "adb.exe").is_file()


def default_ldplayer_candidates(env: Mapping[str, str]) -> list[Path]:
    out: list[Path] = []
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if env.get(var):
            out.append(Path(env[var]) / "LDPlayer" / "LDPlayer14")
    drive = env.get("SystemDrive", "C:")
    out.append(Path(drive + "\\") / "LDPlayer" / "LDPlayer14")
    return out


def discover_ldplayer_dir(candidates: list[Path]) -> Path | None:
    for cand in candidates:
        if is_ldplayer_dir(cand):
            return cand
    return None


def _num(name: str, value: Any, lo: float, hi: float, integer: bool = False) -> Any:
    if isinstance(value, bool):                      # true/false are not numbers (int(True) would silently be 1)
        raise ConfigError(f"Setting '{name}' must be a number, got {value!r}.")
    if integer and isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"Setting '{name}' must be a whole number, got {value!r}.")    # never truncate 2.9 to 2
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"Setting '{name}' must be a number, got {value!r}.") from None
    if not (lo <= number <= hi):
        raise ConfigError(f"Setting '{name}' must be between {lo} and {hi}, got {number}.")
    return number


_FIELDS = {
    "default_timeout_s": (0.1, 3600.0, False),
    "adb_timeout_s": (0.1, 3600.0, False),
    "ldconsole_timeout_s": (0.1, 3600.0, False),
    "retry_count": (0, MAX_RETRIES, True),
    "retry_backoff_s": (0.0, 60.0, False),
    "poll_interval_s": (0.05, 3600.0, False),
    "worker_limit": (1, MAX_WORKERS, True),
}


def load_settings(config_file: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    raw: dict[str, Any] = {}
    if config_file is not None:
        try:
            with open(config_file, "rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"Configuration file not found: {config_file}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Configuration file {config_file} is not valid TOML: {exc}") from None

    for name in _FIELDS:
        var = "LDAP_" + name.upper()
        if var in env:
            raw[name] = env[var]
    for name in ("log_level", "performance_profile", "emergency_stop_hotkey", "data_dir"):
        var = "LDAP_" + name.upper()
        if var in env:
            raw[name] = env[var]
    if "LDPLAYER_DIR" in env:
        raw["ldplayer_dir"] = env["LDPLAYER_DIR"]

    known = set(_FIELDS) | {"log_level", "performance_profile", "emergency_stop_hotkey", "data_dir", "ldplayer_dir"}
    unknown = tuple(sorted(set(raw) - known))
    values: dict[str, Any] = {}
    for name, (lo, hi, integer) in _FIELDS.items():
        if name in raw:
            values[name] = _num(name, raw[name], lo, hi, integer)
    if "log_level" in raw:
        level = str(raw["log_level"]).upper()
        if level not in LOG_LEVELS:
            raise ConfigError(f"Setting 'log_level' must be one of {', '.join(LOG_LEVELS)}, got {raw['log_level']!r}.")
        values["log_level"] = level
    if "performance_profile" in raw:
        profile = str(raw["performance_profile"]).lower()
        if profile not in PROFILES:
            raise ConfigError(f"Setting 'performance_profile' must be one of {', '.join(PROFILES)}, got {raw['performance_profile']!r}.")
        values["performance_profile"] = profile
    if "emergency_stop_hotkey" in raw:
        hotkey = str(raw["emergency_stop_hotkey"]).strip().lower()
        if not hotkey:
            raise ConfigError("Setting 'emergency_stop_hotkey' must not be empty.")
        values["emergency_stop_hotkey"] = hotkey
    if "data_dir" in raw:
        values["data_dir"] = Path(str(raw["data_dir"]))

    if "ldplayer_dir" in raw:
        explicit = Path(str(raw["ldplayer_dir"]))
        if not is_ldplayer_dir(explicit):
            raise ConfigError(
                f"The configured LDPlayer folder '{explicit}' does not contain ldconsole.exe and adb.exe. "
                "Check the 'ldplayer_dir' setting or the LDPLAYER_DIR environment variable.")
        values["ldplayer_dir"] = explicit
    else:
        values["ldplayer_dir"] = discover_ldplayer_dir(default_ldplayer_candidates(env))
    return replace(Settings(), unknown_keys=unknown, **values)
