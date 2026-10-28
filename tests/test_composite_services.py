"""CompositeAutomationServices (app.bootstrap.composite_services): the same real WindowsServices and
LDPlayerServices already exercised by tests/test_gui.py and tests/test_ldplayer_services.py, combined into ONE
AutomationServices -- proving a single application run can discover, group, verify and act on targets from more
than one provider at once, with neither AppController nor the GUI changing at all (see app/gui/app.py's
_render_targets, which groups by TargetEntry.group exactly as it already did for one provider)."""
import unittest
from pathlib import Path

from app.bootstrap.composite_services import CompositeAutomationServices
from app.bootstrap.windows_services import WindowsServices
from app.core.cancel import EmergencyStop
from app.core.errors import SafetyViolation, TargetNotVerified
from app.gui.controller import AppController, TargetStatus
from app.gui.library import MacroLibrary
from app.gui.services import AutomationServices
from app.windows.target import FullWindowsTarget, is_desktop_id
from tests.test_gui import OWN_PID, World as WinWorld
from tests.test_ldplayer_services import rig as ld_rig
from tests.test_mapping import Inst, World as LDWorld
from tests.test_recorder import FakeObserver
from tests.test_windows_provider import info


def windows_services(stop: EmergencyStop) -> WindowsServices:
    world = WinWorld(info(hwnd=100, title="Notes", name="notes.exe", cls="NotesWnd", pid=10),
                     info(hwnd=102, title="Automation Platform", name="python.exe", cls="TkTopLevel", pid=OWN_PID))
    return WindowsServices(world, FakeObserver, stop, own_pid=OWN_PID)     # a fresh observer per watch, as for real


def two_provider_composite(*, ld_world: LDWorld | None = None) -> tuple[CompositeAutomationServices, EmergencyStop]:
    stop = EmergencyStop()
    ld_svc, *_ = ld_rig(ld_world, stop=stop)
    win_svc = windows_services(stop)
    return CompositeAutomationServices([win_svc, ld_svc]), stop


class Discovery(unittest.TestCase):
    def test_one_discover_call_returns_targets_from_both_providers_at_once(self):
        composite, _ = two_provider_composite()
        entries = composite.discover()
        self.assertEqual({e.provider_id for e in entries}, {"windows", "ldplayer-adb"})
        self.assertEqual({e.title for e in entries}, {"Full Windows", "Notes", "LDPlayer-1", "LDPlayer-2"})

    def test_ldplayer_instances_stay_one_group_and_windows_targets_split_into_their_own_two(self):
        composite, _ = two_provider_composite()
        by_title = {e.title: e for e in composite.discover()}
        self.assertEqual(by_title["LDPlayer-1"].group, "LDPlayer")
        self.assertEqual(by_title["LDPlayer-2"].group, "LDPlayer")
        self.assertEqual(by_title["Full Windows"].group, "Full Windows")
        self.assertEqual(by_title["Notes"].group, "Open Windows")
        self.assertEqual({e.group for e in composite.discover()}, {"LDPlayer", "Full Windows", "Open Windows"})

    def test_full_windows_is_present_alongside_a_real_open_window(self):
        composite, _ = two_provider_composite()
        titles = {e.title for e in composite.discover()}
        self.assertIn("Full Windows", titles)
        self.assertIn("Notes", titles)                     # Open Windows is present because a real window exists

    def test_offline_ldplayer_instances_are_not_dropped_when_combined_with_windows(self):
        ld_world = LDWorld(instances=[Inst(1, "LDPlayer-1", running=False, pid=-1, vbox=-1, android_id="a1")],
                           devices=[])
        composite, _ = two_provider_composite(ld_world=ld_world)
        entries = composite.discover()
        offline = next(e for e in entries if e.title == "LDPlayer-1")
        self.assertIn("offline", offline.size)
        self.assertEqual(offline.group, "LDPlayer")          # still grouped, still listed, just honestly offline

    def test_the_emergency_stop_in_one_providers_listing_is_the_stop_not_an_empty_list(self):
        """The review of 2026-10-27: an empty LDPlayer list would drop every chosen LDPlayer target; another
        provider's ordinary failure still just lists nothing."""
        from app.core.errors import EmergencyStopped
        composite, stop = two_provider_composite()
        stop.trigger("test")                         # the LDPlayer side checks the stop before asking adb
        with self.assertRaises(EmergencyStopped):
            composite.discover()
        composite, _ = two_provider_composite()
        broken = composite._services[1]
        broken.discover = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual({e.provider_id for e in composite.discover()}, {"windows"})

    def test_a_stop_during_a_listing_leaves_no_old_row_resolvable_through_a_new_table(self):
        """The second review of 2026-10-27: Windows listed again, then LDPlayer met the stop -- the window keeps its
        old rows, which must never resolve through the new tables (a reused window handle would be another window).
        With the stop set before, nothing is listed again at all."""
        from app.core.errors import EmergencyStopped
        composite, stop = two_provider_composite()
        old = composite.discover()
        windows, ldplayer = composite._services
        asked = []
        listed = windows.discover
        windows.discover = lambda: asked.append(1) or listed()
        stop.trigger("test")
        with self.assertRaises(EmergencyStopped):
            composite.discover()
        self.assertEqual(asked, [])                               # set before: no provider's table changed
        stop.reset()
        self.assertTrue(composite.verify(next(e for e in old if e.title == "Notes")))   # its own table, unchanged

        def stops_midway():
            stop.trigger("test")
            raise EmergencyStopped("stopped while listing")
        ldplayer.discover = stops_midway
        with self.assertRaises(EmergencyStopped):
            composite.discover()
        self.assertEqual(len(asked), 1)                           # Windows had listed again before the stop
        stop.reset()
        for entry in old:
            with self.subTest(entry=entry.title), self.assertRaises(TargetNotVerified):
                composite.verify(entry)

    def test_refs_from_different_providers_never_collide_even_with_the_same_raw_ref(self):
        """A namespaced ref must always resolve back to the entry that issued it, never to a same-named one from
        a different provider (see CompositeAutomationServices' own docstring for why this matters)."""
        composite, _ = two_provider_composite()
        entries = composite.discover()
        ld1 = next(e for e in entries if e.title == "LDPlayer-1")
        target = composite.verify(ld1)
        self.assertEqual(target.provider_id, "ldplayer-adb")


