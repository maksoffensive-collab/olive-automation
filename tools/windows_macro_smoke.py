r"""Macro playback smoke test against a window created by this script (never against any other program).

Creates its own small window (not activated), verifies it, builds a tiny macro (move, click, key press, all inside
that window), runs it dry first (must deliver nothing), then for real through `create_window_playback` (which
reuses `WindowInputSession`), and checks that the window received exactly the expected messages. It also checks
that a macro recorded for another target is refused with nothing delivered, and that an emergency stop delivers
nothing. Then it closes its own window. It aims at no other program.

REAL INPUT: delivery is real, hardware-level `SendInput` (see `app.windows.win32`), not messages posted to a handle.
The live run MOVES THE REAL MOUSE POINTER over this window, brings it to the front and gives it keyboard focus; do
not use the mouse or keyboard while it runs. Run it only with the user's explicit approval.

    .venv\Scripts\python.exe tools\windows_macro_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windows_click_smoke import TestWindow  # noqa: E402

from app.core.cancel import EmergencyStop  # noqa: E402
from app.playback.engine import PlaybackRefused, PlaybackState  # noqa: E402
from app.playback.macro import Macro  # noqa: E402
from app.providers.input import Key  # noqa: E402
from app.providers.recording import PointerButton, RecordedEvent as E, RecordedEventKind as K, Recording  # noqa: E402
from app.windows.playback import create_window_playback  # noqa: E402
from app.windows.target import WindowVerifier  # noqa: E402
from app.windows.win32 import Win32InputBackend  # noqa: E402


def main() -> int:
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    win = TestWindow()
    win.start()
    time.sleep(0.3)
    failures = []
    try:
        stop = EmergencyStop()
        backend = Win32InputBackend()
        verifier = WindowVerifier(backend, stop)
        target = verifier.verify(win.hwnd)
        print(f"VERIFIED dedicated window {target.provider_id}:{target.target_id}")
        events = (E(K.POINTER_MOVE, 0, 40, 30), E(K.BUTTON_DOWN, 20, 40, 30, PointerButton.PRIMARY, 1),
                  E(K.BUTTON_UP, 30, 40, 30, PointerButton.PRIMARY, 1), E(K.KEY_DOWN, 20, key=Key.A),
                  E(K.KEY_UP, 30, key=Key.A))
        macro = Macro(Recording(target.provider_id, target.target_id, events), "smoke")

        # Real input: what OLIVE sent is what carries its mark (`TestWindow.own`); the pointer's travel to a point
        # and whatever the person's hand does over the window are not part of the comparison.
        dry = create_window_playback(macro, target, verifier, backend, stop, dry_run=True).run()
        time.sleep(0.2)
        print(f"dry run: {dry.state.value}, {dry.completed_count}/{dry.actions_total} planned, messages from Olive: "
              f"{len(win.own(moves=True))} ({len(win.messages)} in all)")
        if dry.state is not PlaybackState.COMPLETED or win.own(moves=True):
            failures.append("dry run must complete and deliver nothing")

        start = len(win.messages)
        live = create_window_playback(macro, target, verifier, backend, stop).run()
        time.sleep(0.4)
        own = win.own(start, moves=True)
        got = [m[0] for m in own if m[0] != "move"]
        print(f"live run: {live.state.value}, {live.completed_count}/{live.actions_total}; Olive's messages {[m[0] for m in own]}")
        expected = ["left_down", "left_up", "keydown", "keyup"]
        if live.state is not PlaybackState.COMPLETED or got != expected:
            failures.append(f"expected {expected} (and a move to (40, 30) before them)")
        # a move message is made by Windows from where the pointer IS, without the event's mark: found by place
        if (40, 30) not in [m[1:3] for m in win.messages[start:] if m[0] == "move"]:
            failures.append("the move to (40, 30) did not arrive")
        coords = [m[1:3] for m in own if m[0] in ("left_down", "left_up")]
        if any(c != (40, 30) for c in coords):
            failures.append(f"unexpected client coordinates {coords}")

        before = len(win.messages)
        other = Macro(Recording(target.provider_id, "win-not-this-one", events[:1]))
        try:
            create_window_playback(other, target, verifier, backend, stop).run()
            failures.append("a macro for another target must be refused")
        except PlaybackRefused as exc:
            print(f"refused as expected: {[i.code for i in exc.issues]}")
        stop.trigger("smoke")
        cancelled = create_window_playback(macro, target, verifier, backend, stop).run()
        time.sleep(0.2)
        if cancelled.state is not PlaybackState.CANCELLED or win.own(before, moves=True):
            failures.append("emergency stop must cancel with nothing delivered")
        else:
            print("emergency stop: cancelled, nothing delivered")
    finally:
        win.close()
    print("PASS" if not failures else "FAIL: " + "; ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
