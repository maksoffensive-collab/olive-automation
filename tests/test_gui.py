"""First GUI vertical slice: controller (all behaviour), Windows composition root, Tk view, isolation.

Deterministic. The controller is driven through the REAL `WindowsServices` -> `WindowVerifier` -> `WindowRecorder` ->
`Macro` -> `PlaybackEngine` -> `WindowInputSession` chain, over a simulated desktop, so the tests show that the GUI
layer only sequences existing layers. No real window, hook or input is used; the few view tests build Tk widgets but
never show them (skipped when Tk cannot start)."""
import ast
import re
import tempfile
import time
import unittest
from pathlib import Path

from app.bootstrap.windows_services import WindowsServices
from app.core.cancel import EmergencyStop
from app.gui import formatting
from app.gui import macro_edit
from app.gui import theme as th
from app.gui.controller import AppController, RunState, TargetStatus
from app.providers.target import Capability
from app.gui.i18n import _STRINGS, LANGUAGES
from app.gui.library import LibraryError, MacroLibrary, slugify
from app.gui.macro_list import Scope
from app.gui.prefs import PreferencesStore
from app.gui.services import AutomationServices, TargetEntry
from app.observe import QUERY_CODES
from app.playback.engine import PlaybackState
from app.playback.macro import Macro
from app.providers.input import Key
from app.vision.roi import Roi
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording
from app.windows.backend import InputDeliveryError, WindowInputBackend
from tests.test_recorder import FakeObserver, MS, VK_A, VK_F10
from app.windows.backend import MouseObservationKind as M
from tests.test_windows_provider import FakeDesktop, info

ROOT = Path(__file__).resolve().parents[1] / "app"
OWN_PID = 999


class World(FakeDesktop, WindowInputBackend):
    """Simulated desktop that is also the input backend: records the messages that would be posted."""

    def __init__(self, *windows):
        super().__init__(*windows)
        self.events, self.size, self.on_event = [], (60, 40), None
        self.fronted, self.covered = [], False      # raise_window calls (they send nothing), and its answer

    def raise_window(self, hwnd, *, activate):
        self.fronted.append((hwnd, activate))
        return self.covered

    def client_size(self, hwnd):
        return self.size if hwnd in self.windows else None

    def _rec(self, event):
        self.events.append(event)
        if self.on_event:
            self.on_event(self)

    def post_mouse(self, hwnd, event, x, y):
        self._rec(("mouse", hwnd, event, x, y))

    def post_key(self, hwnd, event, key):
        self._rec(("key", hwnd, event, key))

    def post_char(self, hwnd, char):
        self._rec(("char", hwnd, char))

    # the whole desktop (Full Windows): what is under a point and what has the focus are set by a test
    hit, foreground = 100, 100

    def window_at(self, screen_x, screen_y):
        return self.hit

    def foreground_window(self):
        return self.foreground

    def desktop_mouse(self, event, screen_x, screen_y):
        self._rec(("desktop_mouse", event, screen_x, screen_y))

    def desktop_key(self, event, key):
        self._rec(("desktop_key", event, key))

    def desktop_char(self, char):
        self._rec(("desktop_char", char))

    def release_primary(self):
        self._rec(("release_primary",))

    def release_secondary(self):
        self._rec(("release_secondary",))

    def release_key(self, key):
        self._rec(("release_key", key))


def imports_of(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = World(info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10),
                           info(hwnd=101, title="Calculator", name="calc.exe", cls="CalcWnd", pid=11),
                           info(hwnd=102, title="Automation Platform", name="python.exe", cls="TkTopLevel", pid=OWN_PID),
                           info(hwnd=103, title="", name="helper.exe", cls="Hidden", pid=12))
        self.observer = FakeObserver()
        self.stop = EmergencyStop()
        self.services = WindowsServices(self.world, lambda: self.observer, self.stop, own_pid=OWN_PID)
        self.library = MacroLibrary(Path(self.tmp.name) / "macros")
        self.c = AppController(self.services, self.library)

    def entry(self, title):
        return next(e for e in self.c.entries if e.title == title)

    def pick(self, title="Notes"):
        self.c.refresh()
        self.assertTrue(self.c.select(self.entry(title).ref), self.c.status)

    def record(self, *, clicks=1):
        """Record a click (+ optional key) inside window 100 through the real recorder and observation fake."""
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        for i in range(clicks):
            self.observer.mouse(M.MOVE, 10 + i, 20, 10 + 30 * i)
            self.observer.mouse(M.PRIMARY_DOWN, 10 + i, 20, 20 + 30 * i)
            self.observer.mouse(M.PRIMARY_UP, 10 + i, 20, 30 + 30 * i)
        self.observer.key(VK_A, True, 200)
        self.observer.key(VK_A, False, 230)
        self.assertTrue(self.c.stop_recording(), self.c.status)

    def copied_in(self, name):
        """A recorded macro that reached the library without Olive saving it (copied in by hand, or written by another
        copy of Olive): it remembers no target, so nothing is chosen for it (the person's list of 2026-10-25,
        item 12). Returns its key."""
        self.record()
        path = self.library.save(Macro(self.c.macro.recording, name))
        self.assertTrue(self.c.discard_recording(), self.c.status)
        self.c.refresh_macros()
        return self.library.key_of(path)

    def finish_playback(self):
        self.assertTrue(self.c.engine.wait(10), "playback did not end")
        self.c.poll()
        self.assertFalse(self.c.playing)


class Discovery(Base):
    def test_services_satisfy_the_neutral_interface(self):
        self.assertIsInstance(self.services, AutomationServices)

    def test_refresh_lists_windows_with_human_information_only(self):
        self.assertTrue(self.c.refresh())
        # "Full Windows" (the whole-desktop context, not tied to any one window) is always first, ahead of the
        # individually enumerated windows -- own process's windows and untitled windows are left out of those
        self.assertEqual([e.title for e in self.c.entries], ["Full Windows", "Calculator", "Notes"])
        notes = self.entry("Notes")
        self.assertEqual((notes.process, notes.kind, notes.size, notes.provider_id, notes.group),
                         ("notes.exe", "NotesWnd", "64 x 48", "windows", "Open Windows"))
        self.assertIsInstance(notes.ref, str)                                                  # opaque, not a handle
        full_windows = self.entry("Full Windows")
        self.assertEqual((full_windows.provider_id, full_windows.group), ("windows", "Full Windows"))
        self.assertEqual(self.c.target_status, TargetStatus.NONE)                              # nothing auto-selected

    def test_refresh_rebuilds_the_list(self):
        self.c.refresh()
        self.world.windows.pop(101)
        self.c.refresh()
        self.assertEqual([e.title for e in self.c.entries], ["Full Windows", "Notes"])

    def test_refresh_never_replaces_or_drops_the_selected_target(self):
        self.pick("Notes")
        target = self.c.target
        self.c.refresh()
        self.assertIs(self.c.target, target)
        self.assertEqual(self.c.target_status, TargetStatus.VERIFIED)

    def test_refresh_marks_a_vanished_selection_stale(self):
        self.pick()
        self.world.windows.pop(100)
        self.c.refresh()
        self.assertEqual(self.c.target_status, TargetStatus.STALE)
        self.assertFalse(self.c.can_record())


class Selection(Base):
    def test_explicit_selection_verifies_and_shows_information(self):
        self.pick("Notes")
        c = self.c
        self.assertEqual(c.target_status, TargetStatus.VERIFIED)
        self.assertTrue(c.target_ready and c.target.target_id.startswith("win-"))
        self.assertEqual((c.selected.title, c.selected.process, c.selected.kind), ("Notes", "notes.exe", "NotesWnd"))
        self.assertEqual(c.status.category, "verified")

    def test_empty_selection_is_no_target_never_all(self):
        self.pick()
        self.assertFalse(self.c.select(None))
        c = self.c
        self.assertEqual((c.target, c.target_status, c.status.category), (None, TargetStatus.NONE, "invalid_selection"))
        self.assertFalse(c.can_record() or c.can_play())
        self.assertFalse(c.start_recording())
        self.assertEqual(c.status.category, "invalid_selection")

    def test_unknown_reference_selects_nothing(self):
        self.c.refresh()
        self.assertFalse(self.c.select("w4242"))
        self.assertIsNone(self.c.target)
        self.assertEqual(self.c.status.category, "invalid_selection")

    def test_the_foreground_or_a_single_match_is_never_chosen_implicitly(self):
        self.c.refresh()
        self.assertIsNone(self.c.target)
        self.assertFalse(self.c.start_recording())
        self.assertFalse(self.c.play())
        self.assertEqual(self.world.events, [])

    def test_verification_failure_is_shown_and_blocks_everything(self):
        self.c.refresh()
        entry = self.entry("Notes")
        self.world.windows[100] = info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10, start=0)
        self.assertFalse(self.c.select(entry.ref))
        c = self.c
        self.assertEqual((c.target_status, c.status.category, c.status.level), (TargetStatus.INVALID, "invalid_selection", "error"))
        self.assertIn("cannot be verified", c.status.text)
        self.assertIsNone(c.target)
        self.assertFalse(c.start_recording())
        self.assertFalse(c.play())
        self.assertEqual(self.world.events, [])

    def test_stale_target_is_detected_before_recording_and_before_playing(self):
        self.record()
        self.world.windows.pop(100)
        self.assertFalse(self.c.start_recording(discard_unsaved=True))
        self.assertEqual((self.c.target_status, self.c.status.category), (TargetStatus.STALE, "stale_target"))
        self.assertFalse(self.c.play())
        self.assertEqual(self.world.events, [])

    def test_a_replaced_window_with_the_same_handle_is_stale(self):
        self.pick()
        self.c.macro = Macro(Recording("windows", self.c.target.target_id, (E(K.POINTER_MOVE, 0, 1, 1),)), "m")
        self.world.windows[100] = info(hwnd=100, title="Impostor", name="other.exe", cls="NotesWnd", pid=77)
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.target_status, TargetStatus.STALE)

    def test_target_cannot_change_while_recording_or_playing(self):
        self.pick("Notes")
        self.c.start_recording()
        self.assertFalse(self.c.select(self.entry("Calculator").ref))
        self.assertEqual(self.c.status.category, "busy")
        self.assertEqual(self.c.selected.title, "Notes")
        self.assertFalse(self.c.refresh())
        self.c.cancel()


