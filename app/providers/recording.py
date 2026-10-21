"""Provider-neutral recording vocabulary and recorder session.

A *recording* is an explicit, immutable, inspectable object: what a user did in ONE target, as a list of neutral
events with relative delays. Nothing here plays anything back; a later macro / workflow layer may consume it.

Vocabulary (`RecordedEventKind`)
  pointer_move   the pointer moved to (x, y)
  button_down    a pointer button was pressed at (x, y); `button` and `click_count` (1 single, 2 double click)
  button_up      that button was released at (x, y); same `button`, `click_count` of the press it ends
  key_down       a key (`app.providers.input.Key`) went down
  key_up         that key went up
  text           a short printable string a provider observed as text (a provider that cannot observe text
                 without extra machinery never emits it; the first recorder implementation does not, see docs/ARCHITECTURE.md)
A click is a button_down followed by a button_up; a double click is a pair whose second press has click_count 2;
modifiers are ordinary key events, so Ctrl down, A down, A up, Ctrl up stay four separate events.

Buttons are `primary` / `secondary` (what the user's system treats as the main / context button), the same
vocabulary as the input capabilities. Keys are the neutral `Key` enum; a provider that meets a key outside that set
skips it and counts it in `skipped` ("unsupported_key"); it never substitutes another key.

Timestamp meaning: `delay_ms` is the number of whole milliseconds (rounded to nearest, half up) between the
PREVIOUS RECORDED event and this one, measured on a monotonic high-resolution clock (`time.perf_counter_ns`;
`time.monotonic` can step 15.6 ms on some systems). The first event has delay 0 whatever time passed since recording started. Wall-clock time is never used or stored. Sum of delays = duration from first to
last event. Moves are rate-limited by the provider, so timing noise below a few milliseconds is not preserved.

Coordinates: integer pixels, relative to the target's own content area: (0, 0) is its top-left, x grows right, y
grows down; they follow the target when it moves and are never converted to screen positions or clamped. A value
outside the content area can only appear for a drag that began inside it (the release / moves are then recorded
where they really happened).

Session lifecycle (`RecorderState`): IDLE -> RECORDING -> COMPLETED | CANCELLED | INVALID_TARGET, and
`discard()` -> IDLE. Stopping is atomic, so there is no separate "stopping" state. Only COMPLETED exposes a
`Recording`; cancelled and invalid-target sessions expose only the reason. A finished session refuses `start()`
until `discard()` is called explicitly, so events can never leak into an earlier recording. This module knows
nothing about any operating system, device or application.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from app.core.errors import SafetyViolation
from app.providers.base import TargetIdentity, check_size
from app.providers.input import Key

MAX_TEXT_LENGTH = 256
MAX_EVENTS = 100_000
_COORD_LIMIT = 1_000_000
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RecorderStateError(SafetyViolation):
    """An operation is not allowed in the recorder's current state; nothing changed."""


class RecordedEventKind(str, Enum):
    POINTER_MOVE = "pointer_move"
    BUTTON_DOWN = "button_down"
    BUTTON_UP = "button_up"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    TEXT = "text"


class PointerButton(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class RecorderState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INVALID_TARGET = "invalid_target"


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_reason(reason: object) -> bool:
    return isinstance(reason, str) and bool(_REASON.match(reason))


@dataclass(frozen=True)
class RecordedEvent:
    kind: RecordedEventKind
    delay_ms: int
    x: int | None = None
    y: int | None = None
    button: PointerButton | None = None
    click_count: int | None = None
    key: Key | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecordedEventKind):
            raise ValueError("kind must be a RecordedEventKind.")
        if not _is_int(self.delay_ms) or self.delay_ms < 0:
            raise ValueError("delay_ms must be a non-negative integer.")
        pointer = self.kind in (RecordedEventKind.POINTER_MOVE, RecordedEventKind.BUTTON_DOWN,
                                RecordedEventKind.BUTTON_UP)
        button = self.kind in (RecordedEventKind.BUTTON_DOWN, RecordedEventKind.BUTTON_UP)
        keyed = self.kind in (RecordedEventKind.KEY_DOWN, RecordedEventKind.KEY_UP)
        texted = self.kind is RecordedEventKind.TEXT
        for name, present, wanted in (("x", self.x is not None, pointer), ("y", self.y is not None, pointer),
                                      ("button", self.button is not None, button),
                                      ("click_count", self.click_count is not None, button),
                                      ("key", self.key is not None, keyed), ("text", self.text is not None, texted)):
            if present != wanted:
                raise ValueError(f"{self.kind.value} {'requires' if wanted else 'does not take'} '{name}'.")
        if pointer:
            for v in (self.x, self.y):
                if not _is_int(v) or abs(v) > _COORD_LIMIT:
                    raise ValueError("Coordinates must be integers of sane magnitude.")
        if button:
            if not isinstance(self.button, PointerButton):
                raise ValueError("button must be a PointerButton.")
            if not _is_int(self.click_count) or self.click_count not in (1, 2):
                raise ValueError("click_count must be 1 or 2.")
        if keyed and not isinstance(self.key, Key):
            raise ValueError("key must be a neutral Key.")
        if texted and (not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_TEXT_LENGTH
                       or not all(c.isprintable() for c in self.text)):
            raise ValueError(f"text must be 1..{MAX_TEXT_LENGTH} printable characters.")


