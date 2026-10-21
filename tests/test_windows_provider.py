"""Windows provider: target, verification, screenshot provider, isolation. Deterministic (simulated desktop)."""
import ast
import re
import subprocess
import sys
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.core.errors import CapabilityNotAvailable, EmergencyStopped, TargetNotVerified
from app.providers.base import Screenshot, ScreenshotProvider
from app.providers.target import Capability, Target, require_capability, require_verified
from app.vision.color import find_color
from app.vision.frame import Frame
from app.vision.roi import Roi
from app.windows.backend import DesktopCapture, RawBitmap, WindowBackend, WindowCaptureError, WindowInfo
from app.windows.ids import CAPABILITIES, PROVIDER_ID
from app.windows.png import encode_rgb_png_from_bgra
from app.windows.provider import WindowScreenshotProvider
from app.windows.target import _MINT, WindowTarget, WindowVerifier

ROOT = Path(__file__).resolve().parents[1] / "app"


def info(hwnd=100, pid=10, start=5000, name="editor.exe", cls="EditorWnd", title="Untitled", w=64, h=48, **kw):
    base = dict(hwnd=hwnd, title=title, class_name=cls, pid=pid, process_start=start, process_name=name,
                top_level=True, visible=True, minimized=False, cloaked=False, width=w, height=h)
    base.update(kw)
    return WindowInfo(**base)


class FakeDesktop(WindowBackend):
    def __init__(self, *windows):
        self.windows = {w.hwnd: w for w in windows}
        self.pixel = (30, 60, 200)   # B, G, R of the window content
        self.captures = 0
        self.after_capture = None    # optional hook simulating a change during capture

    def enumerate(self):
        return [w for w in self.windows.values() if w.visible and not w.cloaked]

    def query(self, hwnd):
        return self.windows.get(hwnd)

    def capture(self, hwnd, width, height):
        self.captures += 1
        data = bytes(self.pixel) + b"\x00"
        raw = RawBitmap(width, height, data * (width * height))
        if self.after_capture:
            self.after_capture(self)
        return raw

    desktop = (-80, 0, 160, 90)                   # left, top, width, height of the simulated virtual screen
    desktop_captures = 0

    def virtual_screen(self):
        return self.desktop

    def owner_pid(self, hwnd):
        window = self.windows.get(hwnd)
        return window.pid if window is not None else None

    def capture_desktop(self, max_side):
        """The whole SIMULATED desktop (two monitors side by side, the left one at negative x). Never the real
        one: no test looks at the person's screen. Like the real backend, it refuses a desktop larger than
        `max_side` before capturing anything."""
        left, top, width, height = self.desktop
        if not (0 < width <= max_side and 0 < height <= max_side):
            raise WindowCaptureError("The desktop size is not supported for capture.")
        self.desktop_captures += 1
        pixel = bytes(self.pixel) + b"\x00"
        return DesktopCapture(left, top, RawBitmap(width, height, pixel * (width * height)))


def setup(*windows):
    desk = FakeDesktop(*(windows or (info(),)))
    stop = EmergencyStop()
    return desk, stop, WindowVerifier(desk, stop)


