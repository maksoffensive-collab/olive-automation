"""A single `AutomationServices` that fans discovery and every subsequent operation out across several concrete
providers, so ONE running application can see and act on targets from more than one provider at once (e.g.
Windows and LDPlayer together) without the GUI or `AppController` ever knowing that more than one provider
exists. Nothing above this file changes: `App`/`AppController` already only depend on the `AutomationServices`
protocol (app/gui/services.py), never on a concrete provider, so `CompositeAutomationServices` is just one more
thing that satisfies it -- the same way `WindowsServices`/`LDPlayerServices` already do.

`--provider` (app/bootstrap/main.py) still exists, unchanged, as an explicit override that builds exactly one
concrete provider for a deliberately single-provider run (debugging, a test, or a machine where only one provider
should ever be touched); the normal, no-flag launch instead builds one of these over every provider that is
actually available on this machine.

Two things this class must get right, both because several providers now share one process:

  * `TargetEntry.ref` is "opaque, meaningful only to the services that issued it" (see app/gui/services.py) --
    once several providers' entries sit in the SAME list, their raw refs are no longer guaranteed distinct from
    each other. Every entry's `ref` is re-namespaced on the way out and reversed on the way back in (`_by_ref`),
    so `AppController.select(ref)` -- which looks entries up by `ref` alone -- keeps working unmodified, and a
    ref from one provider can never be mistaken for one from another.
  * Every verified target already carries its own `provider_id` (`Target.identity`, an existing, generic field);
    verify()/recheck()/capture()/create_recorder()/create_playback() all dispatch on that alone to find the ONE
    sub-service that actually owns the target. This is the only "routing" this class does: it never inspects
    `TargetEntry.group`, which stays exactly what it already was -- a provider's own presentation label (e.g.
    every LDPlayer instance already reports group="LDPlayer") -- a group is a display concept, never a provider
    and never something this class invents, merges or overrides.

Safety: every sub-service must already share the SAME `EmergencyStop` instance (construct each with the same
`stop=` before handing them here) -- verified in `__init__`, not assumed, because a composite whose Emergency
Stop button only actually stopped one of its providers would be worse than not having this class at all.
"""
from __future__ import annotations

from dataclasses import replace

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, TargetNotVerified
from app.gui.services import AutomationServices, TargetEntry
from app.playback.engine import PlaybackEngine
from app.playback.macro import Macro
from app.providers.base import Screenshot
from app.providers.recording import Recorder
from app.providers.target import Target

_SEP = "\x1f"   # unit separator: never typed by a user or produced by any provider's own ref scheme


class CompositeAutomationServices:
    """Combines `services` (each already satisfying `AutomationServices`) into one. Construction order is
    preserved in `discover()`'s output, so callers that care about a stable "first provider" ordering get one."""

    def __init__(self, services: list[AutomationServices]) -> None:
        if not services:
            raise ValueError("CompositeAutomationServices needs at least one provider.")
        self._services = list(services)
        self._by_provider = {s.provider_id: s for s in self._services}
        if len(self._by_provider) != len(self._services):
            raise ValueError("Two of the given services report the same provider_id.")
        stops = {id(s.stop) for s in self._services}
        if len(stops) != 1:
            raise ValueError("All composed services must share exactly one EmergencyStop instance "
                             "(construct each with the same stop= before combining them).")
        self._stop: EmergencyStop = self._services[0].stop
        self._by_ref: dict[str, tuple[AutomationServices, TargetEntry]] = {}

    @property
    def provider_id(self) -> str:
        """Informational display text only (e.g. the Settings page): never parsed, matched or dispatched on."""
        return " + ".join(sorted(self._by_provider))

    @property
    def stop(self) -> EmergencyStop:
        return self._stop

    def _service_for(self, provider_id: str) -> AutomationServices:
        service = self._by_provider.get(provider_id)
        if service is None:
            raise SafetyViolation(f"No configured provider matches '{provider_id}'.")
        return service

    def _namespaced(self, service: AutomationServices, entry: TargetEntry) -> TargetEntry:
        ref = f"{service.provider_id}{_SEP}{entry.ref}"
        namespaced = replace(entry, ref=ref)
        self._by_ref[ref] = (service, entry)
        return namespaced

    def discover(self) -> list[TargetEntry]:
        """Every provider is asked in turn; one provider's discovery failing never blinds the others -- it
        simply lists nothing this time, exactly as it would if it had genuinely found no targets. The emergency stop
        is not such a failure: it is raised as the stop (a list emptied by it would drop every chosen target).

        The list rows the window holds stay valid only together with the table that issued them: while the stop is
        set nothing is listed again (no provider's table changes), and a stop that arrives after an earlier provider
        already listed again leaves NO row resolvable -- every old row is refused until a listing succeeds, never
        resolved through another discovery's table (a window handle reused meanwhile would name another window;
        the second review of 2026-10-27)."""
        self._stop.check()
        self._by_ref = {}
        out: list[TargetEntry] = []
        for service in self._services:
            try:
                entries = service.discover()
            except EmergencyStopped:
                self._by_ref = {}
                raise
            except Exception:
                continue
            out.extend(self._namespaced(service, e) for e in entries)
        return out

    def verify(self, entry: TargetEntry) -> Target:
        found = self._by_ref.get(entry.ref)
        if found is None:
            raise TargetNotVerified("That target is not part of the current list. Refresh and select it again.",
                                    reason_code="invalid_handle")
        service, original = found
        return service.verify(original)

    def recheck(self, target: Target) -> TargetEntry:
        service = self._service_for(target.provider_id)
        return self._namespaced(service, service.recheck(target))

    def capture(self, target: Target) -> Screenshot:
        return self._service_for(target.provider_id).capture(target)

    def create_recorder(self, target: Target) -> Recorder:
        return self._service_for(target.provider_id).create_recorder(target)

    def create_playback(self, macro: Macro, target: Target, *, dry_run: bool,
                        allow_different_target: bool) -> PlaybackEngine:
        return self._service_for(target.provider_id).create_playback(
            macro, target, dry_run=dry_run, allow_different_target=allow_different_target)

    def guard_run(self, target: Target):
        return self._service_for(target.provider_id).guard_run(target)

    def bring_to_front(self, target: Target, *, activate: bool = False) -> bool:
        return self._service_for(target.provider_id).bring_to_front(target, activate=activate)

    def screen_rect(self, target: Target):
        return self._service_for(target.provider_id).screen_rect(target)

    def escape_watch(self, entries):
        """The first provider that watches the keyboard (whatever provider the targets are from: Esc is the
        person's, wherever it is pressed)."""
        for service in self._services:
            watch = service.escape_watch(entries)
            if watch is not None:
                return watch
        return None
