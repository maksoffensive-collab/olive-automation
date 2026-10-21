"""LDPlayer/ADB playback adapter: binds a neutral `Macro` to ONE verified `VerifiedTarget` and nothing else.

Mirrors `app/windows/playback.py`: it adds no input mechanism. The `PlaybackEngine` (app/playback) drives the
existing `app.adb.input.InputSession`, so every operation keeps that session's gates (verified target, capability,
emergency stop, proof freshness, current-display bounds, budget) and its serial-addressed command delivery. The
session's budget is exactly the number of macro actions, so a macro cannot send more operations than it has
actions. There is no "all instances", "any online device" or "matching serial" fallback: the target is the one the
caller verified.

The session offers `tap`, `send_keyevent` and `type_text` (see `app.adb.input`), so a macro needing any other
operation (pointer move/drag, secondary or double tap, a held key) is refused by `preflight` before any input is
sent, exactly like a Windows macro that needs an operation the target does not declare. `dry_run=True` is accepted
and sends no real input, but -- like the Windows adapter -- it still runs the session's own per-action gates
(freshness/identity re-verification, current-display bounds for `tap`), so a dry run can never report success for
a target whose identity can no longer be revalidated; see `app.adb.input.InputSession`.

`_LDPlayerInput` below is the one deliberate bridge between the technology-only `app.adb.input.InputSession` (which
never imports `app.providers`, see `app.adb.input`'s own docstring) and the generic `KeyboardProvider`-shaped
method name the engine calls, `key_press(key: Key)`. It translates the one primitive that needs it (`Key` -> the
Android keycode `InputSession.send_keyevent` expects, via `app.ldplayer.keymap.android_keycode`) and passes `tap`
and `type_text` straight through unchanged. This plays the same bridge role for input that
`app.ldplayer.provider.LDPlayerScreenshotProvider` already plays for screenshots; it holds no state and adds no
new gate, safety check or input mechanism of its own.

It also implements `preflight`'s optional `unsupported_reason(action)` value-level hook (see
`app.playback.preflight`): the generic `MacroAction`/`RecordedEvent` layer accepts any printable text up to 256
characters, which is broader than ADB's own transport allows (`app.adb.client.SAFE_TEXT_PATTERN`). Without this
hook, a TEXT action outside that pattern would pass generic preflight and fail only once `InputSession.type_text`
runs it, mid-macro -- after any preceding action (a tap, say) had already been sent. Checking the SAME pattern here,
before playback starts, closes that gap without touching generic preflight, without weakening the pattern itself,
and without asking any other provider to adopt ADB's restriction: the hook is optional and per-adapter, exactly
like `action_limit`/`target_id` already are.
"""
from __future__ import annotations

from app.adb.client import SAFE_TEXT_PATTERN, AdbClient
from app.adb.input import MAX_SESSION_ACTIONS, InputSession
from app.core.cancel import EmergencyStop
from app.instances.mapping import MappingService, VerifiedTarget
from app.ldplayer.keymap import android_keycode
from app.playback.engine import PlaybackEngine
from app.playback.macro import ActionOp, Macro, MacroAction
from app.providers.input import Key

MAX_ACTIONS = MAX_SESSION_ACTIONS     # one limit, owned by the input session


class _LDPlayerInput:
    """Bridges `InputSession` to the generic method names/types the engine calls. See the module docstring."""

    def __init__(self, session: InputSession) -> None:
        self._session = session

    @property
    def session(self) -> InputSession:
        """The wrapped, technology-only session (exposed for tests and introspection only; never a second
        input mechanism -- every method below just delegates to it)."""
        return self._session

    @property
    def target_id(self) -> str:
        return self._session.target_id

    @property
    def action_limit(self) -> int:
        return self._session.action_limit

    @property
    def dry_run(self) -> bool:
        return self._session.dry_run

    @property
    def actions_sent(self) -> int:
        return self._session.actions_sent

    @property
    def intended(self):
        return self._session.intended

    def tap(self, x: int, y: int) -> None:
        self._session.tap(x, y)

    def key_press(self, key: Key) -> None:
        self._session.send_keyevent(android_keycode(key))

    def type_text(self, text: str) -> None:
        self._session.type_text(text)

    def unsupported_reason(self, action: MacroAction) -> str | None:
        """Preflight's optional value-level hook (see app.playback.preflight and the module docstring above).
        Only TEXT actions can fail this: every other operation this adapter offers (tap, key_press) already
        validates its value inside the session itself, at gate time, with no meaningful earlier check possible
        (a tap's bounds depend on the device's CURRENT display, queried live; a key already went through the total,
        collision-free android_keycode table). Text is different: whether a given string is transportable is known
        in advance, from the string alone, so it belongs at preflight, not discovered mid-run."""
        if action.op is ActionOp.TYPE_TEXT and not SAFE_TEXT_PATTERN.match(action.text):
            return ("Text may contain only ASCII letters, digits, space, '.', ',', '_' and '-' "
                    "(1..256 characters).")
        return None


def create_ldplayer_playback(macro: Macro, target: VerifiedTarget, adb: AdbClient, mapping: MappingService,
                             stop: EmergencyStop, *, dry_run: bool = False, allow_different_target: bool = False,
                             max_age_s: float = 60.0, **engine_options) -> PlaybackEngine:
    # A macro above the limit is not an error here: the session advertises its `action_limit`, so the engine's
    # preflight refuses it (`too_many_actions`, typed, before any input, live and dry run alike). Never chunked.
    count = min(len(macro.actions), MAX_SESSION_ACTIONS)
    session = InputSession(target, adb, mapping, stop, max_actions=max(count, 1), max_age_s=max_age_s, dry_run=dry_run)
    return PlaybackEngine(macro, target, _LDPlayerInput(session), stop, dry_run=dry_run,
                          allow_different_target=allow_different_target, **engine_options)
