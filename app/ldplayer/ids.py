"""LDPlayer provider constants: the provider id and the capabilities its verified targets explicitly declare.

Kept in a leaf module so both the identity proof (`app.instances.mapping`) and the screenshot adapter
(`app.ldplayer.provider`) can use them without importing each other.
"""
from __future__ import annotations

from app.providers.target import Capability

PROVIDER_ID = "ldplayer-adb"

# Declared, not inferred: a verified LDPlayer target can be captured and can receive a budgeted tap, a full key
# press, or short allowlisted text (see app.adb.input.InputSession). No secondary/double tap, pointer move/drag,
# or record: a touchscreen has no secondary button, ADB has no portable down/move/up or key-down/up primitive,
# and LDPlayer/ADB has no input-observation mechanism to record from (see docs/ARCHITECTURE.md).
CAPABILITIES: frozenset[Capability] = frozenset({Capability.SCREENSHOT, Capability.TAP, Capability.KEY,
                                                 Capability.TEXT})
