"""Running WAIT UNTIL and IF/THEN/ELSE steps on ONE target, through the existing action path and nothing else.

    observe (ObservationContext) -> Condition -> Decision -> Macro -> create_playback -> PlaybackEngine
                                                                        preflight, the provider's own input
                                                                        session, its budget, the stop

This is the only module in the package that can cause input, and it can do so in exactly one way: it hands a
`Macro` to the `create_playback` factory the caller passes in -- the same factory every provider already exposes
for ordinary macro runs (`AutomationServices.create_playback`) -- and starts the engine that comes back. So an
action chosen by a decision gets everything a recorded macro gets, with no second route around any of it:

    target check     preflight refuses a macro for another target (a match seen on target A cannot become a click
                     on target B: the tap macro is stamped with A), a wrong provider, an unverified target;
    capability       preflight refuses an operation the target does not declare, before any input;
    input session    built fresh by the provider for this one macro, re-verifying its target on every operation;
    budget           the session's budget is exactly the macro's action count;
    stop / cancel    the engine checks the shared emergency stop before and during every action; this run's own
                     cancel is forwarded to the engine as an ordinary cancel.

There is no input session, no tap and no provider in this module; a test asserts that.

A script is finite: at most `MAX_STEPS` steps and `MAX_BLOCKS` blocks in all (the ones inside branches included),
each run at most once and each bounded (a wait by its timeout, a decision by one observation, a macro by its own actions), and branches nest at
most `MAX_NESTING` deep -- so a run cannot loop and cannot run forever. A chosen branch runs its blocks in order, then
the run continues after the IF. A start delay (`start_delay_s`, at most `MAX_START_DELAY_S`, skipped by a dry run)
waits before the first step, in slices that the emergency stop and this run's cancel end at once; `while_acting`, the
caller's own context, is entered only after it -- so a watch for the person taking over starts when the steps begin,
not while nothing is being done. It ends as

    COMPLETED   every step ran;
    TIMED_OUT   a WAIT UNTIL expired (the steps after it do not run: they were written to follow that condition);
    CANCELLED   the emergency stop (`reason="emergency_stop"`) or this run's cancel (`reason="cancelled"`);
    FAILED      the screen could not be observed, a step was invalid, preflight refused the action, or the action
                itself failed. `reason` names which, `issues` carries preflight's own reasons. Text that could not
                be read is `ocr_unavailable`, or `ocr_language_unavailable` when the requested language is the
                reason -- never `timeout`, which only a WAIT UNTIL that ran out of time reports.

Per-target isolation: everything a run uses is passed in -- the target, ITS observation context, the playback
factory, the stop, its cancel event -- and the module keeps nothing between calls. N targets run as N independent
calls (typically N threads) that share only what the platform already shares: the one emergency stop.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from contextlib import AbstractContextManager, nullcontext
from typing import Callable, Sequence

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError, SafetyViolation
from app.observe.context import ObservationContext
from app.observe.errors import (InvalidQuery, ObservationCancelled, ObservationUnavailable, OcrLanguageUnavailable,
                                OcrUnavailable)
from app.playback.engine import ExecutionReport, PlaybackRefused, PlaybackState, PlaybackStateError
from app.playback.macro import Macro
from app.playback.preflight import Issue
from app.providers.size import SizeChanged
from app.providers.target import Target, require_verified

from .condition import ConditionResult, check_cancel, evaluate
from .decision import Choice, Decision, RunMacro, TapMatch, count_blocks, decide, walk
from .errors import ActionCancelled, ActionFailed, ActionRefused, Cancelled, DecisionError, InvalidDecision
from .wait import WaitResult, WaitUntil, _sleep, wait_until

MAX_STEPS = 100                     # steps of a script (its top level)
MAX_BLOCKS = 3 * MAX_STEPS          # blocks in all, the ones inside branches included: every script written before
#                                     branches could hold more than one block (100 steps, an IF's two) still fits
# A start delay is bounded like every wait, by its own bound: 24 hours, the person's decision of 2026-10-20 (it was
# 10 minutes, the bound of a WAIT UNTIL, which stays). Still a bound: a typo can never hold a target for days.
MAX_START_DELAY_S = 24 * 60 * 60
POLL_S = 0.05

# create_playback(macro, target, *, dry_run, allow_different_target) -> an engine that has not started
PlaybackFactory = Callable[..., object]
Step = WaitUntil | Decision | RunMacro        # RunMacro as a step: run that macro unconditionally


class FlowState(str, Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class StepReport:
    index: int                                  # the top-level step this block belongs to
    kind: str                                   # "wait", "decision" or "run"
    wait: WaitResult | None = None
    condition: ConditionResult | None = None    # a decision's answer
    branch: str | None = None                   # "then" / "else" for a decision
    action: ExecutionReport | None = None       # the playback report of the macro that ran, if one did: for a
    #                                             decision, the first block of its branch when that is a macro or a
    #                                             click on the match (the blocks after it have reports of their own)
    path: tuple = ()                            # where the block is: (2,), (2, "then", 1), ...

    def diagnostics(self) -> dict:
        out: dict = {"index": self.index, "kind": self.kind, "path": list(self.path or (self.index,))}
        if self.wait is not None:
            out["wait"] = self.wait.diagnostics()
        if self.condition is not None:
            out["condition"] = self.condition.diagnostics()
            out["branch"] = self.branch
        if self.action is not None:
            out["action"] = {"state": self.action.state.value, "completed": self.action.completed_count,
                             "total": self.action.actions_total,
                             "steps": [s.description for s in self.action.steps]}
        return out


@dataclass(frozen=True)
class FlowReport:
    state: FlowState
    target_id: str
    steps: tuple[StepReport, ...]
    reason: str | None = None                   # a code: timeout, emergency_stop, cancelled, action_refused, ...
    message: str = ""
    issues: tuple[Issue, ...] = field(default_factory=tuple)
    completed: int = 0                          # top-level steps that finished (all of them when COMPLETED)

    @property
    def ok(self) -> bool:
        return self.state is FlowState.COMPLETED


class _End(Exception):
    """Internal: stop the step loop with this outcome."""

    def __init__(self, state: FlowState, reason: str, message: str = "", issues: tuple[Issue, ...] = ()) -> None:
        super().__init__(message)
        self.state, self.reason, self.message, self.issues = state, reason, message, issues


def _check_binding(target: object, context: object) -> Target:
    verified = require_verified(target)
    if not isinstance(context, ObservationContext):
        raise InvalidDecision("A run needs the ObservationContext of its target.")
    if (context.provider_id, context.target_id) != (verified.provider_id, verified.target_id):
        raise SafetyViolation("The observation context looks at a different target than the one that would act; "
                              "a run observes and acts on the same target or does nothing.")
    return verified


def _check_steps(steps: Sequence[Step]) -> tuple[Step, ...]:
    steps = tuple(steps)
    if not steps:
        raise InvalidDecision("A run needs at least one step.")
    for step in steps:
        if not isinstance(step, (WaitUntil, Decision, RunMacro)):
            raise InvalidDecision(f"Not a step: {type(step).__name__}. Steps are WaitUntil, Decision or RunMacro.")
    if len(steps) > MAX_STEPS:
        raise InvalidDecision(f"A run has at most {MAX_STEPS} steps.")
    if count_blocks(steps) > MAX_BLOCKS:
        raise InvalidDecision(f"A run has at most {MAX_BLOCKS} blocks, the ones inside branches included.")
    return steps


def adapting(items: tuple) -> tuple:
    """The blocks with every recorded macro they run set to adapt to the size -- what a script that adapts asks of
    the macros inside it (the provider scales their clicks to the size the target has when they run)."""
    out = []
    for item in items:
        if isinstance(item, RunMacro) and not item.macro.adapt:
            item = replace(item, macro=replace(item.macro, adapt=True))
        elif isinstance(item, Decision):
            item = replace(item, then=adapting(item.then), otherwise=adapting(item.otherwise))
        out.append(item)
    return tuple(out)


def check_start_delay(delay_s: object) -> float:
    """A start delay is 0 (none) to MAX_START_DELAY_S seconds."""
    if isinstance(delay_s, bool) or not isinstance(delay_s, (int, float)) or not 0 <= delay_s <= MAX_START_DELAY_S:
        raise InvalidDecision(f"The start delay is 0 to {MAX_START_DELAY_S:g} seconds.")
    return float(delay_s)


def act(macro: Macro, target: Target, create_playback: PlaybackFactory, *, stop: EmergencyStop,
        cancel: threading.Event | None = None, dry_run: bool = False,
        allow_different_target: bool = False) -> ExecutionReport:
    """Run one macro through the provider's own playback path and wait for it to end.

    Raises `PlaybackRefused` (preflight said no; nothing was sent), `EmergencyStopped` or `Cancelled` if either
    is already set before the engine is built. Once the engine runs, its own report is the outcome."""
    stop.check()
    check_cancel(cancel)
    engine = create_playback(macro, target, dry_run=dry_run, allow_different_target=allow_different_target)
    engine.start_in_background()                  # preflight runs here; PlaybackRefused means zero input
    while not engine.wait(POLL_S):
        if cancel is not None and cancel.is_set():
            try:
                engine.cancel("cancelled")
            except PlaybackStateError:            # it ended between the check and the cancel
                pass
    return engine.report()


def perform(macro: Macro, target: Target, create_playback: PlaybackFactory, *, stop: EmergencyStop,
            cancel: threading.Event | None = None, dry_run: bool = False,
            allow_different_target: bool = False) -> ExecutionReport:
    """`act`, and a macro that did not complete raised in this layer's own words: `ActionRefused` (nothing sent),
    `ActionCancelled`, `ActionFailed`. The one road to playback for the runners that are not this module."""
    try:
        report = act(macro, target, create_playback, stop=stop, cancel=cancel, dry_run=dry_run,
                     allow_different_target=allow_different_target)
    except PlaybackRefused as exc:
        raise ActionRefused(str(exc), exc.issues) from None
    if report.state is PlaybackState.CANCELLED:
        raise ActionCancelled(report.cancel_reason or "cancelled", report)
    if report.state is not PlaybackState.COMPLETED:
        failure = report.failure
        raise ActionFailed(failure.message if failure else report.state.value, failure.code if failure else "",
                           report)
    return report


class _Run:
    """One run's blocks, executed in order -- a chosen branch's blocks before the step after its IF."""

    def __init__(self, target: Target, context: ObservationContext, create_playback: PlaybackFactory,
                 stop: EmergencyStop, cancel: threading.Event | None, dry_run: bool,
                 allow_different_target: bool) -> None:
        self.target, self.context, self.create_playback = target, context, create_playback
        self.stop, self.cancel, self.dry_run, self.allow = stop, cancel, dry_run, allow_different_target
        self.done: list[StepReport] = []

    def perform(self, path: tuple, kind: str, macro: Macro, allow_different_target: bool,
                result: ConditionResult | None = None, branch: str | None = None) -> None:
        """Hand one macro to the existing action path and turn its outcome into the run's."""
        try:
            report = act(macro, self.target, self.create_playback, stop=self.stop, cancel=self.cancel,
                         dry_run=self.dry_run, allow_different_target=allow_different_target)
        except PlaybackRefused as exc:
            self.done.append(StepReport(path[0], kind, condition=result, branch=branch, path=path))
            raise _End(FlowState.FAILED, "action_refused", str(exc), exc.issues) from None
        self.done.append(StepReport(path[0], kind, condition=result, branch=branch, action=report, path=path))
        if report.state is PlaybackState.CANCELLED:
            raise _End(FlowState.CANCELLED, report.cancel_reason or "cancelled")
        if report.state is not PlaybackState.COMPLETED:
            failure = report.failure
            raise _End(FlowState.FAILED, "action_failed", failure.message if failure else report.state.value)

    def block(self, item, path: tuple) -> None:
        self.stop.check()
        check_cancel(self.cancel)
        if isinstance(item, WaitUntil):
            waited = wait_until(item, self.context, stop=self.stop, cancel=self.cancel)
            self.done.append(StepReport(path[0], "wait", wait=waited, path=path))
            if not waited.satisfied:
                raise _End(FlowState.TIMED_OUT, "timeout",
                           f"{item.condition.describe()} did not hold within {item.timeout_s:g}s.")
        elif isinstance(item, RunMacro):
            self.perform(path, "run", item.macro, item.allow_different_target or self.allow)
        elif isinstance(item, Decision):
            self.decision(item, path)
        else:                                   # a click on the match only ever opens a branch (decision.py)
            raise InvalidDecision(f"Not a block that can run here: {type(item).__name__}.")

    def decision(self, decision: Decision, path: tuple) -> None:
        result = evaluate(decision.condition, self.context, cancel=self.cancel)
        items = decision.then if result.satisfied else decision.otherwise
        beside = bool(items) and isinstance(items[0], TapMatch) and items[0].offset != (0, 0)
        # a click at a distance from the match needs the size it was set at in the same proportions (SizeChanged
        # otherwise, nothing sent); a look, and a click on the match itself, follow any size
        choice: Choice = decide(decision, result, scale=self.context.scale() if beside else (1.0, 1.0))
        rest = choice.items
        if choice.macro is None:
            self.done.append(StepReport(path[0], "decision", condition=result, branch=choice.branch, path=path))
            first = 0
        else:                                   # the branch's first block: a macro, or a click on the match
            self.perform(path, "decision", choice.macro, choice.allow_different_target or self.allow, result,
                         choice.branch)
            first = 1
        for offset, item in enumerate(rest[first:], start=first):
            if isinstance(item, TapMatch):      # never anywhere but first (decision.py): never after other blocks
                raise InvalidDecision("A click on the match can only be the first block of its branch.")
            self.block(item, path + (choice.branch, offset))


