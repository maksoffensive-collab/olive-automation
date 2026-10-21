"""C-03 (Stage 8): the Windows playback action limit.

Contract under test: the session/adapter limit is ONE named constant (`MAX_SESSION_ACTIONS`, evidence-based, see
docs/ARCHITECTURE.md); the budget stays exactly the macro's action count; a macro above the limit is refused by the
neutral preflight (typed `PlaybackRefused`, code `too_many_actions`) before any input, in dry run as well as live;
nothing is chunked; no bare ValueError reaches the GUI. Fakes only, no real input."""
import unittest

from app.core.cancel import EmergencyStop
from app.playback.engine import PlaybackRefused, PlaybackState as S
from app.playback.format import macro_from_json, macro_to_json
from app.playback.macro import Macro
from app.playback.preflight import preflight
from app.providers.recording import RecordedEvent as E, RecordedEventKind as K, Recording
from app.windows import input as window_input
from app.windows import playback as window_playback
from app.windows.input import WindowInputSession
from app.windows.playback import create_window_playback
from app.windows.target import WindowVerifier
from tests.test_gui import Base
from tests.test_playback import Clock, FakeInput, FakeTarget, macro as make_macro, move
from tests.test_windows_input import FakeInput as FakeWindowBackend
from tests.test_windows_provider import FakeDesktop, info, setup


def limit():
    return window_input.MAX_SESSION_ACTIONS


def moves(n):
    """n pointer moves (one action each) inside a 60x40 client area."""
    return tuple(E(K.POINTER_MOVE, 0 if i == 0 else 1, i % 50, i % 30) for i in range(n))


def big(n, **kw):
    """An engine for n moves bound to a real WindowInputSession on the fake window backend."""
    desk, stop, verifier = setup(info())
    target = verifier.verify(100)
    m = Macro(Recording(target.provider_id, target.target_id, moves(n)))
    backend = FakeWindowBackend(desk, (60, 40))
    clock = Clock()
    eng = create_window_playback(m, target, verifier, backend, stop, clock=clock, sleep=clock.sleep, **kw)
    return eng, backend


class OversizedMacroIsATypedRefusal(unittest.TestCase):
    def test_over_the_limit_is_refused_by_preflight_not_by_a_bare_value_error(self):
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                eng, backend = big(limit() + 1, dry_run=dry)          # constructing must not raise
                report = eng.preflight()
                self.assertFalse(report.ok)
                self.assertIn("too_many_actions", [i.code for i in report.errors])
                with self.assertRaises(PlaybackRefused) as cm:
                    eng.run()
                self.assertEqual([i.code for i in cm.exception.issues if i.code == "too_many_actions"],
                                 ["too_many_actions"])
                self.assertEqual(backend.events, [], "nothing may be sent before the refusal")
                self.assertEqual(eng._input.actions_sent, 0)

    def test_the_refusal_names_the_count_and_the_limit(self):
        eng, _ = big(limit() + 1)
        issue = next(i for i in eng.preflight().errors if i.code == "too_many_actions")
        self.assertIn(str(limit() + 1), issue.message)
        self.assertIn(str(limit()), issue.message)

    def test_boundary_just_below_exactly_at_and_just_above(self):
        # (actions, runs are exercised in these modes; live runs of a full-size macro only at the limit itself)
        for n, ok, modes in ((limit() - 1, True, (True,)), (limit(), True, (True, False)), (limit() + 1, False, (True,))):
            for dry in modes:
                with self.subTest(actions=n, dry_run=dry):
                    eng, backend = big(n, dry_run=dry)
                    codes = [i.code for i in eng.preflight().errors]
                    self.assertEqual("too_many_actions" in codes, not ok)
                    if not ok:
                        continue
                    report = eng.run()
                    self.assertEqual(report.state, S.COMPLETED)
                    self.assertEqual(report.completed_count, n)
                    self.assertEqual(len(backend.events), 0 if dry else n)
                    self.assertEqual(eng._input.actions_sent, 0 if dry else n)

    def test_budget_is_exactly_the_action_count_and_dry_run_uses_none(self):
        live, backend = big(1500)
        self.assertEqual(live.run().state, S.COMPLETED)
        self.assertEqual((live._input.actions_sent, len(backend.events)), (1500, 1500))
        dry, backend = big(1500, dry_run=True)
        self.assertEqual(dry.run().state, S.COMPLETED)
        self.assertEqual((dry._input.actions_sent, len(backend.events)), (0, 0))
        self.assertEqual(len(dry._input.intended), 1500)


