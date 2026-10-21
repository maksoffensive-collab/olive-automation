"""Windows provider constants: the provider id and the capabilities its verified targets explicitly declare."""
from __future__ import annotations

from app.providers.target import Capability

PROVIDER_ID = "windows"

# Declared, not inferred: a verified window can be captured and can receive the targeted input operations below
# (see input.py). RECORD only observes (recorder.py); nothing else exists: no scrolling, no drag primitive, no system-wide input.
CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.SCREENSHOT, Capability.TAP, Capability.SECONDARY_TAP, Capability.DOUBLE_TAP,
    Capability.POINTER_MOVE, Capability.POINTER_BUTTON, Capability.KEY, Capability.TEXT,
    Capability.RECORD})

# The whole desktop ("Full Windows"): looked at as one picture of the virtual screen, every monitor -- and, on the
# person's explicit decision of 2026-10-19, recorded and played on too: input at a screen point, wherever it lands,
# only through `DesktopInputSession` (app/windows/desktop.py), which gates every event (the same screen layout as
# when chosen, never on this application's own windows, Esc or the person's own mouse stops the run).
DESKTOP_CAPABILITIES: frozenset[Capability] = CAPABILITIES
