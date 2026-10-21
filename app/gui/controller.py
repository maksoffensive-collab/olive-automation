"""GUI controller: all GUI behaviour and state, with no widget code. The Tk view only renders it and forwards clicks.

The controller adds no automation logic. It sequences the existing layers through `AutomationServices`:
discovery -> explicit selection -> the provider's verification -> screenshot provider -> the provider's recorder ->
`Macro` -> `MacroLibrary` -> `PlaybackEngine` (with its own preflight). Every gate that protects input lives in those
layers; the controller only adds the user-facing rules that stop it from asking them the wrong question:

  * a target is only ever chosen by an explicit `select(ref)`; an empty or unknown selection is "no target", never
    "all" and never the foreground window; refresh keeps the chosen target and never replaces it silently;
  * a selection is a specific target IDENTITY, not just a handle: the services verify the listed identity, and what
    the card shows afterwards is read back from the target that was actually verified;
  * a preview belongs to the exact target it was captured for: a result that arrives for any other target is
    discarded, never displayed;
  * recording / playback need a target that verifies RIGHT NOW (`services.recheck`), otherwise the target turns STALE;
  * a macro recorded for another target of the same provider runs only after an explicit `bind()` to the selected one
    (and the binding is forgotten as soon as the target or the macro changes);
  * the target cannot be changed while recording or playing.

Thread model: every controller method and `poll()` run on the UI thread. Recorder and playback work happens in the
providers' own threads (observation hook thread, `PlaybackEngine` background thread); the controller only READS their
state in `poll()`, so no callback ever touches the UI from another thread. `begin_preview` / `finish_preview` split a
screenshot into a UI-thread gate, a job the view may run on a worker thread, and a UI-thread result.

Errors become a `Status` (level, category, user-facing text); tracebacks are never shown, the technical detail goes
to the log the advanced panel displays.

Version-2 macros (conditions and decisions, `app.decide`) use the same workspace: the same list, the same editor
screen (with `flow_edit.FlowSession` in place of the recorded-macro session), the same explicit run targets, the
same batch scheduler and the same history. Only the work done per target differs: a v2 target runs a
`flows.FlowJob` -- `run_steps` with that target's own observation context and cancel -- instead of one
`PlaybackEngine`, and every action it takes still goes through `services.create_playback`. The OCR engine a text
condition needs arrives from the composition root as an opaque object handed on to the observation layer;
the GUI never chooses, names or imports one (`tests.test_ai_ocr.CoreWorksWithoutAiOrOcr`).

The Text screen asks that same reader "what does this screen say?": `begin_read_text` is a UI-thread gate (one
read at a time, a reader, a usable language, the explicitly chosen target re-verified, no emergency stop), the job
captures ONE fresh screenshot of that target through `ObservationContext.read_text_with_frame`, reads it and keeps
that picture, and `finish_read_text` shows the answer (with its picture) only if it still belongs to the latest
request and the same target, and the picture is that target's. It only looks: nothing in that path can send input.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Generic, TypeVar

from app.core.errors import (CapabilityNotAvailable, EmergencyStopped, PlatformError,
                             TargetNotVerified)
from app.decide import Cancelled, Condition, ConditionalMacro, ConditionResult, evaluate, migrate_v1
from app.gui import flow_edit, flows
from app.gui import formatting
from app.gui import history as run_history
from app.gui import macro_edit
from app.gui.history import RunHistory, TargetResult
from app.gui.library import LibraryError, MacroExists, MacroInfo, MacroLibrary, file_stamp
from app.gui.macro_list import Filter, Scope, Sort, counts, visible
from app.gui.meta import MacroMeta
from app.gui.services import AutomationServices, TargetEntry
from app.observe import (InvalidQuery, ObservationCancelled, ObservationContext, ObservationUnavailable,
                         OcrLanguageUnavailable, OcrProvider, OcrResult, OcrUnavailable, TextQuery,
                         applies_text_confidence, check_text_reader, reader_languages)
from app.playback.engine import (ExecutionReport, PlaybackEngine, PlaybackRefused, PlaybackState,
                                 PlaybackStateError)
from app.playback.macro import Macro
from app.providers.base import Screenshot
from app.providers.recording import Recorder, RecorderState, RecorderStateError, Recording
from app.providers.size import SizeChanged
from app.providers.target import Capability, Target, require_capability
from app.vision.bitmap import encode_png
from app.vision.frame import Frame

LOG_LINES = 500
# How many targets a run may drive at once, when the targets themselves allow it. The default is deliberately
# the conservative one: the limit protects the MACHINE (each running target is a real device or application of
# its own), not this code, and a person can raise it in Settings once they know their machine copes.
CONCURRENCY_CHOICES = (1, 2, 4, 8, 16)
DEFAULT_CONCURRENCY = 4
START_NOTICE_S = 10        # a script that waits longer to start says so this long before it does, with a sound
STALE_CODES = frozenset({"window_gone", "process_changed", "class_changed", "not_visible", "not_top_level",
                         "process_unknown", "invalid_handle", "not_verified_target"})


class TargetStatus(str, Enum):
    NONE = "none"              # nothing selected
    SELECTED = "selected"      # chosen, verification in progress / not done
    VERIFIED = "verified"
    INVALID = "invalid"        # the chosen candidate could not be verified
    STALE = "stale"            # was verified, but no longer verifies (closed, replaced, changed)


T = TypeVar("T")


class _Job(Generic[T]):
    """Work run off the UI thread. `release` is what the work itself releases when it ends (a reader's registration
    with the emergency stop and with closing); `abandon()` runs it for a job whose thread could not be started, so
    nothing waits for work that will never run."""

    def __init__(self, work: Callable[[], T], release: Callable[[], None] | None = None) -> None:
        self._work, self._release = work, release

    def __call__(self) -> T:
        return self._work()

    def abandon(self) -> None:
        if self._release is not None:
            self._release()


class _TargetJob(_Job[T]):
    """Work for ONE target, tagged with the identity of the target it was made for, run off the UI thread."""

    def __init__(self, target: Target, work: Callable[[], T], release: Callable[[], None] | None = None) -> None:
        super().__init__(work, release)
        self.provider_id, self.target_id = target.provider_id, target.target_id


class PreviewJob(_TargetJob[Screenshot]):
    """The capture job of ONE preview request."""


@dataclass(frozen=True)
class Screen:
    """One screenshot for a picker: the PNG to show and the decoded frame to read pixels and crop templates from.
    Both come from the same capture of the same verified target."""
    png: bytes
    frame: Frame

    @property
    def width(self) -> int:
        return self.frame.width

    @property
    def height(self) -> int:
        return self.frame.height

    @property
    def target_id(self) -> str:
        return self.frame.target_id


class ScreenJob(_TargetJob[Screen]):
    """Capture AND decode, both off the UI thread (decoding a device-sized PNG takes most of a second)."""

    def __init__(self, target: Target, capture: Callable[[], Screenshot]) -> None:
        def work() -> Screen:
            shot = capture()
            return Screen(shot.png, Frame.from_screenshot(shot))
        super().__init__(target, work)


class ProbeJob(_TargetJob[ConditionResult]):
    """Ask one condition on one target once, through that target's own observation context. Nothing acts."""


class ReadTextJob(_TargetJob[tuple]):
    """Read all the text on ONE target's screen once, for request number `request`: (frame, reader's result, PNG of
    that frame). Nothing acts."""

    def __init__(self, target: Target, request: int, work: Callable[[], tuple],
                 release: Callable[[], None] | None = None) -> None:
        super().__init__(target, work, release)
        self.request = request


class LanguagesJob(_Job[tuple[str, ...] | None]):
    """Ask the text reader once which languages it can read here. Reads no screen; nothing acts."""


# The states of one text reading, and of the reader's list of languages.
READING, READ_DONE, READ_CANCELLED, READ_FAILED = "reading", "done", "cancelled", "failed"
# The list: none (no reader), idle (not asked yet), loading, ready, unknown (the reader cannot say), failed.
LANGS_NONE, LANGS_IDLE, LANGS_LOADING = "none", "idle", "loading"
LANGS_READY, LANGS_UNKNOWN, LANGS_FAILED = "ready", "unknown", "failed"
# The emergency stop as the window shows it (`AppController.stop_phase`).
STOP_IDLE, STOP_ACTIVE, STOP_STOPPING, STOP_STOPPED = "idle", "active", "stopping", "stopped"
# Which part of the library a list shows: recorded macros (version 1 -- and files that cannot be read at all, which
# are listed there so they can be found and removed), scripts (version 2), or everything (the controller's default).
KIND_ALL, KIND_MACROS, KIND_SCRIPTS = "all", "macros", "scripts"


@dataclass(frozen=True)
class ReadBlock:
    """One piece of text the reader found and WHERE it is: x, y (its top-left corner), width and height in the
    screenshot's own pixels -- exactly the reader's box, never adjusted or invented."""
    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(frozen=True)
class TextReading:
    """One "what does this screen say?" request of the Text screen, and its answer once there is one.

    `language` is what was asked for ("" leaves the choice to the reader); `read_in` is the language the reader
    says it actually used ("" when it cannot say), so an automatic choice is never hidden. `reason` says why a
    reading failed (reader | language | no_language | capture | target | stop | error) and `detail` is the
    technical wording. `blocks` are the lines WITH their boxes, as the reader returned them, and `png` / `width` /
    `height` are the very screenshot those boxes belong to, so the screen can draw each box over the image it was
    read from. No confidence value is kept: the screen says when the reader measured none at all
    (`confidence_measured`), rather than showing a number it does not have. `provider_id` and `target_id` name the
    target it was read on: a finished reading is dropped as soon as any other target is chosen (`select`)."""
    request: int
    target_title: str
    provider_id: str
    target_id: str
    language: str
    state: str = READING
    lines: tuple[str, ...] = ()
    read_in: str = ""
    confidence_measured: bool = False
    reason: str = ""
    detail: str = ""
    blocks: tuple[ReadBlock, ...] = ()
    png: bytes = b""
    width: int = 0
    height: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class Status:
    """What the status bar says. `text` is the English sentence (it is also what the log keeps, so a log line
    never changes meaning with the interface language); `key` and `values` are the same sentence as a
    translatable template, which the view renders in the current language. A message with no key -- a provider's
    own technical wording, for instance -- falls back to `text`."""
    level: str                 # "info" | "ok" | "warn" | "error"
    category: str              # e.g. invalid_selection, stale_target, unsupported, preflight, recording, playback,
    text: str                  #      cancelled, emergency_stop, storage, ready, ...
    key: str = ""
    params: tuple = ()         # (name, value) pairs for the template; a tuple so Status stays frozen/comparable

    @property
    def values(self) -> dict:
        return dict(self.params)


def _listed_as(entry: TargetEntry) -> tuple:
    """What a list row says a target IS. Its size and running state may change between two discoveries; these may
    not: the same token with other values names another target."""
    return entry.provider_id, entry.group, entry.title, entry.process, entry.kind


@dataclass(frozen=True)
class _Choice:
    """The run targets chosen FOR one macro, and the version of its file (`file_stamp`) they were chosen for."""
    refs: tuple[str, ...]
    stamp: tuple | None


@dataclass(frozen=True)
class PlaybackView:
    active: bool
    dry_run: bool
    state: str                 # PlaybackState value, or "idle"
    macro_name: str
    index: int                 # actions completed
    total: int
    detail: str = ""


class RunState(str, Enum):
    """One target's place in a run. A run covers one or more targets; how many of them may be under way at once
    is decided by what their providers declare (see `AppController._may_start`)."""
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    REFUSED = "refused"        # never started: not verified, wrong provider, or preflight said no


@dataclass(frozen=True)
class TargetRun:
    ref: str
    title: str
    state: RunState
    completed: int = 0
    total: int = 0
    detail: str = ""           # technical; the history keeps it, the ordinary UI does not show it
    starts_in: int = 0         # whole seconds left of a script's start delay (0: none, or its steps have begun)