@dataclass(frozen=True)
class Recording:
    """A finished recording. `skipped` maps a reason code to how many observed events were deliberately not
    recorded (for example "secure_input", "unsupported_key"): nothing is ever dropped silently. `size` is the size of
    the space its points are in, when it is known and stayed the same while recording (app/providers/size.py): what
    a macro that adapts to the size scales from."""
    provider_id: str
    target_id: str
    events: tuple[RecordedEvent, ...]
    end_reason: str = "stopped"
    skipped: tuple[tuple[str, int], ...] = ()
    size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        TargetIdentity(self.provider_id, self.target_id)      # validates non-empty strings
        if not isinstance(self.events, tuple) or not all(isinstance(e, RecordedEvent) for e in self.events):
            raise ValueError("events must be a tuple of RecordedEvent.")
        if self.events and self.events[0].delay_ms != 0:
            raise ValueError("The first event must have delay 0.")
        if not _valid_reason(self.end_reason):
            raise ValueError("end_reason must be a lower-case code.")
        if list(self.skipped) != sorted(self.skipped) or len({k for k, _ in self.skipped}) != len(self.skipped) \
                or not all(_valid_reason(k) and _is_int(n) and n > 0 for k, n in self.skipped):
            raise ValueError("skipped must be sorted unique (reason, positive count) pairs.")
        object.__setattr__(self, "size", check_size(self.size))

    @property
    def target(self) -> TargetIdentity:
        return TargetIdentity(self.provider_id, self.target_id)

    @property
    def duration_ms(self) -> int:
        return sum(e.delay_ms for e in self.events)

    def __len__(self) -> int:
        return len(self.events)


_CHANGED = object()          # the size changed while recording