class FullWindowsTargetGroup(Base):
    """"Full Windows": a whole-desktop automation context that exists in its own right -- distinct from, and not
    derived from, any specific window (see app/windows/target.py FullWindowsTarget) -- so the platform is not
    just "pick a window and automate it" but has a real, separately selectable "the whole of Windows" target."""

    def test_full_windows_is_always_first_and_belongs_to_its_own_group(self):
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.entries[0].title, "Full Windows")
        full = self.entry("Full Windows")
        self.assertEqual(full.group, "Full Windows")
        self.assertNotEqual(full.group, self.entry("Notes").group)     # a real window is grouped separately

    def test_full_windows_can_be_selected_and_verified_with_no_open_windows_at_all(self):
        self.world.windows.clear()                                    # not even the platform's own window survives
        self.assertTrue(self.c.refresh())
        self.assertEqual([e.title for e in self.c.entries], ["Full Windows"])
        self.assertTrue(self.c.select(self.entry("Full Windows").ref))
        self.assertEqual(self.c.target_status, TargetStatus.VERIFIED)
        # looked at, recorded on and played on: the whole desktop, on the person's explicit decision of 2026-10-19
        # (every event gated by app/windows/desktop.py; tests.test_full_windows)
        for capability in (Capability.SCREENSHOT, Capability.RECORD, Capability.TAP, Capability.KEY):
            self.assertTrue(self.c.target.has_capability(capability), capability)
        self.assertTrue(self.c.can_record())

    def test_full_windows_stays_verified_when_every_real_window_disappears(self):
        self.pick("Full Windows")
        self.world.windows.clear()
        self.assertTrue(self.c.refresh())
        self.assertEqual(self.c.target_status, TargetStatus.VERIFIED)  # never goes stale: it has no handle to lose

    def test_full_windows_and_a_real_window_verify_to_different_stable_ids(self):
        self.pick("Full Windows")
        full_id = self.c.target.target_id
        self.pick("Notes")
        self.assertNotEqual(self.c.target.target_id, full_id)
        self.assertTrue(self.c.target.target_id.startswith("win-"))    # still the windows provider's own id scheme

    def test_selecting_full_windows_then_a_real_window_are_independent_choices(self):
        """Choosing Full Windows never behaves like choosing "every window", and picking a specific window
        afterwards is a separate, ordinary selection -- not a special case of the first."""
        self.pick("Full Windows")
        self.assertEqual(self.c.selected.title, "Full Windows")
        self.pick("Notes")
        self.assertEqual(self.c.selected.title, "Notes")

    def test_full_windows_is_captured_as_the_whole_desktop_never_as_a_window(self):
        """Full Windows is a capture of the whole (here: simulated) virtual screen -- every monitor, the left one at
        negative x -- through the backend's desktop capture. No window is captured in its place."""
        self.pick("Full Windows")
        self.assertTrue(self.c.can_screenshot())
        job = self.c.begin_preview()
        shot = job()
        self.assertTrue(self.c.finish_preview(shot, job))
        left, top, width, height = self.world.desktop
        self.assertEqual((shot.width, shot.height), (width, height))
        self.assertEqual((self.world.desktop_captures, self.world.captures), (1, 0))
        from app.windows.target import desktop_id
        self.assertEqual((shot.source.provider_id, shot.source.target_id), ("windows", desktop_id(self.world.desktop)))

    def test_a_macro_recorded_in_a_window_is_never_played_on_full_windows(self):
        """Its points are that window's client coordinates, not the screen's: on the whole desktop they would land
        elsewhere. Refused before anything is sent (the other direction: tests.test_full_windows)."""
        self.record()                                                             # a real macro, recorded on Notes
        self.assertTrue(self.c.save("Clicks"), self.c.status)
        self.c.refresh_macros()
        self.c.select_macro(next(i.key for i in self.c.macros if i.name == "Clicks"))
        self.c.set_run_refs([self.entry("Full Windows").ref])
        sent = len(self.world.events)
        self.assertTrue(self.c.start_run(), self.c.status)
        deadline = time.time() + 5
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.assertEqual([r.state for r in self.c.run_results], [RunState.REFUSED])
        self.assertEqual(len(self.world.events), sent)                             # nothing was sent anywhere


class CountingOcr:
    """A text reader that only counts: what it was asked to read, and the frames it was handed."""

    reports_confidence = False

    def __init__(self):
        self.frames = []

    def read_text(self, frame, *, roi=None, language="", cancel=None):
        from app.ocr.provider import OcrResult, OcrTextBlock
        from app.vision.roi import Roi
        self.frames.append(frame)
        return OcrResult((OcrTextBlock("Hello", Roi(1, 1, 5, 5), 1.0),), confidence_measured=False)


class NoCaptureTarget:
    """A verified target that declares no capture at all (a stand-in: since the desktop can be captured, no real
    target of this provider is one any more). Only what the read gate looks at."""

    provider_id, target_id = "windows", "win-nocapture"
    capabilities = frozenset()

    def has_capability(self, capability):
        return False


class FullWindowsTextReading(Base):
    """Reading text needs a capture. A target that declares none is refused in words before anything is captured --
    never answered from another window or the desktop. Full Windows is read from ONE picture of the whole desktop,
    its text boxes in that picture's own pixels; a real window still reads exactly as before."""

    def setUp(self):
        super().setUp()
        self.ocr = CountingOcr()
        self.c = AppController(self.services, self.library, ocr=self.ocr)

    def test_a_target_that_cannot_be_captured_is_refused_and_nothing_is_captured_or_read(self):
        self.pick("Notes")
        self.c.target = NoCaptureTarget()
        self.assertIsNone(self.c.begin_read_text())
        self.assertEqual((self.c.status.level, self.c.status.key), ("error", "msg_text_no_screenshot"))
        self.assertIn("Notes", self.c.status.text)
        self.assertIsNone(self.c.reading)                                 # no reading, so no "done" either
        self.assertEqual((self.world.captures, self.world.desktop_captures, self.ocr.frames), (0, 0, []))

    def test_full_windows_reads_one_picture_of_the_whole_desktop(self):
        from app.gui.controller import ReadBlock
        self.pick("Full Windows")
        job = self.c.begin_read_text()
        self.assertIsNotNone(job, self.c.status)
        self.assertTrue(self.c.finish_read_text(job(), job), self.c.status)
        reading = self.c.reading
        self.assertEqual(reading.lines, ("Hello",))
        self.assertEqual(reading.blocks, (ReadBlock("Hello", 1, 1, 5, 5),))     # the reader's box, unchanged
        self.assertEqual((reading.width, reading.height), self.world.desktop[2:])
        self.assertTrue(reading.png.startswith(b"\x89PNG"))
        self.assertEqual((self.world.desktop_captures, self.world.captures, len(self.ocr.frames)), (1, 0, 1))

    def test_a_real_window_still_reads_through_the_existing_path(self):
        self.pick("Notes")
        job = self.c.begin_read_text()
        self.assertIsNotNone(job, self.c.status)
        self.assertTrue(self.c.finish_read_text(job(), job), self.c.status)
        self.assertEqual(self.c.reading.lines, ("Hello",))
        self.assertEqual((self.world.captures, len(self.ocr.frames)), (1, 1))
        self.assertEqual(self.ocr.frames[0].source.target_id, self.c.target.target_id)
        frame = self.ocr.frames[0]
        self.assertEqual((self.c.reading.width, self.c.reading.height), (frame.width, frame.height))

    def test_the_reason_exists_in_both_languages_with_the_same_values(self):
        for key in ("msg_text_no_screenshot", "text_note_no_screenshot"):
            fields = {lang: set(re.findall(r"\{(\w+)\}", _STRINGS[key][lang])) for lang in LANGUAGES}
            self.assertEqual(len(set(map(frozenset, fields.values()))), 1, (key, fields))
            for language in LANGUAGES:
                self.assertTrue(_STRINGS[key][language].strip(), (key, language))


class Recording_(Base):
    def test_injected_input_is_captured_not_silently_discarded(self):
        """Bug found live: WindowRecorder's OWN default (see app/windows/recorder.py) is to ignore injected
        input. WindowsServices.create_recorder() used to build one with that untouched default -- so every click
        and keystroke delivered through a remote-control tool (RDP, AnyDesk, ...), which Windows flags identically
        to a script's synthetic input, was silently discarded: Record, do things over remote control, Stop, and
        the recording was always empty, with no macro ever appearing anywhere. Confirmed with a real OS-level
        hook and real injected input against a real window (see the session's diagnostic scripts): 0/4 events
        before this fix, 4/4 after. WindowsServices now opts in to injected input by default (this platform's
        ordinary deployment IS a remotely-operated Windows host), which this reproduces without any real hook."""
        self.pick("Notes")
        self.assertTrue(self.c.start_recording())
        self.observer.mouse(M.PRIMARY_DOWN, 10, 20, 10, injected=True)
        self.observer.mouse(M.PRIMARY_UP, 10, 20, 20, injected=True)
        self.observer.key(VK_A, True, 200, injected=True)
        self.observer.key(VK_A, False, 230, injected=True)
        self.assertTrue(self.c.stop_recording(), self.c.status)
        self.assertIsNotNone(self.c.macro)
        self.assertEqual(len(self.c.macro.recording), 4)
        self.assertTrue(self.c.unsaved)

    def test_start_stop_shows_human_readable_events(self):
        self.record()
        c = self.c
        self.assertEqual((c.recording_active, c.unsaved, c.status.category), (False, True, "recording"))
        rows = formatting.event_rows(c.macro.recording)
        self.assertEqual([(w, d) for _, w, d in rows],
                         [("Move", "(10, 20)"), ("Left Down", "(10, 20)"), ("Left Up", "(10, 20)"),
                          ("Key Down", "A"), ("Key Up", "A")])
        self.assertEqual(rows[0][0], "00:00.000")
        self.assertRegex(rows[-1][0], r"^\d\d:\d\d\.\d{3}$")

    def test_state_and_counter_while_recording(self):
        self.pick()
        self.c.start_recording()
        self.assertTrue(self.c.recording_active and self.c.busy)
        self.observer.mouse(M.MOVE, 5, 5, 10)
        self.observer.mouse(M.MOVE, 6, 5, 50)
        self.c.poll()
        self.assertEqual(self.c.event_count, 2)
        self.assertFalse(self.c.can_record() or self.c.can_play())
        self.c.cancel()
        self.assertFalse(self.c.recording_active)

    def test_f10_semantics_come_from_the_recorder_and_are_polled(self):
        self.pick()
        self.c.start_recording()
        self.observer.mouse(M.PRIMARY_DOWN, 3, 4, 10)
        self.observer.mouse(M.PRIMARY_UP, 3, 4, 20)
        self.observer.key(VK_F10, True, 30)
        self.observer.key(VK_F10, False, 40)
        self.c.poll()
        self.assertFalse(self.c.recording_active)
        self.assertEqual(len(self.c.macro.recording), 2)                # F10 itself is not recorded
        self.assertEqual(self.c.macro.recording.end_reason, "control_hotkey")
        self.assertIn("F10", self.c.status.text)

    def test_empty_recording_is_not_offered_for_saving(self):
        self.pick()
        self.c.start_recording()
        self.c.stop_recording()
        self.assertIsNone(self.c.macro)
        self.assertEqual(self.c.status.level, "warn")
        self.assertFalse(self.c.save("x"))

    def test_recording_needs_the_record_capability(self):
        self.pick()
        target = self.c.target
        real = type(target).capabilities
        try:
            type(target).capabilities = property(lambda self: frozenset())
            self.assertFalse(self.c.start_recording())
        finally:
            type(target).capabilities = real
        self.assertEqual(self.c.status.category, "unsupported")

    def test_can_record_is_false_for_a_verified_target_without_the_record_capability(self):
        """Bug: can_record() only checked target_ready/busy, never the target's declared capabilities, so the
        Record button looked clickable (and start_recording() only failed, somewhat quietly, after the click) for
        any target that cannot actually record -- e.g. every LDPlayer target. A user could click Record, do
        actions, click Stop, and see nothing happen, with no visible reason why."""
        self.pick()
        target = self.c.target
        real = type(target).capabilities
        try:
            type(target).capabilities = property(lambda self: frozenset())
            self.assertFalse(self.c.can_record())
        finally:
            type(target).capabilities = real
        self.assertTrue(self.c.can_record())          # the real target still can, once capabilities are restored

    def test_recorder_start_failure_is_a_recording_error_without_traceback(self):
        from app.core.errors import PlatformError
        self.pick()
        self.observer.fail = PlatformError("Hooks are not available.")
        self.assertFalse(self.c.start_recording())
        self.assertEqual((self.c.status.category, self.c.status.text), ("recording", "Hooks are not available."))
        self.assertFalse(self.c.recording_active)

    def test_window_lost_during_recording_discards_it_as_stale(self):
        self.pick()
        self.c.start_recording()
        self.observer.mouse(M.MOVE, 5, 5, 10)
        self.world.windows.pop(100)
        self.observer.mouse(M.PRIMARY_DOWN, 5, 5, 20)
        self.c.poll()
        self.assertEqual((self.c.status.category, self.c.target_status), ("stale_target", TargetStatus.STALE))
        self.assertIsNone(self.c.macro)

    def test_unsaved_recording_needs_explicit_discard_before_a_new_one(self):
        self.record()
        self.assertFalse(self.c.start_recording())
        self.assertEqual(self.c.status.category, "unsaved")
        self.assertTrue(self.c.start_recording(discard_unsaved=True))
        self.c.cancel()