class WindowTargetIsAGenericTarget(unittest.TestCase):
    def test_a_verified_window_is_a_neutral_target(self):
        _, _, v = setup()
        t = v.verify(100)
        self.assertIsInstance(t, Target)
        self.assertIs(require_verified(t), t)
        self.assertEqual((t.provider_id, t.target_id[:4]), (PROVIDER_ID, "win-"))
        self.assertEqual(t.capabilities, frozenset(Capability))    # every declared capability, explicitly
        self.assertTrue(t.has_capability(Capability.SCREENSHOT))
        self.assertTrue(t.has_capability(Capability.TAP))           # declared explicitly by this provider
        self.assertEqual(t.verification.methods, ("window_valid", "process_identity", "window_class"))
        self.assertTrue(t.verification.is_verified)
        self.assertIs(require_capability(t, Capability.SCREENSHOT), t)

    def test_generic_code_sees_no_window_details(self):
        _, _, v = setup(info(hwnd=4242, pid=7777))
        t = v.verify(4242)
        neutral = repr((t.identity, t.capabilities, t.verification)) + t.describe() + t.target_id
        for secret in ("4242", "7777", "5000"):
            self.assertNotIn(secret, neutral)
        for attr in ("hwnd", "pid", "class_name"):
            self.assertFalse(hasattr(t.identity, attr))
        self.assertIn("editor.exe", t.identity.display_name)

    def test_identity_is_stable_and_not_title_dependent(self):
        desk, _, v = setup()
        first = v.verify(100)
        desk.windows[100] = replace(desk.windows[100], title="Other title now")
        again = v.verify(100)
        self.assertEqual(first.identity.target_id if hasattr(first.identity, "target_id") else 0, again.target_id)
        self.assertEqual(first.target_id, again.target_id)

    def test_recycled_handle_or_process_gets_a_different_identity(self):
        desk, _, v = setup()
        original = v.verify(100).target_id
        desk.windows[100] = info(pid=11, start=9000)             # same handle number, new process
        self.assertNotEqual(v.verify(100).target_id, original)
        desk.windows[100] = info(start=5001)                     # same pid number, new process instance
        self.assertNotEqual(v.verify(100).target_id, original)

    def test_two_windows_have_different_ids(self):
        _, _, v = setup(info(100), info(200, pid=20, start=6000))
        self.assertNotEqual(v.verify(100).target_id, v.verify(200).target_id)

    def test_provider_identity_holds_no_ldplayer_or_adb_information(self):
        _, _, v = setup()
        t = v.verify(100)
        text = (repr(t.identity) + PROVIDER_ID + repr(CAPABILITIES)).lower()
        for word in ("ldplayer", "adb", "emulator", "serial", "instance"):
            self.assertNotIn(word, text)

    def test_target_cannot_be_forged(self):
        args = ("win-x", 1, 1, 1, "a.exe", "C", "t", 0.0)
        with self.assertRaises(TargetNotVerified):
            WindowTarget(*args)
        with self.assertRaises(TargetNotVerified):
            WindowTarget(*args, _mint=object())
        self.assertIsInstance(WindowTarget(*args, _mint=_MINT), Target)   # only the private mint works

    def test_generic_vision_pipeline_runs_on_a_window_target(self):
        desk, stop, v = setup()
        shot = WindowScreenshotProvider(v.verify(100), v, stop).capture()
        frame = Frame.from_screenshot(shot)
        self.assertEqual((frame.width, frame.height), (64, 48))
        self.assertEqual((frame.source.provider_id, frame.source.target_id), (PROVIDER_ID, shot.source.target_id))
        hits = find_color(frame, (200, 60, 30), tolerance=0, roi=Roi(0, 0, 64, 48))   # RGB of B,G,R (30,60,200)
        self.assertEqual(hits.count, 64 * 48)


class VerificationRefusals(unittest.TestCase):
    def check_refused(self, desk_info, code, hwnd=100, **kw):
        _, _, v = setup(desk_info) if desk_info else setup()
        with self.assertRaises(TargetNotVerified) as ctx:
            v.verify(hwnd, **kw)
        self.assertEqual(ctx.exception.reason_code, code)

    def test_invalid_identifiers(self):
        _, _, v = setup()
        for bad in (0, -1, 2 ** 40, "100", None, 1.5, True, (1,)):
            with self.assertRaises(TargetNotVerified) as ctx:
                v.verify(bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.reason_code, "invalid_handle", bad)

    def test_nonexistent_window(self):
        self.check_refused(None, "window_gone", hwnd=999)

    def test_child_or_invisible_windows(self):
        self.check_refused(info(top_level=False), "not_top_level")
        self.check_refused(info(visible=False), "not_visible")

    def test_unreadable_process_identity_is_refused_not_assumed(self):
        self.check_refused(info(start=0), "process_unknown")
        self.check_refused(info(name=""), "process_unknown")
        self.check_refused(info(pid=0), "process_unknown")

    def test_expected_process_name(self):
        _, _, v = setup()
        self.assertIsNotNone(v.verify(100, expected_process_name="EDITOR.exe"))
        with self.assertRaises(TargetNotVerified):
            v.verify(100, expected_process_name="other.exe")

    def test_emergency_stop_blocks_verification(self):
        _, stop, v = setup()
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            v.verify(100)


