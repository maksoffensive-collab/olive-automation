"""Provider-independent contract for a verified target.

A *target* is whatever a provider can act on: a device, an application, a window, a screen region, ... This module
defines only what every kind of target has in common, so that generic code (orchestration, playback, GUI, vision)
can hold, log, compare and gate on targets without knowing which technology is behind them:

  Capability          an explicit, closed vocabulary of things a target can do. Nothing is ever inferred from the
                      provider id: a target can do exactly what its `capabilities` set says.
  VerificationState   whether the provider has proven the target's identity.
  TargetVerification  the provenance of that proof: state, the names of the independent checks that passed, and
                      a monotonic timestamp. The names are labels for logs and audits, never addresses.
  Target              abstract base: `identity` (provider id + logical target id + display name), `capabilities`,
                      `verification`. A provider adapts its own proof type by subclassing it.
  require_verified / require_capability
                      the gates generic code uses before acting.

The contract deliberately has no field for a handle, address, title, process or any other provider-specific
locator: those stay inside the provider that owns the proof. How strong the proof must be (for example how many
independent checks) is the provider's policy; the contract only records what was done.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from app.core.errors import CapabilityNotAvailable, TargetNotVerified
from app.providers.base import TargetIdentity


class Capability(str, Enum):
    """Closed vocabulary. Adding a member is an architectural decision, not a provider-local one."""
    SCREENSHOT = "screenshot"
    TAP = "tap"                          # one primary-button click at a point
    SECONDARY_TAP = "secondary_tap"      # one secondary-button click at a point
    DOUBLE_TAP = "double_tap"            # one primary-button double click at a point
    POINTER_MOVE = "pointer_move"        # move the pointer to a point (no button change)
    POINTER_BUTTON = "pointer_button"    # press / release the primary button at a point
    KEY = "key"                          # press / release one key (see app.providers.input.Key)
    TEXT = "text"                        # type a short printable string
    RECORD = "record"                    # observe the user's actions in this target (see providers/recording.py)


class VerificationState(Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


@dataclass(frozen=True)
class TargetVerification:
    state: VerificationState
    methods: tuple[str, ...] = ()   # labels of the independent checks that passed (informational)
    verified_at: float = 0.0        # time.monotonic() when the proof was made

    def __post_init__(self) -> None:
        if not isinstance(self.state, VerificationState):
            raise ValueError("state must be a VerificationState.")
        if not all(isinstance(m, str) and m.strip() for m in self.methods):
            raise ValueError("Verification methods must be non-empty strings.")
        if self.state is VerificationState.VERIFIED and not self.methods:
            raise ValueError("A VERIFIED target must name at least one verification method.")

    @property
    def is_verified(self) -> bool:
        return self.state is VerificationState.VERIFIED

    def age_s(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.verified_at


class Target(ABC):
    """What generic code knows about a target. Implemented by each provider's own proof type."""

    @property
    @abstractmethod
    def identity(self) -> TargetIdentity: ...

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Exactly what this target can do; nothing else may be assumed from its provider."""

    @property
    @abstractmethod
    def verification(self) -> TargetVerification: ...

    @property
    def provider_id(self) -> str:
        return self.identity.provider_id

    @property
    def target_id(self) -> str:
        return self.identity.target_id

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def describe(self) -> str:
        i = self.identity
        name = f" ({i.display_name})" if i.display_name else ""
        return f"{i.provider_id}:{i.target_id}{name} [{self.verification.state.value}]"


def require_verified(target: object) -> Target:
    """Refuse anything that is not a Target whose provider has proven its identity."""
    if not isinstance(target, Target):
        raise TargetNotVerified("A verified Target is required.", reason_code="not_a_target")
    caps = target.capabilities
    if not isinstance(caps, frozenset) or not all(isinstance(c, Capability) for c in caps):
        raise TargetNotVerified("A Target must declare its capabilities as a frozenset of Capability.",
                                reason_code="bad_capabilities")
    if not target.verification.is_verified:
        raise TargetNotVerified(f"Target {target.describe()} is not verified.", reason_code="not_verified_target")
    return target


def require_capability(target: object, capability: Capability) -> Target:
    """Refuse a target that does not explicitly declare `capability` (also refuses unverified targets)."""
    verified = require_verified(target)
    if not verified.has_capability(capability):
        raise CapabilityNotAvailable(f"Target {verified.describe()} does not declare the '{capability.value}' capability.")
    return verified
