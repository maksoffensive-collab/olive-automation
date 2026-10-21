"""Which macros the list shows, and in what order: scope + search + sort, as one pure function.

Kept out of both the view and the controller so that "what the user is looking at" can be tested directly and so
that the reason a macro is or is not on screen is always one readable rule. The view never filters a list itself.

A scope answers "which macros", a query narrows it, a sort orders it. `FOLDER` is a real folder on disk;
`FAVORITES` and `RECENT` are views over the same files, never copies of them.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.gui.library import MacroInfo

RECENT_LIMIT = 10


class Scope(str, Enum):
    ALL = "all"
    FAVORITES = "favorites"
    RECENT = "recent"
    FOLDER = "folder"


class Sort(str, Enum):
    NAME = "name"                  # A -> Z
    NAME_DESC = "name_desc"        # Z -> A
    MODIFIED = "modified"          # most recently changed first
    MODIFIED_OLD = "modified_old"
    LAST_RUN = "last_run"          # most recently run first; never-run macros last
    ACTIONS = "actions"            # most actions first


@dataclass(frozen=True)
class Filter:
    scope: Scope = Scope.ALL
    folder: str = ""               # only meaningful when scope is FOLDER
    query: str = ""
    sort: Sort = Sort.MODIFIED

    def cleared(self) -> "Filter":
        """What "reset the current selection" restores: everything, unsorted-by-nothing, no search."""
        return Filter()

    def with_scope(self, scope: Scope, folder: str = "") -> "Filter":
        return replace(self, scope=scope, folder=folder if scope is Scope.FOLDER else "")


def _matches(info: MacroInfo, query: str) -> bool:
    if not query:
        return True
    needle = query.casefold().strip()
    return needle in info.name.casefold() or needle in info.folder.casefold()


def _in_scope(info: MacroInfo, flt: Filter, favorites: frozenset[str]) -> bool:
    if flt.scope is Scope.FAVORITES:
        return info.key in favorites
    if flt.scope is Scope.FOLDER:
        return info.folder == flt.folder
    return True


def _sort_key(sort: Sort, last_runs: dict[str, float]):
    if sort is Sort.NAME:
        return lambda i: (i.name.casefold(), i.key)
    if sort is Sort.NAME_DESC:
        return lambda i: (_Reversed(i.name.casefold()), i.key)
    if sort is Sort.MODIFIED_OLD:
        return lambda i: (i.modified, i.name.casefold())
    if sort is Sort.LAST_RUN:
        return lambda i: (-last_runs.get(i.key, 0.0), i.name.casefold())
    if sort is Sort.ACTIONS:
        return lambda i: (-i.actions, i.name.casefold())
    return lambda i: (-i.modified, i.name.casefold())          # Sort.MODIFIED


class _Reversed:
    """Sorts strings backwards without reversing the whole list (which would also flip the tie-breaker)."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_Reversed") -> bool:
        return other.value < self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reversed) and other.value == self.value


def visible(infos: list[MacroInfo], flt: Filter, *, favorites: frozenset[str] = frozenset(),
            last_runs: dict[str, float] | None = None) -> list[MacroInfo]:
    """The macros to show, in order. RECENT is "the newest few, whatever folder they are in", so it ignores the
    chosen sort on purpose -- a "recent" list in another order would not be recent."""
    runs = last_runs or {}
    if flt.scope is Scope.RECENT:
        newest = sorted(infos, key=lambda i: (-i.modified, i.name.casefold()))
        return [i for i in newest if _matches(i, flt.query)][:RECENT_LIMIT]
    chosen = [i for i in infos if _in_scope(i, flt, favorites) and _matches(i, flt.query)]
    return sorted(chosen, key=_sort_key(flt.sort, runs))


def counts(infos: list[MacroInfo], favorites: frozenset[str] = frozenset()) -> dict[str, int]:
    """Totals for the navigation: how many macros each scope would show (folders by name)."""
    out = {Scope.ALL.value: len(infos), Scope.FAVORITES.value: sum(1 for i in infos if i.key in favorites),
           Scope.RECENT.value: min(len(infos), RECENT_LIMIT)}
    for info in infos:
        if info.folder:
            out[info.folder] = out.get(info.folder, 0) + 1
    return out