class RecheckDetectsChange(unittest.TestCase):
    def test_window_destroyed(self):
        desk, _, v = setup()
        t = v.verify(100)
        del desk.windows[100]
        with self.assertRaises(TargetNotVerified):
            v.recheck(t)

    def test_process_changed_behind_the_same_handle(self):
        desk, _, v = setup()
        t = v.verify(100)
        for changed in (info(pid=11), info(start=5001), info(name="other.exe")):
            desk.windows[100] = changed
            with self.assertRaises(TargetNotVerified) as ctx:
                v.recheck(t)
            self.assertEqual(ctx.exception.reason_code, "process_changed")

    def test_class_changed(self):
        desk, _, v = setup()
        t = v.verify(100)
        desk.windows[100] = info(cls="Another")
        with self.assertRaises(TargetNotVerified) as ctx:
            v.recheck(t)
        self.assertEqual(ctx.exception.reason_code, "class_changed")

    def test_title_change_does_not_void_the_target(self):
        desk, _, v = setup()
        t = v.verify(100)
        desk.windows[100] = info(title="renamed")
        self.assertEqual(v.recheck(t).title, "renamed")

    def test_non_window_target_refused(self):
        _, _, v = setup()
        with self.assertRaises(TargetNotVerified):
            v.recheck("win-x")  # type: ignore[arg-type]


class ScreenshotProviderRules(unittest.TestCase):
    def provider(self, **window):
        desk, stop, v = setup(info(**window))
        return WindowScreenshotProvider(v.verify(100), v, stop), desk, stop, v

    def test_is_a_generic_screenshot_provider_with_correct_provenance(self):
        p, _, _, v = self.provider()
        self.assertIsInstance(p, ScreenshotProvider)
        shot = p.capture()
        self.assertIsInstance(shot, Screenshot)
        self.assertEqual((shot.width, shot.height), (64, 48))
        self.assertEqual(shot.source.provider_id, PROVIDER_ID)
        self.assertEqual(shot.source.target_id, p.target_id)
        self.assertNotIn("100", shot.source.detail)             # the handle never appears in provenance

    def test_invalid_target_types_are_rejected(self):
        _, stop, v = setup()
        for bogus in ("win-x", 100, None, ("windows", "win-x"), object(), info()):
            with self.assertRaises(TargetNotVerified):
                WindowScreenshotProvider(bogus, v, stop)  # type: ignore[arg-type]

    def test_a_target_without_screenshot_capability_is_rejected(self):
        _, stop, v = setup()

        class NoCaps(WindowTarget):
            capabilities = property(lambda self: frozenset())
        t = NoCaps("win-x", 100, 10, 5000, "editor.exe", "EditorWnd", "", 0.0, _mint=_MINT)
        with self.assertRaises(CapabilityNotAvailable):
            WindowScreenshotProvider(t, v, stop)

    def test_a_foreign_provider_target_is_rejected_even_if_it_declares_screenshot(self):
        from app.providers.base import TargetIdentity
        from app.providers.target import TargetVerification, VerificationState

        class Foreign(Target):
            identity = property(lambda s: TargetIdentity("other", "x"))
            capabilities = property(lambda s: frozenset({Capability.SCREENSHOT}))
            verification = property(lambda s: TargetVerification(VerificationState.VERIFIED, ("m",), 0.0))
        _, stop, v = setup()
        with self.assertRaises(TargetNotVerified):
            WindowScreenshotProvider(Foreign(), v, stop)

    def test_capture_reverifies_before_every_capture(self):
        p, desk, _, _ = self.provider()
        p.capture()
        desk.windows[100] = info(pid=99)                        # handle reused by another process
        with self.assertRaises(TargetNotVerified):
            p.capture()
        self.assertEqual(desk.captures, 1)                      # no pixels were copied from the wrong window

    def test_destroyed_window_is_refused_before_capture(self):
        p, desk, _, _ = self.provider()
        del desk.windows[100]
        with self.assertRaises(TargetNotVerified):
            p.capture()
        self.assertEqual(desk.captures, 0)

    def test_unreliable_windows_raise_explicit_errors(self):
        for kw in ({"minimized": True}, {"cloaked": True}, {"w": 0}, {"h": 0}, {"w": 9000}):
            p, desk, _, _ = self.provider(**kw)
            with self.assertRaises(WindowCaptureError, msg=str(kw)):
                p.capture()
            self.assertEqual(desk.captures, 0, kw)

    def test_black_capture_is_refused_as_unreliable(self):
        p, desk, _, _ = self.provider()
        desk.pixel = (0, 0, 0)
        with self.assertRaises(WindowCaptureError) as ctx:
            p.capture()
        self.assertIn("black", str(ctx.exception))

    def test_size_mismatch_and_change_during_capture_are_discarded(self):
        p, desk, _, _ = self.provider()
        desk.capture = lambda hwnd, w, h: RawBitmap(w + 1, h, b"\x01\x02\x03\x00" * ((w + 1) * h))
        with self.assertRaises(WindowCaptureError):
            p.capture()
        p, desk, _, _ = self.provider()
        desk.after_capture = lambda d: d.windows.update({100: replace(d.windows[100], width=80)})
        with self.assertRaises(WindowCaptureError):
            p.capture()
        p, desk, _, _ = self.provider()
        desk.after_capture = lambda d: d.windows.pop(100)
        with self.assertRaises(TargetNotVerified):
            p.capture()

    def test_emergency_stop_blocks_capture(self):
        p, desk, stop, _ = self.provider()
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            p.capture()
        self.assertEqual(desk.captures, 0)

    def test_two_targets_never_share_provenance(self):
        desk, stop, v = setup(info(100), info(200, pid=20, start=6000, name="viewer.exe"))
        a = WindowScreenshotProvider(v.verify(100), v, stop).capture()
        b = WindowScreenshotProvider(v.verify(200), v, stop).capture()
        self.assertFalse(a.source.same_target(b.source))

    def test_png_encoder(self):
        png = encode_rgb_png_from_bgra(bytes([1, 2, 3, 0, 4, 5, 6, 0]), 2, 1)
        from app.vision.bitmap import decode_png
        bm = decode_png(png)
        self.assertEqual((bm.width, bm.height, bytes(bm.rgb)), (2, 1, bytes([3, 2, 1, 6, 5, 4])))
        for w, h, data in ((0, 1, b""), (1, 1, b"\x00"), (20000, 1, b"")):
            with self.assertRaises(ValueError):
                encode_rgb_png_from_bgra(data, w, h)


