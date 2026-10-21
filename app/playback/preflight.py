"""Preflight: decide, BEFORE any input is sent, whether a macro can run on one explicitly chosen target.

`preflight(macro, target, input)` never sends anything and never repairs anything. It returns every problem it
finds as a structured `Issue` (code, message, action index); a macro runs only when there are no errors.

Checks
  target       the target is a verified `Target`; its provider id equals the macro's; its target id equals the macro's
               unless the caller explicitly allows a different target of the same provider (`allow_different_target`:
               a macro recorded in one window / device instance may deliberately be bound to another; the provider is
               never allowed to differ);
  input        the bound input session belongs to the same target and offers a method for every operation used;
  macro        not empty; every action is a well-formed `MacroAction`; coordinates are non-negative (a recorded drag
               that left the target's area cannot be replayed; the upper bound is checked by the provider against the
               live size at execution time);
  capability   every action's operation is declared by the target (`Capability`); operations no provider offers
               (secondary-button hold) are always rejected. Nothing is skipped: ONE unsupported action rejects the
               whole macro, so zero events are sent;
  value        an operation can be declared AND have a bound method and still be refused for the specific VALUE
               an action carries (for example: text containing a character one provider's transport cannot send
               safely, even though another provider would accept the same text). The bound input may expose an
               optional `unsupported_reason(action) -> str | None` method; when present, it is asked about every
               action and a non-None answer is a preflight error, before any input. This is a plain, duck-typed
               extension point exactly like `action_limit`/`target_id` above: preflight never inspects VALUES
               itself and never imports a provider to do so; only the bound input knows its own value-level
               limits;
  sequence     the provider's own held-state rules: no press while held, no release without a press, no click while
               the primary button is held, no key down/press while held, no key up without a down.
A macro that ends with a button or key still held gets a WARNING (the run lets go of it as it ends, where its
input session can); it does not block.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import PlatformError
from app.playback.macro import METHOD, POINTER_OPS, REQUIRED_CAPABILITY, ActionOp, Macro, MacroAction
from app.providers.target import Target, require_verified

ERROR, WARNING = "error", "warning"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    index: int | None = None          # action index the issue is about (None: the macro / binding as a whole)
    severity: str = ERROR


@dataclass(frozen=True)
class PreflightReport:
    issues: tuple[Issue, ...]

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity == WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


_CLICKS = frozenset({ActionOp.TAP, ActionOp.SECONDARY_TAP, ActionOp.DOUBLE_TAP})


def preflight(macro: object, target: object, input: object = None, *, allow_different_target: bool = False) -> PreflightReport:
    issues: list[Issue] = []
    add = lambda code, msg, index=None, sev=ERROR: issues.append(Issue(code, msg, index, sev))   # noqa: E731
    if not isinstance(macro, Macro):
        add("not_a_macro", "A Macro is required.")
        return PreflightReport(tuple(issues))
    try:
        verified: Target | None = require_verified(target)
    except PlatformError as exc:
        verified = None
        add("target_not_verified", str(exc))
    if verified is not None:
        if verified.provider_id != macro.target.provider_id:
            add("wrong_provider", f"The macro was recorded for provider '{macro.target.provider_id}', "
                                  f"the target belongs to '{verified.provider_id}'.")
        elif verified.target_id != macro.target.target_id and not allow_different_target:
            add("target_mismatch", "The macro was recorded for a different target; binding it to this one must be "
                                   "allowed explicitly.")
    if input is not None and verified is not None:
        if getattr(input, "target_id", None) != verified.target_id:
            add("input_bound_to_other_target", "The input session is not bound to the target that runs the macro.")

    actions = macro.actions
    if not actions:
        add("empty_macro", "The macro has no actions.")
    limit = getattr(input, "action_limit", None) if input is not None else None
    if isinstance(limit, int) and not isinstance(limit, bool) and len(actions) > limit:
        add("too_many_actions", f"The macro has {len(actions)} actions; this target's input session allows at most "
                                f"{limit} per run. Nothing was sent.")
    caps = verified.capabilities if verified is not None else None
    held_button, held_keys = False, set()
    for i, a in enumerate(actions):
        if not isinstance(a, MacroAction):
            add("malformed_action", "Not a MacroAction.", i)
            continue
        needed = REQUIRED_CAPABILITY[a.op]
        if needed is None:
            add("unsupported_operation", f"{a.describe()}: no provider offers this operation.", i)
        elif caps is not None and needed not in caps:
            add("unsupported_capability", f"{a.describe()}: the target does not declare '{needed.value}'.", i)
        if input is not None and not callable(getattr(input, METHOD[a.op], None)) and needed is not None:
            add("provider_lacks_operation", f"{a.describe()}: the bound input has no '{METHOD[a.op]}' operation.", i)
        value_check = getattr(input, "unsupported_reason", None) if input is not None else None
        if callable(value_check):
            reason = value_check(a)
            if reason:
                add("unsupported_value", f"{a.describe()}: {reason}", i)
        if a.op in POINTER_OPS and (a.x < 0 or a.y < 0):
            add("invalid_coordinates", f"{a.describe()}: negative client coordinates cannot be replayed.", i)
        if a.op is ActionOp.BUTTON_DOWN:
            if held_button:
                add("button_already_held", f"{a.describe()}: the primary button is already held.", i)
            held_button = True
        elif a.op is ActionOp.BUTTON_UP:
            if not held_button:
                add("button_not_held", f"{a.describe()}: the primary button is not held.", i)
            held_button = False
        elif a.op in _CLICKS and held_button:
            add("click_while_button_held", f"{a.describe()}: a click while the primary button is held.", i)
        elif a.op in (ActionOp.KEY_DOWN, ActionOp.KEY_PRESS):
            if a.key in held_keys:
                add("key_already_held", f"{a.describe()}: the key is already held.", i)
            if a.op is ActionOp.KEY_DOWN:
                held_keys.add(a.key)
        elif a.op is ActionOp.KEY_UP:
            if a.key not in held_keys:
                add("key_not_held", f"{a.describe()}: the key is not held.", i)
            held_keys.discard(a.key)
    if held_button or held_keys:
        add("held_at_end", "The macro ends with a button or key still held; the run lets go of it as it ends.", None,
            WARNING)
    return PreflightReport(tuple(issues))
