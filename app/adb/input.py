"""Controlled input session, built ONLY from a VerifiedTarget.

Three primitives: `tap`, `send_keyevent`, `type_text`. There is deliberately no swipe/drag, no separate
key-down/key-up, no app-launch and no shell method -- see "Deliberately not implemented" below. This module is
technology-only: it knows Android keycodes and ADB serials, never the generic `app.providers` vocabulary (`Key`
translation happens one layer up, in the LDPlayer/ADB playback adapter package -- see `app.ldplayer.keymap` --
exactly as `app.ldplayer.provider` bridges screenshots the same way).

Every operation is gated:
  1. emergency stop not active;
  2. the target's proof is fresh and the device is online (AdbSession gate, re-verifying through the
     MappingService when the proof is stale) -- via `AdbSession.display()` for `tap` (which also needs the
     current display size) or `AdbSession.ensure_online()` for `send_keyevent`/`type_text` (which do not);
  3. `tap` additionally requires integer coordinates inside the device's CURRENT display (queried, not assumed);
  4. the session's explicit action budget (`max_actions`) is not exhausted (live only, see below). One call to any
     of the three methods is one budget unit, matching the generic layer's "one MacroAction, one input-session
     call" contract.

Only this module can mint the `InputGrant` that `AdbClient.input_tap`/`input_keyevent`/`input_text` demand.

Dry run (`dry_run=True`): gates 1-3 above still run in full, including the real freshness/identity re-verification
and (for `tap`) the real display query the bounds check needs. Nothing about establishing that the target is
CURRENTLY valid is skipped for any of the three operations. Only the actual `adb shell input ...` call and the
budget are skipped; the intended action is recorded (`intended`) instead. A dry run can therefore never report
success for a target whose identity can no longer be revalidated, exactly like a live run would refuse it -- it
just sends nothing. This mirrors the Windows session's own dry-run gate (`app/windows/input.py`).

Deliberately not implemented (and why -- see docs/ARCHITECTURE.md for the full record):
  a separate key-up  `adb shell input keyevent` always injects a complete press (down immediately followed by
                     up) as one atomic device-side operation; there is no portable way to send just a down and,
                     independently, a later up. Capability.KEY is still declared because a full press genuinely
                     works end to end; a macro containing a held key (KEY_DOWN without an immediate KEY_UP) is
                     refused by the generic preflight's own `provider_lacks_operation` check (the adapter offers
                     no such method), before any input, exactly like any other operation this session does not
                     offer.
  swipe / drag       the generic macro model represents a drag as discrete `move`/`button_down`/`button_up`
                     actions, each independently timed and independently gated; `adb shell input` has no
                     primitive for an indefinite touch-and-hold with a later, independently-timed release or
                     intermediate move (only the single atomic two-point `input swipe x1 y1 x2 y2 [duration]`,
                     which is a different shape entirely). Forcing recorded down/move/up events onto that shape
                     would silently discard the recorded path and its real timing, so this stage does not
                     implement Capability.POINTER_MOVE / Capability.POINTER_BUTTON for ADB; a macro needing them
                     is refused by preflight's `unsupported_capability` check, before any input.
  secondary_tap      a touchscreen has no secondary mouse button; there is nothing to map this to.
  double_tap         not part of this stage's requested scope; ADB has no atomic double-tap primitive (it would
                     have to be two ordinary `tap`s), so it is left for a future stage rather than guessed at.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from app.core.cancel import EmergencyStop
from app.core.errors import SafetyViolation, TargetNotVerified
from app.instances.mapping import MappingService, VerifiedTarget

from . import client as _client
from .client import AdbClient, AdbError, SAFE_TEXT_PATTERN
from .session import AdbSession

MAX_SESSION_ACTIONS = 1000   # the largest action budget one session accepts


class InputBudgetExceeded(SafetyViolation):
    pass


@dataclass(frozen=True)
class IntendedInput:
    """One action a dry run would have performed, in order (see `InputSession.intended`), in ADB-native terms
    (a raw keycode, not a neutral key name -- this module never sees `Key`). Mirrors the shape of
    `app.windows.input.IntendedInput`; kept as its own type because this module is intentionally self-contained
    (no shared technology-specific type lives in a generic layer)."""
    action: str                        # "tap" | "key_event" | "type_text"
    x: int | None = None
    y: int | None = None
    keycode: int | None = None
    text_length: int | None = None     # the text itself is never recorded (it may be sensitive)

    def describe(self) -> str:
        if self.x is not None:
            return f"{self.action} ({self.x}, {self.y})"
        if self.keycode is not None:
            return f"{self.action} keycode {self.keycode}"
        if self.text_length is not None:
            return f"{self.action} ({self.text_length} characters)"
        return self.action


class InputSession:
    def __init__(self, target: VerifiedTarget, adb: AdbClient, mapping: MappingService, stop: EmergencyStop, *,
                 max_actions: int, max_age_s: float = 60.0, dry_run: bool = False) -> None:
        if not isinstance(target, VerifiedTarget):
            raise TargetNotVerified("An InputSession can only be created from a VerifiedTarget.",
                                    reason_code="not_verified_target")
        if (isinstance(max_actions, bool) or not isinstance(max_actions, int)
                or not 1 <= max_actions <= MAX_SESSION_ACTIONS):
            raise ValueError(f"max_actions must be an explicit integer between 1 and {MAX_SESSION_ACTIONS}")
        self._adb = adb
        self._stop = stop
        self._read = AdbSession(target, adb, mapping, stop, max_age_s=max_age_s)
        self._budget = max_actions
        self._sent = 0
        self._dry_run = bool(dry_run)
        self._intended: list[IntendedInput] = []
        self._lock = threading.Lock()

    @property
    def target(self) -> VerifiedTarget:
        return self._read.target

    @property
    def target_id(self) -> str:
        """Logical id of the verified target (satisfies the provider-independent InputProvider interface)."""
        return self._read.target.logical_id

    @property
    def actions_sent(self) -> int:
        return self._sent

    @property
    def action_limit(self) -> int:
        """The most actions one run through this session may contain (the neutral preflight reads this)."""
        return MAX_SESSION_ACTIONS

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def intended(self) -> tuple[IntendedInput, ...]:
        """Actions a dry run would have sent, in order (empty for a live session)."""
        return tuple(self._intended)

    def tap(self, x: int, y: int) -> None:
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
            raise AdbError("Tap coordinates must be integers.").saying(ru="Координаты нажатия должны быть целыми числами.")
        with self._lock:
            if not self._dry_run and self._sent >= self._budget:
                raise InputBudgetExceeded(f"This input session allows {self._budget} action(s) and they are used up.")
            # Always runs, dry or live: the real freshness/identity re-verification and the real display query
            # the bounds check needs (AdbSession._gate(), via display()). A dry run never skips establishing that
            # the target is CURRENTLY valid; only the tap itself and the budget are skipped below.
            display = self._read.display()
            if not (0 <= x < display.width and 0 <= y < display.height):
                raise AdbError(f"Tap ({x}, {y}) is outside the {display.width}x{display.height} display; nothing sent.").saying(
                    ru=f"Нажатие ({x}, {y}) за пределами экрана {display.width}x{display.height}; ничего не отправлено.")
            if self._dry_run:
                self._intended.append(IntendedInput("tap", x=x, y=y))
                return
            serial = self._read.target.adb_serial
            self._sent += 1  # counted before sending: a tap with an unknown outcome still uses budget
            self._adb.input_tap(serial, x, y, grant=_client.InputGrant(serial, _client._INPUT_MINT))

    def send_keyevent(self, keycode: int) -> None:
        """Send one complete Android key press (`adb shell input keyevent <keycode>`). `keycode` is always the
        output of `app.ldplayer.keymap.android_keycode` -- this module has no notion of a neutral `Key`."""
        if isinstance(keycode, bool) or not isinstance(keycode, int) or not 0 <= keycode <= 999:
            raise AdbError("Android keycode must be an integer between 0 and 999.")
        with self._lock:
            if not self._dry_run and self._sent >= self._budget:
                raise InputBudgetExceeded(f"This input session allows {self._budget} action(s) and they are used up.")
            # Same freshness/identity/device-state gate tap() runs via display() -- there is no coordinate here,
            # so there is nothing to bound-check, but the target must still be proven CURRENTLY valid.
            self._read.ensure_online()
            if self._dry_run:
                self._intended.append(IntendedInput("key_event", keycode=keycode))
                return
            serial = self._read.target.adb_serial
            self._sent += 1
            self._adb.input_keyevent(serial, keycode, grant=_client.InputGrant(serial, _client._INPUT_MINT))

    def type_text(self, text: str) -> None:
        if not isinstance(text, str) or not SAFE_TEXT_PATTERN.match(text):
            raise AdbError("Text may contain only ASCII letters, digits, space, '.', ',', '_' and '-' "
                           "(1..256 characters); nothing sent.")
        with self._lock:
            if not self._dry_run and self._sent >= self._budget:
                raise InputBudgetExceeded(f"This input session allows {self._budget} action(s) and they are used up.")
            self._read.ensure_online()
            if self._dry_run:
                self._intended.append(IntendedInput("type_text", text_length=len(text)))
                return
            serial = self._read.target.adb_serial
            self._sent += 1
            self._adb.input_text(serial, text, grant=_client.InputGrant(serial, _client._INPUT_MINT))
