"""Condition and decision: "is it true?" and "which prepared branch, then?" -- never "how to click".

    ObservationResult (app.observe) -> Condition -> Decision -> Macro -> the provider's existing playback path

Four responsibilities, four places, never mixed:

    app.observe          what is on the screen             (looks; never judges, never acts)
    condition.py         is this condition true, and why   (true/false + the observation behind it)
    wait.py              WAIT UNTIL, bounded                (satisfied / timed out; stop and cancel end it)
    decision.py          which predefined branch           (pure: no capture, no wait, no input)
    run.py               the steps, on one target          (acts ONLY through `create_playback`)

Provider-neutral like `app.observe` and `app.playback`: nothing here knows which provider, operating system,
device or recognition engine is behind the target. A branch is expressed as the platform's existing `Macro`, so
there is no second action model and no second playback path.

    format.py            macro files: v1 unchanged (delegated to app.playback.format), v2 adds these steps
    graph.py             version-4 scripts: blocks joined by arrows (the person's design of 2026-10-25)
    graph_format.py      their files            graph_run.py   their runs, along the arrows
"""
from .condition import Condition, ConditionResult, evaluate
from .decision import (ELSE, MAX_NESTING, THEN, Choice, Decision, RunMacro, TapMatch, count_blocks, decide, tap_macro,
                       walk)
from .format import ConditionalMacro, load_macro_file, migrate_v1, save_macro_file
from .errors import Cancelled, DecisionError, InvalidCondition, InvalidDecision
from .position import MAX_POSITION
from .run import (MAX_BLOCKS, MAX_START_DELAY_S, MAX_STEPS, FlowReport, FlowState, StepReport, act, adapting,
                  run_steps)
from .wait import WaitResult, WaitStatus, WaitUntil, wait_until
from . import graph
from .graph import (AGAIN, ALL, ANY, AT_MATCH, AT_POINT, CHECK, CLICK, CLICK_KINDS, DONE, DOUBLE, DRAG, FOR, FOUND,
                    HOLD, MACRO, MANUAL, MAX_PARTS, MISSING, NEXT, PICTURE, PIXEL, REPEAT, REPEAT_MODES, RIGHT, START,
                    STOP, TAP, TEXT, TIMES, UNTIL, WAIT, CheckNode, ClickNode, Edge, GraphError, MacroNode, Node, Part,
                    RepeatNode, Script, StartNode, StopNode, WaitNode, part_kind)
from .graph_format import script_from_json, script_to_json, validate_script
from .graph_run import CANCELLED, COMPLETED, ENTER, ERROR, FAILED, LEAVE, GraphReport, run_graph

__all__ = [
    "adapting",
    "Condition", "ConditionResult", "evaluate",
    "WaitUntil", "WaitResult", "WaitStatus", "wait_until",
    "Decision", "RunMacro", "TapMatch", "Choice", "decide", "tap_macro", "THEN", "ELSE", "MAX_NESTING", "walk",
    "count_blocks", "MAX_POSITION",
    "run_steps", "act", "FlowReport", "FlowState", "StepReport", "MAX_STEPS", "MAX_BLOCKS", "MAX_START_DELAY_S",
    "ConditionalMacro", "load_macro_file", "save_macro_file", "migrate_v1",
    "DecisionError", "InvalidCondition", "InvalidDecision", "Cancelled",
    # version 4
    "graph", "Script", "Node", "Edge", "Part", "GraphError", "part_kind", "StartNode", "WaitNode", "CheckNode",
    "RepeatNode", "ClickNode", "MacroNode", "StopNode", "START", "WAIT", "CHECK", "REPEAT", "CLICK", "MACRO", "STOP",
    "NEXT", "FOUND", "MISSING", "AGAIN", "DONE", "ANY", "ALL", "MANUAL", "UNTIL", "FOR", "TIMES", "REPEAT_MODES",
    "TAP", "DOUBLE", "RIGHT", "HOLD", "DRAG", "CLICK_KINDS", "AT_POINT", "AT_MATCH", "PIXEL", "PICTURE", "TEXT",
    "MAX_PARTS", "script_from_json", "script_to_json", "validate_script", "run_graph", "GraphReport", "COMPLETED",
    "FAILED", "CANCELLED", "ENTER", "LEAVE", "ERROR",
]
