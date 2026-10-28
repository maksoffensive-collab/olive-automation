"""Several targets of ONE run driven at the same time, through the real chain, over fakes.

The scheduler lives in `AppController`; everything under it is unchanged. So these tests drive the real
`AppController` over the real `LDPlayerServices` -> `MappingService` -> `create_ldplayer_playback` ->
`InputSession` -> `AdbClient` chain, with only the outermost object faked: a recording `AdbClient` over the same
simulated world `tests/test_mapping.py` already uses. Nothing here reaches a real instance, a real adb.exe or a
real window.

What that buys is the one property worth proving more than any other: **isolation**. Each command is observed
where it is actually addressed -- at the client, with its serial -- so "target A never received target B's
command" is measured rather than assumed. The same fake counts how many commands are in flight at once, which is
where the concurrency limit is checked too: at the wire, not in the view's idea of itself.

The scheduling rule under test is expressed only in what a provider declared about its own candidates
(`TargetEntry.concurrent`); no test here, and no code it exercises, asks which provider a target came from.
"""
import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.core.cancel import EmergencyStop
from app.gui.controller import CONCURRENCY_CHOICES, DEFAULT_CONCURRENCY, AppController, RunState
from app.gui.history import COMPLETED, FAILED, PARTIAL, REFUSED, STOPPED, RunHistory, RunRecord
from app.gui.library import MacroLibrary
from app.playback.macro import Macro
from app.providers.recording import Recording
from tests.test_adb_input import RecordingAdb
from tests.test_ldplayer_services import FakeConsole, port_owners_for, read_config_for
from tests.test_mapping import Dev, Inst, World
from tests.test_playback import down, up

TAPS = 6                     # actions per macro: enough to interleave, small enough to stay quick
TAP_MS = 25                  # how long one faked ADB command takes, so runs really do overlap


def ld_world(count: int) -> World:
    """`count` registered, running instances, each with its own identity and its own ADB device."""
    instances = [Inst(i, f"LDPlayer-{i}", pid=1000 + i, vbox=2000 + i, android_id=f"id{i:04d}")
                 for i in range(1, count + 1)]
    devices = [Dev(f"emulator-{5552 + 2 * i}", f"id{i:04d}", 2000 + i) for i in range(1, count + 1)]
    return World(instances=instances, devices=devices)


class WiretapAdb(RecordingAdb):
    """Every input command, with the serial it was addressed to, recorded as `(serial, operation, arguments)`.

    It also measures how many commands are in flight at the same moment -- the honest place to check a
    concurrency limit, since it is the only point where two targets' work could actually collide."""

    def __init__(self, world, *, delay_s: float = TAP_MS / 1000) -> None:
        super().__init__(world)
        self.log: list[tuple[str, str, tuple]] = []
        self.delay_s = delay_s
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0
        self.serials_seen_together: set[frozenset] = set()
        self._active: set[str] = set()

    def _command(self, serial: str, operation: str, arguments: tuple) -> None:
        with self._lock:
            self.log.append((serial, operation, arguments))
            self._in_flight += 1
            self._active.add(serial)
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            if len(self._active) > 1:
                self.serials_seen_together.add(frozenset(self._active))
        time.sleep(self.delay_s)
        with self._lock:
            self._in_flight -= 1
            self._active.discard(serial)

    def input_tap(self, serial, x, y, *, grant):
        super().input_tap(serial, x, y, grant=grant)
        self._command(serial, "tap", (x, y))

    def input_keyevent(self, serial, keycode, *, grant):
        super().input_keyevent(serial, keycode, grant=grant)
        self._command(serial, "keyevent", (keycode,))

    def input_text(self, serial, text, *, grant):
        super().input_text(serial, text, grant=grant)
        self._command(serial, "text", (text,))

    def commands_for(self, serial: str) -> list[tuple[str, tuple]]:
        with self._lock:
            return [(op, args) for s, op, args in self.log if s == serial]


