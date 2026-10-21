"""What the GUI needs from the platform, as one provider-neutral interface.

The GUI (controller and views) never touches an operating system, a device or a provider package. It talks to an
`AutomationServices` object that a composition root (`app.bootstrap`) builds around ONE provider. Every method
maps onto an existing application-layer object: target discovery / verification, the provider's screenshot
provider, the provider's recorder and the `PlaybackEngine` bound to the provider's own input session. There is no
method that clicks, types, reads the pointer or addresses "the foreground window": whatever is not on this
interface cannot be done from the GUI.

`TargetEntry.ref` is an opaque token owned by the services that produced it. The GUI passes it back unchanged and
never interprets it (for a window provider it stands for a handle, which therefore never enters GUI code).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.providers.base import Screenshot
from app.providers.recording import Recorder
from app.providers.target import Target


@dataclass(frozen=True)
class TargetEntry:
    """One discovered (NOT yet verified) candidate, described for humans.

    `group` is a human-readable label a provider assigns to organise its own candidates in a list -- e.g. many
    same-kind candidates (many device instances of one backend, say) can share one group instead of appearing as
    unrelated top-level rows, and a provider that also offers one always-available, non-item-specific context can
    put it in a group of its own, separate from the individual items it can also automate. It is informational
    only, exactly like `kind` or `size`: the GUI groups entries that share a `group` value under one heading, but
    never inspects the string to decide what a target IS or what it can do -- that is entirely
    `Target.capabilities`' job."""
    ref: str            # opaque token, meaningful only to the services that issued it
    title: str
    process: str        # executable / program name
    kind: str           # window class or the provider's equivalent
    size: str           # human readable, e.g. "800 x 600"
    provider_id: str
    group: str          # e.g. "Full Windows", "Open Windows"; a device-backed provider might use its device group
    background: bool = False
    """Whether this target can be driven while its window is minimised or covered.

    A provider that reaches its target over its own connection (a device on the other end of a link, say) sets
    this; one that drives a real on-screen window with real system input cannot, because that input follows the
    focus and the Z order. The GUI only SHOWS it, so a person can tell two same-named candidates apart before
    choosing one -- it is never used to decide what a target can do, which remains `Target.capabilities`' job.
    The default is the cautious answer, so a provider that has not thought about it is never advertised as
    something it may not be."""

    whole_screen: bool = False
    """Whether input to this target lands wherever it points on this computer's whole screen (not in one window
    or device). Shown to the person, so choosing it is a choice made knowingly; never used to decide anything."""

    concurrent: bool = False
    """Whether a run on THIS target may proceed at the same time as a run on another one.

    Deliberately separate from `background`: not needing a window in front says nothing about whether two runs
    can share the provider safely. A provider sets this only when two of its targets are genuinely independent
    end to end -- separate sessions, separate connections, nothing serialised between them -- so a scheduler may
    overlap them. A provider that drives one shared, exclusive resource (the system's own focus and pointer, a
    single physical channel) leaves it at the default, and the scheduler then runs its targets strictly one at a
    time. The GUI reads this flag instead of asking which provider something came from."""


@runtime_checkable
class AutomationServices(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def stop(self) -> EmergencyStop:
        """The single emergency stop shared by every component this provider builds."""

    def discover(self) -> list[TargetEntry]:
        """Candidates only. Nothing is verified, selected or touched."""

    def verify(self, entry: TargetEntry) -> Target:
        """Prove the identity of exactly this candidate. Raises TargetNotVerified."""

    def recheck(self, target: Target) -> TargetEntry:
        """Re-run the provider's identity proof; return fresh human information or raise TargetNotVerified."""

    def capture(self, target: Target) -> Screenshot: ...

    def create_recorder(self, target: Target) -> Recorder:
        """A fresh recorder bound to that one target (not started)."""

    def create_playback(self, macro: Macro, target: Target, *, dry_run: bool,
                        allow_different_target: bool) -> PlaybackEngine:
        """A playback engine bound to that one target and the provider's own input session (not started)."""

    def guard_run(self, target: Target) -> AbstractContextManager:
        """Entered around a whole LIVE run on that target (a script, waits included): whatever the provider needs to
        let the person take over -- an emergency stop on their own input, say. Raises when that cannot be set up,
        and then nothing runs. Most targets need nothing (`contextlib.nullcontext()`)."""