class MacroStorage(Base):
    def test_save_load_round_trip_and_never_overwrites_silently(self):
        self.record()
        self.assertTrue(self.c.save("Farm Development"), self.c.status)
        path = self.library.directory / "farm_development.macro.json"
        self.assertTrue(path.is_file())
        original = path.read_text(encoding="utf-8")
        self.assertFalse(self.c.unsaved)
        self.assertEqual([(m.name, m.events, m.actions, m.provider_id) for m in self.c.macros],
                         [("Farm Development", 5, 3, "windows")])
        self.c.macro = None
        self.assertTrue(self.c.load(path))
        self.assertEqual(self.c.macro.name, "Farm Development")
        self.assertEqual(len(self.c.macro.recording), 5)
        # saving another recording under the same name is refused until the user allows it
        self.record()
        self.assertFalse(self.c.save("farm development"))
        self.assertEqual(self.c.status.category, "exists")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertTrue(self.c.save("farm development", overwrite=True))

    def test_saved_file_holds_no_handles_or_wall_clock_data(self):
        self.record()
        self.c.save("clean")
        text = (self.library.directory / "clean.macro.json").read_text(encoding="utf-8")
        for word in ("hwnd", "pid", "handle", "title", "process", "Notes", "notes.exe"):
            self.assertNotIn(word, text)

    def test_invalid_names_and_files_are_refused_with_messages(self):
        self.record()
        self.assertFalse(self.c.save("   "))
        self.assertFalse(self.c.save("???"))
        self.assertEqual(self.c.status.category, "storage")
        self.library.directory.mkdir(parents=True, exist_ok=True)
        bad = self.library.directory / "broken.macro.json"
        bad.write_text("{not json", encoding="utf-8")
        self.c.refresh_macros()
        self.assertEqual([(m.name, m.valid) for m in self.c.macros], [("broken", False)])
        self.assertFalse(self.c.load(bad))
        self.assertEqual(self.c.status.category, "storage")

    def test_delete_only_touches_files_of_the_macro_folder(self):
        self.record()
        self.c.save("gone")
        outside = Path(self.tmp.name) / "keep.macro.json"
        outside.write_text("{}", encoding="utf-8")
        self.assertFalse(self.c.delete(outside))
        self.assertTrue(outside.exists())
        self.assertTrue(self.c.delete(self.library.directory / "gone.macro.json"))
        self.assertEqual(self.c.macros, [])

    def test_library_helpers(self):
        self.assertEqual(slugify(" Farm / Dev #1 "), "farm_dev_1")
        self.assertEqual(slugify("../../x"), "x")
        with self.assertRaises(LibraryError):
            self.library.path_for("...")
        with self.assertRaises(LibraryError):
            self.library.load(Path(self.tmp.name) / "x.macro.json")


class Playback(Base):
    def macro_for(self, provider_target, *, delay=5, count=3):
        events = [E(K.POINTER_MOVE, 0, 5, 5)] + [E(K.POINTER_MOVE, delay, 6 + i, 5) for i in range(count - 1)]
        return Macro(Recording("windows", provider_target, tuple(events)), "demo")

    def test_dry_run_sends_nothing_and_reports_the_plan(self):
        self.record()
        self.assertTrue(self.c.play(dry_run=True), self.c.status)
        self.finish_playback()
        self.assertEqual(self.world.events, [])
        self.assertEqual(self.c.status.category, "playback")
        self.assertIn("Dry run finished", self.c.status.text)
        self.assertEqual(len(self.c.dry_run_lines), 3)
        self.assertEqual((self.c.playback.index, self.c.playback.total), (3, 3))

    def test_playback_runs_the_engine_against_the_window_only(self):
        self.record()
        self.assertTrue(self.c.play(), self.c.status)
        self.finish_playback()
        self.assertEqual(self.c.last_report.state, PlaybackState.COMPLETED)
        self.assertEqual(self.c.status.level, "ok")
        self.assertTrue(self.world.events)
        self.assertTrue(all(e[1] == 100 for e in self.world.events))       # posted to the verified window handle only

    def test_macro_for_another_window_needs_an_explicit_bind(self):
        self.pick()
        self.c.macro = self.macro_for("win-somewhere-else")
        self.assertTrue(self.c.needs_binding and not self.c.bound)
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "binding")
        self.assertEqual(self.world.events, [])
        self.assertTrue(self.c.bind())
        self.assertTrue(self.c.play(), self.c.status)
        self.finish_playback()
        self.assertEqual(self.c.last_report.state, PlaybackState.COMPLETED)
        self.assertTrue(all(e[1] == 100 for e in self.world.events))

    def test_binding_is_forgotten_when_the_target_changes(self):
        self.pick("Notes")
        self.c.macro = self.macro_for("win-somewhere-else")
        self.c.bind()
        self.assertTrue(self.c.bound)
        self.c.select(self.entry("Calculator").ref)
        self.assertFalse(self.c.bound)
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "binding")

    def test_a_macro_of_another_provider_cannot_be_bound_or_played(self):
        self.pick()
        self.c.macro = Macro(Recording("other-provider", "t", (E(K.POINTER_MOVE, 0, 1, 1),)), "alien")
        self.assertFalse(self.c.bind())
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "preflight")
        self.assertEqual(self.world.events, [])

    def test_preflight_failure_rejects_the_whole_macro_before_any_input(self):
        self.pick()
        events = (E(K.POINTER_MOVE, 0, 3, 3), E(K.BUTTON_DOWN, 5, 3, 3, B.SECONDARY, 1))       # secondary hold: unsupported
        self.c.macro = Macro(Recording("windows", self.c.target.target_id, events), "bad")
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "preflight")
        self.assertIn("nothing was sent", self.c.status.text)
        self.assertTrue(self.c.preflight_lines)
        self.assertEqual(self.world.events, [])
        self.assertFalse(self.c.playing)

    def test_negative_coordinates_fail_preflight(self):
        self.pick()
        events = (E(K.POINTER_MOVE, 0, -3, 3),)
        self.c.macro = Macro(Recording("windows", self.c.target.target_id, events), "neg")
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "preflight")

    def test_nothing_to_play(self):
        self.pick()
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "playback")

    def test_cancel_stops_a_running_playback_and_sends_nothing_more(self):
        self.pick()
        self.c.macro = self.macro_for(self.c.target.target_id, delay=2000, count=4)
        self.assertTrue(self.c.play(), self.c.status)
        self.assertTrue(self.c.playing and self.c.can_play() is False)
        time.sleep(0.05)
        self.c.cancel()
        self.finish_playback()
        sent = len(self.world.events)
        self.assertEqual((self.c.last_report.state, self.c.status.category), (PlaybackState.CANCELLED, "cancelled"))
        self.assertLess(self.c.last_report.completed_count, 4)
        time.sleep(0.1)
        self.assertEqual(len(self.world.events), sent)

    def test_emergency_stop_halts_playback_and_blocks_new_work_until_cleared(self):
        self.pick()
        self.c.macro = self.macro_for(self.c.target.target_id, delay=2000, count=4)
        self.c.play()
        self.c.emergency_stop()
        self.finish_playback()
        self.assertEqual((self.c.last_report.cancel_reason, self.c.status.category), ("emergency_stop", "emergency_stop"))
        self.assertTrue(self.c.emergency_active)
        self.assertFalse(self.c.start_recording(discard_unsaved=True))
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "emergency_stop")
        self.assertTrue(self.c.clear_emergency_stop())
        self.assertFalse(self.c.emergency_active)
        self.assertTrue(self.c.play(dry_run=True))
        self.finish_playback()

    def test_emergency_stop_ends_a_recording(self):
        self.pick()
        self.c.start_recording()
        self.c.emergency_stop()
        self.assertFalse(self.c.recording_active)
        self.assertEqual(self.c.status.category, "emergency_stop")

    def test_window_lost_during_playback_is_a_stale_target_failure(self):
        self.pick()
        self.c.macro = self.macro_for(self.c.target.target_id, delay=30, count=5)
        self.world.on_event = lambda w: w.windows.pop(100, None)
        self.assertTrue(self.c.play())
        self.finish_playback()
        self.assertEqual((self.c.last_report.state, self.c.target_status, self.c.status.category),
                         (PlaybackState.FAILED, TargetStatus.STALE, "stale_target"))
        self.assertNotIn("Traceback", self.c.status.text)

    def test_provider_delivery_failure_is_a_playback_error_with_the_action(self):
        self.pick()
        self.c.macro = self.macro_for(self.c.target.target_id, delay=10, count=3)

        def refuse(world):
            raise InputDeliveryError("The window did not accept the input.")
        self.world.on_event = refuse
        self.c.play()
        self.finish_playback()
        self.assertEqual(self.c.status.category, "playback")
        self.assertIn("action 1 of 3", self.c.status.text)
        self.assertIn("did not accept", self.c.status.text)

    def test_cancel_with_nothing_running_is_harmless(self):
        self.assertTrue(self.c.cancel())
        self.assertEqual(self.world.events, [])

    def test_shutdown_ends_active_work(self):
        self.pick()
        self.c.macro = self.macro_for(self.c.target.target_id, delay=2000, count=3)
        self.c.play()
        self.c.shutdown()
        self.c.poll()
        self.assertFalse(self.c.playing)
        self.pick()
        self.c.start_recording()
        self.c.shutdown()
        self.assertFalse(self.c.recording_active)