class Verification(unittest.TestCase):
    def test_verify_and_recheck_route_to_the_provider_that_actually_issued_the_entry(self):
        composite, _ = two_provider_composite()
        entries = composite.discover()
        notes = next(e for e in entries if e.title == "Notes")
        ld1 = next(e for e in entries if e.title == "LDPlayer-1")

        win_target = composite.verify(notes)
        self.assertEqual(win_target.provider_id, "windows")
        ld_target = composite.verify(ld1)
        self.assertEqual(ld_target.provider_id, "ldplayer-adb")

        fresh_notes = composite.recheck(win_target)
        self.assertEqual((fresh_notes.provider_id, fresh_notes.title), ("windows", "Notes"))
        fresh_ld1 = composite.recheck(ld_target)
        self.assertEqual((fresh_ld1.provider_id, fresh_ld1.title), ("ldplayer-adb", "LDPlayer-1"))

    def test_full_windows_verifies_through_the_composite_exactly_as_alone(self):
        composite, _ = two_provider_composite()
        full = next(e for e in composite.discover() if e.title == "Full Windows")
        target = composite.verify(full)
        self.assertIsInstance(target, FullWindowsTarget)
        self.assertTrue(is_desktop_id(target.target_id))           # the whole desktop, in the layout it was chosen in

    def test_an_entry_not_in_the_current_list_is_refused_not_guessed(self):
        composite, _ = two_provider_composite()
        composite.discover()
        from app.gui.services import TargetEntry
        ghost = TargetEntry("nope", "Ghost", "", "", "", "windows", "Open Windows")
        with self.assertRaises(TargetNotVerified):
            composite.verify(ghost)

    def test_a_target_from_an_unconfigured_provider_is_refused_not_a_crash(self):
        """Defence in depth: dispatch by provider_id must never silently do nothing or pick the wrong service."""
        composite, _ = two_provider_composite()
        composite.discover()
        with self.assertRaises(SafetyViolation):
            composite.capture(_FakeTarget())


class _FakeTarget:
    provider_id = "nonexistent-provider"


class Safety(unittest.TestCase):
    def test_construction_refuses_two_providers_that_do_not_share_one_emergency_stop(self):
        win_svc = windows_services(EmergencyStop())
        ld_svc, *_ = ld_rig()                              # its own, separate EmergencyStop
        with self.assertRaises(ValueError):
            CompositeAutomationServices([win_svc, ld_svc])

    def test_emergency_stop_through_the_composite_halts_every_provider_at_once(self):
        composite, stop = two_provider_composite()
        self.assertFalse(composite.stop.is_set())
        composite.stop.trigger("test")
        self.assertTrue(stop.is_set())                      # the SAME object every sub-service was built with

    def test_two_providers_reporting_the_same_provider_id_are_refused(self):
        stop = EmergencyStop()
        with self.assertRaises(ValueError):
            CompositeAutomationServices([windows_services(stop), windows_services(stop)])

    def test_construction_refuses_an_empty_provider_list(self):
        with self.assertRaises(ValueError):
            CompositeAutomationServices([])


class ThroughTheController(unittest.TestCase):
    """AppController and the GUI's grouping logic are unmodified: a CompositeAutomationServices is just another
    AutomationServices, so the SAME controller code that already worked for one provider works for several."""

    def test_the_controller_sees_one_combined_grouped_list_and_selects_across_providers(self):
        composite, _ = two_provider_composite()
        self.assertIsInstance(composite, AutomationServices)
        c = AppController(composite, MacroLibrary(Path("unused")))
        self.assertTrue(c.refresh())
        self.assertEqual({e.group for e in c.entries}, {"LDPlayer", "Full Windows", "Open Windows"})

        ld1 = next(e for e in c.entries if e.title == "LDPlayer-1")
        self.assertTrue(c.select(ld1.ref))
        self.assertEqual((c.target_status, c.target.provider_id), (TargetStatus.VERIFIED, "ldplayer-adb"))

        notes = next(e for e in c.entries if e.title == "Notes")
        self.assertTrue(c.select(notes.ref))
        self.assertEqual((c.target_status, c.target.provider_id), (TargetStatus.VERIFIED, "windows"))

    def test_group_header_semantics_are_unaffected_by_which_provider_a_target_belongs_to(self):
        """Not a GUI/Tk test (see tests/test_gui.py's View tests for that) -- this proves the controller-facing
        contract a multi-provider group header relies on: an unknown/synthetic ref never selects anything, exactly
        like it already didn't for a single provider."""
        composite, _ = two_provider_composite()
        c = AppController(composite, MacroLibrary(Path("unused")))
        c.refresh()
        self.assertFalse(c.select("\0group\0LDPlayer"))
        self.assertEqual(c.target_status, TargetStatus.NONE)


if __name__ == "__main__":
    unittest.main()
