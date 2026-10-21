"""Run history: what actually happened, in the user's terms, with the technical detail kept out of the way.

One append-only JSON file. A record says WHAT ran, WHERE it ran, how it ended and how far it got; the technical
`detail` (a provider error code, a preflight reason) is stored too but is meant to stay behind a "show details"
control, never in the ordinary list -- a person reading their history wants "stopped after 14 of 42 actions", not
a traceback.

Outcomes are a closed vocabulary so the list can be filtered and coloured without parsing prose. `PARTIAL` exists
because one run can cover several targets and they do not have to agree: three targets where one failed is
neither "completed" nor "failed".

Best-effort storage, like the other GUI-side files: history that cannot be read or written never blocks a run.
The file is capped at `MAX_RECORDS`; the oldest entries fall off.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAX_RECORDS = 500

COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"
REFUSED = "refused"
PARTIAL = "partial"
OUTCOMES = (COMPLETED, PARTIAL, FAILED, STOPPED, REFUSED)


@dataclass(frozen=True)
class TargetResult:
    """What happened on ONE target of a run. A run can cover many targets that do not agree with each other, so
    the aggregate outcome alone cannot say which one failed."""
    target: str
    outcome: str
    completed: int = 0
    total: int = 0
    detail: str = ""

    @classmethod
    def from_dict(cls, data: object) -> "TargetResult | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(str(data["target"]), str(data["outcome"]), int(data.get("completed", 0)),
                       int(data.get("total", 0)), str(data.get("detail", "")))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RunRecord:
    at: float                                  # unix time the run ENDED
    macro: str                                 # display name at the time of the run
    macro_key: str                             # MacroInfo.key, so a macro's own runs can be found again
    outcome: str                               # the aggregate: what the run as a whole did
    targets: tuple[str, ...] = ()              # display names of the targets it ran on
    duration_s: float = 0.0
    actions_total: int = 0
    completed: int = 0
    detail: str = ""                           # technical; shown only behind "show details"
    results: tuple[TargetResult, ...] = ()     # per target; empty in records written before runs could overlap

    @property
    def target_count(self) -> int:
        return len(self.targets)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["targets"] = list(self.targets)
        data["results"] = [asdict(r) for r in self.results]
        return data

    @classmethod
    def from_dict(cls, data: object) -> "RunRecord | None":
        """Anything that is not a record this version understands is dropped, never repaired: a damaged history
        file must not be able to crash the list or invent a run that never happened."""
        if not isinstance(data, dict):
            return None
        try:
            outcome = str(data["outcome"])
            if outcome not in OUTCOMES:
                return None
            targets = data.get("targets") or []
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                return None
            raw_results = data.get("results")
            if not isinstance(raw_results, list):
                raw_results = []            # a record written before runs could overlap simply has none
            results = tuple(r for r in (TargetResult.from_dict(item) for item in raw_results) if r is not None)
            return cls(float(data["at"]), str(data["macro"]), str(data.get("macro_key", "")), outcome,
                       tuple(targets), float(data.get("duration_s", 0.0)), int(data.get("actions_total", 0)),
                       int(data.get("completed", 0)), str(data.get("detail", "")), results)
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class RunHistory:
    path: Path | None = None
    _records: list[RunRecord] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path) if self.path is not None else None
        self._records = self._read()

    def _read(self) -> list[RunRecord]:
        if self.path is None:
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        records = [r for r in (RunRecord.from_dict(item) for item in raw) if r is not None]
        return records[-MAX_RECORDS:]

    def _write(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps([r.as_dict() for r in self._records], indent=2, ensure_ascii=False)
                                 + "\n", encoding="utf-8")
        except OSError:
            pass

    # ---- reading -------------------------------------------------------------------------------
    def list(self) -> list[RunRecord]:
        """Newest first."""
        return sorted(self._records, key=lambda r: r.at, reverse=True)

    def for_macro(self, macro_key: str, limit: int = 0) -> list[RunRecord]:
        runs = [r for r in self.list() if r.macro_key and r.macro_key == macro_key]
        return runs[:limit] if limit else runs

    def last_run_times(self) -> dict[str, float]:
        """macro key -> when it last ran, for "last run" columns and sorting."""
        out: dict[str, float] = {}
        for record in self._records:
            if record.macro_key and record.at > out.get(record.macro_key, 0.0):
                out[record.macro_key] = record.at
        return out

    # ---- writing -------------------------------------------------------------------------------
    def add(self, record: RunRecord) -> RunRecord:
        self._records.append(record)
        del self._records[:-MAX_RECORDS]
        self._write()
        return record

    def record(self, *, macro: str, macro_key: str, outcome: str, targets: tuple[str, ...] = (),
               duration_s: float = 0.0, actions_total: int = 0, completed: int = 0, detail: str = "",
               results: tuple[TargetResult, ...] = (), at: float | None = None) -> RunRecord:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        return self.add(RunRecord(time.time() if at is None else at, macro, macro_key, outcome, tuple(targets),
                                  duration_s, actions_total, completed, detail, tuple(results)))

    def rename_key(self, old: str, new: str) -> None:
        """Keep a macro's past runs attached to it after it is renamed or moved."""
        if old == new or not any(r.macro_key == old for r in self._records):
            return
        self._records = [RunRecord(r.at, r.macro, new, r.outcome, r.targets, r.duration_s, r.actions_total,
                                   r.completed, r.detail, r.results) if r.macro_key == old else r
                         for r in self._records]
        self._write()

    def clear(self) -> None:
        self._records = []
        self._write()


def day_key(at: float) -> str:
    """Local calendar day of a run, as YYYY-MM-DD: the view groups the list by this and translates today /
    yesterday itself."""
    return time.strftime("%Y-%m-%d", time.localtime(at))