class Preview(Base):
    def test_preview_uses_the_screenshot_provider_and_needs_a_verified_target(self):
        self.c.refresh()
        self.assertIsNone(self.c.begin_preview())
        self.pick()
        job = self.c.begin_preview()
        shot = job()
        self.assertTrue(self.c.finish_preview(shot))
        self.assertEqual((self.c.preview.width, self.c.preview.height, self.c.preview.source.provider_id),
                         (64, 48, "windows"))
        self.assertEqual(self.world.captures, 1)

    def test_capture_failure_is_a_preview_error(self):
        self.pick()
        self.world.windows[100] = info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10, minimized=True)
        job = self.c.begin_preview()
        try:
            job()
        except Exception as exc:
            self.assertFalse(self.c.finish_preview(exc))
        self.assertEqual(self.c.status.category, "preview")
        self.assertIsNone(self.c.preview)

    def test_no_preview_while_busy(self):
        self.pick()
        self.c.start_recording()
        self.assertIsNone(self.c.begin_preview())
        self.c.cancel()

    def test_selecting_another_window_clears_the_old_preview(self):
        self.pick()
        self.c.finish_preview(self.c.begin_preview()())
        self.c.select(self.entry("Calculator").ref)
        self.assertIsNone(self.c.preview)


class Diagnostics(Base):
    def test_diagnostics_show_technical_information_and_log(self):
        self.record()
        sections = dict(self.c.diagnostics())
        target = dict(sections["Target"])
        self.assertEqual(target["provider"], "windows")
        self.assertIn("record", target["capabilities"])
        self.assertEqual(target["verification"], "window_valid, process_identity, window_class")
        self.assertEqual(dict(sections["Recorder"])["state"], "idle")
        self.assertEqual(dict(sections["Playback"])["emergency stop"], "clear")
        self.assertTrue(any("[verified]" in line for line in self.c.log))

    def test_unexpected_errors_never_leak_a_traceback(self):
        self.pick()
        self.services.create_recorder = lambda t: (_ for _ in ()).throw(KeyError("boom"))
        self.assertFalse(self.c.start_recording())
        self.assertIn("Unexpected error (KeyError)", self.c.status.text)
        self.assertNotIn("Traceback", self.c.status.text)
        self.assertTrue(any("KeyError" in line for line in self.c.log))


class ControllerStateMachine(Base):
    """Repeated calls and failure paths must never leave the controller claiming a success that did not happen,
    nor lose state a successful prior operation already established. Each test traces one concrete transition
    pair from the controller's actual state fields (macro, unsaved, macro_path, recorder, engine, target_status)."""

    # ---- repeated calls: the second call must fail cleanly, the state from the first must survive -------------
    def test_stop_recording_twice_the_second_call_fails_and_keeps_the_recorded_macro(self):
        self.record()
        macro_after_first_stop = self.c.macro
        self.assertFalse(self.c.stop_recording())
        self.assertEqual(self.c.status.category, "recording")
        self.assertIs(self.c.macro, macro_after_first_stop)      # not cleared or replaced by the failed call
        self.assertTrue(self.c.unsaved)

    def test_cancel_twice_with_nothing_active_the_second_call_is_a_harmless_no_op(self):
        self.assertTrue(self.c.cancel())
        self.assertEqual(self.c.status.category, "ready")
        self.assertTrue(self.c.cancel())
        self.assertEqual(self.c.status.category, "ready")

    def test_save_twice_with_the_same_name_the_second_call_asks_for_confirmation_not_a_silent_overwrite(self):
        self.record()
        self.assertTrue(self.c.save("Twice"))
        first_path = self.c.macro_path
        first_bytes = first_path.read_bytes()
        self.record()                                             # a second, different recording
        self.assertFalse(self.c.save("Twice"))                    # same name again, no overwrite requested
        self.assertEqual(self.c.status.category, "exists")
        self.assertEqual(first_path.read_bytes(), first_bytes)     # the first save is untouched
        self.assertTrue(self.c.unsaved)                            # the second recording is still unsaved

    def test_delete_twice_the_second_call_fails_cleanly_without_touching_macro_path_again(self):
        self.record()
        self.c.save("Gone")
        path = self.c.macro_path
        self.assertTrue(self.c.delete(path))
        self.assertIsNone(self.c.macro_path)
        self.assertFalse(self.c.delete(path))
        self.assertEqual(self.c.status.category, "storage")
        self.assertIsNone(self.c.macro_path)                       # still cleared, not resurrected or corrupted

    def test_load_twice_is_idempotent(self):
        self.record()
        self.c.save("Reloadable")
        path = self.c.macro_path
        self.c.macro = None
        self.assertTrue(self.c.load(path))
        first = self.c.macro
        self.assertTrue(self.c.load(path))
        self.assertEqual(self.c.macro, first)
        self.assertFalse(self.c.unsaved)

    def test_refresh_macros_twice_reflects_the_current_folder_both_times(self):
        self.assertTrue(self.c.refresh_macros())
        self.assertEqual(self.c.macros, [])
        self.record()
        self.c.save("Now visible")
        self.assertTrue(self.c.refresh_macros())
        self.assertEqual([m.name for m in self.c.macros], ["Now visible"])
        self.assertTrue(self.c.refresh_macros())                   # calling it again changes nothing
        self.assertEqual([m.name for m in self.c.macros], ["Now visible"])

    def test_emergency_stop_triggered_twice_stays_triggered_with_the_first_reason(self):
        self.c.emergency_stop()                                    # always an "error"-level status: returns False
        self.assertEqual(self.services.stop.reason, "GUI emergency stop")
        self.c.emergency_stop()                                    # triggering an already-active stop is harmless
        self.assertTrue(self.c.emergency_active)
        self.assertEqual(self.services.stop.reason, "GUI emergency stop")

    # ---- failed operations must not leave stale or falsely-successful state -----------------------------------
    def test_a_failed_save_leaves_the_recording_unsaved_with_no_macro_path(self):
        self.record()
        self.assertFalse(self.c.save("???"))                       # slugifies to empty: refused
        self.assertEqual(self.c.status.category, "storage")
        self.assertTrue(self.c.unsaved)
        self.assertIsNone(self.c.macro_path)

    def test_a_failed_load_leaves_the_previously_loaded_macro_in_place(self):
        self.record()
        self.c.save("Original")
        original_path = self.c.macro_path
        original_macro = self.c.macro
        broken = Path(self.tmp.name) / "macros" / "broken.macro.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json", encoding="utf-8")
        self.assertFalse(self.c.load(broken))
        self.assertEqual(self.c.status.category, "storage")
        self.assertIs(self.c.macro, original_macro)
        self.assertEqual(self.c.macro_path, original_path)

    def test_a_provider_failure_before_playback_starts_leaves_no_engine_and_not_playing(self):
        self.record()
        self.c.save("p")
        self.c.bind()
        self.services.create_playback = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider exploded"))
        self.assertFalse(self.c.play())
        self.assertEqual(self.c.status.category, "playback")
        self.assertIn("Unexpected error (RuntimeError)", self.c.status.text)
        self.assertIsNone(self.c.engine)
        self.assertFalse(self.c.playing)
        self.assertFalse(self.c.busy)

    def test_discard_when_nothing_is_unsaved_is_a_harmless_no_op(self):
        self.pick()
        self.assertFalse(self.c.recording_active)
        self.assertTrue(self.c.start_recording())                  # no unsaved recording to discard; starts clean
        self.assertEqual(self.c.status.category, "recording")

    def test_cancel_when_not_recording_or_playing_never_reports_a_recording_cancelled(self):
        self.pick()
        self.assertTrue(self.c.cancel())
        self.assertEqual(self.c.status.category, "ready")          # not "cancelled": nothing was actually running


class Formatting(unittest.TestCase):
    def test_times_and_events(self):
        self.assertEqual(formatting.format_time(0), "00:00.000")
        self.assertEqual(formatting.format_time(61_250), "01:01.250")
        rec = Recording("p", "t", (E(K.POINTER_MOVE, 0, 320, 180), E(K.BUTTON_DOWN, 120, 320, 180, B.PRIMARY, 1),
                                   E(K.BUTTON_UP, 5, 320, 180, B.SECONDARY, 2), E(K.KEY_DOWN, 300, key=Key.ENTER),
                                   E(K.KEY_UP, 40, key=Key.F5), E(K.TEXT, 1, text="secret")))
        rows = formatting.event_rows(rec)
        self.assertEqual(rows[0], ("00:00.000", "Move", "(320, 180)"))
        self.assertEqual(rows[1], ("00:00.120", "Left Down", "(320, 180)"))
        self.assertEqual(rows[2][1], "Right Up (double)")
        self.assertEqual((rows[3][1], rows[3][2], rows[4][2]), ("Key Down", "Enter", "F5"))
        self.assertEqual(rows[5][2], "6 characters")                      # typed text is never echoed
        self.assertNotIn("secret", str(rows))


