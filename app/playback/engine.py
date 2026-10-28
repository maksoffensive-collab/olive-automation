"""Playback engine: runs a `Macro` through ONE explicitly bound target's neutral input interfaces.

Binding: the caller passes the real `Target` and an input session that the PROVIDER built for that same target
(`tap`, `move`, `key_press`, ... methods; the session checks its own target, capability, bounds and emergency stop
on every operation). The engine never searches for, guesses, or switches targets: there is no foreground fallback,
no "all targets", no re-targeting. It contains no provider knowledge; it only asks the target which `Capability`
values it declares and calls the methods the input session offers.

Lifecycle (`PlaybackState`): IDLE -> RUNNING <-> PAUSED -> COMPLETED | CANCELLED | FAILED.
  run() / start_in_background()  start; refused (`PlaybackRefused`, state stays IDLE, zero input) if preflight
                                  finds any error or the engine was already started; one engine runs one macro once
  pause() / resume()              pause takes effect only BETWEEN complete actions and only while nothing is held
                                  (no button, no key), because a pause never releases anything; until then state
                                  stays RUNNING with `pause_pending`. Resume continues with the next action, whose
                                  recorded delay counts from the resume moment
  cancel(reason)                  RUNNING / PAUSED only; an idle or finished engine raises `PlaybackStateError`
  Anything invalid raises `PlaybackStateError` and changes nothing.

Letting go (the person's decision of 2026-10-20; it replaces P-04, "never release"): a LIVE run that ends with a
button or key still held -- stopped, cancelled, failed, or recorded so -- has its input session let go of exactly
that (`release_held`, offered by sessions that can hold something), before the run is seen to end, so nothing it
pressed outlives it. It is the one input sent after an emergency stop: a release presses nothing new (it can
complete what was cut off -- docs/DECISION.md). A session without
`release_held` is left as it is, a dry run lets go of nothing, and a release that fails never changes the run's own
outcome; `report().released_at_end` says what was let go, `held_at_end` what is still held.

Timing: before each action the engine waits its `delay_ms`, scheduled from the previous action's due time (recorded
delays do not accumulate execution time; if the action ran late the next one is not delayed further). Waits are
sliced (`tick_s`, default 50 ms) on an injectable monotonic clock (default `time.perf_counter`) and sleep so tests are instantaneous, and every
slice checks cancel and the emergency stop, so an interrupted wait sends nothing further.

Before each action: cancel flag, emergency stop, and that the input session is still bound to the same target. The
provider session then applies its own gates (target still valid, capability, arguments, bounds, budget).

Failure: any exception from the provider ends the run as FAILED at that action: no later action is sent,
`current_index` stays on the failed action, `completed_count` counts only fully executed actions, and `failure`
keeps code, message and, if the provider reported partial delivery (`delivered` / `total` attributes), how far it
got. An emergency stop (flag set, or the provider raising the stop error) ends as CANCELLED ("emergency_stop").

Dry run: `dry_run=True` performs preflight and walks the whole plan producing the report, with no real waiting and
zero real input. If the bound input is itself a provider dry-run session (`dry_run` attribute true) the walk goes
through it, so the provider's own gates run and it records what it would do; with no input, or an input without
that attribute, the engine only walks the plan and never calls the input. A live run refuses a dry-run session.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, SafetyViolation, said_params
from app.playback.macro import METHOD, ActionOp, Macro, MacroAction
from app.playback.preflight import Issue, PreflightReport, preflight


class PlaybackState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlaybackStateError(SafetyViolation):
    """The operation is not valid in the engine's current state; nothing changed."""


class PlaybackRefused(SafetyViolation):
    """Playback did not start; no input was sent. `issues` lists every reason."""

    def __init__(self, issues: tuple[Issue, ...]) -> None:
        super().__init__("Playback refused: " + "; ".join(f"{i.code}" + (f"@{i.index}" if i.index is not None else "")
                                                          for i in issues))
        self.issues = issues


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    action_index: int
    delivered: int | None = None      # provider-reported partial delivery of a multi-message operation
    total: int | None = None
    params: tuple[tuple[str, object], ...] = ()   # the names and numbers a translated sentence needs


@dataclass(frozen=True)
class Step:
    index: int
    op: str
    delay_ms: int
    description: str


@dataclass(frozen=True)
class ExecutionReport:
    state: PlaybackState
    dry_run: bool
    actions_total: int
    completed_count: int
    current_index: int                # action running / failed / next to run; == actions_total once completed
    failure: Failure | None
    cancel_reason: str | None
    steps: tuple[Step, ...]           # actions fully executed (or, in a dry run, walked), in order
    held_at_end: tuple[str, ...]      # buttons/keys still held when the run ended (its session could not let go)
    released_at_end: tuple[str, ...] = ()   # what the session let go of as the run ended, in that order


def _caused_by_stop(exc: BaseException) -> bool:
    """True if `exc` was raised explicitly FROM an emergency stop (`raise ... from stop_error`, at any depth).
    A provider that stops a multi-message operation because the stop fired reports it this way. An error that was
    merely raised while a stop was being handled has no explicit cause and is not counted."""
    cause = exc.__cause__
    for _ in range(8):
        if cause is None:
            return False
        if isinstance(cause, EmergencyStopped):
            return True
        cause = cause.__cause__
    return False


