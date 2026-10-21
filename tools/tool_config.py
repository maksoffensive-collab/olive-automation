"""Shared start-up check of the LDPlayer command-line tools: a bad configuration or a missing LDPlayer folder is
reported as a message (exit code 1), never as a traceback."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config.settings import Settings, load_settings  # noqa: E402
from app.core.errors import ConfigError  # noqa: E402


def load_ldplayer_settings() -> Settings | None:
    """The validated settings with a usable LDPlayer folder, or None after printing why not."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        return None
    if settings.ldplayer_dir is None:
        print("LDPlayer was not found (no folder with ldconsole.exe and adb.exe). "
              "Set LDPLAYER_DIR or 'ldplayer_dir' in the configuration file.")
        return None
    return settings