class AppController:
    def __init__(self, services: AutomationServices, library: MacroLibrary, *,
                 meta: MacroMeta | None = None, history: RunHistory | None = None,
                 clock: Callable[[], float] = time.monotonic, ocr: OcrProvider | None = None) -> None:
        self.services, self.library, self._clock = services, library, clock
        # The text reader of the observation layer, chosen by the composition root and handed on untouched: the
        # GUI never reads text itself and never imports the OCR package or an engine. It names the reader's type
        # (the OCR contract) through the observation API, which is where that type belongs. None: no text.
        self.ocr = ocr
        # A probe ("test on target"), a text reading and the language list read on worker threads, each with its
        # own cancel. The emergency stop and closing the window set every one of those cancels -- so a reader ends
        # even if it does not watch the stop itself -- and closing waits for them, so no OCR engine process
        # outlives the application.
        self._closing = threading.Event()
        self._probes: set[threading.Event] = set()          # one done-event per such worker still under way
        self._cancels: set[threading.Event] = set()         # ... and its cancel
        self._probes_lock = threading.Lock()
        # ---- the Text screen: what the chosen target's screen says, read on request; nothing acts -------------
        self.reading: TextReading | None = None             # the latest request and its answer
        self._read_seq = 0
        self._read_cancel: threading.Event | None = None    # the cancel of the reading under way
        self.text_languages: tuple[str, ...] = ()           # what the reader says it can read here
        self.text_languages_state = LANGS_NONE if ocr is None else LANGS_IDLE
        self.text_languages_detail = ""
        self.probe_status: Status | None = None             # what the latest probe's own answer said
        self.meta = meta if meta is not None else MacroMeta(None)
        self.history = history if history is not None else RunHistory(None)
        self.version = 0                                   # bumped on every visible change; the view re-renders
        self.log: deque[str] = deque(maxlen=LOG_LINES)
        self.status = Status("info", "ready", "Ready.", "msg_ready")
        self.entries: list[TargetEntry] = []
        self.selected: TargetEntry | None = None          # human information about the chosen window
        self.target: Target | None = None
        self.target_status = TargetStatus.NONE
        self.preview: Screenshot | None = None
        self.preview_discarded = 0                         # results dropped because they belonged to another target
        self.recorder: Recorder | None = None
        self._recording_started = 0.0
        self.event_count = 0
        self.macro: Macro | None = None                    # the recording / macro shown in the workspace
        self.macro_path: Path | None = None
        self.unsaved = False
        self.bound_target_id: str | None = None
        self.macros: list[MacroInfo] = []
        self.engine: PlaybackEngine | None = None
        self.playback = PlaybackView(False, False, "idle", "", 0, 0)
        self.last_report: ExecutionReport | None = None
        self.preflight_lines: list[str] = []
        self.dry_run_lines: list[str] = []
        # ---- the macro workspace: what the list shows, what is chosen, what is open in the editor ----------
        self.filter = Filter()
        self.selected_key = ""                             # MacroInfo.key of the macro chosen in the list
        self.editor: macro_edit.EditSession | None = None
        self.editor_key = ""                               # the macro the editor is changing ("" = a new script)
        self.editor_folder = ""                            # where a new script is saved the first time
        self.editor_stamp: tuple | None = None             # the version of its file the editor opened (or last saved)
        # ---- an explicit run over one or more targets ------------------------------------------------------
        self.run_refs: tuple[str, ...] = ()                # chosen in the target picker; EMPTY NEVER MEANS ALL
        # The macro those targets were chosen FOR. Choosing another macro drops them (they are only offered again,
        # pre-ticked, in the picker), so a run never inherits targets chosen while looking at a different macro.
        self.run_refs_key = ""
        self.previous_run_refs: tuple[str, ...] = ()
        # Each macro's OWN explicit choice, remembered while the application runs: choosing that macro again brings
        # back the targets chosen for it (never another macro's). Each choice also remembers WHICH version of the
        # file it was made for, so it is forgotten when the content is replaced -- here or outside this application
        # --, when the macro is deleted, when a refresh no longer lists the same target under a token, and on "clear
        # the selection".
        self._targets_of: dict[str, _Choice] = {}
        self.library_kind = KIND_ALL
        self._kind_selection: dict[str, str] = {}          # the macro chosen in each part of the library
        self.run_results: tuple[TargetRun, ...] = ()
        self.run_active = False
        self.run_dry = False
        self.run_macro_name = ""
        self.run_macro_key = ""
        self._run_queue: list[str] = []
        self._running: dict[str, PlaybackEngine | flows.FlowJob] = {}   # ref -> its own engine / job and thread
        self._run_flow: ConditionalMacro | None = None     # the v2 macro of the current run (None: a v1 run)
        self.concurrency = DEFAULT_CONCURRENCY
        self._run_started = 0.0
        self._run_actions = 0
        self._run_delay = 0.0              # a script's start delay, waited ONCE per run (from `_run_started`)
        self._run_noticed = False          # "starts in 10 s" said for this run
        self.attention = 0                 # bumped when the person should HEAR something; the view rings once each

    # ---- small helpers ------------------------------------------------------------------------
    def _touch(self) -> None:
        self.version += 1

    def _log(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def _say(self, level: str, category: str, text: str, detail: str = "", key: str = "", **params) -> bool:
        self.status = Status(level, category, text, key, tuple(params.items()))
        self._log(f"[{category}] {text}" + (f"  ({detail})" if detail else ""))
        self._touch()
        return level != "error"

    def _fail(self, category: str, text: str, exc: BaseException | None = None, key: str = "", **params) -> bool:
        """An error the user reads. When the exception carries its own translatable message (LibraryError,
        EditError), that one is used, so a storage or editing refusal is not English-only."""
        detail = f"{type(exc).__name__}: {exc}" if exc is not None else ""
        if not key and getattr(exc, "key", ""):
            key, params = exc.key, dict(getattr(exc, "params", {}))
        self._say("error", category, text, detail, key, **params)
        return False

    def _explain(self, exc: BaseException, category: str) -> bool:
        """Turn an expected platform error into a Status. Unexpected exceptions never leak a traceback."""
        if isinstance(exc, EmergencyStopped):
            return self._fail("emergency_stop", "Emergency stop is active; nothing was sent. "
                                                "Use “Reset emergency stop” when it is safe to continue.", exc, key="msg_emergency_active")
        if isinstance(exc, PlaybackRefused):
            lines = [i.message for i in exc.issues][:3]
            self.preflight_lines = [f"{i.code}: {i.message}" for i in exc.issues]
            return self._fail("preflight", "Playback refused, nothing was sent: " + " ".join(lines), exc, key="msg_preflight_refused", reason=" ".join(lines))
        if isinstance(exc, CapabilityNotAvailable):
            return self._fail("unsupported", "This action is not available for the selected target.", exc, key="msg_unsupported")
        if isinstance(exc, TargetNotVerified):
            return self._fail(category, str(exc), exc)
        if isinstance(exc, (RecorderStateError, PlaybackStateError, LibraryError, PlatformError)):
            return self._fail(category, str(exc), exc)
        if isinstance(exc, SizeChanged):
            return self._fail(category, str(exc), exc, key="msg_other_proportions", reason=str(exc))
        return self._fail(category, f"Unexpected error ({type(exc).__name__}). Details are in the advanced panel.", exc, key="msg_unexpected", kind=type(exc).__name__)

    # ---- derived state -------------------------------------------------------------------------
    @property
    def recording_active(self) -> bool:
        return self.recorder is not None

    @property
    def playing(self) -> bool:
        return self.engine is not None or bool(self._running)

    @property
    def busy(self) -> bool:
        return self.recording_active or self.playing or self.run_active

    @property
    def emergency_active(self) -> bool:
        return self.services.stop.is_set()

    @property
    def stop_phase(self) -> str:
        """Where the emergency stop stands, derived only from `busy` and the stop itself (no state of its own):
        "idle" (nothing that can send input is under way, no stop), "active" (something is; the stop can be
        pressed), "stopping" (the stop is set and work is still ending) or "stopped" (set, everything ended:
        only an explicit reset lets new work start)."""
        if self.emergency_active:
            return STOP_STOPPING if self.busy else STOP_STOPPED
        return STOP_ACTIVE if self.busy else STOP_IDLE

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._recording_started if self.recording_active else 0.0

    @property
    def target_ready(self) -> bool:
        return self.target is not None and self.target_status is TargetStatus.VERIFIED

    @property
    def needs_binding(self) -> bool:
        return (self.macro is not None and self.target is not None
                and self.macro.target.target_id != self.target.target_id)

    @property
    def bound(self) -> bool:
        return self.target is not None and self.bound_target_id == self.target.target_id

    def can_record(self) -> bool:
        return self.target_ready and not self.busy and self.target.has_capability(Capability.RECORD)

    def can_screenshot(self) -> bool:
        """Same shape as can_record(): most targets declare Capability.SCREENSHOT, but one that does not must show
        a disabled button, not a clickable one that only fails after the click."""
        return self.target_ready and not self.busy and self.target.has_capability(Capability.SCREENSHOT)

    def can_play(self) -> bool:
        return self.target_ready and self.macro is not None and not self.busy

    # ---- targets -------------------------------------------------------------------------------
    def refresh(self) -> bool:
        if self.busy:
            return self._fail("busy", "Wait until the recording or playback has finished before refreshing.", key="msg_busy_refresh")
        before = {e.ref: _listed_as(e) for e in self.entries}     # the list every remembered choice was made in
        try:
            self.entries = self.services.discover()
        except Exception as exc:
            return self._explain(exc, "discovery")
        dropped = self._keep_targets_still_listed(before)
        stale_note = stale_reason = ""
        if self.target is not None and self.target_status is TargetStatus.VERIFIED:
            try:
                self.selected = self.services.recheck(self.target)
            except Exception as exc:
                self.target_status = TargetStatus.STALE
                stale_note = f" The selected window is no longer valid: {exc}"
                stale_reason = str(exc)
                self._log(f"[stale_target] {exc}")
        if stale_note and dropped:
            self._say("warn", "stale_target", f"Found {len(self.entries)} target(s)." + stale_note + " Run targets "
                                              "that are gone or now name something else were dropped: choose them "
                                              "again.", key="msg_found_stale_dropped", count=len(self.entries),
                      reason=stale_reason)
        elif stale_note:
            self._say("warn", "stale_target", f"Found {len(self.entries)} target(s)." + stale_note,
                      key="msg_found_stale", count=len(self.entries), reason=stale_reason)
        elif dropped:
            self._say("warn", "discovery", f"Found {len(self.entries)} target(s). Run targets that are gone or now "
                                           "name something else were dropped: choose them again.",
                      key="msg_found_dropped", count=len(self.entries))
        else:
            self._say("info", "discovery", f"Found {len(self.entries)} target(s).", key="msg_found",
                      count=len(self.entries))
        return True

    def _keep_targets_still_listed(self, before: dict[str, tuple]) -> bool:
        """After a new discovery. A token names a target only within the discovery that issued it (a closed window's
        handle, a deleted instance's number can come back as something else), so a chosen run target stays chosen
        only while the new list shows the SAME target under it. One that vanished or now names another target is
        dropped: from the current choice, from every macro's remembered one and from the suggestion. True when a
        chosen one was."""
        now = {e.ref: _listed_as(e) for e in self.entries}

        def same(refs: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(ref for ref in refs if ref in now and before.get(ref) == now[ref])

        kept = same(self.run_refs)
        dropped = kept != self.run_refs
        self.run_refs = kept
        if not kept:
            self.run_refs_key = ""
        self.previous_run_refs = same(self.previous_run_refs)
        for key, choice in list(self._targets_of.items()):
            refs = same(choice.refs)
            if refs != choice.refs:
                dropped = True
                if refs:
                    self._targets_of[key] = replace(choice, refs=refs)
                else:
                    del self._targets_of[key]
        if dropped:
            self._log("[discovery] run targets that are gone or now name something else were dropped")
        return dropped

    def select(self, ref: str | None) -> bool:
        """Explicitly choose ONE discovered window and verify it. None / unknown never selects anything."""
        if self.busy:
            return self._fail("busy", "The target cannot be changed while recording or playing.", key="msg_busy_target")
        previous_id = self.target.target_id if self.target is not None else None
        self.target, self.preview, self.preflight_lines = None, None, []
        if ref is None:
            self.selected, self.target_status = None, TargetStatus.NONE
            self.bound_target_id = None
            self._keep_reading_for(None)
            return self._fail("invalid_selection", "No window selected. Select exactly one window.", key="msg_no_selection")
        entry = next((e for e in self.entries if e.ref == ref), None)
        if entry is None:
            self.selected, self.target_status = None, TargetStatus.NONE
            self.bound_target_id = None
            self._keep_reading_for(None)
            return self._fail("invalid_selection", "That window is not in the list. Refresh and select again.", key="msg_unknown_target")
        self.selected, self.target_status = entry, TargetStatus.SELECTED
        try:
            self.target = self.services.verify(entry)
            self.selected = self.services.recheck(self.target)      # what was ACTUALLY verified, not the list row
        except Exception as exc:
            self.target, self.target_status = None, TargetStatus.INVALID
            self.bound_target_id = None
            self._keep_reading_for(None)
            return self._explain(exc, "invalid_selection")
        self.target_status = TargetStatus.VERIFIED
        if previous_id != self.target.target_id:
            self.bound_target_id = None
        self._keep_reading_for(self.target)
        return self._say("ok", "verified", f"Verified: {self.selected.title or self.selected.process}. "
                                           f"Target id {self.target.target_id}.", key="msg_verified", name=(self.selected.title or self.selected.process))

    def _keep_reading_for(self, target: Target | None) -> None:
        """A finished reading belongs to the target it was read on: once another target -- or none -- is chosen,
        it is dropped, so it can be neither shown nor copied as if it were the new target's text. A reading still
        under way is left to `finish_read_text`, which discards its answer for a changed target."""
        reading = self.reading
        if reading is None or reading.state == READING:
            return
        if target is None or (target.provider_id, target.target_id) != (reading.provider_id, reading.target_id):
            self.reading = None

    def _verified_target(self, what: str) -> Target | None:
        """The selected target, proven again right now, or None (with the reason in the status)."""
        if self.target is None or self.target_status is not TargetStatus.VERIFIED:
            self._fail("invalid_selection", f"Select and verify a window before {what}.", key="msg_verify_first")
            return None
        try:
            self.selected = self.services.recheck(self.target)
        except EmergencyStopped as exc:
            self._explain(exc, "emergency_stop")
            return None
        except Exception as exc:
            self.target_status = TargetStatus.STALE
            self._fail("stale_target", f"The selected window is no longer valid ({exc}) Select it again.", exc, key="msg_target_stale", reason=str(exc))
            return None
        return self.target

    # ---- preview ---------------------------------------------------------------------------------
    def begin_preview(self) -> PreviewJob | None:
        """UI-thread gate. Returns the job that captures (safe to run on a worker thread) or None."""
        if self.busy:
            return None
        target = self._verified_target("capturing a preview")
        if target is None:
            return None
        try:
            require_capability(target, Capability.SCREENSHOT)
        except Exception as exc:
            self._explain(exc, "unsupported")
            return None
        return PreviewJob(target, lambda: self.services.capture(target))

    def _is_current(self, provider_id: str, target_id: str) -> bool:
        t = self.target
        return (t is not None and self.target_status is TargetStatus.VERIFIED
                and (t.provider_id, t.target_id) == (provider_id, target_id))

    def finish_preview(self, result: Screenshot | BaseException, job: PreviewJob | None = None) -> bool:
        """UI-thread result of a preview. It is applied only if it belongs to the CURRENT verified target: the job
        that produced it and, for an image, the provenance stamped on it by the provider must both name that target.
        Anything else is discarded (counted in `preview_discarded`), never shown and never turned into an error."""
        stale = job is not None and not self._is_current(job.provider_id, job.target_id)
        if not stale and isinstance(result, Screenshot):
            stale = not self._is_current(result.source.provider_id, result.source.target_id)
        if stale:
            self.preview_discarded += 1
            self._log("[preview] discarded: it belongs to a target that is no longer the selected one")
            return False
        if isinstance(result, BaseException):
            self.preview = None
            return self._explain(result, "preview")
        self.preview = result
        self._log(f"[preview] {result.width}x{result.height}")
        self._touch()
        return True

    # ---- a screenshot for the editor's pickers, and asking one condition once ----------------------------
    def _screen_target(self, what: str) -> Target | None:
        """The same gate a preview passes: nothing running, the chosen target proven again right now, and a
        target that declares it can be captured."""
        if self.busy:
            self._fail("busy", "Wait until the recording or run has finished.", key="msg_busy_wait")
            return None
        target = self._verified_target(what)
        if target is None:
            return None
        try:
            require_capability(target, Capability.SCREENSHOT)
        except Exception as exc:
            self._explain(exc, "unsupported")
            return None
        return target

    def begin_screen(self) -> ScreenJob | None:
        """UI-thread gate for a picker's screenshot. The returned job captures and decodes; run it off the UI
        thread and hand what it returns (or raised) to `finish_screen`."""
        target = self._screen_target("taking a screenshot")
        if target is None:
            return None
        return ScreenJob(target, lambda: self.services.capture(target))

    def finish_screen(self, result: object, job: ScreenJob) -> Screen | None:
        """The screenshot, but only if it belongs to the target that is STILL the chosen one: a region or a
        template must never be taken from one target's screen and used as if it were another's."""
        if isinstance(result, BaseException):
            self._explain(result, "preview")
            return None
        if not isinstance(result, Screen) or not self._is_current(job.provider_id, job.target_id) \
                or (result.frame.source.provider_id, result.frame.source.target_id) != (job.provider_id,
                                                                                         job.target_id):
            self.preview_discarded += 1
            self._say("warn", "preview", "The target changed while the screenshot was being taken; it was not "
                                         "used.", key="msg_screen_discarded")
            return None
        self._log(f"[screen] {result.width}x{result.height} from {job.target_id}")
        return result

    def begin_probe(self, condition: Condition, *, picked_on: tuple[int, int] | None = None) -> ProbeJob | None:
        """Ask one condition on the chosen target once, through that target's own observation context (with the
        configured OCR engine for text), at the target's size when `picked_on` is the size it was made at (as a run
        of a script that adapts asks it). It only looks; the answer is shown, nothing is ever done about it."""
        if not isinstance(condition, Condition):
            self._fail("editor", "There is no condition to test.", key="msg_probe_nothing")
            return None
        target = self._screen_target("testing a condition")
        if target is None:
            return None
        context = ObservationContext(flows.ServiceScreens(self.services, target), stop=self.services.stop,
                                     ocr=self.ocr, picked_on=picked_on)
        done, cancel = self._observing()

        def probe() -> ConditionResult:
            try:
                return evaluate(condition, context, cancel=cancel)
            except (Cancelled, ObservationCancelled):
                self.services.stop.check()      # the emergency stop sets this cancel too: then it IS the stop
                raise
            finally:
                self._observed(done, cancel)
        return ProbeJob(target, probe, lambda: self._observed(done, cancel))

    def _observing(self) -> tuple[threading.Event, threading.Event]:
        """(done, cancel) of one worker that will read a target's screen, registered NOW, on the UI thread, so that
        the emergency stop and `shutdown` reach it -- and `shutdown` waits for it -- even if its thread has not
        started yet."""
        done, cancel = threading.Event(), threading.Event()
        with self._probes_lock:
            self._probes.add(done)
            self._cancels.add(cancel)
        if self._closing.is_set():
            cancel.set()
        return done, cancel

    def _observed(self, done: threading.Event, cancel: threading.Event) -> None:
        done.set()
        with self._probes_lock:
            self._probes.discard(done)
            self._cancels.discard(cancel)

    def _cancel_observers(self) -> None:
        """Set the cancel of every worker still reading a screen: its reader's process is ended."""
        with self._probes_lock:
            cancels = list(self._cancels)
        for cancel in cancels:
            cancel.set()

    @property
    def text_confidence_reported(self) -> bool:
        """Whether the configured text reader reports a real confidence. When it does not, a text condition's
        minimum confidence cannot be applied, and the editor must not offer it as if it could."""
        return applies_text_confidence(self.ocr)

    def _keep_stop_said(self, before: Status) -> None:
        """An answer that arrives while the emergency stop is active -- a reading or probe that finished just
        before it -- is still applied and logged, but the status bar keeps saying the stop: nothing may make it
        look as if work went on after it."""
        if self.emergency_active and before.category == "emergency_stop" \
                and self.status.category != "emergency_stop":
            self.status = before
            self._touch()

    def finish_probe(self, result: object, job: ProbeJob) -> ConditionResult | None:
        """The probe's answer, or None. `probe_status` is what THIS answer says -- for the dialog that asked, which
        must show why its probe failed even when the status bar keeps saying the emergency stop."""
        before = self.status
        try:
            return self._finish_probe(result, job)
        finally:
            self.probe_status = self.status
            self._keep_stop_said(before)

    def _finish_probe(self, result: object, job: ProbeJob) -> ConditionResult | None:
        if isinstance(result, BaseException):
            self._explain(result, "probe")
            return None
        if not isinstance(result, ConditionResult) or not self._is_current(job.provider_id, job.target_id):
            self._say("warn", "probe", "The target changed while the condition was being tested; the answer was "
                                       "not used.", key="msg_probe_discarded")
            return None
        self._say("ok" if result.satisfied else "info", "probe",
                  f"Tested on {job.target_id}: {'true' if result.satisfied else 'false'}.",
                  key="msg_probe_true" if result.satisfied else "msg_probe_false")
        return result

    # ---- the Text screen: read what the chosen target's screen says -------------------------------------
    @property
    def reading_text(self) -> bool:
        return self.reading is not None and self.reading.state == READING

    def begin_read_text(self, language: str = "") -> ReadTextJob | None:
        """UI-thread gate of "read the text on the chosen target's screen". The job (run it off the UI thread and
        hand what it returns or raises to `finish_read_text`) takes ONE fresh screenshot of the explicitly chosen,
        re-verified target and reads it with the configured reader, in `language` ("" leaves the choice to the
        reader). Refused, with the reason in the status, while a reading is under way, with no reader, for a
        language the reader cannot be asked for, under the emergency stop, or without a ready target."""
        if self.reading_text:
            self._fail("text", "Text is already being read. Wait for it, or cancel it.", key="msg_text_busy")
            return None
        if self.ocr is None:
            self._fail("text", "No text reader is configured, so text cannot be read.", key="msg_text_no_reader")
            return None
        language = language.strip()
        try:
            check_text_reader(self.ocr, TextQuery("", language=language))
        except (InvalidQuery, OcrLanguageUnavailable) as exc:
            self._fail("text", f"The text reader cannot be asked to read “{language}”.", exc,
                       key="msg_text_language_bad", language=language)
            return None
        if self.emergency_active:
            self._explain(EmergencyStopped("The emergency stop is active."), "emergency_stop")
            return None
        if self.target_ready and not self.target.has_capability(Capability.SCREENSHOT):
            # Said in words before anything is attempted: a target that declares no capture is never captured,
            # and nothing else is ever captured in its place.
            name = (self.selected.title or self.selected.process) if self.selected is not None else ""
            self._fail("unsupported", f"{name or self.target.target_id} cannot be captured, so its text cannot be "
                                      "read.", key="msg_text_no_screenshot", name=name or self.target.target_id)
            return None
        target = self._screen_target("reading text")
        if target is None:
            return None
        context = ObservationContext(flows.ServiceScreens(self.services, target), stop=self.services.stop,
                                     ocr=self.ocr)
        done, cancel = self._observing()

        def read() -> tuple:
            try:
                frame, result = context.read_text_with_frame(language=language, cancel=cancel)
                return frame, result, encode_png(frame.bitmap)   # the picture the boxes belong to, off the UI thread
            finally:
                self._observed(done, cancel)

        self._read_seq += 1
        name = (self.selected.title or self.selected.process) if self.selected is not None else ""
        self.reading = TextReading(self._read_seq, name or target.target_id, target.provider_id, target.target_id,
                                   language)
        self._read_cancel = cancel
        self._say("info", "text", f"Reading the text on {self.reading.target_title}…", key="msg_text_reading",
                  name=self.reading.target_title)
        return ReadTextJob(target, self._read_seq, read, lambda: self._observed(done, cancel))

    def cancel_read_text(self) -> bool:
        """End the reading under way: its reader process is ended and its answer, if one still arrives, is
        discarded. A new reading may start straight away."""
        reading = self.reading
        if reading is None or reading.state != READING:
            return self._say("info", "ready", "Nothing to cancel.", key="msg_nothing_to_cancel")
        if self._read_cancel is not None:
            self._read_cancel.set()
        self.reading = replace(reading, state=READ_CANCELLED)
        return self._say("warn", "cancelled", "Text reading cancelled.", key="msg_text_cancelled")

    def finish_read_text(self, outcome: object, job: ReadTextJob) -> bool:
        """UI-thread answer of one reading. Applied only if it answers the LATEST request, which is still under
        way, for the target that is STILL the chosen one; anything else is discarded, never shown. True when the
        reader's answer (text, or that there is none) is what the screen now shows."""
        before = self.status
        try:
            return self._finish_read_text(outcome, job)
        finally:
            self._keep_stop_said(before)

    def _finish_read_text(self, outcome: object, job: ReadTextJob) -> bool:
        reading = self.reading
        if reading is None or reading.request != job.request or reading.state != READING:
            self._log(f"[text] discarded the answer of reading {job.request}: it was cancelled or replaced")
            return False
        self._read_cancel = None
        if not self._is_current(job.provider_id, job.target_id):
            self.reading = replace(reading, state=READ_FAILED, reason="target")
            self._say("warn", "text", "The target changed while its text was being read; the text was not shown.",
                      key="msg_text_target_changed")
            return False
        frame = png = None
        if isinstance(outcome, tuple) and len(outcome) == 3 and isinstance(outcome[1], OcrResult):
            frame, outcome, png = outcome
            if not isinstance(frame, Frame) or (frame.source.provider_id, frame.source.target_id) != (
                    job.provider_id, job.target_id):
                # the picture must be the one of the target that was asked: anything else is not shown
                self.reading = replace(reading, state=READ_FAILED, reason="target")
                return self._fail("text", "The target changed while its text was being read; the text was not "
                                          "shown.", key="msg_text_target_changed")
        if isinstance(outcome, OcrResult) and frame is None:
            # a reading always comes with the picture it was read from: without one, where its text came from cannot
            # be checked, so it is not shown
            self.reading = replace(reading, state=READ_FAILED, reason="error")
            return self._fail("text", "The text reader gave an answer that cannot be shown.",
                              key="msg_text_reader_failed")
        if isinstance(outcome, OcrResult):
            lines = tuple(block.text for block in outcome.blocks)
            blocks = tuple(ReadBlock(b.text, b.bounds.x, b.bounds.y, b.bounds.width, b.bounds.height)
                           for b in outcome.blocks)
            self.reading = replace(reading, state=READ_DONE, lines=lines, read_in=outcome.language,
                                   confidence_measured=outcome.confidence_measured, blocks=blocks,
                                   png=png if isinstance(png, bytes) else b"",
                                   width=frame.width if frame is not None else 0,
                                   height=frame.height if frame is not None else 0)
            if lines:
                return self._say("ok", "text", f"Read {len(lines)} line(s) of text on {reading.target_title}.",
                                 key="msg_text_read", count=len(lines), name=reading.target_title)
            return self._say("info", "text", f"No text was found on {reading.target_title}.", key="msg_text_none",
                             name=reading.target_title)
        if isinstance(outcome, ObservationCancelled):
            self.reading = replace(reading, state=READ_CANCELLED)
            self._say("warn", "cancelled", "Text reading cancelled.", key="msg_text_cancelled")
            return False
        detail = f"{type(outcome).__name__}: {outcome}" if isinstance(outcome, BaseException) else repr(outcome)
        if isinstance(outcome, EmergencyStopped):
            self.reading = replace(reading, state=READ_FAILED, reason="stop", detail=detail)
            return self._explain(outcome, "emergency_stop")
        if isinstance(outcome, OcrLanguageUnavailable) and not reading.language:
            # Automatic: no language was asked for, so there is none to name. "None installed" only when the
            # reader's own list says so: Automatic may choose among some languages only (Windows: the user's own),
            # so it can fail while others are installed.
            if self.text_languages_state == LANGS_READY and not self.text_languages:
                self.reading = replace(reading, state=READ_FAILED, reason="no_language", detail=detail)
                return self._fail("text", "The text reader has no language installed on this computer, so it "
                                          "cannot read anything.", outcome, key="msg_text_no_language")
            self.reading = replace(reading, state=READ_FAILED, reason="auto_language", detail=detail)
            return self._fail("text", "Automatic could not choose a language the text reader can read here.",
                              outcome, key="msg_text_auto_language")
        if isinstance(outcome, OcrLanguageUnavailable):
            self.reading = replace(reading, state=READ_FAILED, reason="language", detail=detail)
            return self._fail("text", f"The text reader cannot read “{reading.language}” on this computer.",
                              outcome, key="msg_text_language_missing", language=reading.language)
        if isinstance(outcome, OcrUnavailable):
            self.reading = replace(reading, state=READ_FAILED, reason="reader", detail=detail)
            return self._fail("text", "The text reader could not read this screen.", outcome,
                              key="msg_text_reader_failed")
        if isinstance(outcome, ObservationUnavailable):
            self.reading = replace(reading, state=READ_FAILED, reason="capture", detail=detail)
            return self._fail("text", f"The screen of {reading.target_title} could not be captured.", outcome,
                              key="msg_text_capture_failed", name=reading.target_title)
        self.reading = replace(reading, state=READ_FAILED, reason="error", detail=detail)
        if isinstance(outcome, BaseException):
            return self._explain(outcome, "text")
        return self._fail("text", "The text reader gave an answer that cannot be shown.", key="msg_text_reader_failed")

    def begin_text_languages(self) -> LanguagesJob | None:
        """UI-thread gate of "which languages can the reader read here?". The job asks the reader (off the UI
        thread; hand what it returns or raises to `finish_text_languages`). None when there is no reader, the
        list is already being asked for, the emergency stop is active (it is asked again once the stop is
        cleared) or the window is closing."""
        if (self.ocr is None or self.text_languages_state == LANGS_LOADING or self.emergency_active
                or self._closing.is_set()):
            return None
        ocr = self.ocr
        done, cancel = self._observing()
        self.text_languages_state, self.text_languages_detail = LANGS_LOADING, ""
        self._touch()

        def languages() -> tuple[str, ...] | None:
            try:
                return reader_languages(ocr, cancel=cancel)
            except ObservationCancelled:
                self.services.stop.check()      # the emergency stop sets this cancel too: then it IS the stop
                raise
            finally:
                self._observed(done, cancel)
        return LanguagesJob(languages, lambda: self._observed(done, cancel))

    def finish_text_languages(self, outcome: object) -> bool:
        """The reader's answer. A list ended by the emergency stop or by closing did not fail: it goes back to
        "not asked yet", so the screen asks again once it may, instead of saying it could not be loaded."""
        if isinstance(outcome, tuple):
            self.text_languages, self.text_languages_state = outcome, LANGS_READY
            self._log(f"[text] the reader can read: {', '.join(outcome) or '(no language)'}")
        elif outcome is None:
            self.text_languages, self.text_languages_state = (), LANGS_UNKNOWN
            self._log("[text] the reader cannot say which languages it reads")
        elif isinstance(outcome, (EmergencyStopped, ObservationCancelled)):
            self.text_languages, self.text_languages_state, self.text_languages_detail = (), LANGS_IDLE, ""
            self._log(f"[text] listing the languages was ended ({type(outcome).__name__}: {outcome})")
        else:
            detail = f"{type(outcome).__name__}: {outcome}" if isinstance(outcome, BaseException) else repr(outcome)
            self.text_languages, self.text_languages_state = (), LANGS_FAILED
            self.text_languages_detail = detail
            self._log(f"[text] the reader could not list its languages ({detail})")
        self._touch()
        return self.text_languages_state != LANGS_FAILED

    # ---- recording -------------------------------------------------------------------------------
    def start_recording(self, *, discard_unsaved: bool = False) -> bool:
        if self.busy:
            return self._fail("busy", "A recording or playback is already active.", key="msg_busy_active")
        if self.unsaved and not discard_unsaved:
            self._say("warn", "unsaved", "The current recording has not been saved.", key="msg_unsaved_recording")
            return False
        target = self._verified_target("recording")
        if target is None:
            return False
        try:
            require_capability(target, Capability.RECORD)
            recorder = self.services.create_recorder(target)
            recorder.start()
        except Exception as exc:
            return self._explain(exc, "recording")
        self.recorder, self.event_count, self._recording_started = recorder, 0, self._clock()
        self.macro, self.macro_path, self.unsaved, self.bound_target_id = None, None, False, None
        self.dry_run_lines, self.last_report = [], None
        return self._say("info", "recording", "Recording. Switch to the selected window and do the actions; "
                                              "press F10 or Stop to finish.", key="msg_recording_started")

    def stop_recording(self) -> bool:
        if self.recorder is None:
            return self._fail("recording", "No recording is running.", key="msg_not_recording")
        try:
            recording = self.recorder.stop()
        except Exception as exc:
            self._end_recorder()
            return self._explain(exc, "recording")
        return self._recording_done(recording)

    def _end_recorder(self) -> None:
        recorder, self.recorder = self.recorder, None
        if recorder is not None:
            try:
                recorder.discard()
            except Exception:
                pass

    def _recording_done(self, recording: Recording) -> bool:
        self._end_recorder()
        if len(recording) == 0:
            self.macro = None
            return self._say("warn", "recording", "Nothing was recorded. Only actions inside the selected window "
                                                  "are recorded.", key="msg_nothing_recorded")
        self.macro, self.macro_path, self.unsaved = Macro(recording, ""), None, True
        self.bound_target_id = None
        skipped = formatting.format_skipped(recording)
        note = f" Skipped: {skipped}." if skipped else ""
        return self._say("ok", "recording", f"Recorded {len(recording)} events "
                                            f"({formatting.format_time(recording.duration_ms)}). "
                                            f"{formatting.explain_end_reason(recording.end_reason)} "
                                            f"Name it and save it.{note}", key="msg_recorded", events=len(recording), time=formatting.format_time(recording.duration_ms))

    def discard_recording(self) -> bool:
        """Forget the current unsaved recording without saving it and without starting a new one (unlike
        start_recording(discard_unsaved=True), which discards the old one only as a side effect of immediately
        starting a new recording). Harmless, and reports success, if there is nothing unsaved to discard."""
        if self.busy:
            return self._fail("busy", "Wait until the recording or playback has finished.", key="msg_busy_wait")
        self.macro, self.macro_path, self.unsaved, self.bound_target_id = None, None, False, None
        self.dry_run_lines, self.last_report, self.preflight_lines = [], None, []
        return self._say("info", "ready", "Discarded.", key="msg_discarded")

    # ---- macros ------------------------------------------------------------------------------------
    def refresh_macros(self) -> bool:
        try:
            self.macros = self.library.list()
        except Exception as exc:
            return self._explain(exc, "storage")
        keys = frozenset(info.key for info in self.macros)
        self.meta.keep_only(keys)                      # forget stars/notes of macros that no longer exist
        if self.selected_key and self.selected_key not in keys:
            self.selected_key = ""
        if self.run_refs_key and self.run_refs_key not in keys:
            # the macro these targets were chosen for is gone (deleted, or vanished from the folder): they belong
            # to nothing now, and must not pass to another macro that later takes the same file name
            if self.run_refs:
                self.previous_run_refs, self.run_refs = self.run_refs, ()
            self.run_refs_key = ""
        paths = {info.key: info.path for info in self.macros}
        for key in list(self._targets_of):
            if key not in paths:
                del self._targets_of[key]              # nor may a remembered choice of a macro that is gone
            elif not self._stamp_holds(key, paths[key]):
                self._forget_targets_of(key)           # its file was written since (outside this application, say)
        self._touch()
        return True

    # ---- the macro list: which macros are shown, and which one is chosen -----------------------------
    def _fits(self, info: MacroInfo) -> bool:
        """Whether `info` belongs to the part of the library being looked at."""
        if self.library_kind == KIND_MACROS:
            return not info.is_v2
        if self.library_kind == KIND_SCRIPTS:
            return info.is_v2
        return True

    def visible_macros(self) -> list[MacroInfo]:
        """Exactly what the list shows, by the one rule in app/gui/macro_list.py, for the part of the library
        being looked at (recorded macros, scripts, or everything)."""
        return visible([i for i in self.macros if self._fits(i)], self.filter, favorites=self.meta.favorites(),
                       last_runs=self.history.last_run_times())

    def macro_counts(self) -> dict[str, int]:
        return counts([i for i in self.macros if self._fits(i)], self.meta.favorites())

    def set_library_kind(self, kind: str) -> bool:
        """Look at recorded macros, at scripts, or at everything. Each part remembers the item chosen in it, and the
        choice is changed through `select_macro`, so targets always follow the item that is actually chosen."""
        if kind not in (KIND_ALL, KIND_MACROS, KIND_SCRIPTS):
            return False
        if kind == self.library_kind:
            return True
        self._kind_selection[self.library_kind] = self.selected_key
        self.library_kind = kind
        wanted = ""
        for key in (self.selected_key, self._kind_selection.get(kind, "")):
            info = self.macro_by_key(key) if key else None
            if info is not None and self._fits(info):
                wanted = info.key
                break
        if wanted != self.selected_key:
            self.select_macro(wanted)
        self._touch()
        return True

    def folders(self) -> list[str]:
        try:
            return self.library.folders()
        except OSError:
            return []

    def macro_by_key(self, key: str) -> MacroInfo | None:
        return next((info for info in self.macros if info.key == key), None)

    def selected_macro(self) -> MacroInfo | None:
        return self.macro_by_key(self.selected_key)

    def shown_macro(self) -> MacroInfo | None:
        """The chosen item only if it belongs to the page being looked at: the Macros page never names a chosen
        script (in its details or its header), the Scripts page never a recorded macro."""
        info = self.selected_macro()
        return info if info is not None and self._fits(info) else None

    def select_macro(self, key: str) -> bool:
        """Choose one macro in the list. An unknown key selects nothing (and never silently keeps the old one)."""
        info = self.macro_by_key(key) if key else None
        self.selected_key = info.key if info is not None else ""
        if self.run_refs_key != self.selected_key:
            if self.run_refs:
                # targets chosen for another macro are not this macro's targets: they are kept only as the
                # picker's suggestion, which still has to be confirmed explicitly
                self.previous_run_refs = self.run_refs
            choice = self._targets_of.get(self.selected_key)
            if choice is not None and not self._stamp_holds(self.selected_key, info.path):
                # its file was written since they were chosen for it: offered again, to confirm, never ready to run
                del self._targets_of[self.selected_key]
                self.previous_run_refs, choice = choice.refs, None
            # this macro's own earlier choice, if it has one -- chosen explicitly FOR it, and shown before Run
            self.run_refs, self.run_refs_key = (choice.refs, self.selected_key) if choice else ((), "")
        self._touch()
        return info is not None

    def set_scope(self, scope: Scope, folder: str = "") -> bool:
        self.filter = self.filter.with_scope(scope, folder)
        self._touch()
        return True

    def set_query(self, text: str) -> bool:
        if text != self.filter.query:
            self.filter = replace(self.filter, query=text)
            self._touch()
        return True

    def set_sort(self, sort: Sort) -> bool:
        self.filter = replace(self.filter, sort=sort)
        self._touch()
        return True

    def reset_view(self) -> bool:
        """"Clear the current selection": the search, the scope, the chosen macro and the chosen run targets.
        It deletes nothing -- no macro, no folder and no history entry is touched."""
        self.filter = self.filter.cleared()
        self.selected_key, self.run_refs = "", ()
        self.run_refs_key, self.previous_run_refs = "", ()     # cleared means cleared: not offered again either
        self._targets_of.clear()
        self._kind_selection.clear()
        return self._say("info", "ready", "Search and selection cleared. No macro, folder or history was deleted.", key="msg_view_cleared")

    # ---- favourites and descriptions -----------------------------------------------------------------
    def is_favorite(self, key: str) -> bool:
        return self.meta.is_favorite(key)

    def toggle_favorite(self, key: str) -> bool:
        if self.macro_by_key(key) is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        self.meta.toggle_favorite(key)
        self._touch()
        return True

    def description(self, key: str) -> str:
        return self.meta.description(key)

    def set_description(self, key: str, text: str) -> bool:
        if self.macro_by_key(key) is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        self.meta.set_description(key, text)
        self._touch()
        return True

    # ---- macro files: create, rename, move, duplicate, delete ------------------------------------------
    def create_macro(self, name: str, *, folder: str = "", description: str = "") -> bool:
        """A macro made by hand, with no actions yet: the user adds them in the editor.

        It needs the CURRENTLY VERIFIED target, because a macro is always "for" one provider and target -- an
        invented identity would make a macro that preflight could never run anywhere."""
        if self.busy:
            return self._fail("busy", "Wait until the recording or run has finished.", key="msg_busy_wait")
        if self.target is None or self.target_status is not TargetStatus.VERIFIED:
            return self._fail("invalid_selection", "Select and verify a target first: a macro is always made "
                                                   "for one target.", key="msg_macro_needs_target")
        try:
            empty = Macro(Recording(self.target.provider_id, self.target.target_id, ()), name.strip())
            path = self.library.save(empty, folder=folder)
        except (MacroExists, LibraryError, ValueError) as exc:
            return self._fail("exists" if isinstance(exc, MacroExists) else "storage", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.refresh_macros()
        key = self.library.key_of(path)
        if description.strip():
            self.meta.set_description(key, description)
        self.select_macro(key)
        return self._say("ok", "created", f"Created “{name.strip()}”. It has no actions yet.", key="msg_macro_created", name=name.strip())

    def rename_macro(self, key: str, new_name: str) -> bool:
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        before = file_stamp(info.path)
        try:
            path = self.library.rename(info.path, new_name)
        except (MacroExists, LibraryError) as exc:
            return self._fail("exists" if isinstance(exc, MacroExists) else "storage", str(exc), exc)
        return self._after_move(info, path, before, f"Renamed to “{new_name.strip()}”.",
                                key="msg_macro_renamed", name=new_name.strip())

    def move_macro(self, key: str, folder: str) -> bool:
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        before = file_stamp(info.path)
        try:
            path = self.library.move(info.path, folder)
        except (MacroExists, LibraryError) as exc:
            return self._fail("exists" if isinstance(exc, MacroExists) else "storage", str(exc), exc)
        where = f"“{folder}”" if folder else "the top level"
        return self._after_move(info, path, before, f"Moved to {where}.",
                                key="msg_macro_moved" if folder else "msg_macro_moved_root", name=folder)

    def _after_move(self, info: MacroInfo, path: Path, before: tuple | None, message: str, *, key: str = "",
                    **params) -> bool:
        """A renamed or moved macro keeps its star, its note and its past runs (a run under way included): they
        follow its new key. `before` is the stamp of its file just before the move: run targets chosen for that very
        version, and an editor opened on it, follow the file; any chosen or opened for an older one do not."""
        new_key = self.library.key_of(path)
        self.meta.rename_key(info.key, new_key)
        self.history.rename_key(info.key, new_key)
        if self.macro_path is not None and Path(info.path).resolve() == self.macro_path.resolve():
            self.macro_path = path
        if self.editor_key == info.key:
            self.editor_key = new_key
            if self.editor_stamp is not None and self.editor_stamp == before:
                self.editor_stamp = file_stamp(path)        # (renaming rewrites the file: still the version opened)
        if self.run_macro_key == info.key:
            self.run_macro_key = new_key                    # a run under way is recorded under the name it has now
        choice = self._targets_of.get(info.key)
        if choice is None or choice.stamp is None or choice.stamp != before:
            self._forget_targets_of(info.key)               # written since they were chosen: they do not follow
        if self.run_refs_key == info.key:
            self.run_refs_key = new_key                     # the same macro under a new name keeps its targets
        choice = self._targets_of.pop(info.key, None)
        if choice is not None:                              # (renaming rewrites the file: the stamp is the new one)
            self._targets_of[new_key] = replace(choice, stamp=file_stamp(path))
        for part, key in list(self._kind_selection.items()):
            if key == info.key:
                self._kind_selection[part] = new_key
        self.refresh_macros()
        self.select_macro(new_key)
        return self._say("ok", "storage", message, key=key, **params)

    def duplicate_macro(self, key: str, new_name: str) -> bool:
        """An independent copy: its own file, its own note, and no past runs of its own yet."""
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        try:
            macro = self.library.load(info.path)
            copy = (Macro(macro.recording, new_name.strip()) if isinstance(macro, Macro)
                    else replace(macro, name=new_name.strip()))      # all but the name: delay, places too
            path = self.library.save(copy, folder=info.folder)
        except (MacroExists, LibraryError, ValueError) as exc:
            return self._fail("exists" if isinstance(exc, MacroExists) else "storage", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.refresh_macros()
        new_key = self.library.key_of(path)
        if self.meta.description(info.key):
            self.meta.set_description(new_key, self.meta.description(info.key))
        self.select_macro(new_key)
        return self._say("ok", "created", f"Copied to “{new_name.strip()}”.", key="msg_macro_copied", name=new_name.strip())

    def convert_to_v2(self, key: str, new_name: str) -> bool:
        """A NEW v2 macro made from a v1 one by `migrate_v1` (one step that runs the recording). The v1 file is
        left exactly as it is; a name that is taken is refused like any other save, never overwritten."""
        if self.busy:
            return self._fail("busy", "Wait until the recording or run has finished.", key="msg_busy_wait")
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        try:
            macro = self.library.load(info.path)
            if not isinstance(macro, Macro):
                return self._fail("storage", "This macro is already version 2.", key="msg_already_v2")
            converted = migrate_v1(Macro(macro.recording, new_name.strip()))
            path = self.library.save(converted, folder=info.folder)
        except (MacroExists, LibraryError, ValueError) as exc:
            return self._fail("exists" if isinstance(exc, MacroExists) else "storage", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.refresh_macros()
        new_key = self.library.key_of(path)
        if self.meta.description(info.key):
            self.meta.set_description(new_key, self.meta.description(info.key))
        self.select_macro(new_key)
        return self._say("ok", "created", f"Converted to version 2 as “{new_name.strip()}”; the original is "
                                          "unchanged.", key="msg_converted", name=new_name.strip())

    def recorded_macros(self) -> list[MacroInfo]:
        """The v1 macros a v2 step can run: valid ones only, by name."""
        return sorted((i for i in self.macros if i.valid and i.version == 1),
                      key=lambda i: (i.name.casefold(), i.key))

    def load_recorded(self, key: str) -> Macro | None:
        """One v1 macro, read from its file now, for a v2 step to embed."""
        info = self.macro_by_key(key)
        if info is None:
            self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
            return None
        try:
            macro = self.library.load(info.path)
        except Exception as exc:
            self._explain(exc, "storage")
            return None
        if not isinstance(macro, Macro):
            self._fail("storage", "Only a recorded (version 1) macro can be run from a step.", key="msg_need_v1")
            return None
        return macro

    def delete_macro(self, key: str) -> bool:
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        if not self.delete(info.path):
            return False
        self.meta.forget(info.key)          # the run history keeps what happened; only the macro is gone
        if self.editor_key == info.key:
            self.close_editor()
        if self.selected_key == info.key:
            self.selected_key = ""
        return True

    # ---- folders -------------------------------------------------------------------------------------
    def create_folder(self, name: str) -> bool:
        try:
            folder = self.library.create_folder(name)
        except LibraryError as exc:
            return self._fail("storage", str(exc), exc)
        self.refresh_macros()
        self.set_scope(Scope.FOLDER, folder)
        return self._say("ok", "storage", f"Folder “{folder}” created.", key="msg_folder_created", name=folder)

    def rename_folder(self, old: str, new: str) -> bool:
        try:
            folder = self.library.rename_folder(old, new)
        except LibraryError as exc:
            return self._fail("storage", str(exc), exc)
        for info in [i for i in self.macros if i.folder == old]:
            moved = f"{folder}/{Path(info.key).name}"
            self.meta.rename_key(info.key, moved)
            self.history.rename_key(info.key, moved)
        self.refresh_macros()
        if self.filter.scope is Scope.FOLDER and self.filter.folder == old:
            self.set_scope(Scope.FOLDER, folder)
        return self._say("ok", "storage", f"Folder renamed to “{folder}”.", key="msg_folder_renamed", name=folder)

    def delete_folder(self, name: str) -> bool:
        try:
            self.library.delete_folder(name)
        except LibraryError as exc:
            return self._fail("storage", str(exc), exc)
        self.refresh_macros()
        if self.filter.scope is Scope.FOLDER and self.filter.folder == name:
            self.set_scope(Scope.ALL)
        return self._say("ok", "storage", f"Folder “{name}” deleted.", key="msg_folder_deleted", name=name)

    # ---- the editor ------------------------------------------------------------------------------------
    @property
    def editor_dirty(self) -> bool:
        return self.editor is not None and self.editor.dirty

    def open_editor(self, key: str) -> bool:
        if self.busy:
            return self._fail("busy", "Wait until the recording or run has finished.", key="msg_busy_wait")
        info = self.macro_by_key(key)
        if info is None:
            return self._fail("storage", "That macro is no longer in the list.", key="msg_macro_gone")
        if not info.valid:
            return self._fail("storage", f"This file is not a valid macro: {info.error}", key="msg_macro_invalid", reason=info.error)
        stamp = file_stamp(info.path)                # BEFORE the read: a change after it is seen, never hidden
        try:
            macro = self.library.load(info.path)
            self.editor = (flow_edit.FlowSession(macro) if isinstance(macro, ConditionalMacro)
                           else macro_edit.EditSession(macro))
        except macro_edit.EditError as exc:
            return self._fail("editor", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.editor_key, self.editor_stamp = info.key, stamp
        return self._say("info", "editor", f"Editing “{macro.name}”.", key="msg_editing", name=macro.name)

    @property
    def editing_v2(self) -> bool:
        return isinstance(self.editor, flow_edit.FlowSession)

    def editor_apply(self, edit, *args, **kwargs) -> bool:
        """Run one pure edit from app/gui/macro_edit.py. A refused edit changes nothing and explains itself."""
        if self.editor is None:
            return self._fail("editor", "No macro is open.", key="msg_no_editor")
        try:
            self.editor.apply(edit, *args, **kwargs)
        except macro_edit.EditError as exc:
            return self._fail("editor", str(exc), exc)
        self._touch()
        return True

    def note(self, level: str, category: str, text: str) -> bool:
        """Say something in the status area from the view (an already-translated sentence the view built)."""
        return self._say(level, category, text)

    def editor_undo(self) -> bool:
        if self.editor is None or not self.editor.undo():
            return False
        self._touch()
        return True

    def editor_redo(self) -> bool:
        if self.editor is None or not self.editor.redo():
            return False
        self._touch()
        return True

    def new_script(self, name: str, *, folder: str = "") -> bool:
        """Open the editor on a NEW, empty script. Nothing is written until it has a step and is saved: a script is
        never an empty recorded macro, and a new script that was left untouched leaves nothing behind."""
        if self.busy:
            return self._fail("busy", "Wait until the recording or run has finished.", key="msg_busy_wait")
        try:
            draft = flow_edit.new_script(name)
            taken = self.library.path_for(draft.name, folder).exists()
        except macro_edit.EditError as exc:
            return self._fail("editor", str(exc), exc)
        except LibraryError as exc:
            return self._fail("storage", str(exc), exc)
        if taken:
            # refused before any work is put into it: a new script never replaces a file (recordings and scripts
            # share one folder, and the Scripts page does not list recordings)
            exc = MacroExists(f"A macro named “{draft.name}” already exists.", "err_macro_exists", name=draft.name)
            return self._fail("exists", str(exc), exc)
        self.editor, self.editor_key, self.editor_folder = flow_edit.FlowSession(draft), "", folder
        self.editor_stamp = None
        return self._say("info", "editor", f"New script “{draft.name}”: add its first step.", key="msg_script_new",
                         name=draft.name)

    @property
    def editing_new(self) -> bool:
        """A script that has never been saved is open in the editor."""
        return self.editor is not None and not self.editor_key

    def rename_new_script(self, name: str) -> bool:
        """Another name for the never-saved script in the editor -- when a file made meanwhile has taken its name.
        Its steps stay as they are. A name that is taken too is refused, so nothing is ever written over."""
        if not self.editing_new:
            return False
        try:
            if self.library.path_for(name, self.editor_folder).exists():
                raise MacroExists(f"A macro named “{name.strip()}” already exists.", "err_macro_exists",
                                  name=name.strip())
            self.editor.rename(name)
        except MacroExists as exc:
            return self._fail("exists", str(exc), exc)
        except (macro_edit.EditError, LibraryError) as exc:
            return self._fail("editor", str(exc), exc)
        name = self.editor.document.name
        return self._say("info", "editor", f"The script is now called “{name}”.", key="msg_script_renamed", name=name)

    def free_script_name(self) -> str:
        """What a new script whose name was taken is offered instead: its name with the first number after it that
        no file has taken (its own name when none is free)."""
        if not self.editing_new:
            return ""
        name = self.editor.document.name
        for number in range(2, 100):
            candidate = f"{name} {number}"
            try:
                if not self.library.path_for(candidate, self.editor_folder).exists():
                    return candidate
            except LibraryError:
                break
        return name

    def _save_new_script(self) -> bool:
        try:
            macro = self.editor.macro()                    # refuses a script with no step: nothing empty is saved
            path = self.library.save(macro, folder=self.editor_folder)
            if self.library.load(path) != macro:
                return self._fail("storage", "The saved macro did not read back as it was saved.",
                                  key="msg_saved_mismatch")
        except MacroExists as exc:
            return self._fail("exists", str(exc), exc)
        except macro_edit.EditError as exc:
            return self._fail("editor", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.editor.mark_saved()
        self.editor_key, self.editor_stamp = self.library.key_of(path), file_stamp(path)
        self.refresh_macros()
        self.select_macro(self.editor_key)
        return self._say("ok", "saved", f"Saved “{macro.name}”.", key="msg_saved", name=macro.name)

    def save_editor(self, *, replace_outside: bool = False) -> bool:
        """Write the editor's macro to exactly the file it was opened from. With nothing changed here nothing is
        written, so a newer version saved outside meanwhile is never replaced by the copy opened earlier; with
        changes, such a newer version is replaced only when the person says so (`replace_outside`)."""
        if self.editor is None:
            return self._fail("editor", "No macro is open.", key="msg_no_editor")
        if self.editing_new:
            return self._save_new_script()
        info = self.macro_by_key(self.editor_key)
        if info is None:
            return self._fail("storage", "The file of this macro is gone; it cannot be saved.", key="msg_editor_file_gone")
        name = self.editor.document.name
        if not self.editor.dirty:
            self.refresh_macros()                          # (targets the file no longer fits are dropped by it)
            return self._say("info", "saved", f"“{name}” has no unsaved changes.", key="msg_saved_unchanged",
                             name=name)
        if file_stamp(info.path) != self.editor_stamp and not replace_outside:
            return self._fail("changed_outside", f"“{name}” was changed outside this application after it was "
                                                 "opened here; saving would replace that change.",
                              key="msg_editor_changed_outside", name=name)
        try:
            macro = self.editor.macro()
            path = self.library.save_over(macro, info.path)     # the file being edited, never one of the same name
            if isinstance(macro, ConditionalMacro) and self.library.load(path) != macro:
                # read back from disk, templates included and hash-checked: what was saved is what will run
                return self._fail("storage", "The saved macro did not read back as it was saved.",
                                  key="msg_saved_mismatch")
        except macro_edit.EditError as exc:
            return self._fail("editor", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.editor.mark_saved()
        self.editor_stamp = file_stamp(path)
        cleared = self._forget_targets_of(self.library.key_of(path))    # it does something else now
        self.refresh_macros()
        if isinstance(macro, Macro) and self.macro_path is not None \
                and info.path.resolve() == self.macro_path.resolve():
            self.macro, self.unsaved = macro, False         # the copy held for playback follows the file
        if cleared:
            return self._say("ok", "saved", f"Saved “{macro.name}”. It now does something else, so its run targets "
                                            "were cleared: choose them again.", key="msg_saved_targets_cleared",
                             name=macro.name)
        return self._say("ok", "saved", f"Saved “{macro.name}”.", key="msg_saved", name=macro.name)

    def close_editor(self) -> bool:
        self.editor, self.editor_key, self.editor_folder, self.editor_stamp = None, "", "", None
        self._touch()
        return True

    # ---- running a macro on one or more targets ----------------------------------------------------------
    def set_run_refs(self, refs) -> bool:
        """The explicit choice of WHERE to run. An empty choice means no targets and never "all": with none
        chosen the run is refused, it is not widened (see also app/automation/orchestrator.py, which enforces
        the same rule in the backend)."""
        known = {e.ref for e in self.entries}
        chosen = tuple(dict.fromkeys(r for r in refs if r in known))
        if self.selected_key:
            info = self.selected_macro()
            if chosen and info is not None:
                stamp = file_stamp(info.path)
                if stamp is None or stamp != info.stamp:
                    # the file changed after the list -- and the details shown from it -- were read: what would run
                    # is not what was looked at. The list is read again; the person looks, then chooses again.
                    self.refresh_macros()
                    return self._fail("changed_outside", f"“{info.name}” was changed outside this application. "
                                                         "Check it, then choose its run targets again.",
                                      key="msg_run_targets_look_again", name=info.name)
                self._targets_of[self.selected_key] = _Choice(chosen, stamp)   # FOR this macro and this version
            else:
                self._targets_of.pop(self.selected_key, None)
        if chosen != self.run_refs or self.run_refs_key != self.selected_key or self.previous_run_refs:
            self.run_refs, self.run_refs_key = chosen, self.selected_key     # chosen for THIS macro
            self.previous_run_refs = ()                                      # a choice was made: no suggestion left
            self._touch()
        return True

    def _forget_targets_of(self, key: str) -> bool:
        """The content of macro `key` was just replaced. Targets chosen for what it USED to do are not trusted for
        what it does now: they are dropped (offered again only as a suggestion) and must be chosen again. True when
        there were any."""
        had = self._targets_of.pop(key, None) is not None or bool(self.run_refs and self.run_refs_key == key)
        if self.run_refs_key == key:
            if self.run_refs:
                self.previous_run_refs = self.run_refs
            self.run_refs, self.run_refs_key = (), ""
        return had

    def suggested_run_refs(self) -> tuple[str, ...]:
        """What the target picker opens with ticked: this macro's targets, or else the ones last chosen for another
        macro -- only as a suggestion, which the person confirms in the picker; nothing runs on it by itself."""
        return self.run_refs or self.previous_run_refs

    def _stamp_holds(self, key: str, path: Path) -> bool:
        """`key` has targets chosen for it, and its file is still the very version they were chosen for."""
        choice = self._targets_of.get(key)
        return choice is not None and choice.stamp is not None and choice.stamp == file_stamp(path)

    def _restamp(self, key: str, path: Path) -> bool:
        """This application has just rewritten `key`'s file without changing what it does: its targets still hold
        for the new version. Always False (nothing was cleared)."""
        choice = self._targets_of.get(key)
        if choice is not None:
            self._targets_of[key] = replace(choice, stamp=file_stamp(path))
        return False

    def _holds(self, path: Path, macro: Macro) -> bool:
        """The file at `path` already holds exactly `macro`: writing it again changes nothing."""
        try:
            return self.library.load(path) == macro
        except Exception:
            return False

    def _run_refs_belong_here(self, info: MacroInfo) -> bool:
        return self.run_refs_key == info.key and self._stamp_holds(info.key, info.path)

    def set_concurrency(self, value: object) -> bool:
        """How many targets a run may drive at once. Only the offered choices are accepted, exactly as written,
        so a stored preference -- which arrives as text, possibly from another version -- can never widen the
        limit silently, and an unreadable one leaves the default in place instead of failing to start. Nothing
        is rounded or coerced towards a choice: 4.5 is not 4."""
        if isinstance(value, str):
            value = int(value) if value.strip().isdigit() else None
        if type(value) is not int or value not in CONCURRENCY_CHOICES:
            return False
        if value != self.concurrency:
            self.concurrency = value
            self._touch()
        return True

    def run_target_titles(self) -> tuple[str, ...]:
        by_ref = {e.ref: (e.title or e.process) for e in self.entries}
        return tuple(by_ref.get(ref, ref) for ref in self.run_refs)

    def can_run(self) -> bool:
        info = self.selected_macro()
        return (bool(self.run_refs) and info is not None and info.valid and not self.busy
                and self._run_refs_belong_here(info))

    def why_cannot_run(self) -> str:
        """The concrete reason the Run button is disabled, as a translation key the view renders -- never a bare
        "cannot execute", and never English-only. Empty means the run can start."""
        if self.busy:
            return "msg_busy_active"
        info = self.selected_macro()
        if info is None:
            return "msg_pick_macro"
        if not info.valid:
            return "msg_macro_not_valid"
        if not self.run_refs:
            return "msg_no_run_targets"
        if self.run_refs_key != info.key:
            return "msg_run_targets_other_macro"
        if not self._run_refs_belong_here(info):
            return "msg_run_targets_file_changed"
        return ""

    def start_run(self, *, dry_run: bool = False) -> bool:
        if self.busy:
            return self._fail("busy", "A recording or run is already active.", key="msg_busy_active")
        info = self.selected_macro()
        if info is None:
            return self._fail("playback", "Select a macro to run.", key="msg_pick_macro")
        if not self.run_refs:
            return self._fail("no_targets", "No run targets chosen. Choose at least one target; an empty "
                                            "choice never means all of them.", key="msg_no_run_targets")
        if self.run_refs_key != info.key:
            # defence in depth: select_macro() already drops another macro's targets
            return self._fail("no_targets", "These targets were chosen for another macro or script. Choose the "
                                            "targets for this one.", key="msg_run_targets_other_macro")
        if not self._run_refs_belong_here(info):
            # its file was written since they were chosen (outside this application, say): not what they were for
            return self._fail("no_targets", "This macro or script was changed since its targets were chosen. "
                                            "Choose them again.", key="msg_run_targets_file_changed")
        try:
            macro = self.library.load(info.path)
        except Exception as exc:
            return self._explain(exc, "storage")
        if isinstance(macro, ConditionalMacro):
            self._run_flow = macro                          # v2: each target runs the steps as a FlowJob
            size = len(macro.steps)
        else:
            self._run_flow = None
            self.macro, self.macro_path, self.unsaved = macro, info.path, False
            size = len(macro.actions)
        self.preflight_lines, self.dry_run_lines, self.last_report = [], [], None
        titles = {e.ref: (e.title or e.process) for e in self.entries}
        self.run_results = tuple(TargetRun(ref, titles.get(ref, ref), RunState.WAITING) for ref in self.run_refs)
        self.run_active, self.run_dry = True, dry_run
        self._running = {}
        self.run_macro_name, self.run_macro_key = macro.name or info.name, info.key
        self._run_queue = list(self.run_refs)
        self._run_started, self._run_actions = self._clock(), size
        # a script's start delay is the RUN's, waited once (the person's decision of 2026-10-20): a target that starts
        # later waits only what is left of it, so targets run one after another do not each wait it again
        delay = float(macro.start_delay_s) if isinstance(macro, ConditionalMacro) and not dry_run else 0.0
        self._run_delay, self._run_noticed = delay, False
        self._say("info", "playback", ("Dry run of “" if dry_run else "Running “") + self.run_macro_name
                  + f"” on {len(self.run_refs)} target(s).",
                  key="msg_dry_started" if dry_run else "msg_run_started", name=self.run_macro_name,
                  count=len(self.run_refs))
        self._advance_run()
        return True

    def _set_run_result(self, ref: str, state: RunState, *, completed: int = 0, total: int = 0,
                        detail: str = "", starts_in: int = 0) -> None:
        self.run_results = tuple(TargetRun(r.ref, r.title, state, completed, total, detail, starts_in)
                                 if r.ref == ref else r for r in self.run_results)
        self._touch()

    def _entry_of(self, ref: str):
        return next((e for e in self.entries if e.ref == ref), None)

    def _may_start(self, entry) -> bool:
        """The whole scheduling rule, expressed only in what the PROVIDER declared about a candidate.

        A target whose provider says its targets are independent end to end may overlap with other such targets,
        up to the configured limit. One that does not say so runs strictly alone: nothing else may be running
        when it starts, and nothing else starts while it runs. Candidates keep the order the user chose, so a
        solitary target acts as a barrier instead of being quietly reordered around. No provider is named here
        -- `TargetEntry.concurrent` is the only thing consulted."""
        running = [self._entry_of(ref) for ref in self._running]
        if not entry.concurrent:
            return not self._running
        if any(other is None or not other.concurrent for other in running):
            return False
        return len(self._running) < self.concurrency

    def _advance_run(self) -> None:
        """Fill every free slot the rules allow, then return. Polling calls this again as slots come free, so
        this never blocks and never owns a thread: each engine already runs in its own
        (`PlaybackEngine.start_in_background`)."""
        while self._run_queue:
            if self.emergency_active:
                return self._abort_run("emergency_stop")
            ref = self._run_queue[0]
            entry = self._entry_of(ref)
            if entry is None:
                self._run_queue.pop(0)
                self._set_run_result(ref, RunState.REFUSED, detail="the target is no longer in the list")
                continue
            if not self._may_start(entry):
                return                      # every slot is busy, or a solitary target is in the way
            self._run_queue.pop(0)
            self._start_one(ref, entry)
        if not self._running:
            self._finish_run()

    def _start_one(self, ref: str, entry) -> None:
        """One target, through exactly the chain the single-target path uses: verify -> provider match ->
        create_playback -> preflight -> start. Nothing in that chain is skipped or shared because another
        target happens to be running; each target gets its own engine, its own input session and its own
        verified identity."""
        try:
            target = self.services.verify(entry)
        except Exception as exc:
            self._set_run_result(ref, RunState.REFUSED, detail=f"{type(exc).__name__}: {exc}")
            self._log(f"[run] {ref}: not verified ({exc})")
            return
        if self._run_flow is not None:
            return self._start_flow(ref, target)
        if target.provider_id != self.macro.target.provider_id:
            self._set_run_result(ref, RunState.REFUSED, detail=f"recorded for {self.macro.target.provider_id}")
            return
        different = target.target_id != self.macro.target.target_id
        try:
            engine = self.services.create_playback(self.macro, target, dry_run=self.run_dry,
                                                   allow_different_target=different)
            report = engine.preflight()
            if not report.ok:
                detail = "; ".join(f"{i.code}: {i.message}" for i in report.errors)
                self.preflight_lines = [f"{i.severity} {i.code}: {i.message}" for i in report.issues]
                self._set_run_result(ref, RunState.REFUSED, total=self._run_actions, detail=detail)
                return
            engine.start_in_background()
        except Exception as exc:
            self._set_run_result(ref, RunState.REFUSED, detail=f"{type(exc).__name__}: {exc}")
            return
        self._running[ref] = engine
        self._set_run_result(ref, RunState.RUNNING, total=self._run_actions)
        self.playback = PlaybackView(True, self.run_dry, PlaybackState.RUNNING.value, self.run_macro_name,
                                     0, self._run_actions)

    def _start_flow(self, ref: str, target: Target) -> None:
        """One target of a v2 run: the checks a v1 target gets, made before anything is observed -- what the target
        declares it can do, and preflight of every recorded macro the steps may run -- then its own `FlowJob`."""
        flow = self._run_flow
        problem = flows.refusal(flow, target)
        if problem:
            self._set_run_result(ref, RunState.REFUSED, total=self._run_actions, detail=problem)
            return
        different = flows.allow_different(flow, target)      # the explicit choice of targets, never the file
        try:
            for macro in flows.recorded_macros(flow):
                report = self.services.create_playback(macro, target, dry_run=self.run_dry,
                                                       allow_different_target=different).preflight()
                if not report.ok:
                    detail = "; ".join(f"{i.code}: {i.message}" for i in report.errors)
                    self.preflight_lines = [f"{i.severity} {i.code}: {i.message}" for i in report.issues]
                    self._set_run_result(ref, RunState.REFUSED, total=self._run_actions, detail=detail)
                    return
            # what is left, as a duration: never more than the delay itself (a start time plus the delay, minus a
            # clock read at the same moment, can come out a hair above it in floating point)
            delay = 0.0 if self.run_dry else max(0.0, self._run_delay - (self._clock() - self._run_started))
            job = flows.FlowJob(flow, target, self.services, ocr=self.ocr, dry_run=self.run_dry,
                                allow_different_target=different, delay_s=delay)
            job.start_in_background()
        except Exception as exc:
            self._set_run_result(ref, RunState.REFUSED, detail=f"{type(exc).__name__}: {exc}")
            return
        self._running[ref] = job
        self._set_run_result(ref, RunState.RUNNING, total=self._run_actions)
        self.playback = PlaybackView(True, self.run_dry, PlaybackState.RUNNING.value, self.run_macro_name,
                                     0, self._run_actions)

    def _poll_batch(self) -> None:
        """The ONE place a batch's threads are observed, and it runs on the UI thread: engines are only read
        here, never written to from a worker, and nothing else in the application touches their state."""
        if self.emergency_active and (self._run_queue or self._running):
            self._abort_run("emergency_stop")
        for ref, engine in list(self._running.items()):
            if isinstance(engine, flows.FlowJob):
                if engine.running:
                    self._notice_start(engine)
                    # a script waiting to start says how long it still waits, once a second
                    left = int(engine.starts_in() + 0.999)
                    shown = next((r.starts_in for r in self.run_results if r.ref == ref), 0)
                    if left != shown:
                        self._set_run_result(ref, RunState.RUNNING, total=engine.total, starts_in=left)
                    continue
                del self._running[ref]
                state, done, detail = engine.outcome()
                self._log_let_go(ref, engine.let_go()[0])
                self._set_run_result(ref, RunState(state), completed=done, total=engine.total, detail=detail)
                continue
            report = engine.report()
            if report.state in (PlaybackState.RUNNING, PlaybackState.PAUSED):
                self._set_run_result(ref, RunState.RUNNING, completed=report.completed_count,
                                     total=report.actions_total or self._run_actions)
                continue
            del self._running[ref]
            self._record_target(ref, report)
        self._advance_run()

    def _notice_start(self, job: flows.FlowJob) -> None:
        """START_NOTICE_S before a delayed run begins to act, it says so -- once per run, with a sound the view
        plays, heard even when this window is behind the program the run will act on. A delay no longer than that
        (the person has only just pressed Run) says nothing, and so does a dry run: it does not wait."""
        if self._run_noticed or self._run_delay <= START_NOTICE_S or self.emergency_active or job.cancel_reason:
            return                                # the RUN's delay: a slow first target may get less than the notice
        left = job.starts_in()
        if not 0 < left <= START_NOTICE_S:
            return
        self._run_noticed = True
        self.attention += 1
        seconds = int(left + 0.999)
        self._say("warn", "playback", f"“{self.run_macro_name}” starts in {seconds} s; Stop cancels it.",
                  key="msg_run_starts_soon", name=self.run_macro_name, seconds=seconds)

    def _record_target(self, ref: str, report: ExecutionReport) -> None:
        """One target ended. Its outcome is its own: a failure here never stops the others (only the emergency
        stop and an explicit batch stop do that)."""
        if report.state is PlaybackState.COMPLETED:
            state, detail = RunState.COMPLETED, ""
        elif report.state is PlaybackState.CANCELLED:
            state = RunState.STOPPED
            detail = report.cancel_reason or "cancelled"
        else:
            state = RunState.FAILED
            failure = report.failure
            detail = f"{failure.code}: {failure.message}" if failure else "failed"
        self._log_let_go(ref, report.released_at_end)
        if report.held_at_end:
            detail = "; ".join(filter(None, (detail, "still held: " + ", ".join(report.held_at_end))))
        self._set_run_result(ref, state, completed=report.completed_count,
                             total=report.actions_total or self._run_actions, detail=detail)

    def _log_let_go(self, ref: str, released: tuple[str, ...]) -> None:
        if released:
            self._log(f"[run] {ref}: let go as it ended: {', '.join(released)}")

    def _abort_run(self, reason: str) -> None:
        """Stop the WHOLE batch: nothing waiting starts, everything running is cancelled. The batch is only
        finished once those engines have actually ended -- polling drains them -- so a run is never reported as
        stopped while something is still being sent."""
        self._run_queue = []
        for result in self.run_results:
            if result.state is RunState.WAITING:
                self._set_run_result(result.ref, RunState.STOPPED, detail=reason)
        for engine in list(self._running.values()):
            try:
                engine.cancel(reason)
            except PlaybackStateError:
                pass                        # it just ended on its own; the next poll records its real outcome
        if not self._running:
            self._finish_run()

    def _finish_run(self) -> None:
        if not self.run_active:
            return                          # already finished: aborting an idle batch must not record it twice
        # A finished batch leaves NO target in a pending state. One can reach here only through a forced close:
        # shutdown() gives each engine two seconds and then ends the run whether or not it stopped, and an
        # in-flight provider command can outlast that. Leaving such a target as "running" would both claim
        # something untrue once the application has gone and write a value into the history that is not one of
        # its outcomes -- which the History panel would print as a raw key.
        for result in self.run_results:
            if result.state in (RunState.WAITING, RunState.RUNNING):
                self._set_run_result(result.ref, RunState.STOPPED, completed=result.completed,
                                     total=result.total,
                                     detail=result.detail or "the run ended before this target finished")
        self.run_active, self._run_queue, self._running, self._run_flow = False, [], {}, None
        self.playback = PlaybackView(False, self.run_dry, "idle", self.run_macro_name, 0, self._run_actions)
        states = [r.state for r in self.run_results]
        done = states.count(RunState.COMPLETED)
        if states and done == len(states):
            outcome = run_history.COMPLETED
        elif done:
            outcome = run_history.PARTIAL
        elif states and all(s is RunState.REFUSED for s in states):
            outcome = run_history.REFUSED
        elif RunState.STOPPED in states:
            outcome = run_history.STOPPED
        else:
            outcome = run_history.FAILED
        detail = "; ".join(f"{r.title}: {r.state.value}" + (f" ({r.detail})" if r.detail else "")
                           for r in self.run_results)
        if not self.run_dry:            # a dry run sent nothing, so it is not something that "happened"
            self.history.record(macro=self.run_macro_name, macro_key=self.run_macro_key, outcome=outcome,
                                targets=tuple(r.title for r in self.run_results),
                                duration_s=max(self._clock() - self._run_started, 0.0),
                                # what the run set out to do IN TOTAL: the macro once per target. Summing the
                                # completed counts across targets against one macro's length would read as
                                # "53 of 60" for two targets that each did 27 of 60.
                                actions_total=self._run_actions * len(self.run_results),
                                completed=sum(r.completed for r in self.run_results), detail=detail,
                                results=tuple(TargetResult(r.title, r.state.value, r.completed, r.total,
                                                           r.detail) for r in self.run_results))
        level = {run_history.COMPLETED: "ok", run_history.PARTIAL: "warn", run_history.STOPPED: "warn"}.get(
            outcome, "error")
        kind = "Dry run" if self.run_dry else "Run"
        self._say(level, "playback", f"{kind} {outcome}: “{self.run_macro_name}” on {len(self.run_results)} "
                                     f"target(s), {done} finished.",
                  key=("msg_dry_result" if self.run_dry else f"msg_run_{outcome}"), name=self.run_macro_name,
                  total=len(self.run_results), done=done)

    def save(self, name: str, *, folder: str = "", overwrite: bool = False) -> bool:
        if self.macro is None:
            return self._fail("storage", "There is no recording to save.", key="msg_nothing_to_save")
        name = name.strip()
        try:
            named = Macro(self.macro.recording, name)
            there = self.library.path_for(name, folder)
            same = overwrite and self._stamp_holds(self.library.key_of(there), there) and self._holds(there, named)
            path = self.library.save(named, folder=folder, overwrite=overwrite)
        except MacroExists as exc:
            return self._fail("exists", str(exc), exc)
        except ValueError as exc:
            return self._fail("storage", str(exc), exc)
        except Exception as exc:
            return self._explain(exc, "storage")
        self.macro, self.macro_path, self.unsaved = named, path, False
        written = self.library.key_of(path)
        # the very same recording written again keeps its targets; anything else under this name -- a different
        # recording over it, or a new file where one was deleted outside -- never inherits them
        cleared = self._restamp(written, path) if same else self._forget_targets_of(written)
        self.refresh_macros()
        if cleared:
            return self._say("ok", "saved", f"Saved “{name}”. It now does something else, so its run targets were "
                                            "cleared: choose them again.", key="msg_saved_targets_cleared", name=name)
        return self._say("ok", "saved", f"Saved “{name}” ({path.name}).", key="msg_saved", name=name)

    def load(self, path: Path) -> bool:
        if self.busy:
            return self._fail("busy", "Wait until the recording or playback has finished.", key="msg_busy_wait")
        try:
            macro = self.library.load(path)
        except Exception as exc:
            return self._explain(exc, "storage")
        if not isinstance(macro, Macro):
            # the single-target path plays one recording; a v2 macro runs through the list's Run, on chosen targets
            return self._fail("playback", "A version-2 macro runs from the macro list, on the targets you choose.",
                              key="msg_v2_runs_from_list")
        self.macro, self.macro_path, self.unsaved = macro, Path(path), False
        self.bound_target_id = None
        self.dry_run_lines, self.last_report, self.preflight_lines = [], None, []
        return self._say("info", "loaded", f"Loaded “{macro.name}”: {len(macro.recording)} events, "
                                           f"{len(macro.actions)} actions, recorded for {macro.target.provider_id}.", key="msg_loaded", name=macro.name, actions=len(macro.actions))

    def delete(self, path: Path) -> bool:
        if self.busy:
            return self._fail("busy", "Wait until the recording or playback has finished.", key="msg_busy_wait")
        try:
            self.library.delete(path)
        except Exception as exc:
            return self._explain(exc, "storage")
        if self.macro_path is not None and Path(path).resolve() == self.macro_path.resolve():
            self.macro_path = None
        self.refresh_macros()
        return self._say("info", "deleted", f"Deleted {Path(path).name}.", key="msg_deleted", name=Path(path).name)

    def bind(self) -> bool:
        """Explicitly allow the loaded macro to run on the selected target (same provider only)."""
        if self.macro is None:
            return self._fail("binding", "Load or record a macro first.", key="msg_pick_macro")
        target = self._verified_target("binding a macro")
        if target is None:
            return False
        if target.provider_id != self.macro.target.provider_id:
            return self._fail("binding", f"This macro was recorded for “{self.macro.target.provider_id}”; "
                                         f"the selected target is “{target.provider_id}”. It cannot be bound.")
        self.bound_target_id = target.target_id
        label = (self.selected.title or self.selected.process) if self.selected else target.target_id
        return self._say("ok", "binding", f"Macro bound to {label}.")

    # ---- playback ----------------------------------------------------------------------------------
    def play(self, *, dry_run: bool = False) -> bool:
        if self.busy:
            return self._fail("busy", "A recording or playback is already active.", key="msg_busy_active")
        if self.macro is None:
            return self._fail("playback", "Load or record a macro first.")
        target = self._verified_target("dry run" if dry_run else "playing")
        if target is None:
            return False
        if target.provider_id != self.macro.target.provider_id:
            return self._fail("preflight", f"Incompatible: the macro was recorded for “{self.macro.target.provider_id}”, "
                                           f"the selected target is “{target.provider_id}”.")
        different = self.needs_binding
        if different and not self.bound:
            return self._fail("binding", "This macro was recorded for another window. Use “Bind to selected” "
                                         "to run it on the selected window on purpose.")
        self.preflight_lines, self.dry_run_lines, self.last_report = [], [], None
        try:
            engine = self.services.create_playback(self.macro, target, dry_run=dry_run, allow_different_target=different)
            report = engine.preflight()
            self.preflight_lines = [f"{i.severity} {i.code}: {i.message}" for i in report.issues]
            if not report.ok:
                return self._fail("preflight", "Preflight failed, nothing was sent: "
                                  + " ".join(i.message for i in report.errors[:3]))
            engine.start_in_background()
        except Exception as exc:
            return self._explain(exc, "playback")
        self.engine = engine
        name = self.macro.name or "unnamed recording"
        self.playback = PlaybackView(True, dry_run, PlaybackState.RUNNING.value, name, 0, len(self.macro.actions))
        return self._say("info", "playback", ("Dry run of “" if dry_run else "Playing “") + name + "”.")

    def cancel(self) -> bool:
        if self.run_active:
            # NOT "stopped" yet: said before aborting, so that if engines are still running the status stays
            # "Stopping…" until they confirm, and if the batch ends right away its own result replaces this.
            self._say("warn", "cancelled", "Stopping… nothing more will be sent.", key="msg_stopping")
            self._abort_run("cancelled")
            return True
        if self.engine is not None:
            try:
                self.engine.cancel("cancelled")
            except PlaybackStateError:
                pass                                   # it just finished; poll() reports the real end state
            return self._say("warn", "cancelled", "Cancelling… nothing more will be sent.", key="msg_stopping")
        if self.recorder is not None:
            try:
                self.recorder.cancel()
            except Exception as exc:
                return self._explain(exc, "recording")
            self._end_recorder()
            return self._say("warn", "cancelled", "Recording cancelled; nothing was kept.", key="msg_recording_cancelled")
        return self._say("info", "ready", "Nothing to cancel.", key="msg_nothing_to_cancel")

    def emergency_stop(self) -> bool:
        self.services.stop.trigger("GUI emergency stop")
        self._cancel_observers()            # after the trigger, so a reading ended this way reports the STOP
        self.poll()
        return self._say("error", "emergency_stop", "EMERGENCY STOP. Recording and playback are halted and nothing "
                                                    "further will be sent. Use “Reset emergency stop” to continue.", key="msg_emergency_stop")

    def clear_emergency_stop(self) -> bool:
        if self.busy:
            self.poll()
        if self.busy:
            return self._fail("busy", "Wait for the recording or playback to end before resetting the emergency "
                                      "stop.", key="msg_busy_clear_stop")
        self.services.stop.reset()
        return self._say("info", "ready", "Emergency stop reset.", key="msg_emergency_cleared")

    def shutdown(self) -> None:
        """Called when the window closes: end whatever is active. Nothing is left running -- every engine of a
        batch is cancelled and waited for, so no worker thread outlives the application -- and the batch is
        still closed off and written to the history: a run that really did send actions must not vanish
        because the window was shut while it was going."""
        self._closing.set()
        self._cancel_observers()            # ends every probe, reading and language list: engine processes killed
        if self.run_active:
            self._abort_run("app_closed")
            for engine in list(self._running.values()):
                engine.wait(2.0)
            self._poll_batch()              # each engine's real outcome, then the one history record
            self._finish_run()              # an engine that would not stop in time must not hold the run open
        self._running = {}
        if self.engine is not None:
            try:
                self.engine.cancel("app_closed")
                self.engine.wait(2.0)
            except PlaybackStateError:
                pass
        if self.recorder is not None:
            try:
                self.recorder.cancel()
            except Exception:
                pass
            self._end_recorder()
        with self._probes_lock:
            probes = list(self._probes)
        deadline = time.monotonic() + 2.0       # one shared bound for all of them, not two seconds each
        for done in probes:
            done.wait(max(0.0, deadline - time.monotonic()))

    # ---- polling (UI thread, called by the view's timer) ----------------------------------------------
    def poll(self) -> None:
        if self.recorder is not None:
            self._poll_recorder()
        if self.engine is not None:
            self._poll_engine()
        if self.run_active:
            self._poll_batch()

    def _poll_recorder(self) -> None:
        recorder = self.recorder
        state = recorder.state
        if state is RecorderState.RECORDING:
            count = getattr(recorder, "event_count", self.event_count)
            if count != self.event_count:
                self.event_count = count
                self._touch()
            return
        reason, recording = recorder.end_reason, recorder.recording
        if state is RecorderState.COMPLETED and recording is not None:
            self._recording_done(recording)
            return
        self._end_recorder()
        if state is RecorderState.INVALID_TARGET:
            self.target_status = TargetStatus.STALE
            self._fail("stale_target", "The window stopped being valid during the recording, so it was discarded "
                                       f"({reason}). Select the window again.")
        elif reason == "emergency_stop":
            self._fail("emergency_stop", "Emergency stop: the recording was discarded.")
        else:
            self._fail("recording", f"The recording was discarded. {formatting.explain_end_reason(reason)}")

    def _poll_engine(self) -> None:
        engine = self.engine
        report = engine.report()
        running = report.state in (PlaybackState.RUNNING, PlaybackState.PAUSED)
        view = PlaybackView(running, self.playback.dry_run, report.state.value, self.playback.macro_name,
                            report.completed_count, report.actions_total or self.playback.total)
        if running:
            if view != self.playback:
                self.playback = view
                self._touch()
            return
        self.engine, self.last_report, self.playback = None, report, view
        if self.playback.dry_run:
            self.dry_run_lines = [f"{s.index + 1:>4}. +{s.delay_ms:>5} ms  {s.description}" for s in report.steps]
        name = self.playback.macro_name
        if report.state is PlaybackState.COMPLETED:
            self._say("ok", "playback", ("Dry run finished: " if self.playback.dry_run else "Finished: ")
                      + f"{report.completed_count} actions of “{name}”" + (", nothing was sent." if self.playback.dry_run else "."))
        elif report.state is PlaybackState.CANCELLED:
            if report.cancel_reason == "emergency_stop":
                self._fail("emergency_stop", f"Emergency stop. Playback halted after {report.completed_count} of "
                                             f"{report.actions_total} actions; nothing more was sent.")
            else:
                self._say("warn", "cancelled", f"Cancelled after {report.completed_count} of {report.actions_total} "
                                               "actions; nothing more was sent.")
        else:
            f = report.failure
            if f is not None and f.code in STALE_CODES:
                self.target_status = TargetStatus.STALE
                self._fail("stale_target", f"The window is no longer valid ({f.message}). Playback stopped at "
                                           f"action {f.action_index + 1}; nothing more was sent.")
            else:
                where = f" at action {f.action_index + 1} of {report.actions_total}" if f is not None else ""
                why = f": {f.message}" if f is not None else "."
                self._fail("playback", f"Playback failed{where}{why}")
        if report.released_at_end:
            self._log(f"[playback] let go as it ended: {', '.join(report.released_at_end)}")
        if report.held_at_end:
            self._log(f"[playback] left held: {', '.join(report.held_at_end)}")

    # ---- diagnostics for the advanced panel ---------------------------------------------------------------
    def diagnostics(self) -> list[tuple[str, list[tuple[str, str]]]]:
        t, e = self.target, self.selected
        target_rows = [("status", self.target_status.value)]
        if e is not None:
            target_rows += [("title", e.title), ("process", e.process), ("class", e.kind), ("size", e.size)]
        if t is not None:
            v = t.verification
            target_rows += [("provider", t.provider_id), ("target id", t.target_id),
                            ("capabilities", ", ".join(sorted(c.value for c in t.capabilities))),
                            ("verification", ", ".join(v.methods)), ("verified age", f"{v.age_s():.1f} s")]
        recorder_rows = [("state", self.recorder.state.value if self.recorder else "idle"),
                         ("events", str(self.event_count)),
                         ("elapsed", formatting.format_time(int(self.elapsed_s * 1000)))]
        p, r = self.playback, self.last_report
        playback_rows = [("state", p.state), ("action", f"{p.index} / {p.total}"),
                         ("emergency stop", "ACTIVE" if self.emergency_active else "clear"),
                         ("macro", self.macro.name if self.macro and self.macro.name else "(unnamed)"),
                         ("macro target", f"{self.macro.target.provider_id}:{self.macro.target.target_id}" if self.macro else "-"),
                         ("bound to selection", "yes" if self.bound else "no")]
        if r is not None:
            playback_rows += [("failure", f"{r.failure.code}: {r.failure.message}" if r.failure else "-"),
                              ("cancel reason", r.cancel_reason or "-"), ("held at end", ", ".join(r.held_at_end) or "-"),
                              ("let go at end", ", ".join(r.released_at_end) or "-")]
        return [("Target", target_rows), ("Recorder", recorder_rows), ("Playback", playback_rows)]