class Base(unittest.TestCase):
    """A controller over `count` LDPlayer instances and one macro recorded for the first of them."""

    instances = 2
    delay_s = TAP_MS / 1000

    def setUp(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.world = ld_world(self.instances)
        self.adb = WiretapAdb(self.world, delay_s=self.delay_s)
        self.stop = EmergencyStop()
        self.services = LDPlayerServices(FakeConsole(self.world), self.adb, read_config_for(self.world),
                                         port_owners_for(self.world), self.stop)
        self.library = MacroLibrary(root / "macros")
        self.history = RunHistory(root / "history.json")
        self.c = AppController(self.services, self.library, history=self.history)
        self.addCleanup(self.c.shutdown)
        self.c.refresh()
        self.key = self._save_macro()
        self.c.select_macro(self.key)

    # ---- fixtures -------------------------------------------------------------------------------
    def _save_macro(self, name: str = "Batch", taps: int = TAPS) -> str:
        """A real macro recorded against the first instance: `taps` taps, each at its own coordinates so a
        command that reached the wrong target would be visible as a wrong coordinate too."""
        target = self.services.verify(self.entries()[0])
        events = []
        for i in range(taps):
            events.extend((down(0 if i == 0 else 20, 100 + i, 200 + i), up(10, 100 + i, 200 + i)))
        self.library.save(Macro(Recording(target.provider_id, target.target_id, tuple(events)), name))
        self.c.refresh_macros()
        return next(i.key for i in self.c.macros if i.name == name)

    def entries(self):
        return sorted(self.c.entries, key=lambda e: e.title)

    def serial_of(self, index: int) -> str:
        return f"emulator-{5552 + 2 * index}"

    def choose(self, *indices: int) -> None:
        """Choose instances by their 1-based index, in that order."""
        entries = {int(e.title.rsplit("-", 1)[1]): e for e in self.c.entries}
        self.c.set_run_refs([entries[i].ref for i in indices])

    def choose_all(self) -> None:
        self.choose(*range(1, self.instances + 1))

    def run_to_end(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while self.c.run_active and time.monotonic() < deadline:
            self.c.poll()
            time.sleep(0.005)
        self.c.poll()
        self.assertFalse(self.c.run_active, f"run did not finish: {self.c.run_results}")

    def states(self) -> dict[str, RunState]:
        return {r.title: r.state for r in self.c.run_results}


# ---------------------------------------------------------------------------------------------------
# The one that matters most: every command, with its serial, checked against the target it belongs to.
# ---------------------------------------------------------------------------------------------------
class SerialRouting(Base):
    instances = 4

    def test_each_target_receives_only_its_own_commands_and_exactly_the_macro(self):
        """Four instances at once, each addressed by its own verified serial. Automatically checked: every
        target got its own commands, no target got another's, each got exactly as many actions as the macro
        has, and the number in flight never passed the configured limit."""
        self.c.set_concurrency(4)
        self.choose_all()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()

        self.assertEqual(set(self.states().values()), {RunState.COMPLETED})
        expected = [("tap", (100 + i, 200 + i)) for i in range(TAPS)]
        wanted = {self.serial_of(i) for i in range(1, self.instances + 1)}
        self.assertEqual({serial for serial, _, _ in self.adb.log}, wanted)     # no serial that is not a target
        for index in range(1, self.instances + 1):
            with self.subTest(instance=index):
                self.assertEqual(self.adb.commands_for(self.serial_of(index)), expected)
        self.assertEqual(len(self.adb.log), TAPS * self.instances)              # nothing sent twice
        self.assertLessEqual(self.adb.peak_in_flight, 4)

    def test_the_four_targets_really_did_overlap_rather_than_taking_turns(self):
        self.c.set_concurrency(4)
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        self.assertGreater(self.adb.peak_in_flight, 1, "the targets were serialised, not run together")
        self.assertTrue(any(len(group) > 1 for group in self.adb.serials_seen_together))

    def test_every_command_carries_a_grant_minted_for_the_serial_it_was_sent_to(self):
        """The identity chain below the scheduler is untouched: a grant still belongs to one serial, and the
        client still refuses one that does not match. Running four at once must not blur that."""
        self.c.set_concurrency(4)
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(len(self.adb.taps), TAPS * self.instances)
        for serial, _x, _y, grant in self.adb.taps:
            self.assertEqual(grant.serial, serial)


# ---------------------------------------------------------------------------------------------------
class OneInstanceTwoWays(Base):
    """Two rows of the list that are ONE instance (reached two ways over ADB, 2026-10-27), both ticked: the macro
    runs on it once, never twice at once; the second row is refused and says why."""

    def test_two_rows_of_one_instance_run_it_once(self):
        self.world.devices.append(Dev("127.0.0.1:5555", "id0001", 2001))   # LDPlayer-1 again, by `adb connect`
        self.c.refresh()
        rows = [e.ref for e in self.c.entries if e.ref.startswith("ld1@")]
        self.assertEqual(len(rows), 2, [e.ref for e in self.c.entries])
        self.c.set_concurrency(4)
        self.c.set_run_refs(rows)
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual(sorted(r.state.value for r in self.c.run_results), ["completed", "refused"])
        refused = next(r for r in self.c.run_results if r.state is RunState.REFUSED)
        self.assertTrue(refused.detail.startswith("same_target"), refused.detail)
        self.assertEqual(len(self.adb.log), TAPS)                              # the macro once, not twice


class SaidInRussianAfterARun(Base):
    """Why a run stopped, in the results and the history, in the person's language: the provider's own words travel
    with the detail line (`message_ru`), so the screens never hold them (2026-10-27)."""
    instances = 1

    def test_a_device_gone_during_a_run_is_said_in_the_persons_language(self):
        from types import SimpleNamespace

        from app.gui.i18n import Translator
        from app.gui.view import fmt
        world, tap = self.world, self.adb.input_tap

        def tap_then_gone(serial, x, y, *, grant):
            tap(serial, x, y, grant=grant)
            world.devices.clear()                   # the instance's device leaves ADB after the first tap
        self.adb.input_tap = tap_then_gone
        self.choose_all()
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        result = self.c.run_results[0]
        self.assertEqual(result.state, RunState.FAILED, result)
        ru = " ".join(fmt.explain_lines(SimpleNamespace(t=Translator("ru")), result.detail))
        en = " ".join(fmt.explain_lines(SimpleNamespace(t=Translator("en")), result.detail))
        self.assertIn("больше нет в списке устройств ADB", ru)
        self.assertIn("is no longer listed by ADB", en)
        self.assertEqual(len(self.adb.log), 1)


class Scheduling(Base):
    instances = 6

    def test_a_single_target_behaves_exactly_as_it_did_before(self):
        """A. One chosen target: one engine, one history record, one target's commands, nothing overlapping."""
        self.choose(1)
        self.assertTrue(self.c.start_run(), self.c.status)
        self.run_to_end()
        self.assertEqual([r.state for r in self.c.run_results], [RunState.COMPLETED])
        self.assertEqual({serial for serial, _, _ in self.adb.log}, {self.serial_of(1)})
        self.assertEqual(self.adb.peak_in_flight, 1)
        record = self.history.list()[0]
        self.assertEqual((record.outcome, record.target_count), (COMPLETED, 1))

    def test_two_targets_run_at_the_same_time(self):
        """B."""
        self.choose(1, 2)
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(list(self.states().values()), [RunState.COMPLETED] * 2)
        self.assertEqual(self.adb.peak_in_flight, 2)

    def test_more_targets_than_slots_all_finish_and_each_gets_its_whole_macro(self):
        """C."""
        self.c.set_concurrency(2)
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(list(self.states().values()), [RunState.COMPLETED] * self.instances)
        for index in range(1, self.instances + 1):
            self.assertEqual(len(self.adb.commands_for(self.serial_of(index))), TAPS)

    def test_the_concurrency_limit_is_never_exceeded_and_freed_slots_are_refilled(self):
        """D. Six targets at a limit of two: never more than two in flight, and all six still ran."""
        self.c.set_concurrency(2)
        self.choose_all()
        self.c.start_run()
        while self.c.run_active:
            self.assertLessEqual(len([r for r in self.c.run_results if r.state is RunState.RUNNING]), 2)
            self.c.poll()
            time.sleep(0.002)
        self.c.poll()
        self.assertLessEqual(self.adb.peak_in_flight, 2)
        self.assertEqual(len(self.adb.log), TAPS * self.instances)

    def test_a_limit_of_one_makes_a_batch_strictly_sequential_again(self):
        self.c.set_concurrency(1)
        self.choose(1, 2, 3)
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(self.adb.peak_in_flight, 1)
        self.assertEqual(self.adb.serials_seen_together, set())

    def test_the_queue_keeps_the_order_the_person_chose(self):
        self.c.set_concurrency(1)
        self.choose(3, 1, 2)
        self.c.start_run()
        self.run_to_end()
        first_seen = []
        for serial, _, _ in self.adb.log:
            if serial not in first_seen:
                first_seen.append(serial)
        self.assertEqual(first_seen, [self.serial_of(3), self.serial_of(1), self.serial_of(2)])


# ---------------------------------------------------------------------------------------------------
class FailureIsolation(Base):
    instances = 4

    def test_one_target_that_cannot_be_verified_does_not_stop_the_others(self):
        """F + I: a disconnected instance is refused; the rest of the batch runs to the end."""
        self.world.devices = [d for d in self.world.devices if d.serial != self.serial_of(2)]
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        states = self.states()
        self.assertEqual(states["LDPlayer-2"], RunState.REFUSED)
        self.assertEqual([states[f"LDPlayer-{i}"] for i in (1, 3, 4)], [RunState.COMPLETED] * 3)
        self.assertEqual(self.adb.commands_for(self.serial_of(2)), [])
        self.assertEqual(len(self.adb.commands_for(self.serial_of(3))), TAPS)
        self.assertEqual(self.history.list()[0].outcome, PARTIAL)

    def test_several_failures_in_one_batch_are_each_recorded_and_the_rest_still_finish(self):
        """G."""
        self.world.devices = [d for d in self.world.devices
                              if d.serial not in (self.serial_of(1), self.serial_of(3))]
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        states = self.states()
        self.assertEqual([states[f"LDPlayer-{i}"] for i in (1, 3)], [RunState.REFUSED] * 2)
        self.assertEqual([states[f"LDPlayer-{i}"] for i in (2, 4)], [RunState.COMPLETED] * 2)
        record = self.history.list()[0]
        self.assertEqual(record.outcome, PARTIAL)
        self.assertEqual(sum(1 for r in record.results if r.outcome == REFUSED), 2)

    def test_a_target_whose_identity_changes_under_it_fails_alone(self):
        """H-shaped: the session's own re-verification refuses mid-run. One target's engine ends; the others
        are untouched, because nothing about the batch is shared below the scheduler."""
        self.choose(1, 2)
        self.c.start_run()
        for device in self.world.devices:                 # LDPlayer-1's device stops answering as itself
            if device.serial == self.serial_of(1):
                device.android_id = "changed"
        self.run_to_end()
        states = self.states()
        self.assertEqual(states["LDPlayer-2"], RunState.COMPLETED)
        self.assertIn(states["LDPlayer-1"], (RunState.FAILED, RunState.COMPLETED))
        self.assertEqual(len(self.adb.commands_for(self.serial_of(2))), TAPS)

    def test_a_macro_recorded_for_another_provider_refuses_every_target_and_sends_nothing(self):
        alien = Macro(Recording("windows", "win-1", self.library.load(
            self.c.macro_by_key(self.key).path).recording.events), "Batch")
        self.library.save(alien, overwrite=True)
        self.c.refresh_macros()
        self.c.select_macro(self.key)
        self.choose(1, 2)
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(list(self.states().values()), [RunState.REFUSED] * 2)
        self.assertEqual(self.adb.log, [])
        self.assertEqual(self.history.list()[0].outcome, REFUSED)


# ---------------------------------------------------------------------------------------------------
class Cancellation(Base):
    instances = 4
    delay_s = 0.05               # slow enough that a stop always lands mid-run

    def test_stopping_the_batch_ends_every_target_and_starts_no_new_one(self):
        """J + L: running targets are cancelled, waiting ones never start."""
        self.c.set_concurrency(2)
        self.choose_all()
        self.c.start_run()
        self.c.poll()
        self.assertTrue(self.c.cancel())
        self.run_to_end()
        self.assertNotIn(RunState.RUNNING, self.states().values())
        self.assertNotIn(RunState.WAITING, self.states().values())
        started = {serial for serial, _, _ in self.adb.log}
        self.assertLessEqual(len(started), 2, "a waiting target started after the stop")
        self.assertIn(self.history.list()[0].outcome, (STOPPED, PARTIAL))

    def test_the_emergency_stop_ends_the_whole_batch_while_commands_are_in_flight(self):
        """K. Nothing further is sent, every engine ends, and the run is recorded once."""
        self.c.set_concurrency(4)
        self.choose_all()
        self.c.start_run()
        self.c.poll()
        while not self.adb.log:                          # wait until real commands are actually going out
            time.sleep(0.005)
        self.c.emergency_stop()
        sent_at_stop = len(self.adb.log)
        self.run_to_end()
        self.assertTrue(self.c.emergency_active)
        self.assertNotIn(RunState.COMPLETED, self.states().values())
        self.assertLess(len(self.adb.log), TAPS * self.instances)
        self.assertLessEqual(len(self.adb.log) - sent_at_stop, self.instances,
                             "commands kept going out after the emergency stop")
        self.assertEqual(len(self.history.list()), 1)

    def test_the_emergency_stop_marks_waiting_targets_stopped_without_ever_starting_them(self):
        """L."""
        self.c.set_concurrency(1)
        self.choose_all()
        self.c.start_run()
        self.c.poll()
        self.c.emergency_stop()
        self.run_to_end()
        states = self.states()
        self.assertEqual([states[f"LDPlayer-{i}"] for i in (2, 3, 4)], [RunState.STOPPED] * 3)
        self.assertEqual({serial for serial, _, _ in self.adb.log} - {self.serial_of(1)}, set())

    def test_no_worker_thread_outlives_a_finished_batch(self):
        """M."""
        before = threading.active_count()
        self.c.set_concurrency(4)
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        deadline = time.monotonic() + 5
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertLessEqual(threading.active_count(), before)

    def test_a_target_that_will_not_stop_in_time_is_recorded_as_stopped_not_as_still_running(self):
        """A forced close gives each engine two seconds and then ends the run either way -- an in-flight
        provider command can outlast that. The target it belonged to must not be left, or recorded, as
        "running": the application has gone, and "running" is not one of the history's outcomes, so the panel
        would print the raw key `outcome_running` at the person."""
        from app.gui import history as store
        from app.gui.i18n import Translator
        from app.playback.engine import PlaybackState

        class Stuck:
            """What an engine looks like when its provider call is wedged: cancel is accepted, nothing ends."""

            def __init__(self, real):
                self.real = real

            def __getattr__(self, name):
                return getattr(self.real, name)

            def report(self):
                return dataclasses.replace(self.real.report(), state=PlaybackState.RUNNING)

            def wait(self, timeout=None):
                return False

        self.choose(1)
        self.c.start_run()
        self.c.poll()
        self.c._running = {ref: Stuck(engine) for ref, engine in self.c._running.items()}
        self.c.shutdown()

        self.assertFalse(self.c.run_active)
        self.assertEqual([r.state for r in self.c.run_results], [RunState.STOPPED])
        record = self.history.list()[0]
        self.assertEqual(record.outcome, STOPPED)
        self.assertEqual([r.outcome for r in record.results], [STOPPED])
        for result in record.results:
            self.assertIn(result.outcome, store.OUTCOMES)
            self.assertNotEqual(Translator("en")(f"outcome_{result.outcome}"), f"outcome_{result.outcome}")

    def test_closing_the_application_stops_every_running_target_and_joins_its_thread(self):
        """N."""
        before = threading.active_count()
        self.c.set_concurrency(4)
        self.choose_all()
        self.c.start_run()
        self.c.poll()
        self.c.shutdown()
        self.assertFalse(self.c.run_active)
        deadline = time.monotonic() + 5
        while threading.active_count() > before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertLessEqual(threading.active_count(), before)


# ---------------------------------------------------------------------------------------------------
class History(Base):
    instances = 3

    def test_one_batch_is_one_record_carrying_both_the_aggregate_and_every_target(self):
        """O."""
        self.world.devices = [d for d in self.world.devices if d.serial != self.serial_of(3)]
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        self.assertEqual(len(self.history.list()), 1)
        record = self.history.list()[0]
        self.assertEqual(record.outcome, PARTIAL)                       # the aggregate
        self.assertEqual(record.target_count, 3)
        self.assertEqual({r.target for r in record.results},
                         {"LDPlayer-1", "LDPlayer-2", "LDPlayer-3"})    # and each target on its own
        by_target = {r.target: r for r in record.results}
        self.assertEqual(by_target["LDPlayer-1"].outcome, COMPLETED)
        self.assertEqual(by_target["LDPlayer-3"].outcome, REFUSED)
        self.assertEqual(by_target["LDPlayer-1"].total, TAPS)
        self.assertEqual(by_target["LDPlayer-1"].completed, TAPS)

    def test_the_recorded_progress_counts_the_whole_batch_not_one_macro(self):
        """Completed actions are summed across targets, so the total they are measured against has to be the
        batch's: three targets running a six-action macro is "18", never "6" with 18 completed against it."""
        self.choose_all()
        self.c.start_run()
        self.run_to_end()
        record = self.history.list()[0]
        self.assertEqual(record.actions_total, TAPS * self.instances)
        self.assertEqual(record.completed, TAPS * self.instances)
        self.assertLessEqual(record.completed, record.actions_total)

    def test_a_single_target_still_records_exactly_the_macro_length(self):
        self.choose(1)
        self.c.start_run()
        self.run_to_end()
        record = self.history.list()[0]
        self.assertEqual((record.actions_total, record.completed), (TAPS, TAPS))

    def test_per_target_results_survive_a_write_and_a_re_read(self):
        self.choose(1, 2)
        self.c.start_run()
        self.run_to_end()
        reloaded = RunHistory(self.history.path).list()[0]
        self.assertEqual([(r.target, r.outcome, r.completed) for r in reloaded.results],
                         [(r.target, r.outcome, r.completed) for r in self.history.list()[0].results])

    def test_a_record_written_before_runs_could_overlap_still_loads(self):
        """P. Old files have no `results` key at all; they must read as a record with no per-target detail,
        never be dropped and never crash the list."""
        old = {"at": 1700000000.0, "macro": "Old", "macro_key": "old.macro.json", "outcome": COMPLETED,
               "targets": ["LDPlayer-1"], "duration_s": 1.5, "actions_total": 4, "completed": 4, "detail": ""}
        record = RunRecord.from_dict(old)
        self.assertIsNotNone(record)
        self.assertEqual((record.macro, record.outcome, record.target_count), ("Old", COMPLETED, 1))
        self.assertEqual(record.results, ())
        self.assertEqual(RunRecord.from_dict(record.as_dict()), record)   # and it round-trips unchanged

    def test_a_damaged_per_target_entry_is_dropped_without_losing_the_record(self):
        old = {"at": 1700000000.0, "macro": "Old", "macro_key": "k", "outcome": FAILED, "targets": ["A"],
               "results": [{"target": "A", "outcome": FAILED, "completed": 1, "total": 2}, "nonsense", None]}
        record = RunRecord.from_dict(old)
        self.assertIsNotNone(record)
        self.assertEqual([r.target for r in record.results], ["A"])


# ---------------------------------------------------------------------------------------------------
class MixedProviders(unittest.TestCase):
    """Q + R. One run covering both providers at once: the ones that declared themselves independent may
    overlap, the ones that did not are never overlapped with anything -- and no code consulted a provider id to
    work that out."""

    def setUp(self):
        from tests.test_composite_services import two_provider_composite

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.services, self.stop = two_provider_composite(ld_world=ld_world(3))
        self.library = MacroLibrary(Path(self.tmp.name) / "macros")
        self.history = RunHistory(Path(self.tmp.name) / "history.json")
        self.c = AppController(self.services, self.library, history=self.history)
        self.addCleanup(self.c.shutdown)
        self.c.refresh()

    def entry(self, title):
        return next(e for e in self.c.entries if e.title == title)

    def test_the_two_providers_disagree_about_overlapping_and_each_says_so_itself(self):
        self.assertTrue(all(self.entry(f"LDPlayer-{i}").concurrent for i in (1, 2, 3)))
        self.assertFalse(self.entry("Notes").concurrent)
        self.assertFalse(self.entry("Full Windows").concurrent)

    def test_a_target_that_did_not_declare_itself_independent_never_shares_the_run(self):
        """The rule the scheduler applies, read straight off the candidates."""
        self.c.set_concurrency(4)
        self.c.set_run_refs([self.entry(t).ref for t in ("LDPlayer-1", "Notes", "LDPlayer-2", "LDPlayer-3")])
        entries = {e.ref: e for e in self.c.entries}
        self.c._running = {}
        self.assertTrue(self.c._may_start(entries[self.entry("LDPlayer-1").ref]))
        self.assertTrue(self.c._may_start(entries[self.entry("Notes").ref]))
        self.c._running = {self.entry("LDPlayer-1").ref: object()}
        self.assertTrue(self.c._may_start(entries[self.entry("LDPlayer-2").ref]))
        self.assertFalse(self.c._may_start(entries[self.entry("Notes").ref]),
                         "a target that must run alone was allowed to join a running one")
        self.c._running = {self.entry("Notes").ref: object()}
        self.assertFalse(self.c._may_start(entries[self.entry("LDPlayer-1").ref]),
                         "a target joined one that had said it must run alone")
        self.c._running = {}

    def test_a_windows_macro_over_two_windows_targets_still_runs_one_at_a_time(self):
        """R. The Windows provider's behaviour is unchanged: two of its targets are never in flight together,
        whatever the concurrency setting says."""
        entry = self.entry("Notes")
        target = self.services.verify(entry)
        events = tuple(e for i in range(3) for e in (down(0 if i == 0 else 20, 5 + i, 6 + i),
                                                     up(10, 5 + i, 6 + i)))
        self.library.save(Macro(Recording(target.provider_id, target.target_id, events), "WinMacro"))
        self.c.refresh_macros()
        self.c.select_macro(next(i.key for i in self.c.macros if i.name == "WinMacro"))
        self.c.set_concurrency(16)
        self.c.set_run_refs([entry.ref, self.entry("Full Windows").ref])
        self.assertTrue(self.c.start_run(), self.c.status)
        deadline = time.monotonic() + 30
        while self.c.run_active and time.monotonic() < deadline:
            self.assertLessEqual(len([r for r in self.c.run_results if r.state is RunState.RUNNING]), 1,
                                 "two Windows targets were running at once")
            self.c.poll()
            time.sleep(0.002)
        self.c.poll()
        self.assertFalse(self.c.run_active)

    def test_a_macro_recorded_for_one_provider_is_refused_on_the_other_targets_of_the_same_run(self):
        """Mixing providers in one selection never retargets a macro: the foreign targets are refused, the
        matching ones still run."""
        entry = self.entry("LDPlayer-1")
        target = self.services.verify(entry)
        events = (down(0, 10, 10), up(10, 10, 10))
        self.library.save(Macro(Recording(target.provider_id, target.target_id, events), "LdMacro"))
        self.c.refresh_macros()
        self.c.select_macro(next(i.key for i in self.c.macros if i.name == "LdMacro"))
        self.c.set_run_refs([entry.ref, self.entry("Notes").ref, self.entry("LDPlayer-2").ref])
        self.c.start_run()
        deadline = time.monotonic() + 30
        while self.c.run_active and time.monotonic() < deadline:
            self.c.poll()
            time.sleep(0.005)
        self.c.poll()
        states = {r.title: r.state for r in self.c.run_results}
        self.assertEqual(states["Notes"], RunState.REFUSED)
        self.assertEqual(states["LDPlayer-1"], RunState.COMPLETED)
        self.assertEqual(states["LDPlayer-2"], RunState.COMPLETED)


# ---------------------------------------------------------------------------------------------------
class ConcurrencySetting(Base):
    """T + U."""

    def test_the_default_is_four_and_only_the_offered_choices_are_accepted(self):
        self.assertEqual(DEFAULT_CONCURRENCY, 4)
        self.assertEqual(CONCURRENCY_CHOICES, (1, 2, 4, 8, 16))
        self.assertEqual(self.c.concurrency, 4)
        for value in CONCURRENCY_CHOICES:
            self.assertTrue(self.c.set_concurrency(value))
            self.assertEqual(self.c.concurrency, value)

    def test_a_value_that_was_never_offered_is_rejected_and_leaves_the_limit_alone(self):
        self.c.set_concurrency(2)
        for bad in (0, -1, 3, 17, 100, None, "many", 4.5, [4]):
            self.assertFalse(self.c.set_concurrency(bad), bad)
        self.assertEqual(self.c.concurrency, 2)

    def test_a_stored_preference_arrives_as_text_and_is_still_validated(self):
        self.assertTrue(self.c.set_concurrency("8"))
        self.assertEqual(self.c.concurrency, 8)
        self.assertFalse(self.c.set_concurrency("18"))
        self.assertEqual(self.c.concurrency, 8)

    def test_an_empty_choice_of_targets_still_runs_nothing_at_all(self):
        """U. Parallel execution changes nothing about this: no target chosen means no run, never "all"."""
        self.c.set_concurrency(16)
        self.c.set_run_refs([])
        self.assertFalse(self.c.can_run())
        self.assertFalse(self.c.start_run())
        self.assertFalse(self.c.run_active)
        self.assertEqual(self.adb.log, [])
        self.assertEqual(self.history.list(), [])


if __name__ == "__main__":
    unittest.main()
