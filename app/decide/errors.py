"""What can go wrong while deciding, kept apart from what a condition can answer.

The rule inherited from `app.observe`: **a condition that is false is an answer, not an error.** Nothing in this
module is raised to mean "not there" or "not yet". An error here always means a question or a plan was malformed,
or that the run was told to stop.

    InvalidCondition   a condition could not be built: not an observation query, a bad label. A caller's bug,
                       raised where the condition is written, before anything is captured.
    InvalidDecision    a decision or a step list is malformed: no branch at all, a branch that needs a match the
                       observation can never have, a result of one condition handed to the decision of another.
    Cancelled          the caller cancelled this one run (its own cancellation event). Deliberately NOT the
                       emergency stop: a cancel ends one target's run, the emergency stop ends everything and
                       still arrives as the platform's own `EmergencyStopped`, unchanged.

Failing to LOOK keeps the observation layer's own errors (`ObservationUnavailable`, `OcrUnavailable`,
`InvalidQuery`); they are not wrapped, so "I could not see" stays distinguishable from "I decided".
"""
from __future__ import annotations

from app.core.errors import PlatformError


class DecisionError(PlatformError):
    """Base for a malformed condition, decision or step list."""


class InvalidCondition(DecisionError):
    """The condition itself is wrong; it would be wrong for any screen."""


class InvalidDecision(DecisionError):
    """The decision or step list cannot be run as written."""


class Cancelled(PlatformError):
    """This run was cancelled by its caller. Not an emergency stop and not a failure."""


# How a macro a run played ended, when it did not complete (`run.perform`, the one road to playback): in this
# layer's own words, so nothing but run.py needs to know the playback engine's.
class ActionRefused(DecisionError):
    """Preflight said no; nothing was sent. `issues` say why."""

    def __init__(self, message: str, issues=()) -> None:
        super().__init__(message)
        self.issues = tuple(issues)


class ActionFailed(DecisionError):
    """The macro started and failed part-way; `code` is the playback's own, `report` its report (what it let go
    of as it ended, and what it could not)."""

    def __init__(self, message: str, code: str = "", report=None) -> None:
        super().__init__(message)
        self.code, self.report = code, report


class ActionCancelled(Cancelled):
    """The macro was cancelled while it played; `reason` says by what ("emergency_stop", "cancelled", ...),
    `report` is its report."""

    def __init__(self, reason: str, report=None) -> None:
        super().__init__(reason)
        self.reason, self.report = reason, report