class MoreThanTheOldLimitRuns(unittest.TestCase):
    def test_1001_and_5000_actions_run_live(self):
        for n in (1001, 5000):
            with self.subTest(actions=n):
                eng, backend = big(n)
                report = eng.run()
                self.assertEqual((report.state, report.completed_count), (S.COMPLETED, n))
                self.assertEqual(len(backend.events), n)

    def test_no_chunking_one_session_serves_the_whole_run(self):
        eng, backend = big(2500)
        session = eng._input
        eng.run()
        self.assertIs(eng._input, session)
        self.assertEqual(session.actions_sent, 2500)


class OneSharedLimit(unittest.TestCase):
    def test_everything_the_recorder_can_produce_is_playable(self):
        from app.providers.recording import MAX_EVENTS
        self.assertGreaterEqual(window_input.MAX_SESSION_ACTIONS, MAX_EVENTS)

    def test_adapter_and_session_use_the_same_constant(self):
        self.assertEqual(window_playback.MAX_ACTIONS, window_input.MAX_SESSION_ACTIONS)

    def test_session_accepts_the_limit_and_refuses_one_more(self):
        desk, stop = FakeDesktop(info()), EmergencyStop()
        v = WindowVerifier(desk, stop)
        s = WindowInputSession(v.verify(100), v, FakeWindowBackend(desk), stop, max_actions=limit())
        self.assertEqual(s.action_limit, limit())
        for bad in (0, -1, limit() + 1, True, 1.0, None, "1"):
            with self.assertRaises(ValueError):
                WindowInputSession(v.verify(100), v, FakeWindowBackend(desk), stop, max_actions=bad)  # type: ignore[arg-type]


class NeutralPreflightLimit(unittest.TestCase):
    """The neutral preflight reads a generic `action_limit` of the bound input; it knows no number and no provider."""

    def run_preflight(self, n, action_limit="absent"):
        m = make_macro(*[move(0 if i == 0 else 1, i % 5, 1) for i in range(n)])
        session = FakeInput()
        if action_limit != "absent":
            session.action_limit = action_limit
        return preflight(m, FakeTarget(), session)

    def test_at_the_limit_is_fine_and_above_is_refused(self):
        self.assertTrue(self.run_preflight(3, 3).ok)
        report = self.run_preflight(4, 3)
        self.assertEqual([i.code for i in report.errors], ["too_many_actions"])

    def test_no_limit_attribute_means_no_limit(self):
        self.assertTrue(self.run_preflight(50).ok)


class LargeMacrosAreStillStoredAndLoaded(unittest.TestCase):
    def test_a_macro_above_the_playback_limit_survives_save_format_and_load(self):
        events = moves(limit() + 1)
        m = Macro(Recording("windows", "win-x", events), "big")
        back = macro_from_json(macro_to_json(m))
        self.assertEqual(len(back.actions), limit() + 1)
        self.assertEqual(back.recording.events, events)


class GuiShowsAClearRefusal(Base):
    def test_over_limit_macro_gives_a_clear_message_not_an_unexpected_error(self):
        self.record()
        target = self.c.macro.recording.target
        self.c.macro = Macro(Recording(target.provider_id, target.target_id, moves(limit() + 1)), "big")
        for dry in (False, True):
            with self.subTest(dry_run=dry):
                self.assertFalse(self.c.play(dry_run=dry))
                text = self.c.status.text
                self.assertEqual(self.c.status.level, "error")
                self.assertNotIn("Unexpected", text)
                self.assertNotIn("ValueError", text)
                self.assertIn(str(limit()), text)
                self.assertIsNone(self.c.engine)
                self.assertFalse(self.c.playing)


if __name__ == "__main__":
    unittest.main()
