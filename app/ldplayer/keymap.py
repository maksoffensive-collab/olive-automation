"""Translation of the provider-neutral `Key` into Android `KeyEvent` integer codes, for the LDPlayer/ADB playback
adapter's input bridge only. Android keycodes exist only in this module; `app.adb` -- the technology layer that
adapter bridges to the generic vocabulary -- never imports `app.providers` at all (see `app.ldplayer.provider` for
the same bridge role on the screenshot side). The table is total over `Key`: a test checks that every member maps
to its real, documented `android.view.KeyEvent` constant, and anything else is rejected -- never guessed, never
silently substituted.

Why `app.adb.input.InputSession` offers only `send_keyevent(keycode)`, and this adapter's `key_press` only ever a
full press, never a separate down/up: `adb shell input keyevent <code>` always injects a complete press
(ACTION_DOWN immediately followed by ACTION_UP) as one atomic device-side operation; the standard `input` command
has no portable way to send just a down, wait, and later send an independent up. Splitting a "held key" across two
separate `adb shell` invocations would not reproduce a real hold -- it would either do nothing between the calls or
require an unsupported, version-specific mechanism (raw `sendevent` device codes), which this stage deliberately
does not add (see docs/ARCHITECTURE.md, ADB input capability expansion).
"""
from __future__ import annotations

from app.providers.input import Key

# android.view.KeyEvent int constants (stable, public Android SDK values; not invented here).
_LETTERS: dict[Key, int] = {getattr(Key, c.upper()): 29 + i           # KEYCODE_A=29 .. KEYCODE_Z=54
                            for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
_DIGITS: dict[Key, int] = {getattr(Key, f"DIGIT_{d}"): 7 + d for d in range(10)}    # KEYCODE_0=7 .. KEYCODE_9=16
_FUNCTION: dict[Key, int] = {getattr(Key, f"F{i}"): 130 + i for i in range(1, 13)}  # KEYCODE_F1=131 .. F12=142
_NAMED: dict[Key, int] = {
    Key.ENTER: 66, Key.ESCAPE: 111, Key.SPACE: 62, Key.TAB: 61,
    Key.BACKSPACE: 67,     # KEYCODE_DEL: Android's name for the backspace key
    Key.DELETE: 112,       # KEYCODE_FORWARD_DEL: deletes ahead of the insertion point
    Key.LEFT: 21, Key.RIGHT: 22, Key.UP: 19, Key.DOWN: 20,             # KEYCODE_DPAD_*
    Key.SHIFT: 59, Key.CTRL: 113, Key.ALT: 57,                          # the *_LEFT variant of each modifier
}
_TABLE: dict[Key, int] = {**_LETTERS, **_DIGITS, **_FUNCTION, **_NAMED}


def android_keycode(key: Key) -> int:
    if not isinstance(key, Key):
        raise ValueError(f"Not a Key: {key!r}")
    try:
        return _TABLE[key]
    except KeyError:
        raise ValueError(f"No Android keyevent mapping for {key!r}") from None