def imports_of(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class Isolation(unittest.TestCase):
    def test_generic_layers_do_not_import_the_windows_provider(self):
        for pkg in ("core", "logging", "recovery", "providers", "vision"):
            for path in (ROOT / pkg).glob("*.py"):
                self.assertFalse({m for m in imports_of(path) if m.startswith("app.windows")}, path.name)

    def test_windows_provider_imports_only_core_providers_and_itself(self):
        for path in (ROOT / "windows").glob("*.py"):
            for m in imports_of(path):
                if m.startswith("app."):
                    self.assertIn(m.split(".")[1], ("core", "providers", "playback", "windows"), f"{path.name}: {m}")
                self.assertFalse(m.startswith(("app.ldplayer", "app.adb", "app.instances", "app.macros")), m)

    def test_only_win32_module_touches_windows_apis(self):
        for path in (ROOT / "windows").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "win32.py":
                self.assertNotIn("ctypes", text, path.name)
                self.assertNotIn("windll", text, path.name)

    def test_windows_provider_has_no_global_input_or_window_management_apis(self):
        # "sendinput" and "setforegroundwindow" are deliberately not banned here any more -- see
        # test_sendinput_is_confined_to_known_call_sites and test_setforegroundwindow_is_confined_to_one_call_site
        # below for what replaces a blanket ban on them specifically (mouse and keyboard delivery are real,
        # hardware-level input everywhere now, on two successive explicit user decisions, but each API must
        # still not spread beyond its own known, reviewed call sites).
        text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "windows").glob("*.py")).lower()
        for word in ("mouse_event", "setcursorpos", "keybd_event", "getcursorpos",
                     "getasynckeystate", "setkeyboardstate", "attachthreadinput",
                     "setfocus", "setactivewindow", "blockinput", "closewindow", "destroywindow", "createprocess",
                     "shellexecute", "sendmessage", "wm_mousewheel", "wm_mbutton", "wm_xbutton", "wm_hotkey"):
            self.assertNotIn(word, text)

    def test_sendinput_is_confined_to_known_call_sites(self):
        # SendInput is real, hardware-level input -- it is now this project's universal mouse- and keyboard-
        # delivery mechanism (two successive, explicit, informed user decisions after narrower versions proved
        # insufficient), but every OTHER global/focus-changing API name stays fully banned (test above), and
        # SendInput itself must stay confined to exactly its three known sending functions -- so it cannot
        # silently grow a fourth, unreviewed use without this test having to be touched.
        for path in (ROOT / "windows").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "win32.py":
                self.assertNotIn("SendInput", text, path.name)
        text = (ROOT / "windows" / "win32.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("u.SendInput"), 1)          # exactly one API binding
        self.assertEqual(text.count("self._SendInput("), 3)     # exactly three call sites
        known_senders = ("_send_real_click", "_send_real_key", "_send_real_char")
        for name in known_senders:
            self.assertIn(f"def {name}(", text)
        # every call site lives inside one of the three known senders, not scattered somewhere else in the file
        defs = sorted(text.index(f"def {name}(") for name in known_senders)
        bounds = defs + [text.index("\n    def ", defs[-1] + 1)]
        for pos in (m.start() for m in re.finditer(r"self\._SendInput\(", text)):
            self.assertTrue(any(lo < pos < hi for lo, hi in zip(bounds, bounds[1:])),
                            "every SendInput call must live inside one of the three known sending methods")

    def test_setforegroundwindow_is_confined_to_one_call_site(self):
        # SetForegroundWindow changes system-wide focus -- the most invasive API this module uses. It exists
        # only because real keyboard input needs the target to actually hold focus, unlike a real click, and
        # must stay confined to exactly one file, one binding and one call site, immediately followed by a
        # re-check that it actually worked (see _ensure_real_focus) -- never used for mouse, and never assumed
        # to succeed.
        for path in (ROOT / "windows").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "win32.py":
                self.assertNotIn("SetForegroundWindow", text, path.name)
        text = (ROOT / "windows" / "win32.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("u.SetForegroundWindow"), 1)          # exactly one API binding
        self.assertEqual(text.count("self._SetForegroundWindow("), 1)     # exactly one call site
        self.assertIn("def _ensure_real_focus(", text)
        call_at = text.index("self._SetForegroundWindow(")
        method_at = text.index("def _ensure_real_focus(")
        next_def = text.index("\n    def ", method_at + 1)
        self.assertTrue(method_at < call_at < next_def,
                        "the SetForegroundWindow call must live inside _ensure_real_focus")

    def test_observation_reads_exist_only_inside_win32(self):
        # The recorder needs to read which window is under the pointer / has focus and to install pass-through hooks.
        for path in (ROOT / "windows").glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            if path.name != "win32.py":
                for word in ("windowfrompoint", "getforegroundwindow", "setwindowshookex", "callnexthookex"):
                    self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_the_only_input_call_is_posting_messages_to_a_window_inside_win32(self):
        for path in (ROOT / "windows").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "win32.py":
                self.assertNotIn("PostMessage", text.replace("`PostMessage`", ""), path.name)

    def test_clean_interpreter_windows_provider_loads_no_other_provider(self):
        code = textwrap.dedent("""
            import sys
            import app.windows.provider, app.windows.target
            bad = [m for m in sys.modules if m.split(".")[:2] in (["app","adb"],["app","ldplayer"],["app","instances"],["app","macros"])]
            assert not bad, bad
            print("CLEAN")
        """)
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "CLEAN", r.stderr)

    def test_ldplayer_and_windows_targets_share_only_the_generic_contract(self):
        from app.ldplayer.ids import CAPABILITIES as LD_CAPS, PROVIDER_ID as LD_ID
        self.assertNotEqual(LD_ID, PROVIDER_ID)
        self.assertLess(LD_CAPS, CAPABILITIES)    # each provider declares its own set independently
        self.assertEqual(LD_CAPS, frozenset({Capability.SCREENSHOT, Capability.TAP, Capability.KEY, Capability.TEXT}))


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealWindowDiscovery(unittest.TestCase):
    """Read-only smoke of the real Win32 backend. Tolerates a desktop with no windows; captures nothing."""

    def test_enumerate_and_query_real_windows(self):
        from app.windows.win32 import Win32Backend
        backend = Win32Backend()
        windows = backend.enumerate()
        for w in windows[:20]:
            self.assertIsInstance(w, WindowInfo)
            self.assertTrue(w.top_level and w.visible and not w.cloaked)
            self.assertGreater(w.hwnd, 0)
            self.assertGreater(w.width, 0)
            again = backend.query(w.hwnd)
            self.assertIsNotNone(again)
            self.assertEqual((again.pid, again.class_name), (w.pid, w.class_name))

    def test_real_backend_rejects_invalid_identifiers(self):
        from app.windows.win32 import Win32Backend
        backend = Win32Backend()
        for bad in (0, -5, 2 ** 40, "12", None, True):
            self.assertIsNone(backend.query(bad))
        self.assertIsNone(backend.query(0x7FFFFFF0))              # a handle value that is not a window
        with self.assertRaises(WindowCaptureError):
            backend.capture(1, 0, 10)


if __name__ == "__main__":
    unittest.main()
