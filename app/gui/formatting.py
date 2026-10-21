"""Human-readable text for recordings and results. Pure functions: no Tk, no provider knowledge."""
from __future__ import annotations

from app.providers.recording import PointerButton, RecordedEvent, RecordedEventKind as K, Recording


def format_time(ms: int) -> str:
    ms = max(int(ms), 0)
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{ms % 1000:03d}"


def _key_label(value: str) -> str:
    return value.upper() if len(value) == 1 or (value[0] == "f" and value[1:].isdigit()) else value.capitalize()


def describe_event(event: RecordedEvent) -> tuple[str, str]:
    """(what, detail). Text content is never echoed, only its length."""
    if event.kind is K.POINTER_MOVE:
        return "Move", f"({event.x}, {event.y})"
    if event.kind in (K.BUTTON_DOWN, K.BUTTON_UP):
        side = "Left" if event.button is PointerButton.PRIMARY else "Right"
        word = "Down" if event.kind is K.BUTTON_DOWN else "Up"
        double = " (double)" if event.click_count == 2 else ""
        return f"{side} {word}{double}", f"({event.x}, {event.y})"
    if event.kind in (K.KEY_DOWN, K.KEY_UP):
        return ("Key Down" if event.kind is K.KEY_DOWN else "Key Up"), _key_label(event.key.value)
    return "Text", f"{len(event.text)} characters"


def event_rows(recording: Recording) -> list[tuple[str, str, str]]:
    """(time since the first event, what, detail) for every recorded event."""
    rows, elapsed = [], 0
    for event in recording.events:
        elapsed += event.delay_ms
        what, detail = describe_event(event)
        rows.append((format_time(elapsed), what, detail))
    return rows


def format_skipped(recording: Recording) -> str:
    names = {"secure_input": "password-field keystrokes", "unsupported_key": "unsupported keys"}
    return ", ".join(f"{n} {names.get(reason, reason)}" for reason, n in recording.skipped)


END_REASONS = {
    "stopped": "Stopped.",
    "control_hotkey": "Stopped with F10.",
    "cancelled": "Cancelled.",
    "emergency_stop": "Emergency stop.",
    "event_limit": "The recording reached the event limit and was discarded.",
    "internal_error": "The recorder hit an internal error and discarded the recording.",
    "start_failed": "Recording could not be started.",
}


def explain_end_reason(reason: str | None) -> str:
    return END_REASONS.get(reason or "", f"Ended ({reason}).")