def run_steps(steps: Sequence[Step], *, target: Target, context: ObservationContext,
              create_playback: PlaybackFactory, stop: EmergencyStop, cancel: threading.Event | None = None,
              dry_run: bool = False, allow_different_target: bool = False, start_delay_s: float = 0.0,
              while_acting: Callable[[], AbstractContextManager] | None = None) -> FlowReport:
    """Run the steps in order on one target. Malformed input raises before anything is observed; after that
    every outcome, including failures, is a `FlowReport`.

    `allow_different_target` is the caller's RUN-TIME choice, exactly the one an ordinary macro run offers:
    it lets a prepared macro recorded on another target of the same provider run on this one. It is never
    read from a file, so a stored macro cannot grant itself a different target. `start_delay_s` waits before the
    first step (not in a dry run); `while_acting()` is held from the first step to the end of the run."""
    verified = _check_binding(target, context)
    steps = _check_steps(steps)
    delay = check_start_delay(start_delay_s)
    if not isinstance(stop, EmergencyStop):
        raise InvalidDecision("A run needs the platform's EmergencyStop.")
    run = _Run(verified, context, create_playback, stop, cancel, dry_run, allow_different_target)
    finished = [0]

    def end(state: FlowState, reason: str | None = None, message: str = "", issues=()) -> FlowReport:
        completed = len(steps) if state is FlowState.COMPLETED else finished[0]
        return FlowReport(state, verified.target_id, tuple(run.done), reason, message, tuple(issues), completed)

    try:
        # A question the run could never ask (text with no OCR reader, a language the reader cannot be asked for)
        # is refused here, before the first step -- never discovered half-way, after earlier steps acted.
        for _path, item in walk(steps):
            if isinstance(item, (WaitUntil, Decision)):
                context.check_can_answer(item.condition.query)
        if delay and not dry_run:
            _sleep(delay, stop, cancel)               # the stop and this run's cancel end it within one slice
        with (while_acting() if while_acting is not None else nullcontext()):
            for index, step in enumerate(steps):
                run.block(step, (index,))
                finished[0] = index + 1
    except _End as ended:
        return end(ended.state, ended.reason, ended.message, ended.issues)
    except EmergencyStopped as exc:
        return end(FlowState.CANCELLED, "emergency_stop", str(exc))
    except (Cancelled, ObservationCancelled) as exc:
        if stop.is_set():                       # the stop outranks a cancel that happened at the same time
            return end(FlowState.CANCELLED, "emergency_stop", str(exc))
        return end(FlowState.CANCELLED, "cancelled", str(exc))
    except OcrLanguageUnavailable as exc:
        return end(FlowState.FAILED, "ocr_language_unavailable", str(exc))
    except OcrUnavailable as exc:
        return end(FlowState.FAILED, "ocr_unavailable", str(exc))
    except ObservationUnavailable as exc:
        return end(FlowState.FAILED, "observation_unavailable", str(exc))
    except InvalidQuery as exc:
        return end(FlowState.FAILED, "invalid_query", str(exc))
    except DecisionError as exc:
        return end(FlowState.FAILED, "invalid_decision", str(exc))
    except PlaybackRefused as exc:                # e.g. a watch for the person taking over that could not start
        return end(FlowState.FAILED, "action_refused", str(exc), exc.issues)
    except SizeChanged as exc:                    # the target's proportions changed during the run
        return end(FlowState.FAILED, "size_proportions_changed", str(exc))
    except PlatformError as exc:
        return end(FlowState.FAILED, getattr(exc, "reason_code", None) or type(exc).__name__, str(exc))
    return end(FlowState.COMPLETED)
