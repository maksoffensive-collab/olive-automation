"""Running a version-4 script on one target: from the start block along the arrows, one block at a time.

Like `run.py`, it acts ONLY through `create_playback` (the provider's own playback path, with its preflight, its input
gates and its emergency-stop checks) and looks ONLY through the target's `ObservationContext`. It adds what the
drawing needs:

  * the next block is the one the current block's output leads to; an output with no arrow ends the run, finished;
  * a check looks once (one fresh picture of the target for all its parts) and answers FOUND or MISSING; the last
    place a check found something is kept for a click "where it was found";
  * a repeat counts its passes and keeps its own clock from its first pass; when its setting is reached it answers
    DONE (and forgets, so a later loop through it starts again); "until stopped by hand" never does. A loop pass
    waits at least LOOP_MIN_S, on the stop, so a loop with nothing in it cannot spin;
  * a click is made as a small macro of the platform's own events (a click, a double click, a right click, a hold,
    a drag), at its point scaled from the size it was picked at -- only in the same proportions (`fit`: a click
    where nothing is looked at; docs/DECISION.md) -- or at the last match;
  * every block reports itself as it starts and ends (`on_event`), so the editor can light the block that runs and
    turn it red when it fails.

The emergency stop is checked before every block and ends every wait, loop pause and playback at once; it is never
turned into another error. A failure ends the run with the block it happened in, a code and its numbers
(`GraphReport`), so a screen can say what happened, where, and what to do.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Callable, Mapping

from app.core.cancel import EmergencyStop
from app.core.errors import EmergencyStopped, PlatformError, said_params
from app.observe.context import ObservationContext
from app.observe.errors import (InvalidQuery, ObservationCancelled, ObservationUnavailable, OcrLanguageUnavailable,
                                OcrUnavailable)
from app.playback.macro import Macro
from app.providers.recording import PointerButton, RecordedEvent, RecordedEventKind as K, Recording
from app.providers.size import SizeChanged, fit
from app.providers.target import Target, require_verified

from .condition import ConditionResult, check_cancel, evaluate
from .errors import ActionCancelled, ActionFailed, ActionRefused, Cancelled
from .graph import (AGAIN, AT_MATCH, CHECK, CLICK, DONE, DOUBLE, DRAG, FOR, FOUND, HOLD, MACRO, MANUAL, MISSING, NEXT,
                    REPEAT, RIGHT, START, STOP, TIMES, UNTIL, WAIT, ALL, CheckNode, ClickNode, MacroNode, Node,
                    RepeatNode, Script, StartNode, StopNode, WaitNode)
from .run import perform
from .wait import _sleep

LOOP_MIN_S = 0.1            # a pass of a loop takes at least this long (on the stop): a loop of nothing cannot spin
MAX_BLOCK_RUNS = 10_000_000  # blocks run in one run; only a loop "until stopped by hand" can come near it

COMPLETED, FAILED, CANCELLED = "completed", "failed", "cancelled"
ENTER, LEAVE, ERROR = "enter", "leave", "error"
# create_playback(macro, target, *, dry_run, allow_different_target) -> an engine that has not started
PlaybackFactory = Callable[..., object]
Listener = Callable[[str, str], None]


@dataclass(frozen=True)
class GraphReport:
    """How a run of one target ended. `node` is the block it ended in (None: it ended where an output led nowhere);
    `reason` a code a screen translates, with `params`; `message` the same in English, for the log."""
    state: str
    target_id: str
    node: str | None = None
    reason: str = ""
    message: str = ""
    params: Mapping = field(default_factory=dict)
    issues: tuple = ()
    blocks_run: int = 0                 # blocks run, every pass of a loop counted
    stopped_by_block: bool = False      # a stop block ended it (with an error, or finished)
    visited: int = 0                    # different blocks the run reached
    released: tuple = ()                # what the run's playbacks let go of as they ended
    held: tuple = ()                    # ... and what they could not let go of (its release was refused)

    @property
    def ok(self) -> bool:
        return self.state == COMPLETED


class _End(Exception):
    def __init__(self, state: str, reason: str = "", message: str = "", *, params=None, issues=(),
                 by_block: bool = False) -> None:
        super().__init__(message)
        self.state, self.reason, self.message = state, reason, message
        self.params, self.issues, self.by_block = dict(params or {}), tuple(issues), by_block


class _Loop:
    """What a repeat circle remembers between its passes."""

    def __init__(self, now: float, wall: _dt.datetime) -> None:
        self.passes, self.started, self.wall = 0, now, wall


def _until(wall: _dt.datetime, clock: str) -> _dt.datetime:
    """The next time of day `clock` ("HH:MM") from `wall` on (tomorrow's when today's has passed)."""
    hours, minutes = (int(v) for v in clock.split(":"))
    when = wall.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return when if when > wall else when + _dt.timedelta(days=1)


class _GraphRun:
    def __init__(self, script: Script, target: Target, context: ObservationContext, macros: Mapping[str, Macro],
                 create_playback: PlaybackFactory, stop: EmergencyStop, cancel: threading.Event | None,
                 dry_run: bool, allow_different_target: bool, on_event: Listener | None,
                 clock: Callable[[], float], wall: Callable[[], _dt.datetime], start_delay_s: float | None = None,
                 while_acting: Callable[[], AbstractContextManager] | None = None) -> None:
        self.start_delay = start_delay_s
        self.while_acting = while_acting
        self.script, self.target, self.context, self.macros = script, target, context, macros
        self.create_playback, self.stop, self.cancel = create_playback, stop, cancel
        self.dry_run, self.allow, self.on_event = dry_run, allow_different_target, on_event
        self.clock, self.wall = clock, wall
        self.loops: dict[str, _Loop] = {}
        self.match: tuple[int, int] | None = None     # where the last check found something, in its frame's pixels
        self.runs = 0
        self.current: str | None = None                # the block under way (where a run that ends ended)
        self.visited: set[str] = set()
        self.released: list[str] = []
        self.held: list[str] = []

    def emit(self, node: str, phase: str) -> None:
        if phase == ENTER:
            self.visited.add(node)
        if self.on_event is not None:
            try:
                self.on_event(node, phase)
            except Exception:
                pass                                   # a screen that cannot show it must not end the run

    def check_running(self) -> None:
        self.stop.check()
        check_cancel(self.cancel)

    # ---- the blocks -----------------------------------------------------------------------------------------
    def block(self, node: Node) -> str | None:
        """Run one block; the output it answers (None for a stop)."""
        if isinstance(node, StartNode):
            return NEXT                                # its delay was waited before the walk (`walk`)
        if isinstance(node, WaitNode):
            if not self.dry_run:
                _sleep(node.seconds, self.stop, self.cancel)
            return NEXT
        if isinstance(node, CheckNode):
            return self.check(node)
        if isinstance(node, RepeatNode):
            return self.repeat(node)
        if isinstance(node, ClickNode):
            self.click(node)
            return NEXT
        if isinstance(node, MacroNode):
            self.run_macro(node)
            return NEXT
        if isinstance(node, StopNode):
            if node.success:
                raise _End(COMPLETED, "stop_block", node.message, by_block=True)
            raise _End(FAILED, "stop_block_error", node.message or "A stop block ended the run with an error.",
                       params={"message": node.message}, by_block=True)
        raise _End(FAILED, "unknown_block", f"Not a block that can run: {node.kind}.")

    def check(self, node: CheckNode) -> str:
        if not node.parts:
            raise _End(FAILED, "check_empty", "This check has nothing to look for yet.")
        results: list[ConditionResult] = []
        self.context.forget()                          # one fresh picture of the target for all the parts
        for index, part in enumerate(node.parts):
            results.append(evaluate(part.condition, self.context, cancel=self.cancel, picked_on=part.size,
                                    max_age_s=0.0 if index == 0 else 3600.0))
        found = all(r.satisfied for r in results) if node.mode == ALL else any(r.satisfied for r in results)
        # a click "where it was found" clicks what the LAST check found: after a "missing" there is nothing found
        seen = next((r for r in results if r.satisfied and r.center is not None), None) if found else None
        self.match = (seen.center.x, seen.center.y) if seen is not None else None
        return FOUND if found else MISSING

    def repeat(self, node: RepeatNode) -> str:
        loop = self.loops.get(node.id)
        if loop is None:
            loop = self.loops[node.id] = _Loop(self.clock(), self.wall())
        loop.passes += 1
        if self.dry_run:
            again = loop.passes <= 1                    # a dry run goes round a loop once: it must end by itself
        elif node.mode == MANUAL:
            again = True
        elif node.mode == TIMES:
            again = loop.passes <= node.times
        elif node.mode == FOR:
            again = self.clock() - loop.started < node.seconds
        else:                                           # UNTIL a time of day: the next one after the first pass
            again = self.wall() < _until(loop.wall, node.until)
        if not again:
            del self.loops[node.id]                    # a later loop through this circle starts again
            return DONE
        if self.stop.wait(LOOP_MIN_S):
            self.stop.check()
        check_cancel(self.cancel)
        self.match = None                               # what an earlier pass found is not there to click now
        return AGAIN

    def where(self, node: ClickNode) -> tuple[tuple[int, int], tuple[int, int] | None]:
        """(the point to click, where a drag ends) in the target's pixels now."""
        if node.where == AT_MATCH:
            if self.match is None:
                raise _End(FAILED, "click_no_match", "There is nothing found to click: no check before this click "
                                                     "found anything.")
            return self.match, None
        if not node.ready:
            raise _End(FAILED, "click_no_point", "This click has no point picked on the screen yet.")
        frame = self.context.frame(max_age_s=1.0)
        sx, sy = 1.0, 1.0
        if node.size is not None:
            scale = fit(node.size, (frame.width, frame.height))     # SizeChanged for other proportions
            sx, sy = scale or (1.0, 1.0)

        def placed(point: tuple[int, int]) -> tuple[int, int]:
            x, y = round(point[0] * sx), round(point[1] * sy)
            if not (0 <= x < frame.width and 0 <= y < frame.height):
                raise _End(FAILED, "click_outside", f"The point ({x}, {y}) is outside the target's "
                                                    f"{frame.width}x{frame.height} screen.",
                           params={"x": x, "y": y, "image": f"{frame.width}×{frame.height}"})
            return x, y
        assert node.point is not None
        return placed(node.point), (placed(node.to) if node.to is not None else None)

    def click(self, node: ClickNode) -> None:
        (x, y), to = self.where(node)
        button = PointerButton.SECONDARY if node.how == RIGHT else PointerButton.PRIMARY
        events = [RecordedEvent(K.BUTTON_DOWN, 0, x, y, button, 1)]
        if node.how == HOLD:
            events.append(RecordedEvent(K.BUTTON_UP, round(node.hold_s * 1000), x, y, button, 1))
        elif node.how == DRAG and to is not None:
            steps = 8
            for i in range(1, steps + 1):
                events.append(RecordedEvent(K.POINTER_MOVE, 30, round(x + (to[0] - x) * i / steps),
                                            round(y + (to[1] - y) * i / steps)))
            events.append(RecordedEvent(K.BUTTON_UP, 30, to[0], to[1], button, 1))
        else:
            events.append(RecordedEvent(K.BUTTON_UP, 0, x, y, button, 1))
            if node.how == DOUBLE:
                events += [RecordedEvent(K.BUTTON_DOWN, 0, x, y, button, 2),
                           RecordedEvent(K.BUTTON_UP, 0, x, y, button, 2)]
        source = self.context.source
        self.play(Macro(Recording(source.provider_id, source.target_id, tuple(events))), node.id)

    def run_macro(self, node: MacroNode) -> None:
        macro = self.macros.get(node.macro) if node.macro else None
        if macro is None:
            raise _End(FAILED, "macro_missing", "The macro this block runs is not in the library (any more).",
                       params={"macro": node.macro})
        self.play(macro, node.id, allow=True)

    def play(self, macro: Macro, node_id: str, *, allow: bool = False) -> None:
        try:
            self._let_go(perform(macro, self.target, self.create_playback, stop=self.stop, cancel=self.cancel,
                                 dry_run=self.dry_run, allow_different_target=self.allow or allow))
        except ActionRefused as exc:
            named = next((i for i in exc.issues if getattr(i, "params", ())), None)
            if named is not None:                # the playback's own reason, with the names its sentence needs
                raise _End(FAILED, named.code, str(exc), params=dict(named.params)) from None
            raise _End(FAILED, "action_refused", str(exc), issues=exc.issues) from None
        except ActionCancelled as exc:
            self._let_go(exc.report)
            raise _End(CANCELLED, exc.reason) from None
        except ActionFailed as exc:
            self._let_go(exc.report)
            failure = getattr(exc.report, "failure", None)
            if failure is not None and failure.params:
                raise _End(FAILED, failure.code, str(exc), params=dict(failure.params)) from None
            raise _End(FAILED, "action_failed", str(exc), params={"code": exc.code}) from None

    def _let_go(self, report) -> None:
        """What a playback let go of as it ended, and what it could not (kept for the run's report)."""
        if report is not None:
            self.released += list(getattr(report, "released_at_end", ()))
            self.held += list(getattr(report, "held_at_end", ()))

    # ---- the walk -----------------------------------------------------------------------------------------
    def walk(self) -> GraphReport:
        start = self.script.start
        self.current = start.id
        try:
            # a question the run could never ask (text with no reader, a flat picture, any size without the fast
            # search) is refused here, before the start delay and before anything is done
            for part in (p for n in self.script.nodes if isinstance(n, CheckNode) for p in n.parts):
                self.context.check_can_answer(part.condition.query)
            delay = self.script.start.delay_s if self.start_delay is None else self.start_delay
            self.emit(start.id, ENTER)
            if delay and not self.dry_run:
                _sleep(delay, self.stop, self.cancel)  # the stop and this run's cancel end it within one slice
            with (self.while_acting() if self.while_acting is not None else nullcontext()):
                return self._walk(start)
        except _End as ended:
            self._mark(LEAVE if ended.state == COMPLETED else ERROR)
            return self.report(ended.state, ended.reason, ended.message, ended.params, ended.issues,
                               by_block=ended.by_block)
        except EmergencyStopped as exc:
            self._mark(ERROR)
            return self.report(CANCELLED, "emergency_stop", str(exc))
        except (Cancelled, ObservationCancelled) as exc:
            self._mark(ERROR)
            return self.report(CANCELLED, "emergency_stop" if self.stop.is_set() else "cancelled", str(exc))
        except SizeChanged as exc:
            self._mark(ERROR)
            return self.report(FAILED, "size_proportions_changed", str(exc))
        except InvalidQuery as exc:
            self._mark(ERROR)
            code = getattr(exc, "code", "")
            return self.report(FAILED, f"query_{code}" if code else "invalid_query", str(exc),
                               getattr(exc, "params", {}))
        except OcrLanguageUnavailable as exc:
            self._mark(ERROR)
            return self.report(FAILED, "ocr_language_unavailable", str(exc))
        except OcrUnavailable as exc:
            self._mark(ERROR)
            return self.report(FAILED, "ocr_unavailable", str(exc))
        except ObservationUnavailable as exc:
            self._mark(ERROR)
            return self.report(FAILED, "observation_unavailable", str(exc), said_params(exc))
        except PlatformError as exc:
            self._mark(ERROR)
            return self.report(FAILED, getattr(exc, "reason_code", None) or type(exc).__name__, str(exc),
                               said_params(exc))

    def _mark(self, phase: str) -> None:
        if self.current is not None:
            self.emit(self.current, phase)

    def _walk(self, node: Node) -> GraphReport:
        first = True
        while True:
            self.check_running()
            self.runs += 1
            if self.runs > MAX_BLOCK_RUNS:
                raise _End(FAILED, "too_many_blocks", "The run went through more blocks than a run may.")
            self.current = node.id
            if not first:
                self.emit(node.id, ENTER)
            first = False
            output = self.block(node)
            self.emit(node.id, LEAVE)
            edge = self.script.edge_from(node.id, output) if output else None
            if edge is None:
                self.current = None
                return self.report(COMPLETED)
            node = self.script.node(edge.target)

    def report(self, state: str, reason: str = "", message: str = "", params=None, issues=(), *,
               by_block: bool = False) -> GraphReport:
        return GraphReport(state, self.target.target_id, self.current, reason, message, dict(params or {}),
                           tuple(issues), self.runs, by_block, len(self.visited), tuple(self.released),
                           tuple(self.held))