class Isolation(unittest.TestCase):
    # rglob, not glob: the view is a package now, and every one of its modules is held to the same rules.
    GUI = sorted((ROOT / "gui").rglob("*.py"))
    BANNED = ("SendInput", "SetCursorPos", "mouse_event", "keybd_event", "GetForegroundWindow", "SetForegroundWindow",
              "GetCursorPos", "PostMessage", "SendMessage", "EnumWindows", "ctypes", "win32", "pywin", "pyautogui",
              "subprocess", "hwnd", "WindowVerifier", "WindowInputSession", "PrintWindow")

    def test_gui_layer_uses_no_provider_operating_system_or_input_api(self):
        self.assertTrue(len(self.GUI) >= 6)
        for path in self.GUI:
            src = path.read_text(encoding="utf-8")
            for word in self.BANNED:
                self.assertNotIn(word, src, f"{path.name} mentions {word}")
            bad = {m for m in imports_of(path)
                   if m.startswith(("app.windows", "app.adb", "app.ldplayer", "app.instances", "app.bootstrap",
                                    "app.macros", "app.automation"))}
            self.assertEqual(bad, set(), path.name)

    def test_gui_layer_is_target_agnostic(self):
        pattern = re.compile(r"(?<![a-z])(adb|ldplayer|leidian|ldconsole|viking|emulator)", re.I)
        for path in self.GUI:
            self.assertEqual(pattern.findall(path.read_text(encoding="utf-8")), [], path.name)

    # GUI -> use case / domain -> observation / decision -> provider / action. The GUI may build on the
    # provider-neutral CONTRACTS of the layers below it, never on an engine behind them.
    ALLOWED_PACKAGES = {"core", "gui", "playback", "providers"}
    ALLOWED_MODULES = {"app.decide", "app.decide.format",      # conditions, decisions, run_steps, the v2 file
                       "app.observe",                          # queries, results, the per-target context
                       "app.vision.bitmap", "app.vision.frame", "app.vision.roi"}   # image data, no algorithm
    ENGINES = ("app.ocr", "app.vision.template", "app.vision.color", "app.vision.coords",
               "app.observe.observer", "app.decide.run", "app.decide.wait", "app.ai")

    def test_gui_only_imports_allowed_layers(self):
        for path in self.GUI:
            for m in imports_of(path):
                if m.startswith("app."):
                    self.assertTrue(m.split(".")[1] in self.ALLOWED_PACKAGES or m in self.ALLOWED_MODULES,
                                    f"{path.name}: {m}")

    def test_gui_reaches_no_recognition_engine_or_run_internals(self):
        """The GUI asks questions through `ObservationContext` and runs steps through `run_steps` (the package's
        public surface); which OCR engine reads text, and how pixels are matched, stays out of it."""
        for path in self.GUI:
            bad = {m for m in imports_of(path) if m.startswith(self.ENGINES)}
            self.assertEqual(bad, set(), path.name)
            source = path.read_text(encoding="utf-8")
            for word in ("WindowsOcrProvider", "winrt", "match_template", "find_color", "powershell"):
                self.assertNotIn(word, source, f"{path.name} mentions {word}")

    def test_nothing_else_depends_on_the_gui_or_the_composition_root(self):
        for path in ROOT.rglob("*.py"):
            top = path.relative_to(ROOT).parts[0]
            if top in ("gui", "bootstrap", "__main__.py"):
                continue
            bad = {m for m in imports_of(path) if m.startswith(("app.gui", "app.bootstrap"))}
            self.assertEqual(bad, set(), str(path))

    def test_composition_root_adds_no_input_primitives(self):
        for path in (ROOT / "bootstrap").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            for word in ("post_mouse", "post_key", "post_char", "SendInput", "SetCursorPos", "PostMessage", "mouse_event"):
                self.assertNotIn(word, src, f"{path.name} mentions {word}")

    def test_only_the_view_imports_tkinter(self):
        """Tk stays inside the view: `app.py` (the shell) and `app/gui/view/*` (the screens). Every logic module
        -- controller, library, metadata, history, list rule, editor bridge, theme, i18n, prefs -- must stay
        importable, and testable, with no display at all."""
        for path in self.GUI:
            has_tk = any(m.split(".")[0] == "tkinter" for m in imports_of(path))
            may = path.name == "app.py" or path.parent.name == "view"
            if not may:
                self.assertFalse(has_tk, f"{path.name} imports tkinter but may not")

    def test_the_view_package_holds_no_automation_state(self):
        """Screens render the controller; they never keep their own copy of a macro, target or engine."""
        for path in (ROOT / "gui" / "view").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("PlaybackEngine", "create_playback", "services.verify", "RecordingSession"):
                self.assertNotIn(forbidden, source, f"{path.name} mentions {forbidden}")

    def test_entry_point_exists(self):
        self.assertTrue((ROOT / "__main__.py").is_file())
        self.assertIn("app.bootstrap.main", (ROOT / "__main__.py").read_text(encoding="utf-8"))


class ThemeSystem(unittest.TestCase):
    """`app.gui.theme`: no Tk needed -- pure data plus the small colour-lookup helpers."""

    def tearDown(self):
        th.set_theme(th.DEFAULT_THEME)          # ACTIVE is process-wide global state: never leak it into other tests

    def test_two_themes_exist_and_light_green_is_the_default(self):
        self.assertEqual(set(th.THEMES), {"light_green", "dark_green"})
        self.assertEqual(th.THEME_ORDER, ("light_green", "dark_green"))
        self.assertEqual(th.DEFAULT_THEME, "light_green")
        self.assertIs(th.ACTIVE, th.THEMES["light_green"])

    def test_set_theme_swaps_active_and_falls_back_on_an_unknown_id(self):
        self.assertIs(th.set_theme("dark_green"), th.THEMES["dark_green"])
        self.assertEqual(th.ACTIVE.id, "dark_green")
        self.assertIs(th.set_theme("not_a_real_theme"), th.THEMES[th.DEFAULT_THEME])

    def test_every_theme_defines_a_distinct_looking_surface_and_sidebar(self):
        for theme in th.THEMES.values():
            self.assertNotEqual(theme.surface, theme.sidebar_bg, theme.id)     # a card never blends into the sidebar
            self.assertTrue(theme.id and theme.label)

    def test_semantic_colour_helpers_read_the_active_theme(self):
        th.set_theme("light_green")
        light = (th.level_colour("error"), th.target_colour("verified"), th.dot_colour("red"))
        th.set_theme("dark_green")
        dark = (th.level_colour("error"), th.target_colour("verified"), th.dot_colour("red"))
        self.assertEqual(light, (th.THEMES["light_green"].error, th.THEMES["light_green"].success,
                                 th.THEMES["light_green"].error))
        self.assertEqual(dark, (th.THEMES["dark_green"].error, th.THEMES["dark_green"].success,
                                th.THEMES["dark_green"].error))
        self.assertNotEqual(light, dark)            # each theme picks its own legible shade, not one hex everywhere


class PreferencesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gui_settings.json"

    def test_missing_file_loads_as_empty_and_never_raises(self):
        self.assertEqual(PreferencesStore(self.path).load(), {})

    def test_save_then_load_round_trips(self):
        store = PreferencesStore(self.path)
        store.save({"theme": "dark_green", "language": "ru"})
        self.assertEqual(PreferencesStore(self.path).load(), {"theme": "dark_green", "language": "ru"})

    def test_none_path_never_touches_disk(self):
        store = PreferencesStore(None)
        store.save({"theme": "dark_green"})
        self.assertEqual(store.load(), {})
        self.assertFalse(self.path.exists())

    def test_a_corrupt_file_loads_as_empty_instead_of_raising(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(PreferencesStore(self.path).load(), {})


class I18nCoverage(unittest.TestCase):
    def _referenced_keys(self) -> set[str]:
        source = "".join((ROOT / "gui" / name).read_text(encoding="utf-8")
                         for name in ("controller.py", "library.py", "macro_edit.py", "flow_edit.py", "flows.py"))
        keys = set(re.findall(r'key="(msg_[a-z_]+)"', source)) | set(re.findall(r'"(err_[a-z_]+)"', source))
        keys |= {f"msg_run_{outcome}" for outcome in                       # built as f"msg_run_{outcome}"
                 ("completed", "partial", "failed", "stopped", "refused")}
        keys |= {"msg_macro_not_valid"}                                    # returned by why_cannot_run()
        keys |= {"msg_probe_false"}                                        # the other half of a conditional key
        keys |= {f"err_query_{code}" for code in QUERY_CODES}             # built as f"err_query_{code}"
        return keys

    def test_every_sentence_the_controller_can_show_has_a_translation(self):
        """The status bar, and every storage/editing refusal, must be readable in both languages: the English
        text stays as the log line, but what the user reads comes from a key."""
        missing = sorted(key for key in self._referenced_keys() if key not in _STRINGS)
        self.assertEqual(missing, [], "controller/library/editor messages with no translation")

    def test_a_translated_sentence_takes_the_same_values_in_both_languages(self):
        """A template whose languages disagree about placeholders would fall back to English at runtime."""
        mismatched = {}
        for key in self._referenced_keys() & set(_STRINGS):
            fields = {lang: set(re.findall(r"\{(\w+)\}", _STRINGS[key][lang])) for lang in LANGUAGES}
            if len(set(map(frozenset, fields.values()))) != 1:
                mismatched[key] = fields
        self.assertEqual(mismatched, {})

    def test_a_search_refusal_is_said_with_the_numbers_it_carries(self):
        """2026-10-25: a picture larger than its area (and every other refusal a person can meet) was English; each
        code's sentence takes exactly the numbers its error carries, in both languages."""
        from app.observe import InvalidQuery
        from app.observe.scale import scaled_query
        from app.vision.bitmap import Bitmap, VisionError
        from app.vision.template import search_template, check_template
        from app.observe import TemplateQuery
        from tests.test_vision import frame_of
        picture = Bitmap(30, 20, bytes((x * 7 + y * 13 + c * 50) % 256 for y in range(20) for x in range(30)
                                       for c in range(3)))
        raised = []
        for call in (lambda: search_template(frame_of(Bitmap(64, 48, bytes(64 * 48 * 3))), picture,
                                             roi=Roi(0, 0, 20, 15)),
                     lambda: check_template(Bitmap(4, 4, bytes(48))),
                     lambda: Roi(10, 10, 100, 50).check_within(64, 48),
                     lambda: scaled_query(TemplateQuery(picture), 0.05, 0.05, 64, 48)):
            try:
                call()
            except (VisionError, InvalidQuery) as exc:
                raised.append(exc)
        self.assertEqual([e.code for e in raised], ["picture_larger_than_area", "flat_picture",
                                                    "area_outside_image", "picture_too_small"])
        for exc in raised:
            for lang in LANGUAGES:
                text = _STRINGS[f"err_query_{exc.code}"][lang].format(**exc.params)
                self.assertNotIn("{", text)

    def test_every_string_is_translated_into_every_supported_language(self):
        missing = {key: [lang for lang in LANGUAGES if lang not in entry] for key, entry in _STRINGS.items()}
        missing = {k: v for k, v in missing.items() if v}
        self.assertEqual(missing, {}, "keys missing a translation (see app/gui/i18n.py)")


try:
    import tkinter as tk
except ImportError:                                   # pragma: no cover
    tk = None


def _descendants(widget):
    """Every widget under `widget`, so a test can ask what the screen actually built."""
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def find_combobox(widget):
    """Depth-first search for the first ttk.Combobox under `widget` (the Settings page builds it inline, without
    keeping its own attribute -- the same widget the user actually clicks)."""
    for child in widget.winfo_children():
        if child.winfo_class() == "TCombobox":
            return child
        found = find_combobox(child)
        if found is not None:
            return found
    return None


@unittest.skipIf(tk is None, "tkinter is not available")
class Shell(Base):
    """The real window, built but never shown: navigation, theme, language and the always-present stop."""

    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.answers = []
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False,
                       confirm=lambda t, m: self.answers.append((t, m)) or True)
        self.c = self.app.controller
        self.macros = self.app.screens["macros"]
        self.c.refresh()
        self.addCleanup(self.app.close)

    def stub(self, name, value):
        """Answer one modal dialog without showing it."""
        from app.gui.view import dialogs

        class Stub:
            def __init__(self, *args, **kwargs):
                pass

            def show(self):
                return value

        original = getattr(dialogs, name)
        setattr(dialogs, name, Stub)
        self.addCleanup(setattr, dialogs, name, original)

    def make(self, name="Farm", *, folder="", remembered=True):
        """A macro recorded on Notes and saved, chosen in the list. `remembered=False`: one copied into the library
        instead, which remembers no target."""
        if remembered:
            self.record()
            self.assertTrue(self.c.save(name, folder=folder), self.c.status)
            self.c.refresh_macros()
            key = next(i.key for i in self.c.macros if i.name == name)
        else:
            key = self.copied_in(name)
        self.macros.select_row(key)
        return key

    def test_the_application_opens_on_the_macro_list(self):
        self.assertEqual(self.app.screen, "macros")
        self.assertEqual(self.app.root.title(), "Olive Automation")
        self.assertEqual(self.app.lbl_title.cget("text"), "Macros")              # the page header names the page
        self.assertEqual(self.app.lbl_where.cget("text"), self.app.t("sub_macros"))

    def test_the_emergency_stop_is_hidden_while_idle_and_on_every_screen_while_something_runs(self):
        for screen in ("macros", "history", "text", "settings", "diagnostics"):
            self.app.show_screen(screen)
            self.assertFalse(self.app.stop_bar.winfo_manager(), screen)       # nothing to stop: not shown
        self.pick()
        self.assertTrue(self.c.start_recording(), self.c.status)
        for screen in ("macros", "history", "text", "settings", "diagnostics"):
            self.app.show_screen(screen)
            self.assertTrue(self.app.stop_bar.winfo_manager(), screen)
            self.assertTrue(self.app.btn_estop.winfo_manager(), screen)
            self.assertNotIn("disabled", self.app.btn_estop.state(), screen)
        self.assertTrue(self.c.cancel())

    def test_the_navigation_rail_switches_screens_and_marks_the_active_one(self):
        button, _ = self.app.nav_buttons["history"]
        button.invoke()
        self.assertEqual(self.app.screen, "history")
        self.assertEqual(str(button.cget("style")), "NavActive.TButton")
        self.assertEqual(str(self.app.nav_buttons["macros"][0].cget("style")), "Nav.TButton")
        self.assertEqual(self.app.lbl_title.cget("text"), "History")
        self.assertEqual(set(self.app.nav_buttons), {"macros", "scripts", "text", "history", "settings"})

    def test_ctrl_shift_d_shows_diagnostics_and_goes_back(self):
        self.assertEqual(self.app._toggle_diagnostics(), "break")
        self.assertEqual(self.app.screen, "diagnostics")
        self.app._toggle_diagnostics()
        self.assertEqual(self.app.screen, "macros")

    def test_the_breadcrumb_says_where_you_are(self):
        self.assertEqual(self.app._breadcrumb(), "Macros")
        self.app.show_screen("history")
        self.assertEqual(self.app._breadcrumb(), "History")

    def test_language_switches_both_ways(self):
        self.assertEqual(self.app.nav_buttons["history"][0].cget("text"), "History")
        self.app.apply_language("ru")
        self.assertEqual(self.app.root.title(), "Olive Automation")                # the product's name
        self.assertEqual(self.app.nav_buttons["history"][0].cget("text"), "История")
        self.assertEqual(self.app.lbl_title.cget("text"), "Макросы")
        self.app.apply_language("en")
        self.assertEqual(self.app.nav_buttons["history"][0].cget("text"), "History")

    def test_the_settings_language_box_applies_the_language(self):
        self.app.show_screen("settings")
        combo = find_combobox(self.app.screens["settings"].frame)
        self.assertIsNotNone(combo)
        combo.set("Русский")
        combo.event_generate("<<ComboboxSelected>>")
        self.assertEqual(self.app.t.language, "ru")

    def test_a_theme_change_reaches_the_raw_tk_widgets_too(self):
        self.assertEqual(th.ACTIVE.id, "light_green")
        self.app.apply_theme("dark_green")
        self.assertEqual(str(self.app.root.cget("background")), th.ACTIVE.bg)
        self.app.render()
        scope = self.macros.nav_items["all"]                        # a raw tk row of the macro list's view picker
        self.assertEqual(str(scope["row"].cget("background")), th.ACTIVE.primary_soft)
        self.assertEqual(str(self.app._mark.cget("background")), th.ACTIVE.sidebar_bg)
        self.app.apply_theme("light_green")
        self.assertEqual(str(self.app.root.cget("background")), th.ACTIVE.bg)
        self.app.render()
        self.assertEqual(str(scope["row"].cget("background")), th.ACTIVE.primary_soft)

    def test_the_theme_is_remembered_between_runs(self):
        from app.gui.app import App
        from app.gui.prefs import PreferencesStore
        store = PreferencesStore(Path(self.tmp.name) / "gui.json")
        first_root = tk.Tk()
        first_root.withdraw()
        first = App(self.services, self.library, root=first_root, auto_refresh=False, prefs=store,
                    confirm=lambda t, m: True)
        first.apply_theme("dark_green")
        first.close()
        second_root = tk.Tk()
        second_root.withdraw()
        second = App(self.services, self.library, root=second_root, auto_refresh=False, prefs=store,
                     confirm=lambda t, m: True)
        self.addCleanup(second.close)
        self.assertEqual(th.ACTIVE.id, "dark_green")


