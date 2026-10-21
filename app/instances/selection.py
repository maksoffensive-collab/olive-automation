"""Explicit target selection. An empty selection means NO target, never "all"."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.errors import NoTargetSelected, TargetNotVerified
from app.instances.mapping import MappingService, VerifiedTarget
from app.instances.profile import InstanceRegistry


class SelectionMode(Enum):
    NONE = "none"
    EXPLICIT = "explicit"
    ALL = "all"


@dataclass(frozen=True)
class Selection:
    mode: SelectionMode = SelectionMode.NONE
    ids: tuple[str, ...] = ()

    @classmethod
    def none(cls) -> "Selection":
        return cls(SelectionMode.NONE, ())

    @classmethod
    def of(cls, *ids: str) -> "Selection":
        """Explicit ids. Blank or missing ids give NONE (never ALL)."""
        clean = tuple(dict.fromkeys(i.strip() for i in ids if isinstance(i, str) and i.strip()))
        return cls(SelectionMode.EXPLICIT, clean) if clean else cls.none()

    @classmethod
    def all_verified(cls) -> "Selection":
        """The only way to mean 'all instances'. Still verifies each one individually."""
        return cls(SelectionMode.ALL, ())

    @property
    def is_empty(self) -> bool:
        return self.mode is SelectionMode.NONE


@dataclass
class SelectionResult:
    targets: list[VerifiedTarget] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)  # logical id -> reason shown to the user

    def require_any(self) -> list[VerifiedTarget]:
        if not self.targets:
            detail = "; ".join(f"{k}: {v}" for k, v in self.failures.items())
            raise NoTargetSelected("No verified target is available." + (f" ({detail})" if detail else ""))
        return self.targets

    def require_all(self) -> list[VerifiedTarget]:
        """All requested instances verified, or raise. Use when partial execution is not acceptable."""
        if self.failures:
            raise TargetNotVerified("Not all selected instances could be verified: "
                                    + "; ".join(f"{k}: {v}" for k, v in self.failures.items()),
                                    reason_code="partial")
        return self.require_any()


def resolve_selection(selection: Selection, registry: InstanceRegistry, mapping: MappingService) -> SelectionResult:
    """Turn a Selection into verified targets. Nothing is verified for an empty selection."""
    if selection.mode is SelectionMode.NONE:
        raise NoTargetSelected("No instance is selected. Choose one or more instances, or use Select All explicitly.")
    if selection.mode is SelectionMode.ALL:
        ids = [p.logical_id for p in registry.all()]
    else:
        ids = list(selection.ids)
    result = SelectionResult()
    for logical_id in ids:
        try:
            result.targets.append(mapping.verify(logical_id))
        except TargetNotVerified as exc:
            result.failures[logical_id] = str(exc)
    serials = [t.adb_serial for t in result.targets]
    if len(set(serials)) != len(serials):
        raise TargetNotVerified("Two selected instances resolved to the same ADB serial; refusing to continue.",
                                reason_code="serial_collision")
    return result
