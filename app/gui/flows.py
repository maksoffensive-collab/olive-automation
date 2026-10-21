"""Running a version-2 macro from the GUI: the controller's existing batch, with `run_steps` doing each target's work.

    controller (explicit targets, verify, scheduling, history)
      -> refusal() / preflight of every recorded step        nothing observed, nothing sent yet
      -> FlowJob: THIS target's own ObservationContext, own cancel event, own thread
           -> app.decide.run_steps -> services.create_playback -> the provider's preflight / input session /
              budget / emergency stop

There is no second playback path here and no provider: a screenshot is `AutomationServices.capture` of the one
verified target (wrapped as the `ScreenshotProvider` an `ObservationContext` expects), and every action is a
`Macro` handed by `run_steps` to the same `create_playback` factory a v1 run uses. What a file cannot decide stays
a run-time choice: `allow_different_target` is computed here from the targets the person chose, never read from
the macro.

Per-target isolation: a `FlowJob` shares nothing with another one except what the platform already shares (the
emergency stop and the OCR engine, which holds no per-target state). Each has its own context, so one target's
frame can never answer another target's condition.
"""
from __future__ import annotations

import contextlib
import threading
import time

from app.decide import Decision, FlowReport, FlowState, RunMacro, TapMatch, WaitUntil, adapting, run_steps, walk
from app.decide.format import ConditionalMacro
from app.gui.services import AutomationServices
from app.observe import ObservationContext, OcrProvider
from app.playback.macro import REQUIRED_CAPABILITY, ActionOp, Macro
from app.providers.base import Screenshot, ScreenshotProvider
from app.providers.target import Capability, Target


class ServiceScreens(ScreenshotProvider):
    """The services' own capture of ONE verified target, as a `ScreenshotProvider`. Nothing is re-implemented:
    every capture is `services.capture(target)`, which verifies the target before it takes the picture."""

    def __init__(self, services: AutomationServices, target: Target) -> None:
        self._services, self._target = services, target

    @property
    def identity(self):
        return self._target.identity

    def capture(self) -> Screenshot:
        return self._services.capture(self._target)


def _steps(flow: ConditionalMacro) -> tuple:
    """The steps as they run: a script that adapts to the size has the macros inside it adapt too."""
    return adapting(flow.steps) if flow.adapt else flow.steps


def _blocks(flow: ConditionalMacro) -> list:
    """Every block of the flow, the ones inside branches (at any depth) included."""
    return [item for _path, item in walk(_steps(flow))]


def recorded_macros(flow: ConditionalMacro) -> list[Macro]:
    """Every recorded macro the flow may run, as a step or inside a branch."""
    return [part.macro for part in _blocks(flow) if isinstance(part, RunMacro)]


def tap_ops(flow: ConditionalMacro) -> list[ActionOp]:
    return [part.op for part in _blocks(flow) if isinstance(part, TapMatch)]


def observes(flow: ConditionalMacro) -> bool:
    return any(isinstance(part, (WaitUntil, Decision)) for part in _blocks(flow))


def refusal(flow: ConditionalMacro, target: Target) -> str:
    """Why this flow cannot run on this target at all, before anything is observed -- or "".

    Only what the target DECLARES is consulted (its capabilities and provider id); the same questions preflight
    asks of every action, asked early so a run that could never finish does not start looking first."""
    if observes(flow) and not target.has_capability(Capability.SCREENSHOT):
        return "unsupported_capability: this target cannot take screenshots, so it cannot be observed."
    for macro in recorded_macros(flow):
        if macro.target.provider_id != target.provider_id:
            return (f"wrong_provider: “{macro.name}” was recorded for {macro.target.provider_id}, "
                    f"the target belongs to {target.provider_id}.")
    for op in tap_ops(flow):
        needed = REQUIRED_CAPABILITY.get(op)
        if needed is None or not target.has_capability(needed):
            return f"unsupported_capability: the target does not declare '{op.value}'."
    return ""


def allow_different(flow: ConditionalMacro, target: Target) -> bool:
    """True when a recorded step was made on another target of the same provider. Choosing the targets of a run
    explicitly is what allows that, exactly as for a v1 run; it is never taken from the file."""
    return any(macro.target.target_id != target.target_id for macro in recorded_macros(flow))


def completed_steps(report: FlowReport) -> int:
    """How many top-level steps finished: all of them, or those before the one the run ended in (counted by the
    run itself, which knows where a branch's blocks belong)."""
    return report.completed


