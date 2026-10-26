"""The target a macro or a script remembers, and finding it again (the person's list of 2026-10-25, item 12).

"A script remembers the target it was made on, a recorded macro too; Olive finds it again by itself and asks only
when it finds none or more than one." A window's handle and its process die with the program, so what is remembered
is what a person would recognise it by -- its provider, its program, its kind (the window class), its title -- and
finding it again is a match on those, among the targets listed now:

  * chosen by itself: the ONE listed target with the same provider, program, kind AND title. The title is part of
    the identity because the rest often is not one: the instances a device provider lists, every window of a
    browser, every app a system frame hosts share their program and kind (the independent review of 2026-10-26: a
    lone alike target with another title is another instance, another app -- choosing it would be a guess, and
    a target is never guessed);
  * only offered: when nothing matches so, but exactly one listed target has the same provider, program and kind
    (a game whose title shows its level, say), it is offered pre-ticked for one confirmation (`suggestion`);
  * the whole screen (Full Windows) matches the whole-screen entry of the same provider.

The match only CHOOSES which listed target to verify: the provider's own verification of it still comes before
anything is done, as for a target chosen by hand. Pure: no Tk, no files.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.gui.services import TargetEntry

MAX_TEXT = 400


@dataclass(frozen=True)
class Remembered:
    provider_id: str
    process: str
    kind: str
    title: str
    whole_screen: bool = False

    def as_dict(self) -> dict:
        return {"provider": self.provider_id, "process": self.process, "kind": self.kind, "title": self.title,
                "whole_screen": self.whole_screen}

    @classmethod
    def from_dict(cls, data: object) -> "Remembered | None":
        """Read back strictly: anything not exactly as written (a missing field, a number for a name, "false" for
        whole_screen) is no memory at all -- never coerced into another one."""
        if not isinstance(data, dict) or set(data) != {"provider", "process", "kind", "title", "whole_screen"}:
            return None
        values = (data["provider"], data["process"], data["kind"], data["title"])
        whole = data["whole_screen"]
        if not all(isinstance(v, str) and len(v) <= MAX_TEXT for v in values) or not values[0] \
                or not isinstance(whole, bool):
            return None
        return cls(*values, whole_screen=whole)

    def label(self) -> str:
        return self.title or self.process or self.provider_id


def remember(entry: TargetEntry) -> Remembered:
    return Remembered(entry.provider_id, entry.process, entry.kind, entry.title, entry.whole_screen)


def candidates(memory: Remembered, entries) -> list[TargetEntry]:
    """The listed targets that may be the remembered one (same provider, program and kind; for the whole screen,
    the whole-screen entry), best first: those with the same title before the others."""
    if memory.whole_screen:
        return [e for e in entries if e.whole_screen and e.provider_id == memory.provider_id]
    alike = [e for e in entries if not e.whole_screen and e.provider_id == memory.provider_id
             and e.process == memory.process and e.kind == memory.kind]
    return sorted(alike, key=lambda e: e.title != memory.title)


def find(memory: Remembered | None, entries) -> TargetEntry | None:
    """The one listed target that IS the remembered one -- the same title too -- or None."""
    if memory is None:
        return None
    found = candidates(memory, entries)
    if memory.whole_screen:
        return found[0] if len(found) == 1 else None
    same_title = [e for e in found if e.title == memory.title]
    return same_title[0] if len(same_title) == 1 else None


def suggestion(memory: Remembered | None, entries) -> TargetEntry | None:
    """When `find` finds nothing: the one listed target that only LOOKS like the remembered one (the same provider,
    program and kind, another title), to offer for a confirmation -- never to choose by itself. Else None."""
    if memory is None or find(memory, entries) is not None:
        return None
    found = candidates(memory, entries)
    return found[0] if len(found) == 1 else None