class PlaybackEngine:
    def __init__(self, macro: Macro, target: object, input: object, stop: EmergencyStop, *, dry_run: bool = False,
                 allow_different_target: bool = False, clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep, tick_s: float = 0.05) -> None:
        if not tick_s > 0:
            raise ValueError("tick_s must be positive.")
        self._macro, self._target, self._input, self._stop = macro, target, input, stop
        self._dry, self._allow_other = bool(dry_run), allow_different_target
        self._clock, self._sleep, self._tick = clock, sleep, tick_s
        self._cond = threading.Condition()
        self._state = PlaybackState.IDLE
        self._plan: tuple[MacroAction, ...] = ()
        self._index = self._completed = 0
        self._failure: Failure | None = None
        self._cancel_flag: str | None = None
        self._cancel_reason: str | None = None
        self._pause_requested = False
        self._steps: list[Step] = []
        self._held: set[str] = set()
        self._released: tuple[str, ...] = ()
        self._thread: threading.Thread | None = None
        self._resumed = False

    # ---- read side --------------------------------------------------------
    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def completed_count(self) -> int:
        return self._completed

    @property
    def failure(self) -> Failure | None:
        return self._failure

    @property
    def pause_pending(self) -> bool:
        return self._pause_requested and self._state is PlaybackState.RUNNING

    def preflight(self) -> PreflightReport:
        return preflight(self._macro, self._target, self._input if self._live_input() else None,
                         allow_different_target=self._allow_other)

    def report(self) -> ExecutionReport:
        with self._cond:
            return ExecutionReport(self._state, self._dry, len(self._plan),
                                   self._completed, self._index, self._failure, self._cancel_reason,
                                   tuple(self._steps), tuple(sorted(self._held)), self._released)

    # ---- control ------------------------------------------------------------
    def run(self) -> ExecutionReport:
        """Start and run to a terminal state in the calling thread."""
        self._begin()
        self._loop()
        return self.report()

    def start_in_background(self) -> None:
        self._begin()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="playback")
        self._thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a background run; True if it has ended."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return self._state not in (PlaybackState.RUNNING, PlaybackState.PAUSED)

    def pause(self) -> None:
        with self._cond:
            if self._state is not PlaybackState.RUNNING:
                raise PlaybackStateError(f"Cannot pause: the playback is {self._state.value}.")
            self._pause_requested = True

    def resume(self) -> None:
        with self._cond:
            if self._state is PlaybackState.PAUSED or (self._state is PlaybackState.RUNNING and self._pause_requested):
                self._pause_requested = False
                self._cond.notify_all()
                return
            raise PlaybackStateError(f"Cannot resume: the playback is {self._state.value}, not paused.")

    def cancel(self, reason: str = "cancelled") -> None:
        with self._cond:
            if self._state not in (PlaybackState.RUNNING, PlaybackState.PAUSED):
                raise PlaybackStateError(f"Cannot cancel: the playback is {self._state.value}.")
            self._cancel_flag = reason
            self._cond.notify_all()

    # ---- internals ----------------------------------------------------------------
    def _live_input(self) -> object | None:
        """The input the plan is executed through, or None if this run must not touch any input."""
        if self._input is None:
            return None
        if self._dry:
            return self._input if getattr(self._input, "dry_run", False) is True else None
        return self._input

    def _begin(self) -> None:
        with self._cond:
            if self._state is not PlaybackState.IDLE:
                raise PlaybackStateError(f"Cannot start: the playback is {self._state.value}.")
            report = self.preflight()
            issues = list(report.errors)
            if not self._dry:
                if self._input is None:
                    issues.append(Issue("no_input", "A live run needs the target's input session."))
                elif getattr(self._input, "dry_run", False) is True:
                    issues.append(Issue("input_is_dry_run", "The bound input session is a dry run; a live playback "
                                                            "must not be bound to it."))
            if issues:
                raise PlaybackRefused(tuple(issues))
            self._plan = self._macro.actions
            self._index = 0
            self._state = PlaybackState.RUNNING

    def _finish(self, state: PlaybackState, reason: str | None = None, failure: Failure | None = None) -> None:
        released = self._let_go()                 # before the run is seen to end: nothing it pressed outlives it
        still = self._still_held()
        with self._cond:
            self._released = released
            self._held.difference_update(released)
            self._held.update(still)
            self._state, self._cancel_reason, self._failure = state, reason, failure
            self._cond.notify_all()

    def _let_go(self) -> tuple[str, ...]:
        """What a LIVE run still holds as it ends, let go of by its input session -- which alone knows how."""
        target_input = self._live_input()
        if self._dry or target_input is None:
            return ()                             # asked even when the run saw nothing held: a click cut off between
            #                                       its press and its release is down, and only the session knows
        release = getattr(target_input, "release_held", None)
        if release is None:
            return ()                             # a session that cannot let go: nothing is invented for it
        try:
            return tuple(release())
        except Exception:
            return ()                             # the run's own outcome stands; `held_at_end` says what is held

    def _still_held(self) -> tuple[str, ...]:
        """What the live session says it still holds after letting go (`still_held`, offered by sessions that can let
        go): a press it could not release -- also one the run never saw held, a click cut off between its press and
        its release, which only the session knows of."""
        target_input = self._live_input()
        if self._dry or target_input is None:
            return ()
        try:
            return tuple(getattr(target_input, "still_held", ()))
        except Exception:
            return ()

    def _abort_reason(self) -> str | None:
        if self._cancel_flag is not None:
            return self._cancel_flag
        if self._stop.is_set():
            return "emergency_stop"
        return None

    def _boundary(self) -> str | None:
        """Between actions: honour a pause (only when nothing is held); return a cancel reason or None."""
        with self._cond:
            while True:
                reason = self._abort_reason()
                if reason is not None:
                    return reason
                if not (self._pause_requested and not self._held):
                    if self._state is PlaybackState.PAUSED:
                        self._state = PlaybackState.RUNNING
                    return None
                self._state = PlaybackState.PAUSED
                self._resumed = True
                self._cond.wait(self._tick)          # also polls the emergency stop while paused

    def _wait_until(self, due: float) -> str | None:
        while True:
            reason = self._abort_reason()
            if reason is not None:
                return reason
            remaining = due - self._clock()
            if remaining <= 0:
                return None
            self._sleep(min(remaining, self._tick))

    def _loop(self) -> None:
        due = self._clock()
        self._resumed = False
        try:
            for index, action in enumerate(self._plan):
                self._index = index
                reason = self._boundary()
                if reason is not None:
                    return self._finish(PlaybackState.CANCELLED, reason)
                if self._resumed:
                    self._resumed, due = False, self._clock()
                if not self._dry:
                    reason = self._wait_until(due + action.delay_ms / 1000.0)
                    if reason is not None:
                        return self._finish(PlaybackState.CANCELLED, reason)
                due += action.delay_ms / 1000.0
                if not self._execute(index, action):
                    return
            self._index = len(self._plan)
            self._finish(PlaybackState.COMPLETED)
        except Exception as exc:                 # the engine itself must never die silently
            self._finish(PlaybackState.FAILED, None, Failure("engine_error", type(exc).__name__, self._index))

    def _aim(self, index: int, action: MacroAction) -> MacroAction | None:
        """The action as it is to be performed now, just before it is handed to the input session -- a dry run's
        session too, which checks it and sends nothing: here unchanged. A provider may aim it anew (where the thing
        it acts on is now) or raise to refuse it; None ends the run as cancelled (it was stopped or cancelled while
        aiming). A provider's aiming must do nothing that changes the screen in a dry run (`self._dry`)."""
        return action

    def _execute(self, index: int, action: MacroAction) -> bool:
        target_input = self._live_input()
        try:
            if target_input is not None:
                if getattr(target_input, "target_id", None) != self._target.target_id:
                    raise SafetyViolation("The input session is no longer bound to the target.")
                aimed = self._aim(index, action)
                if aimed is None:                # the run was ended while the action was being aimed
                    self._finish(PlaybackState.CANCELLED, self._abort_reason() or "cancelled")
                    return False
                action = aimed
                method = getattr(target_input, METHOD[action.op])
                if action.op in (ActionOp.KEY_PRESS, ActionOp.KEY_DOWN, ActionOp.KEY_UP):
                    method(action.key)
                elif action.op is ActionOp.TYPE_TEXT:
                    method(action.text)
                else:
                    method(action.x, action.y)
        except EmergencyStopped:
            self._finish(PlaybackState.CANCELLED, "emergency_stop")
            return False
        except Exception as exc:
            if _caused_by_stop(exc):
                # the emergency stop interrupted the operation: a cancel, with the partial delivery kept visible
                partial = getattr(exc, "delivered", None)
                failure = None if partial is None else Failure("emergency_stop", str(exc), index, partial,
                                                               getattr(exc, "total", None))
                self._finish(PlaybackState.CANCELLED, "emergency_stop", failure)
                return False
            code = getattr(exc, "reason_code", None) or type(exc).__name__
            self._finish(PlaybackState.FAILED, None, Failure(str(code), str(exc), index,
                                                             getattr(exc, "delivered", None), getattr(exc, "total", None),
                                                             tuple(getattr(exc, "params", ()) or ())
                                                             + tuple(said_params(exc).items())))
            return False
        with self._cond:
            self._completed += 1
            self._steps.append(Step(index, action.op.value, action.delay_ms, action.describe()))
            if action.op is ActionOp.BUTTON_DOWN:
                self._held.add("primary_button")
            elif action.op is ActionOp.BUTTON_UP:
                self._held.discard("primary_button")
            elif action.op is ActionOp.KEY_DOWN:
                self._held.add(f"key:{action.key.value}")
            elif action.op is ActionOp.KEY_UP:
                self._held.discard(f"key:{action.key.value}")
        return True