class FlowJob:
    """One v2 run on one target, in a thread of its own. Shaped like the part of `PlaybackEngine` the batch uses
    (`start_in_background`, `cancel`, `wait`) so the scheduler drives both the same way."""

    def __init__(self, flow: ConditionalMacro, target: Target, services: AutomationServices, *,
                 ocr: OcrProvider | None, dry_run: bool, allow_different_target: bool,
                 delay_s: float | None = None) -> None:
        self.flow, self.target = flow, target
        self.total = len(flow.steps)
        self._services = services
        self._dry_run, self._allow = dry_run, allow_different_target
        self._cancel = threading.Event()
        self.cancel_reason = ""
        # a script that adapts asks its points and areas at the size the target has now (picked at `flow.size`)
        self.context = ObservationContext(ServiceScreens(services, target), stop=services.stop, ocr=ocr,
                                          picked_on=flow.size if flow.adapt else None)
        self.report: FlowReport | None = None
        self.error: BaseException | None = None
        # how long THIS target waits before its first step: what is left of the run's one delay (`delay_s`, from the
        # controller), or the script's own; a dry run does not wait to start
        self.delay_s = 0.0 if dry_run else float(flow.start_delay_s if delay_s is None else delay_s)
        self.acting = False                   # set once the start delay is over and the steps begin
        self._started_at: float | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"flow-{target.target_id}")

    def starts_in(self) -> float:
        """Seconds left of the start delay (0 once the steps have begun, or when there is none)."""
        if self.acting or not self.delay_s or self._started_at is None:
            return 0.0
        return max(0.0, self._started_at + self.delay_s - time.monotonic())

    @contextlib.contextmanager
    def _acting(self):
        """From the first step to the end of the run. After a start delay the target is checked again first (it
        may have closed or changed meanwhile). A live run is then watched for the person taking over for its whole
        length, its waits included (the whole desktop needs that; most targets need nothing) -- but not during the
        start delay, when nothing is being done; a watch that cannot start ends the run here."""
        if self.delay_s:
            self._services.recheck(self.target)
        # other proportions, or a picture too small at this size: refused before anything is done
        self.context.check_size(item.condition.query for _path, item in walk(self.flow.steps)
                                if isinstance(item, (WaitUntil, Decision)))
        self.acting = True
        guard = contextlib.nullcontext() if self._dry_run else self._services.guard_run(self.target)
        with guard:
            yield

    def _run(self) -> None:
        try:
            self.report = run_steps(_steps(self.flow), target=self.target, context=self.context,
                                    create_playback=self._services.create_playback, stop=self._services.stop,
                                    cancel=self._cancel, dry_run=self._dry_run, allow_different_target=self._allow,
                                    start_delay_s=self.delay_s, while_acting=self._acting)
        except BaseException as exc:          # malformed input is raised before anything is observed
            self.error = exc

    def start_in_background(self) -> None:
        self._started_at = time.monotonic()
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def cancel(self, reason: str = "cancelled") -> None:
        if not self.cancel_reason:
            self.cancel_reason = reason
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def let_go(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """What the macros of this run let go of as each ended, and what is still held (its release was refused)."""
        released: list[str] = []
        held: list[str] = []
        for step in self.report.steps if self.report is not None else ():
            if step.action is not None:
                released += step.action.released_at_end
                held += step.action.held_at_end
        return tuple(released), tuple(held)

    def outcome(self) -> tuple[str, int, str]:
        """(state, completed steps, detail) once the thread has ended; state is a `RunState` value. What a macro of
        the run could not let go of is added to the detail ("still held: ...")."""
        state, done, detail = self._outcome()
        held = self.let_go()[1]
        if held:
            detail = "; ".join(filter(None, (detail, "still held: " + ", ".join(held))))
        return state, done, detail

    def _outcome(self) -> tuple[str, int, str]:
        if self.error is not None:
            return "failed", 0, f"{type(self.error).__name__}: {self.error}"
        report = self.report
        if report is None:
            return "stopped", 0, "the run ended before this target finished"
        done = completed_steps(report)
        if report.state is FlowState.COMPLETED:
            return "completed", done, ""
        if report.state is FlowState.CANCELLED:
            reason = report.reason if report.reason == "emergency_stop" else (self.cancel_reason or "cancelled")
            return "stopped", done, reason
        detail = f"{report.reason}: {report.message}" if report.message else (report.reason or "failed")
        if report.issues:
            detail += " (" + "; ".join(f"{i.code}: {i.message}" for i in report.issues) + ")"
        if report.reason == "action_refused" and done == 0 and len(report.steps) <= 1:
            return "refused", done, detail
        return "failed", done, detail
