"""Provider-neutral input vocabulary: keys and the pointer / keyboard interfaces.

  Key               a small, closed set of keys named independently of any operating system or device (letters,
                    digits, Enter, Escape, Space, Tab, Backspace, Delete, arrows, Shift, Ctrl, Alt, F1-F12).
                    Providers translate a `Key` into whatever their target understands; no numeric key codes
                    exist here. Anything else is rejected by `Key.parse`.
  PointerProvider   structural interface for targets that accept pointer input at client-relative pixel points.
  KeyboardProvider  structural interface for targets that accept key and text input.

The existing `InputProvider` (base.py: `tap`) is unchanged, so providers that only tap keep satisfying it. Which of
these operations a target may actually perform is decided by its explicitly declared `Capability` set, never by
which interface a class happens to implement.
"""
from __future__ import annotations

import string
from enum import Enum
from typing import Protocol, runtime_checkable


def _key_members() -> dict[str, str]:
    members = {c.upper(): c for c in string.ascii_lowercase}
    members.update({f"DIGIT_{d}": d for d in string.digits})
    members.update({n.upper(): n for n in ("enter", "escape", "space", "tab", "backspace", "delete", "left", "right",
                                           "up", "down", "shift", "ctrl", "alt")})
    members.update({f"F{i}": f"f{i}" for i in range(1, 13)})
    return members


Key = Enum("Key", _key_members(), type=str, module=__name__)
Key.__doc__ = "A provider-neutral key. Use Key.parse('a') / Key.parse('enter') / Key.parse('f5') for names."


def _parse(cls, name: object):
    if not isinstance(name, str):
        raise ValueError(f"Unknown key: {name!r}")
    try:
        return cls(name.strip().lower())
    except ValueError:
        raise ValueError(f"Unknown key: {name!r}") from None


Key.parse = classmethod(_parse)  # type: ignore[attr-defined]


@runtime_checkable
class PointerProvider(Protocol):
    """Pointer input at integer pixel points in the target's own client-relative coordinate space."""

    @property
    def target_id(self) -> str: ...

    def secondary_tap(self, x: int, y: int) -> None: ...

    def double_tap(self, x: int, y: int) -> None: ...

    def move(self, x: int, y: int) -> None: ...

    def button_down(self, x: int, y: int) -> None: ...

    def button_up(self, x: int, y: int) -> None: ...


@runtime_checkable
class KeyboardProvider(Protocol):
    @property
    def target_id(self) -> str: ...

    def key_press(self, key: "Key") -> None: ...

    def key_down(self, key: "Key") -> None: ...

    def key_up(self, key: "Key") -> None: ...

    def type_text(self, text: str) -> None: ...