def run_graph(script: Script, *, target: Target, context: ObservationContext, macros: Mapping[str, Macro],
              create_playback: PlaybackFactory, stop: EmergencyStop, cancel: threading.Event | None = None,
              dry_run: bool = False, allow_different_target: bool = False, on_event: Listener | None = None,
              clock: Callable[[], float] = time.monotonic,
              wall: Callable[[], _dt.datetime] = _dt.datetime.now, start_delay_s: float | None = None,
              while_acting: Callable[[], AbstractContextManager] | None = None) -> GraphReport:
    """Run `script` on `target` (verified; its `context` bound to it). `macros` are the recorded macros its macro
    blocks name, by key, read before the run. A dry run looks, decides and walks the arrows but sends nothing and
    waits for nothing (each playback is the provider's dry run). `start_delay_s` replaces the start block's own
    delay (what is left of a run's one delay, for a target that starts later); `while_acting` is entered once it is
    over, around every block (the provider's watch for the person taking over, a last check of the target)."""
    if not isinstance(script, Script):
        raise TypeError("run_graph needs a version-4 Script.")
    verified = require_verified(target)
    if context.target_id != verified.target_id or context.provider_id != verified.provider_id:
        raise PlatformError("The observation context belongs to another target than the run.")
    return _GraphRun(script, verified, context, macros, create_playback, stop, cancel, dry_run,
                     allow_different_target, on_event, clock, wall, start_delay_s, while_acting).walk()


__all__ = ["run_graph", "GraphReport", "COMPLETED", "FAILED", "CANCELLED", "ENTER", "LEAVE", "ERROR", "LOOP_MIN_S",
           "START", "WAIT", "CHECK", "REPEAT", "CLICK", "MACRO", "STOP", "FOUND", "MISSING", "AGAIN", "DONE", "NEXT",
           "FOR", "UNTIL", "TIMES", "DRAG", "HOLD"]
