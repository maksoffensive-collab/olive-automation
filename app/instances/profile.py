"""Logical instance profiles and the registry that holds them.

A profile ties a stable logical id to an LDPlayer index AND name. Both must match the live
`ldconsole list2` output before an instance can be verified. The ADB serial is NOT stored
here as truth: serials change, so they are rediscovered and re-verified every time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.core.errors import PlatformError
from app.ldplayer.console import LDInstance


class RegistryError(PlatformError):
    pass


@dataclass(frozen=True)
class InstanceProfile:
    logical_id: str
    ld_index: int
    ld_name: str
    display_name: str = ""
    group: str | None = None
    game_package: str | None = None
    notes: str = ""

    def label(self) -> str:
        return self.display_name or self.ld_name


class InstanceRegistry:
    def __init__(self, profiles: Iterable[InstanceProfile] = ()) -> None:
        self._by_id: dict[str, InstanceProfile] = {}
        for p in profiles:
            self.add(p)

    def add(self, profile: InstanceProfile) -> None:
        if not profile.logical_id.strip():
            raise RegistryError("A logical instance id must not be empty.")
        if profile.logical_id in self._by_id:
            raise RegistryError(f"Logical id '{profile.logical_id}' is already used.")
        for other in self._by_id.values():
            if other.ld_index == profile.ld_index:
                raise RegistryError(
                    f"LDPlayer instance {profile.ld_index} is already registered as '{other.logical_id}'.")
        self._by_id[profile.logical_id] = profile

    def get(self, logical_id: str) -> InstanceProfile | None:
        return self._by_id.get(logical_id)

    def all(self) -> list[InstanceProfile]:
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    @classmethod
    def from_discovery(cls, instances: Iterable[LDInstance]) -> "InstanceRegistry":
        """One profile per discovered instance. Ids are 'ld<index>'. No game or group is assumed."""
        return cls(InstanceProfile(f"ld{i.index}", i.index, i.name) for i in instances)