@unittest.skipIf(tk is None, "tkinter is not available")
class MacroScreenView(Shell):
    """The main screen, driven the way a person drives it."""

    def test_nothing_is_chosen_at_first_and_run_says_what_is_missing(self):
        self.assertEqual(self.c.selected_key, "")
        self.assertIn("disabled", self.macros.btn_run.state())
        self.assertEqual(self.c.why_cannot_run(), "msg_pick_macro")

    def test_choosing_a_macro_fills_the_panel_that_carries_run(self):
        self.make("Farm", remembered=False)
        self.app.render()
        self.assertEqual(self.macros.lbl_name.cget("text"), "Farm")
        self.assertIn("disabled", self.macros.btn_run.state())          # still no targets
        self.assertEqual(self.c.why_cannot_run(), "msg_no_run_targets")

    def test_a_recorded_macro_shows_the_target_it_was_recorded_on_ready_to_run(self):
        """Item 12 of 2026-10-25: a macro remembers its target -- here the window it was recorded on."""
        self.make("Farm")
        self.app.render()
        self.assertIn("Notes", self.macros.lbl_targets.cget("text"))
        self.assertNotIn("disabled", self.macros.btn_run.state())
        self.assertEqual(self.c.run_refs, (self.entry("Notes").ref,))

    def test_the_group_of_a_target_is_said_in_the_persons_language(self):
        """Seen in the visual check of item 12: "Open Windows" stood in English in the Russian panel."""
        self.make("Farm")
        self.app.apply_language("ru")
        self.app.render()
        shown = self.macros.lbl_targets.cget("text")
        self.assertIn("Открытые окна", shown)
        self.assertNotIn("Open Windows", shown)
        from app.gui.view import fmt
        self.assertEqual(fmt.group_label(self.app, "Full Windows"), "Full Windows")     # a name stays a name
        self.c.set_run_refs([self.entry("Full Windows").ref])
        self.app.render()
        self.assertNotIn("Full Windows  ·  Full Windows", self.macros.lbl_targets.cget("text"))   # said once

    def test_run_becomes_available_only_once_targets_are_chosen(self):
        self.make("Farm")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.app.render()
        self.assertNotIn("disabled", self.macros.btn_run.state())
        self.assertIn("1", self.macros.btn_run.cget("text"))
        self.assertEqual(self.c.why_cannot_run(), "")

    def test_search_narrows_the_list_and_a_chip_says_why(self):
        self.make("Farm")
        self.make("Daily")
        self.macros.search_var.set("far")
        self.assertEqual([i.name for i in self.c.visible_macros()], ["Farm"])
        self.assertTrue(self.macros._chips.winfo_children())
        self.macros._clear_search()
        self.assertEqual(len(self.c.visible_macros()), 2)

    def test_favourites_are_reachable_from_the_navigation(self):
        key = self.make("Farm")
        self.c.toggle_favorite(key)
        self.macros._pick_scope(Scope.FAVORITES)
        self.assertEqual([i.key for i in self.c.visible_macros()], [key])
        self.macros._pick_scope(Scope.ALL)
        self.assertEqual(len(self.c.visible_macros()), 1)

    def test_creating_a_folder_shows_it_in_the_navigation(self):
        self.stub("TextPrompt", "Tests")
        self.macros._new_folder()
        self.assertIn("folder:Tests", self.macros.nav_items)

    def test_recording_stop_and_save_puts_the_macro_in_the_list(self):
        """The failure this redesign exists to fix: after Stop the result must be visible and keepable."""
        self.stub("ChooseTargets", (self.entry("Notes").ref,))
        self.stub("RecordingResult", ("Recorded macro", ""))
        self.macros._toggle_recording()
        self.assertTrue(self.c.recording_active)
        self.observer.mouse(M.PRIMARY_DOWN, 4, 4, 10)
        self.observer.mouse(M.PRIMARY_UP, 4, 4, 20)
        self.macros._toggle_recording()
        self.assertFalse(self.c.recording_active)
        self.assertIn("Recorded macro", [i.name for i in self.c.macros])
        self.assertEqual(self.c.macro_by_key(self.c.selected_key).name, "Recorded macro")
        self.assertEqual(self.macros.lbl_name.cget("text"), "Recorded macro")

    def test_cancelling_the_recording_dialog_keeps_nothing(self):
        self.stub("ChooseTargets", (self.entry("Notes").ref,))
        self.stub("RecordingResult", None)
        self.macros._toggle_recording()
        self.observer.mouse(M.PRIMARY_DOWN, 4, 4, 10)
        self.observer.mouse(M.PRIMARY_UP, 4, 4, 20)
        self.macros._toggle_recording()
        self.assertEqual(self.c.macros, [])
        self.assertIsNone(self.c.macro)

    def test_renaming_through_the_menu_renames_the_file_too(self):
        key = self.make("Before")
        self.stub("TextPrompt", "After")
        self.macros._rename(key)
        self.assertEqual(self.c.macro_by_key(self.c.selected_key).name, "After")
        self.assertTrue(self.c.macro_by_key(self.c.selected_key).path.name.startswith("after"))

    def test_duplicating_offers_a_name_and_makes_a_second_file(self):
        key = self.make("Farm")
        self.stub("TextPrompt", "Farm copy")
        self.macros._duplicate(key)
        self.assertEqual(sorted(i.name for i in self.c.macros), ["Farm", "Farm copy"])

    def test_deleting_asks_first(self):
        key = self.make("Doomed")
        self.macros._delete(key)
        self.assertEqual(self.answers[-1][0], self.app.t("dlg_delete_title"))
        self.assertEqual(self.c.macros, [])

    def test_an_invalid_macro_is_listed_but_cannot_be_run_or_edited(self):
        self.library.directory.mkdir(parents=True, exist_ok=True)
        (self.library.directory / "broken.macro.json").write_text("{not json", encoding="utf-8")
        self.c.refresh_macros()
        self.macros.select_row("broken.macro.json")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.app.render()
        self.assertIn("broken", [i.name for i in self.c.macros])
        self.assertIn("disabled", self.macros.btn_run.state())
        self.assertFalse(self.c.open_editor("broken.macro.json"))

    def test_running_from_the_screen_writes_one_run_to_the_history(self):
        self.make("Farm")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.macros._run()
        deadline = time.time() + 10
        while self.c.run_active and time.time() < deadline:
            self.c.poll()
            time.sleep(0.01)
        self.c.poll()
        self.app.render()
        self.assertEqual(len(self.c.history.list()), 1)
        self.assertEqual(self.c.history.list()[0].macro, "Farm")

    def test_a_row_cannot_start_a_run_neither_by_a_button_nor_through_its_menu(self):
        """A row Run used to start that macro at once on targets chosen earlier, perhaps for another macro."""
        from app.gui.view import macros as view
        key = self.make("Farm")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.app.render()
        row = self.macros._row_widgets[key]["frame"]
        styles = [str(w.cget("style")) for w in _descendants(row) if w.winfo_class() == "TButton"]
        self.assertNotIn("Primary.TButton", styles)                      # the row's buttons: Edit and "..."
        offered = []
        original = view.w.popup
        view.w.popup = lambda ui, widget, items: offered.extend(i for i in items if i)
        self.addCleanup(setattr, view.w, "popup", original)
        self.macros._row_menu(key)
        self.assertNotIn("menu_run", [item[0] for item in offered])
        self.assertIn("menu_edit", [item[0] for item in offered])
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.world.events, [])

    def test_the_panel_names_the_macro_and_the_targets_it_will_run_on(self):
        key = self.make("Farm")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.app.render()
        self.assertEqual(self.macros.lbl_name.cget("text"), "Farm")
        self.assertIn("Notes", self.macros.lbl_targets.cget("text"))
        self.assertEqual(self.macros.btn_run.cget("text"), self.app.t("btn_run_on").format(count=1))
        self.macros._run()
        self.assertEqual((self.c.run_macro_key, tuple(r.ref for r in self.c.run_results)),
                         (key, (self.entry("Notes").ref,)))
        self.assertTrue(self.c.cancel())

    def test_another_macro_does_not_show_or_use_the_targets_chosen_for_the_first(self):
        self.make("Farm")
        self.c.set_run_refs([self.entry("Notes").ref])
        self.make("Daily", remembered=False)                             # chosen in the list: another macro
        self.app.render()
        self.assertEqual(self.macros.lbl_name.cget("text"), "Daily")
        shown = self.macros.lbl_targets.cget("text")
        self.assertIn(self.app.t("targets_none_chosen"), shown)
        self.assertIn(self.app.t("targets_not_carried"), shown)
        self.assertNotIn("Notes", shown)
        self.assertIn("disabled", self.macros.btn_run.state())
        self.macros._run()                                               # even if Run is invoked anyway
        self.assertFalse(self.c.run_active)

    def test_change_targets_offers_the_last_choice_but_only_a_confirmation_makes_it_this_macros(self):
        from app.gui.view import dialogs
        self.make("Farm")
        notes = self.entry("Notes").ref
        self.c.set_run_refs([notes])
        self.make("Daily", remembered=False)
        offered, answers = [], [None, (notes,)]

        class Picker:
            def __init__(self, ui, entries, chosen, **kwargs):
                offered.append(tuple(chosen))

            def show(self):
                return answers.pop(0)

        original = dialogs.ChooseTargets
        dialogs.ChooseTargets = Picker
        self.addCleanup(setattr, dialogs, "ChooseTargets", original)
        self.macros._choose_targets()                                    # opened, then cancelled
        self.assertEqual(offered, [(notes,)])                            # pre-ticked as a suggestion
        self.assertEqual(self.c.run_refs, ())                            # ... which a cancel does not apply
        self.macros._choose_targets()                                    # opened and confirmed
        self.assertEqual(self.c.run_refs, (notes,))
        self.app.render()
        self.assertNotIn("disabled", self.macros.btn_run.state())
        self.assertIn("Notes", self.macros.lbl_targets.cget("text"))


