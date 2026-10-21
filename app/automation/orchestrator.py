"""Multi-target orchestration. Bounded workers, explicit targets, global stop.

Provider-neutral: jobs run on any already-verified `Target` (see `app.providers.target`), whatever provider
produced it. An empty target list raises NoTargetSelected; it never means "all". Modes: SEQUENTIAL,
SIMULTANEOUS (bounded pool), STAGGERED (start offset per target). One failing target does not stop the others,
but the emergency stop stops every one.

A provider's own selection step (for example `app.instances.selection.resolve_selection`, which also refuses two
selected instances that resolve to the same underlying device) is expected to have already turned a user's choice
into a list of verified targets before it reaches this module; this module knows nothing about how that choice
was made.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, NoTargetSelected, TargetNotVerified
from app.providers.target import Target, require_verified

MAX_WORKERS_LIMIT = 16


class Mode(Enum):
    SEQUENTIAL = "sequential"
    SIMULTANEOUS = "simultaneous"
    STAGGERED = "staggered"


class Status(Enum):
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class InstanceOutcome:
    target_id: str
    status: Status = Status.READY
    result: Any = None
    error: str | None = None


@dataclass
class RunReport:
    outcomes: dict[str, InstanceOutcome] = field(default_factory=dict)
    notification_errors: list[str] = field(default_factory=list)   # `on_status` callbacks that raised (never fatal)

    @property
    def all_completed(self) -> bool:
        return bool(self.outcomes) and all(o.status is Status.COMPLETED for o in self.outcomes.values())


def run_on_targets(targets: list[Target], job: Callable[[Target], Any], stop: EmergencyStop, *,
                   mode: Mode = Mode.SEQUENTIAL, max_workers: int = 4, stagger_s: float = 0.5,
                   on_status: Callable[[str, Status], None] | None = None) -> RunReport:
    if not targets:
        raise NoTargetSelected("No verified target was given; an empty list never means all instances.")
    for t in targets:
        require_verified(t)                        # provider-neutral: any Target whose provider has proven it
    # target_id is the report/callback key, so it must be unique across the whole call even across providers
    # (a provider's own selection step refuses a same-device collision within itself; see the module docstring).
    if len({t.target_id for t in targets}) != len(targets):
        raise TargetNotVerified("Duplicate targets given.", reason_code="duplicate_target")
    if not 1 <= max_workers <= MAX_WORKERS_LIMIT or stagger_s < 0:
        raise ValueError(f"max_workers must be 1..{MAX_WORKERS_LIMIT} and stagger_s >= 0")
    stop.check()
    report = RunReport({t.target_id: InstanceOutcome(t.target_id) for t in targets})
    lock = threading.Lock()

    def set_status(o: InstanceOutcome, s: Status) -> None:
        with lock:
            o.status = s
        if on_status:
            try:
                on_status(o.target_id, s)
            except Exception as exc:  # noqa: BLE001 - a broken observer must not change or abort the run
                with lock:
                    report.notification_errors.append(f"{o.target_id} {s.value}: {type(exc).__name__}: {exc}")

    def work(t: Target, delay: float) -> None:
        o = report.outcomes[t.target_id]
        if (delay and stop.wait(delay)) or stop.is_set():
            set_status(o, Status.STOPPED)
            return
        set_status(o, Status.RUNNING)
        try:
            o.result = job(t)
            set_status(o, Status.STOPPED if stop.is_set() else Status.COMPLETED)
        except EmergencyStopped as exc:
            o.error = str(exc)
            set_status(o, Status.STOPPED)
        except Exception as exc:  # noqa: BLE001 - isolate per-instance failures
            o.error = f"{type(exc).__name__}: {exc}"
            set_status(o, Status.FAILED)

    if mode is Mode.SEQUENTIAL:
        for t in targets:
            work(t, 0)
    else:
        workers = min(max_workers, len(targets))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="instance") as pool:
            futures = [pool.submit(work, t, i * stagger_s if mode is Mode.STAGGERED else 0)
                       for i, t in enumerate(targets)]
            for f in futures:
                f.result()
    return report
