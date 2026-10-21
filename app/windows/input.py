"""Windows input session: small, target-scoped pointer / keyboard operations on ONE verified window.

Implements the provider-neutral input interfaces (`InputProvider`, `PointerProvider`, `KeyboardProvider`) for a
`WindowTarget`. It is built only from a verified `WindowTarget`, with an explicit action budget, and it can never
address anything but that window: the operating-system layer (`WindowInputBackend`) resolves every pointer
operation to a real, OS-verified screen point within that SAME verified window's own hierarchy before sending a
real click there (see `app/windows/win32.py`'s module docstring), refusing rather than ever misdirecting a click
if another window covers it; key and character operations still post a message to that same window or a
descendant of it. There is still no fallback to the foreground window or to global coordinates, and no "all
windows" behaviour -- with one exception that presses nothing: `release_held` lets go, where things are now, of
what this session itself left down at the end of a live run.

Operations and the capability each one needs (declared explicitly by the target, checked on every call)
  tap(x, y)              TAP             primary-button click                 press, release
  secondary_tap(x, y)    SECONDARY_TAP   secondary-button click               press, release
  double_tap(x, y)       DOUBLE_TAP      primary-button double click          press, release | double-click, release
  move(x, y)             POINTER_MOVE    pointer move (no button change)      one move
  button_down(x, y)      POINTER_BUTTON  primary button press                 one press
  button_up(x, y)        POINTER_BUTTON  primary button release               one release
  key_press(key)         KEY             key press                            down, up
  key_down(key)          KEY             key down                             one down
  key_up(key)            KEY             key up                               one up
  type_text(text)        TEXT            printable text                       one character message per character
"One operation" means exactly the messages in the last column, no more. It counts as ONE action against the
budget. A sequence is split into phases (the pairs above, or one character each); the full set of gates runs before
the first phase AND again before every later phase. If a gate fails or a message is refused after some messages
were already delivered, the rest is NOT sent and `PartialInputDelivery` reports how far it got: it never continues
silently. Held buttons/keys are tracked by the session (`left_button_held`, `held_keys`): a press while already
held, a release without a press, and clicks while the primary button is held are refused. At the end of a live run
that left something pressed, `release_held` lets go of exactly that (the person's decision of 2026-10-20).

Coordinate model (all pointer operations, including move / button_down / button_up)
  * origin      top-left pixel of the window's CLIENT area (the drawable area inside the frame, title bar and
                borders excluded) is (0, 0); x grows to the right, y grows downward;
  * units       integer pixels of the client area as measured by a per-monitor-DPI-aware caller (physical
                pixels). No scaling is ever applied. This is a meaningful scale for the target only if the target
                is DPI-aware for its monitor; for a DPI-unaware target on a scaled monitor the scale is ambiguous
                and every operation is REFUSED (`WindowInfo.dpi_ambiguous`). Nothing above 96 dpi is verified;
  * shared      this is exactly the coordinate space of `WindowScreenshotProvider` images: screenshot pixel (x, y)
                is client coordinate (x, y);
  * bounds      0 <= x < client width and 0 <= y < client height, measured at operation time. Anything else and any
                non-integer (bool, float, str) is REJECTED. Nothing is ever clamped or rounded;
  * moving      coordinates are relative to the window, so moving it changes nothing and cannot redirect input;
  * resizing    bounds are re-read at every gate; a coordinate that no longer fits is rejected, one that still fits
                refers to the window's NEW layout. The caller owns knowing what is at that point;
  * move        a move message carries the point; button_down / button_up carry the point they are given, so a drag
                is later composed as move, button_down, move, button_up with the session tracking the held button.

Keyboard model: keys are the neutral `app.providers.input.Key` (letters, digits, Enter, Escape, Space, Tab,
Backspace, Delete, arrows, Shift, Ctrl, Alt, F1-F12); anything else is rejected. Key messages are per-key: modifier
state is not tracked or combined, so shortcuts (Ctrl+C) are unsupported. Text is 1..256 printable characters of the
basic multilingual plane, validated completely before anything is sent; control characters (newline, tab) are
rejected: use `key_press` for those.

Gates before every real input (any failure sends zero events for that step):
  capability declared and target verified (WindowTarget only) -> integer/valid arguments -> emergency stop ->
  budget -> `WindowVerifier.recheck` (live, visible, top-level, same process instance, same class) -> not minimized,
  not hidden by the desktop manager, not disabled -> coordinates inside the CURRENT client area -> held-state
  rules -> emergency stop once more immediately before sending.
A dry run performs all gates and then records what it would have done (`IntendedInput`) without sending anything and
without using budget; it also tracks held state so a dry-run sequence behaves like the live one. This mirrors the
dry-run pattern of the LDPlayer side (verify, check bounds, report, send nothing).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from app.core.cancel import EmergencyStop
from app.core.errors import PlatformError, SafetyViolation, TargetNotVerified
from app.providers.input import Key
from app.providers.target import Capability, Target, require_capability, require_verified
from app.windows.backend import InputDeliveryError, KeyEvent, MouseEvent, WindowInputBackend
from app.windows.target import WindowTarget, WindowVerifier

MAX_TEXT_LENGTH = 256
MAX_SESSION_ACTIONS = 100_000   # the largest action budget one session accepts (measured, see docs/ARCHITECTURE.md)
INPUT_CAPABILITIES = frozenset({Capability.TAP, Capability.SECONDARY_TAP, Capability.DOUBLE_TAP,
                                Capability.POINTER_MOVE, Capability.POINTER_BUTTON, Capability.KEY, Capability.TEXT})


class InputRefused(SafetyViolation):
    """The operation was refused by a safety gate; no input event was sent."""


class InputBudgetExceeded(InputRefused):
    pass


class PartialInputDelivery(InputDeliveryError):
    """A multi-message operation stopped after some messages were delivered; the remainder was NOT sent."""

    def __init__(self, message: str, *, delivered: int, total: int) -> None:
        super().__init__(message)
        self.delivered, self.total = delivered, total


@dataclass(frozen=True)
class IntendedInput:
    provider_id: str
    target_id: str
    action: str
    x: int | None = None
    y: int | None = None
    key: str | None = None
    text_length: int | None = None      # the text itself is never recorded (it may be sensitive)

    def describe(self) -> str:
        detail = ""
        if self.x is not None:
            detail = f" at client ({self.x}, {self.y})"
        elif self.key is not None:
            detail = f" key {self.key}"
        elif self.text_length is not None:
            detail = f" ({self.text_length} characters)"
        return f"[{self.provider_id}:{self.target_id}] {self.action}{detail}"


IntendedTap = IntendedInput   # the single-click stage's name, kept for compatibility

_Send = Callable[[], None]


def _validate_text(text: object) -> str:
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT_LENGTH:
        raise InputRefused(f"Text must be a string of 1..{MAX_TEXT_LENGTH} characters; nothing sent.")
    for ch in text:
        if not ch.isprintable() or ord(ch) > 0xFFFF or 0xD800 <= ord(ch) <= 0xDFFF:
            raise InputRefused("Text may only contain printable characters of the basic multilingual plane "
                               "(use key_press for Enter/Tab); nothing sent.")
    return text


class WindowInputSession:
    def __init__(self, target: Target, verifier: WindowVerifier, backend: WindowInputBackend, stop: EmergencyStop, *,
                 max_actions: int, dry_run: bool = False) -> None:
        require_verified(target)
        self._check_target(target)
        if not (target.capabilities & INPUT_CAPABILITIES):
            require_capability(target, Capability.TAP)          # raises CapabilityNotAvailable with a clear message
        if isinstance(max_actions, bool) or not isinstance(max_actions, int) or not 1 <= max_actions <= MAX_SESSION_ACTIONS:
            raise ValueError(f"max_actions must be an explicit integer between 1 and {MAX_SESSION_ACTIONS}")
        if not isinstance(backend, WindowInputBackend):
            raise TypeError("backend must be a WindowInputBackend")
        self._target = target
        self._verifier = verifier
        self._backend = backend
        self._stop = stop
        self._budget = max_actions
        self._sent = 0
        self._dry_run = bool(dry_run)
        self._intended: list[IntendedInput] = []
        self._left_held = False
        self._held_keys: set[Key] = set()
        # what is DOWN, message by message as delivered -- also a click or key press cut off between its press and its
        # release, which no held-state rule ever counted: what `release_held` lets go of
        self._buttons_down: set[str] = set()
        self._keys_down: set[Key] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _check_target(target: Target) -> None:
        if not isinstance(target, WindowTarget):
            raise TargetNotVerified("The Windows input session can only act on a WindowTarget.",
                                    reason_code="not_verified_target")

    # ---- read-only state -------------------------------------------------
    @property
    def target_id(self) -> str:
        """Satisfies the provider-independent interfaces."""
        return self._target.target_id

    @property
    def provider_id(self) -> str:
        return self._target.provider_id

    @property
    def action_limit(self) -> int:
        """The most actions one run through this session may contain (the neutral preflight reads this)."""
        return MAX_SESSION_ACTIONS

    @property
    def actions_sent(self) -> int:
        return self._sent

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def intended(self) -> tuple[IntendedInput, ...]:
        """Actions a dry run would have performed (empty for a live session)."""
        return tuple(self._intended)

    @property
    def left_button_held(self) -> bool:
        return self._left_held

    @property
    def held_keys(self) -> frozenset[Key]:
        return frozenset(self._held_keys)

    # ---- pointer operations ---------------------------------------------
    def tap(self, x: int, y: int) -> None:
        self._pointer(Capability.TAP, "tap", x, y, [[MouseEvent.LEFT_DOWN, MouseEvent.LEFT_UP]], click=True)

    def secondary_tap(self, x: int, y: int) -> None:
        self._pointer(Capability.SECONDARY_TAP, "secondary_tap", x, y,
                      [[MouseEvent.RIGHT_DOWN, MouseEvent.RIGHT_UP]], click=True)

    def double_tap(self, x: int, y: int) -> None:
        self._pointer(Capability.DOUBLE_TAP, "double_tap", x, y,
                      [[MouseEvent.LEFT_DOWN, MouseEvent.LEFT_UP], [MouseEvent.LEFT_DOUBLE, MouseEvent.LEFT_UP]],
                      click=True)

    def move(self, x: int, y: int) -> None:
        event = MouseEvent.MOVE_LEFT_HELD if self._left_held else MouseEvent.MOVE
        self._pointer(Capability.POINTER_MOVE, "move", x, y, [[event]])

    def button_down(self, x: int, y: int) -> None:
        if self._left_held:
            raise InputRefused("The primary button is already held; nothing sent.")
        self._pointer(Capability.POINTER_BUTTON, "button_down", x, y, [[MouseEvent.LEFT_DOWN]],
                      after=lambda: setattr(self, "_left_held", True))

    def button_up(self, x: int, y: int) -> None:
        if not self._left_held:
            raise InputRefused("The primary button is not held; nothing sent.")
        self._pointer(Capability.POINTER_BUTTON, "button_up", x, y, [[MouseEvent.LEFT_UP]],
                      after=lambda: setattr(self, "_left_held", False))

    # ---- keyboard operations --------------------------------------------
    def key_press(self, key: Key) -> None:
        self._require_key(key)
        if key in self._held_keys:
            raise InputRefused(f"Key {key.value} is already held; nothing sent.")
        self._keyboard(Capability.KEY, "key_press", key, [[(KeyEvent.DOWN, key), (KeyEvent.UP, key)]])

    def key_down(self, key: Key) -> None:
        self._require_key(key)
        if key in self._held_keys:
            raise InputRefused(f"Key {key.value} is already held; nothing sent.")
        self._keyboard(Capability.KEY, "key_down", key, [[(KeyEvent.DOWN, key)]],
                       after=lambda: self._held_keys.add(key))

    def key_up(self, key: Key) -> None:
        self._require_key(key)
        if key not in self._held_keys:
            raise InputRefused(f"Key {key.value} is not held; nothing sent.")
        self._keyboard(Capability.KEY, "key_up", key, [[(KeyEvent.UP, key)]],
                       after=lambda: self._held_keys.discard(key))

    def type_text(self, text: str) -> None:
        require_capability(self._target, Capability.TEXT)
        text = _validate_text(text)
        hwnd = self._target.hwnd
        phases = [[(lambda c=ch: self._backend.post_char(hwnd, c))] for ch in text]
        self._execute(IntendedInput(self.provider_id, self.target_id, "type_text",
                                                     text_length=len(text)), None, phases, None)

    # ---- letting go ---------------------------------------------------------------------------------------------
    @property
    def still_held(self) -> tuple[str, ...]:
        """What this session pressed and has not let go of -- whatever is down, also a press whose release was cut
        off -- in the names `release_held` uses: for the run's report, after it let go of what it could."""
        with self._lock:
            return tuple(f"{b}_button" for b in ("primary", "secondary") if b in self._buttons_down) + tuple(
                f"key:{k.value}" for k in sorted(self._keys_down, key=lambda k: k.value))

    def release_held(self) -> tuple[str, ...]:
        """Let go of what THIS session still holds -- at the end of a live run that left something pressed (stopped,
        failed, or recorded so; the person's decision of 2026-10-20). A release presses nothing, so it is sent where
        things are now (no pointer move, no focus change), marked as this application's own and without the gates:
        it is the one input sent after an emergency stop. Only what this session pressed, the button first (a drag
        ends before the key that went with it). Whatever is down counts: also a click or key press cut off between its
        press and its release. Returns what was let go ("primary_button", "secondary_button", "key:shift", ...);
        what the system refuses stays held. A dry run lets go of nothing (it never pressed anything)."""
        with self._lock:                         # (a dry run delivers nothing, so nothing of it is down)
            released: list[str] = []
            for button, release in (("primary", self._backend.release_primary),
                                    ("secondary", self._backend.release_secondary)):
                if button not in self._buttons_down:
                    continue
                try:
                    release()
                except InputDeliveryError:
                    continue
                self._buttons_down.discard(button)
                if button == "primary":
                    self._left_held = False
                released.append(f"{button}_button")
            for key in sorted(self._keys_down, key=lambda k: k.value):
                try:
                    self._backend.release_key(key)
                except InputDeliveryError:
                    continue
                self._keys_down.discard(key)
                self._held_keys.discard(key)
                released.append(f"key:{key.value}")
            return tuple(released)

    # ---- internals --------------------------------------------------------
    @staticmethod
    def _require_key(key: object) -> None:
        if not isinstance(key, Key):
            raise InputRefused("Not a valid key (use app.providers.input.Key); nothing sent.")

    @staticmethod
    def _require_point(x: object, y: object) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (x, y)):
            raise InputRefused("Coordinates must be integers; nothing sent.")

    def _pointer(self, capability: Capability, action: str, x: int, y: int, events: list[list[MouseEvent]], *,
                 click: bool = False, after: Callable[[], None] | None = None) -> None:
        require_capability(self._target, capability)
        self._require_point(x, y)
        if click and self._left_held:
            raise InputRefused("The primary button is held; release it before clicking; nothing sent.")
        hwnd = self._target.hwnd
        phases = [[self._noting(e, lambda e=e: self._backend.post_mouse(hwnd, e, x, y)) for e in phase]
                  for phase in events]
        self._execute(IntendedInput(self.provider_id, self.target_id, action, x, y), (x, y), phases, after)

    def _keyboard(self, capability: Capability, action: str, key: Key, events: list[list[tuple[KeyEvent, Key]]], *,
                  after: Callable[[], None] | None = None) -> None:
        require_capability(self._target, capability)
        hwnd = self._target.hwnd
        phases = [[self._noting_key(e, k, lambda e=e, k=k: self._backend.post_key(hwnd, e, k)) for e, k in phase]
                  for phase in events]
        self._execute(IntendedInput(self.provider_id, self.target_id, action, key=key.value), None, phases, after)

    _PRESSES = {MouseEvent.LEFT_DOWN: "primary", MouseEvent.LEFT_DOUBLE: "primary",   # a double click's 2nd press
                MouseEvent.RIGHT_DOWN: "secondary"}
    _RELEASES = {MouseEvent.LEFT_UP: "primary", MouseEvent.RIGHT_UP: "secondary"}

    def _noting(self, event: MouseEvent, send: _Send) -> _Send:
        """`send`, then what is down now: a delivered press is down until its release is delivered."""
        def sent() -> None:
            send()
            if event in self._PRESSES:
                self._buttons_down.add(self._PRESSES[event])
            elif event in self._RELEASES:
                self._buttons_down.discard(self._RELEASES[event])
        return sent

    def _noting_key(self, event: KeyEvent, key: Key, send: _Send) -> _Send:
        def sent() -> None:
            send()
            if event is KeyEvent.DOWN:
                self._keys_down.add(key)
            else:
                self._keys_down.discard(key)
        return sent

    def _gate(self, point: tuple[int, int] | None) -> None:
        """Every check that must hold immediately before input is sent."""
        self._stop.check()
        info = self._verifier.recheck(self._target)             # live, visible, top-level, same process and class
        if info.minimized:
            raise InputRefused("The window is minimized; nothing sent.")
        if info.cloaked:
            raise InputRefused("The window is hidden by the desktop manager; nothing sent.")
        if info.dpi_ambiguous:
            raise InputRefused("The window's DPI scale is ambiguous (a DPI-unaware program on a scaled monitor), "
                               "so pixel coordinates are not reliable; nothing sent.")
        if not info.enabled:
            raise InputRefused("The window is disabled and would ignore input; nothing sent.")
        if point is not None:
            size = self._backend.client_size(self._target.hwnd)
            if size is None or size[0] <= 0 or size[1] <= 0:
                raise InputRefused("The window's client area cannot be read; nothing sent.")
            x, y = point
            if not (0 <= x < size[0] and 0 <= y < size[1]):
                raise InputRefused(f"Point ({x}, {y}) is outside the {size[0]}x{size[1]} client area; nothing sent.")
        self._stop.check()

    def _execute(self, intent: IntendedInput, point: tuple[int, int] | None,
                 phases: list[list[_Send]], after: Callable[[], None] | None) -> None:
        with self._lock:
            self._stop.check()
            if not self._dry_run and self._sent >= self._budget:
                raise InputBudgetExceeded(f"This input session allows {self._budget} action(s) and they are used up.")
            self._gate(point)
            if self._dry_run:
                self._intended.append(intent)
                if after:
                    after()
                return
            self._sent += 1                                      # counted before sending: unknown outcome uses budget
            total = sum(len(p) for p in phases)
            delivered = 0
            for index, phase in enumerate(phases):
                if index:                                        # later phases: full gate again
                    try:
                        self._gate(point)
                    except PlatformError as exc:
                        raise PartialInputDelivery(
                            f"{intent.action} stopped after {delivered} of {total} messages: {exc}",
                            delivered=delivered, total=total) from exc
                for send in phase:
                    try:
                        send()
                    except InputDeliveryError as exc:
                        if not delivered:
                            raise
                        raise PartialInputDelivery(
                            f"{intent.action} stopped after {delivered} of {total} messages: {exc}",
                            delivered=delivered, total=total) from exc
                    delivered += 1
            if after:
                after()