@unittest.skipIf(tk is None, "tkinter is not available")
class FullWindowsTextScreenView(Base):
    """The Read text screen: a target that declares no capture has Read off and says why; Full Windows is read
    from one picture of the whole desktop and says its coordinates are for looking only."""

    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:                    # no display
            self.skipTest(f"Tk cannot start: {exc}")
        self.root.withdraw()
        from app.gui.app import App
        self.ocr = CountingOcr()
        self.app = App(self.services, self.library, root=self.root, auto_refresh=False, confirm=lambda t, m: True,
                       preview_runner=lambda fn: fn(), ocr=self.ocr)
        self.c = self.app.controller
        self.addCleanup(self.app.close)
        self.screen = self.app.screens["text"]

    def show(self, title):
        self.pick(title)
        self.app.show_screen("text")
        self.app._drain_jobs()
        self.app.render()

    def capture_less(self):
        self.show("Notes")
        self.c.target = NoCaptureTarget()
        self.app.render()

    def test_read_is_off_for_a_target_that_cannot_be_captured_and_the_note_says_why(self):
        self.capture_less()
        self.assertIn("disabled", self.screen.btn_read.state())
        self.assertIn(self.app.t("text_note_no_screenshot"), self.screen.lbl_note.cget("text"))
        self.assertNotIn("disabled", self.screen.btn_target.state())       # another target can still be chosen
        self.screen._read()                                                # invoked anyway: refused, in words
        self.app._drain_jobs()
        self.assertEqual(self.c.status.key, "msg_text_no_screenshot")
        self.assertEqual(self.screen.result_text(), self.app.t("text_result_empty"))   # no "success" shown
        self.assertEqual((self.world.captures, self.world.desktop_captures, self.ocr.frames), (0, 0, []))

    def test_full_windows_is_read_and_its_boxes_are_drawn_over_the_desktop_picture(self):
        self.show("Full Windows")
        self.assertNotIn("disabled", self.screen.btn_read.state())
        self.screen._read()
        self.app._drain_jobs()
        self.app.render()
        self.root.update_idletasks()
        self.screen._draw_preview(force=True)
        canvas = self.screen.preview
        self.assertTrue(canvas.find_withtag("all"))
        self.assertEqual([i for i in canvas.find_all() if canvas.type(i) == "image"].__len__(), 1)
        self.assertEqual(self.world.desktop_captures, 1)
        # the desktop takes clicks now (Full Windows macros): its coordinates are not marked "for looking only"
        self.assertNotIn(self.app.t("text_coords_readonly"), self.screen.lbl_coords.cget("text"))

    def test_read_works_as_before_for_a_real_window(self):
        self.show("Notes")
        self.assertNotIn("disabled", self.screen.btn_read.state())
        self.screen._read()
        self.app._drain_jobs()
        self.app.render()
        self.assertEqual(self.c.reading.lines, ("Hello",))
        self.assertEqual(self.world.captures, 1)

    def test_the_note_follows_the_language(self):
        self.capture_less()
        self.app.apply_language("ru")
        self.assertIn(_STRINGS["text_note_no_screenshot"]["ru"], self.screen.lbl_note.cget("text"))


@unittest.skipIf(tk is None, "tkinter is not available")
class TargetChoice(Shell):
    """Telling two same-named candidates apart before one of them is chosen.

    The same program can be offered by two providers under the very same display name -- as a device by one and
    as a window by another. Choosing the wrong one silently changes what a run means (whether it can go on while
    the window is minimised, and whether it takes over the screen), so the row must say more than the name."""

    def entries_named_alike(self):
        """Two candidates that differ in everything that matters and in nothing that shows."""
        device = TargetEntry("dev-1", "Twin-9", "Runner", "instance", "index 9, running", "device-provider",
                             "Devices", background=True)
        window = TargetEntry("win-1", "Twin-9", "runner.exe", "RunnerMainFrame", "551 x 327", "windows",
                             "Open Windows")
        return device, window

    def test_a_target_entry_is_cautious_about_background_use_unless_its_provider_says_otherwise(self):
        plain = TargetEntry("r", "t", "p", "k", "s", "provider", "group")
        self.assertFalse(plain.background)

    def test_the_windows_provider_does_not_claim_background_use(self):
        self.c.refresh()
        self.assertTrue(self.c.entries)
        self.assertTrue(all(not e.background for e in self.c.entries),
                        [e.title for e in self.c.entries if e.background])

    def test_two_candidates_with_the_same_name_are_still_told_apart_in_the_picker(self):
        from app.gui.view.dialogs import ChooseTargets
        device, window = self.entries_named_alike()
        picker = ChooseTargets(self.app, [device, window], ())
        try:
            self.assertEqual(picker.rows[device.ref]["title"].cget("text"),
                             picker.rows[window.ref]["title"].cget("text"))      # the names really are identical
            device_detail = picker.rows[device.ref]["detail"].cget("text")
            window_detail = picker.rows[window.ref]["detail"].cget("text")
            self.assertNotEqual(device_detail, window_detail)
            self.assertIn("device-provider", device_detail)                      # who offers it
            self.assertIn("index 9, running", device_detail)                     # and what they say about it
            self.assertIn("windows", window_detail)
            self.assertIn("551 x 327", window_detail)
        finally:
            picker.top.destroy()

    def test_the_picker_says_what_choosing_each_one_means_for_the_screen(self):
        from app.gui.view.dialogs import ChooseTargets
        device, window = self.entries_named_alike()
        picker = ChooseTargets(self.app, [device, window], ())
        try:
            self.assertEqual(picker.rows[device.ref]["hint"].cget("text"), self.app.t("target_background"))
            self.assertEqual(picker.rows[window.ref]["hint"].cget("text"), self.app.t("target_needs_focus"))
            # a background-capable target is stated as an ordinary capability, never as a warning
            self.assertEqual(str(picker.rows[device.ref]["hint"].cget("style")), "TargetBackground.TLabel")
            self.assertEqual(str(picker.rows[window.ref]["hint"].cget("style")), "Muted.TLabel")
        finally:
            picker.top.destroy()

    def test_both_sentences_exist_in_every_language(self):
        for key in ("target_background", "target_needs_focus"):
            for language in LANGUAGES:
                self.assertTrue(_STRINGS[key][language].strip(), (key, language))

    def test_the_chosen_targets_shown_next_to_run_name_their_group(self):
        """"Twin-9" beside the Run button cannot say whether the run goes to a device or to a window."""
        device, window = self.entries_named_alike()
        self.make("Farm")
        self.c.entries = [device, window]          # after make(): recording refreshes the real candidate list
        self.c.set_run_refs([device.ref])
        self.app.render()
        self.assertIn("Devices", self.macros.lbl_targets.cget("text"))
        self.c.set_run_refs([window.ref])
        self.app.render()
        self.assertIn("Open Windows", self.macros.lbl_targets.cget("text"))

    def test_choosing_is_still_explicit_and_empty_still_means_none(self):
        device, window = self.entries_named_alike()
        self.make("Farm")
        self.c.entries = [device, window]
        self.c.set_run_refs([])
        self.assertEqual(self.c.run_refs, ())
        self.assertFalse(self.c.can_run())
        self.assertFalse(self.c.start_run())
        self.c.set_run_refs([window.ref])
        self.assertEqual(self.c.run_refs, (window.ref,))          # exactly the one that was ticked, nothing else