class RecordingSession:
    """State machine + append-only event log for ONE recording of ONE target. Thread-safe.

    Providers call `record()` from their capture code; consumers call `stop()` / `cancel()` and read `recording`.
    `clock_ns` must be monotonic and fine-grained (sub-millisecond); tests inject a fake."""

    def __init__(self, *, clock_ns: Callable[[], int] = time.perf_counter_ns, max_events: int = MAX_EVENTS) -> None:
        self._clock = clock_ns
        self._max = max_events
        self._lock = threading.RLock()
        self._reset()

    def _reset(self) -> None:
        self._state = RecorderState.IDLE
        self._target: TargetIdentity | None = None
        self._events: list[RecordedEvent] = []
        self._skipped: dict[str, int] = {}
        self._last_ns: int | None = None
        self._recording: Recording | None = None
        self._end_reason: str | None = None
        self._end_detail = ""
        self._size = None

    # ---- read side -----------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        """Held by providers around one whole observation so several steps stay atomic."""
        return self._lock

    @property
    def state(self) -> RecorderState:
        return self._state

    @property
    def target(self) -> TargetIdentity | None:
        return self._target

    @property
    def recording(self) -> Recording | None:
        """The finished recording; only in COMPLETED, otherwise None."""
        return self._recording

    @property
    def end_reason(self) -> str | None:
        return self._end_reason

    @property
    def end_detail(self) -> str:
        return self._end_detail

    @property
    def event_count(self) -> int:
        return len(self._events)

    # ---- transitions -----------------------------------------------------
    def start(self, target: TargetIdentity) -> None:
        with self._lock:
            if not isinstance(target, TargetIdentity):
                raise RecorderStateError("A recording needs an explicit target identity.")
            if self._state is RecorderState.RECORDING:
                raise RecorderStateError("A recording is already in progress.")
            if self._state is not RecorderState.IDLE:
                raise RecorderStateError("The previous recording must be discarded (discard()) before starting a new one.")
            self._target = target
            self._state = RecorderState.RECORDING

    def record(self, kind: RecordedEventKind, *, x: int | None = None, y: int | None = None,
               button: PointerButton | None = None, click_count: int | None = None, key: Key | None = None,
               text: str | None = None, at_ns: int | None = None) -> RecordedEvent:
        """Append one event. `at_ns` is the monotonic time it happened (default: now); it may not go backwards."""
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError("Events can only be recorded while recording.")
            now = self._clock() if at_ns is None else at_ns
            if not _is_int(now) or (self._last_ns is not None and now < self._last_ns):
                raise RecorderStateError("Event time must be a monotonic integer (nanoseconds).")
            delay = 0 if self._last_ns is None else (now - self._last_ns + 500_000) // 1_000_000
            event = RecordedEvent(kind, delay, x, y, button, click_count, key, text)   # validates
            if len(self._events) >= self._max:
                self._end(RecorderState.CANCELLED, "event_limit", f"more than {self._max} events")
                raise RecorderStateError("Event limit reached; the recording was cancelled.")
            self._events.append(event)
            self._last_ns = now
            return event

    def note_size(self, size: tuple[int, int]) -> None:
        """The size of the space the points recorded now are in. A recording whose size changed while recording keeps
        none: its points are not all in one space, so it cannot be adapted to another size."""
        size = check_size(size)
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError("Only a running recording can note its size.")
            if self._size is None:
                self._size = size
            elif self._size != size:
                self._size = _CHANGED

    def note_skipped(self, reason: str) -> None:
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError("Only a running recording can note skipped events.")
            if not _valid_reason(reason):
                raise ValueError("reason must be a lower-case code.")
            self._skipped[reason] = self._skipped.get(reason, 0) + 1

    def stop(self, reason: str = "stopped") -> Recording:
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError(f"Cannot stop: the recorder is {self._state.value}, not recording.")
            recording = Recording(self._target.provider_id, self._target.target_id, tuple(self._events), reason,
                                  tuple(sorted(self._skipped.items())),      # validated before any state change
                                  None if self._size is _CHANGED else self._size)
            self._end(RecorderState.COMPLETED, reason, "")
            self._recording = recording
            return recording

    def cancel(self, reason: str = "cancelled", detail: str = "") -> None:
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError(f"Cannot cancel: the recorder is {self._state.value}, not recording.")
            self._end(RecorderState.CANCELLED, reason, detail)

    def invalidate(self, reason: str, detail: str = "") -> None:
        """The target can no longer be trusted: end deterministically, expose no recording, keep the reason."""
        with self._lock:
            if self._state is not RecorderState.RECORDING:
                raise RecorderStateError(f"Cannot invalidate: the recorder is {self._state.value}, not recording.")
            self._end(RecorderState.INVALID_TARGET, reason, detail)

    def discard(self) -> None:
        """Forget a finished (or empty) session and return to IDLE. Refused while recording."""
        with self._lock:
            if self._state is RecorderState.RECORDING:
                raise RecorderStateError("Stop or cancel the running recording before discarding.")
            self._reset()

    def _end(self, state: RecorderState, reason: str, detail: str) -> None:
        if not _valid_reason(reason):
            raise ValueError("reason must be a lower-case code.")
        self._state, self._end_reason, self._end_detail = state, reason, detail
        self._events = []           # a session that did not complete keeps no events
        if state is not RecorderState.COMPLETED:
            self._skipped = {}


@runtime_checkable
class Recorder(Protocol):
    """What every provider's recorder offers. It is bound to ONE explicitly chosen target at construction."""

    @property
    def state(self) -> RecorderState: ...

    @property
    def target_identity(self) -> TargetIdentity: ...

    @property
    def recording(self) -> Recording | None: ...

    @property
    def end_reason(self) -> str | None: ...

    def start(self) -> None: ...

    def stop(self) -> Recording: ...

    def cancel(self) -> None: ...

    def discard(self) -> None: ...
