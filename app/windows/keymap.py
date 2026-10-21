"""Translation of the provider-neutral `Key` into Win32 virtual-key data. Pure functions, no Windows API calls.

Win32 numeric codes exist only in this module; the generic layers never see them. The tables are total over `Key`
(a test checks that every member maps), and anything else is rejected.
"""
from __future__ import annotations

from app.providers.input import Key

_NAMED = {
    Key.ENTER: 0x0D, Key.ESCAPE: 0x1B, Key.SPACE: 0x20, Key.TAB: 0x09, Key.BACKSPACE: 0x08, Key.DELETE: 0x2E,
    Key.LEFT: 0x25, Key.UP: 0x26, Key.RIGHT: 0x27, Key.DOWN: 0x28,
    Key.SHIFT: 0x10, Key.CTRL: 0x11, Key.ALT: 0x12,
}
_EXTENDED = {Key.DELETE, Key.LEFT, Key.UP, Key.RIGHT, Key.DOWN}
_SYSTEM = {Key.ALT}   # Alt is delivered as the "system key" message pair


def virtual_key(key: Key) -> int:
    if not isinstance(key, Key):
        raise ValueError(f"Not a Key: {key!r}")
    if key in _NAMED:
        return _NAMED[key]
    value = key.value
    if len(value) == 1 and value.isalpha():
        return ord(value.upper())
    if len(value) == 1 and value.isdigit():
        return ord(value)
    if value.startswith("f") and value[1:].isdigit():
        return 0x6F + int(value[1:])        # F1 = 0x70
    raise ValueError(f"No mapping for {key!r}")


def is_extended(key: Key) -> bool:
    return key in _EXTENDED


def is_system(key: Key) -> bool:
    return key in _SYSTEM


def key_lparam(scan_code: int, *, extended: bool, system: bool, up: bool) -> int:
    """lParam of a key message: repeat count 1, scan code, extended flag, context (Alt) flag and transition bits."""
    if not 0 <= scan_code <= 0xFF:
        raise ValueError("scan code out of range")
    value = 1 | (scan_code << 16) | (int(extended) << 24) | (int(system) << 29)
    if up:
        value |= (1 << 30) | (1 << 31)        # previous state = down, transition = release
    return value


def _inverse() -> dict[int, Key]:
    table = {virtual_key(k): k for k in Key}
    # Observation reports the left/right modifier variants; the neutral vocabulary has one key per modifier.
    table.update({0xA0: Key.SHIFT, 0xA1: Key.SHIFT, 0xA2: Key.CTRL, 0xA3: Key.CTRL, 0xA4: Key.ALT, 0xA5: Key.ALT})
    return table


_FROM_VK = _inverse()


def key_from_virtual_key(vk: int) -> Key | None:
    """The neutral key for an observed virtual-key code, or None if it is outside the neutral vocabulary
    (never a substitute key). Left and right modifiers both map to the one neutral modifier."""
    return _FROM_VK.get(vk) if isinstance(vk, int) and not isinstance(vk, bool) else None