@unittest.skipIf(tk is None, "tkinter is not available")
class EditorScreenView(Shell):
    def open_editor(self, name="Farm"):
        key = self.make(name)
        self.macros._edit_selected()
        self.assertEqual(self.app.screen, "editor")
        return key

    def test_editing_opens_the_steps_of_that_macro(self):
        self.open_editor()
        editor = self.app.screens["editor"]
        self.assertGreater(len(editor.tree.get_children()), 0)
        self.assertIn("Farm", editor.lbl_title.cget("text"))

    def test_saving_is_only_offered_when_something_changed(self):
        self.open_editor()
        editor = self.app.screens["editor"]
        self.assertIn("disabled", editor.btn_save.state())
        self.c.editor_apply(macro_edit.set_point, 0, 12, 34)
        self.app.render()
        self.assertNotIn("disabled", editor.btn_save.state())
        self.assertEqual(editor.lbl_state.cget("text"), self.app.t("editor_unsaved"))

    def test_leaving_with_unsaved_changes_asks_and_can_save(self):
        key = self.open_editor()
        self.c.editor_apply(macro_edit.set_point, 0, 12, 34)
        self.stub("UnsavedChanges", "save")
        self.app.screens["editor"].go_back()
        self.assertEqual(self.app.screen, "macros")
        self.assertIsNone(self.c.editor)
        self.assertEqual(self.library.load(self.c.macro_by_key(key).path).actions[0].x, 12)

    def test_leaving_can_also_discard(self):
        key = self.open_editor()
        original = self.library.load(self.c.macro_by_key(key).path).actions[0].x
        self.c.editor_apply(macro_edit.set_point, 0, 99, 99)
        self.stub("UnsavedChanges", "discard")
        self.app.screens["editor"].go_back()
        self.assertEqual(self.library.load(self.c.macro_by_key(key).path).actions[0].x, original)

    def test_a_pause_is_shown_in_seconds_not_milliseconds(self):
        self.open_editor()
        editor = self.app.screens["editor"]
        self.c.editor_apply(macro_edit.set_pause, 1, 1500)
        self.app.render()
        rows = [editor.tree.item(iid, "values") for iid in editor.tree.get_children()]
        self.assertTrue(any(row[1] == self.app.t("act_pause") and "1.5" in row[2] for row in rows), rows)


@unittest.skipIf(tk is None, "tkinter is not available")
class BatchProgressView(Shell):
    """What a run over many targets looks like while it is going.

    A batch can cover far more targets than there is room for, so the panel shows a summary, one bar, and only
    the lines worth reading -- what is under way and what went wrong -- with the rest one click away. These
    tests drive the VIEW: the controller's run state is set directly, because what is under test is what the
    panel makes of it, not how it got there."""

    def batch(self, *states):
        """Put the panel into "a run is going" with one target per given state."""
        from app.gui.controller import TargetRun

        self.make("Farm")
        self.c.run_results = tuple(TargetRun(f"r{i}", f"Target {i}", state, completed=3, total=10)
                                   for i, state in enumerate(states))
        self.c.run_active = True
        self.addCleanup(setattr, self.c, "run_active", False)
        self.app.render()
        return self.macros.lbl_targets.cget("text")

    def test_the_summary_says_how_many_are_done_and_how_many_are_in_each_state(self):
        text = self.batch(RunState.RUNNING, RunState.RUNNING, RunState.COMPLETED, RunState.FAILED,
                          RunState.WAITING)
        self.assertIn(self.app.t("run_progress_done").format(done=2, total=5), text)
        for state, count in ((RunState.RUNNING, 2), (RunState.FAILED, 1), (RunState.WAITING, 1)):
            self.assertIn(f"{count} {self.app.t('run_state_' + state.value)}", text)

    def test_one_bar_stands_for_the_whole_batch_not_one_per_target(self):
        self.batch(*([RunState.COMPLETED] * 4 + [RunState.RUNNING] * 2 + [RunState.WAITING] * 12))
        bars = [w for w in _descendants(self.macros.frame) if w.winfo_class() == "TProgressbar"]
        self.assertEqual(len(bars), 1, "a progress widget per target was built")
        self.assertEqual((int(bars[0].cget("value")), int(bars[0].cget("maximum"))), (4, 18))

    def test_only_the_active_and_the_broken_targets_are_listed_until_the_list_is_opened(self):
        states = [RunState.COMPLETED] * 10 + [RunState.RUNNING] * 2 + [RunState.FAILED]
        text = self.batch(*states)
        shown = [line for line in text.splitlines() if line.startswith("Target ")]
        self.assertEqual(len(shown), 3)                       # the two running and the one that failed
        self.assertNotIn("Target 0", text)                    # a target that simply finished is only a count

    def test_the_full_list_is_one_click_away_and_folds_back(self):
        states = [RunState.COMPLETED] * 10 + [RunState.RUNNING] * 2 + [RunState.FAILED]
        self.batch(*states)
        self.macros._toggle_targets()
        self.app.render()
        text = self.macros.lbl_targets.cget("text")
        self.assertEqual(len([line for line in text.splitlines() if line.startswith("Target ")]), 13)
        self.assertEqual(self.macros.btn_show_all.cget("text"), self.app.t("btn_show_less"))
        self.macros._toggle_targets()
        self.app.render()
        self.assertEqual(self.macros.btn_show_all.cget("text"), self.app.t("btn_show_all"))

    def test_a_long_list_says_how_many_it_is_not_showing(self):
        text = self.batch(*([RunState.FAILED] * 9))
        self.assertIn(self.app.t("details_more_targets").format(count=4), text)

    def test_the_stop_that_ends_the_whole_batch_is_the_button_in_place_of_run(self):
        self.batch(RunState.RUNNING, RunState.WAITING)
        self.assertTrue(self.macros.btn_stop.winfo_ismapped() or self.macros.btn_stop.winfo_manager())
        self.assertFalse(self.macros.btn_run.winfo_manager())

    def test_a_single_target_is_not_offered_a_list_to_expand(self):
        self.batch(RunState.RUNNING)
        self.assertFalse(self.macros.btn_show_all.winfo_manager())

    def test_the_panel_leaves_no_batch_widgets_behind_once_the_run_ends(self):
        self.batch(RunState.RUNNING, RunState.WAITING)
        self.c.run_active = False
        self.app.render()
        self.assertFalse(self.macros.run_bar.winfo_manager())
        self.assertFalse(self.macros.btn_show_all.winfo_manager())
        self.assertTrue(self.macros.btn_run.winfo_manager())

    def test_a_full_batch_panel_still_fits_the_smallest_supported_window(self):
        """1120x680 is the declared minimum; the busiest state the panel can reach must fit inside it."""
        self.batch(*([RunState.RUNNING] * 4 + [RunState.FAILED] * 3 + [RunState.WAITING] * 11))
        self.macros._toggle_targets()
        self.app.render()
        self.root.geometry("1120x680")
        self.root.update_idletasks()
        self.assertEqual(self.root.minsize(), (1120, 680))
        self.assertLessEqual(self.macros.frame.winfo_reqwidth(), 1120)


@unittest.skipIf(tk is None, "tkinter is not available")
class ConcurrencySettingView(Shell):
    """The concurrency limit as a person sets it: on the Settings page, remembered for next time."""

    def combobox(self):
        self.app.show_screen("settings")
        self.app.render()
        card = self.app.screens["settings"].concurrency_var
        boxes = [w for w in _descendants(self.app.screens["settings"].frame)
                 if w.winfo_class() == "TCombobox" and str(w.cget("textvariable")) == str(card)]
        self.assertEqual(len(boxes), 1)
        return boxes[0]

    def test_the_offered_choices_are_exactly_the_supported_ones_and_four_is_selected(self):
        box = self.combobox()
        self.assertEqual(list(box.cget("values")), ["1", "2", "4", "8", "16"])
        self.assertEqual(box.get(), "4")
        self.assertEqual(self.c.concurrency, 4)

    def test_choosing_a_value_applies_it_at_once(self):
        box = self.combobox()
        box.set("8")
        box.event_generate("<<ComboboxSelected>>")
        self.assertEqual(self.c.concurrency, 8)

    def test_the_choice_is_remembered_for_the_next_run_of_the_application(self):
        from app.gui.app import App
        from app.gui.prefs import PreferencesStore

        store = PreferencesStore(Path(self.tmp.name) / "gui.json")
        first_root = tk.Tk()
        first_root.withdraw()
        first = App(self.services, self.library, root=first_root, auto_refresh=False, prefs=store,
                    confirm=lambda t, m: True)
        first.apply_concurrency("16")
        self.assertEqual(first.controller.concurrency, 16)
        first.close()

        second_root = tk.Tk()
        second_root.withdraw()
        second = App(self.services, self.library, root=second_root, auto_refresh=False, prefs=store,
                     confirm=lambda t, m: True)
        self.addCleanup(second.close)
        self.assertEqual(second.controller.concurrency, 16)

    def test_exactly_one_theme_card_says_active_even_after_the_language_changes(self):
        """A blanket "set every registered label back to its key" on a language change used to light the word
        up on all three cards at once, so the page said three themes were in use."""
        self.app.show_screen("settings")
        self.app.render()

        def marked():
            return [w.cget("text") for w in _descendants(self.app.screens["settings"].frame)
                    if w.winfo_class() == "TLabel" and w.cget("text") in (self.app.t("theme_active"), "")
                    and w.cget("text")]

        self.assertEqual(len(marked()), 1)
        self.app.apply_language("ru")
        self.assertEqual(marked(), [self.app.t("theme_active")])
        self.app.apply_theme("dark_green")
        self.app.apply_language("en")
        self.assertEqual(marked(), [self.app.t("theme_active")])
        self.app.apply_theme("light_green")

    def test_a_preference_file_that_says_something_impossible_leaves_the_default_in_place(self):
        from app.gui.app import App
        from app.gui.prefs import PreferencesStore

        path = Path(self.tmp.name) / "bad.json"
        path.write_text('{"concurrency": "64"}', encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        app = App(self.services, self.library, root=root, auto_refresh=False, prefs=PreferencesStore(path),
                  confirm=lambda t, m: True)
        self.addCleanup(app.close)
        self.assertEqual(app.controller.concurrency, 4)


if __name__ == "__main__":
    unittest.main()
