"""The macro workspace stores: folders on disk (library), favourites/descriptions (meta), run history, and the
one rule that decides what the list shows (macro_list).

Folders are real directories, so these tests check the file system as well as the objects: a macro's folder is
where its file is, and nothing may ever leave the macro folder.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from app.gui.history import COMPLETED, FAILED, MAX_RECORDS, PARTIAL, STOPPED, RunHistory, RunRecord, day_key
from app.gui.library import LibraryError, MacroExists, MacroInfo, MacroLibrary, clean_folder
from app.gui.macro_list import Filter, Scope, Sort, counts, visible
from app.gui.meta import MacroMeta
from app.playback.macro import Macro
from app.providers.recording import PointerButton as B, RecordedEvent as E, RecordedEventKind as K, Recording


def macro(name: str, *, target: str = "win-1", clicks: int = 1) -> Macro:
    events = []
    for i in range(clicks):
        events += [E(K.BUTTON_DOWN, 0 if not events else 100, 10 + i, 20, B.PRIMARY, 1),
                   E(K.BUTTON_UP, 0, 10 + i, 20, B.PRIMARY, 1)]
    return Macro(Recording("windows", target, tuple(events)), name)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "macros"
        self.library = MacroLibrary(self.root)


class Folders(Base):
    def test_a_new_library_has_no_folders(self):
        self.assertEqual(self.library.folders(), [])
        self.assertEqual(self.library.list(), [])

    def test_creating_a_folder_makes_a_real_directory(self):
        self.library.create_folder("Tests")
        self.assertTrue((self.root / "Tests").is_dir())
        self.assertEqual(self.library.folders(), ["Tests"])

    def test_folder_names_that_could_escape_the_macro_folder_are_refused(self):
        for bad in ("..", ".", "a/b", "a\\b", "c:", "x?", 'q"', "trailing.", "x" * 61):
            with self.assertRaises(LibraryError, msg=bad):
                self.library.create_folder(bad)
        self.assertEqual(self.library.folders(), [])

    def test_surrounding_whitespace_is_stripped_rather_than_refused(self):
        """Windows silently drops a trailing space from a directory name, so keeping one would leave the folder
        on disk named differently from the folder the user typed. A trailing DOT is refused for the same reason:
        there is no stripping that would still mean what they asked for."""
        self.assertEqual(self.library.create_folder("  Tests  "), "Tests")
        self.assertEqual(self.library.folders(), ["Tests"])

    def test_an_empty_name_means_the_root_not_an_error(self):
        self.assertEqual(clean_folder("   "), "")
        self.assertEqual(self.library.folder_path(""), self.root)

    def test_a_duplicate_folder_is_refused(self):
        self.library.create_folder("Tests")
        with self.assertRaises(LibraryError):
            self.library.create_folder("Tests")

    def test_a_folder_with_macros_in_it_is_never_deleted_by_accident(self):
        self.library.create_folder("Tests")
        self.library.save(macro("one"), folder="Tests")
        with self.assertRaises(LibraryError):
            self.library.delete_folder("Tests")
        self.assertTrue((self.root / "Tests").is_dir())

    def test_an_empty_folder_can_be_deleted(self):
        self.library.create_folder("Tests")
        self.library.delete_folder("Tests")
        self.assertEqual(self.library.folders(), [])

    def test_renaming_a_folder_moves_the_macros_with_it(self):
        self.library.create_folder("Old")
        self.library.save(macro("one"), folder="Old")
        self.library.rename_folder("Old", "New")
        self.assertEqual(self.library.folders(), ["New"])
        info = self.library.list()[0]
        self.assertEqual((info.folder, info.key), ("New", "New/one.macro.json"))


class Macros(Base):
    def test_saving_into_a_folder_puts_the_file_there(self):
        self.library.create_folder("Tests")
        path = self.library.save(macro("first"), folder="Tests")
        self.assertEqual(path.parent, self.root / "Tests")
        self.assertTrue(path.is_file())

    def test_listing_covers_the_root_and_every_folder(self):
        self.library.save(macro("loose"))
        self.library.create_folder("Tests")
        self.library.save(macro("inside"), folder="Tests")
        listed = {i.name: i for i in self.library.list()}
        self.assertEqual(set(listed), {"loose", "inside"})
        self.assertEqual(listed["loose"].folder, "")
        self.assertEqual(listed["inside"].folder, "Tests")
        self.assertEqual(listed["inside"].key, "Tests/inside.macro.json")
        self.assertGreater(listed["loose"].modified, 0.0)

    def test_the_same_name_may_exist_once_per_folder(self):
        self.library.create_folder("Tests")
        self.library.save(macro("same"))
        self.library.save(macro("same"), folder="Tests")          # different folder: not a collision
        with self.assertRaises(MacroExists):
            self.library.save(macro("same"), folder="Tests")
        self.assertEqual(len(self.library.list()), 2)

    def test_moving_a_macro_moves_its_file(self):
        self.library.create_folder("Tests")
        path = self.library.save(macro("movable"))
        moved = self.library.move(path, "Tests")
        self.assertFalse(path.exists())
        self.assertEqual(moved.parent, self.root / "Tests")
        self.assertEqual(self.library.load(moved).name, "movable")

    def test_moving_onto_an_existing_name_is_refused_and_changes_nothing(self):
        self.library.create_folder("Tests")
        here = self.library.save(macro("clash"))
        self.library.save(macro("clash"), folder="Tests")
        with self.assertRaises(MacroExists):
            self.library.move(here, "Tests")
        self.assertTrue(here.exists())

    def test_moving_back_to_the_root_works(self):
        self.library.create_folder("Tests")
        path = self.library.save(macro("out"), folder="Tests")
        moved = self.library.move(path, "")
        self.assertEqual(moved.parent, self.root)

    def test_renaming_changes_the_file_name_and_the_name_inside_it_together(self):
        path = self.library.save(macro("before"))
        renamed = self.library.rename(path, "After Rename")
        self.assertFalse(path.exists())
        self.assertEqual(renamed.name, "after_rename.macro.json")
        self.assertEqual(self.library.load(renamed).name, "After Rename")

    def test_renaming_keeps_the_macro_in_its_folder(self):
        self.library.create_folder("Tests")
        path = self.library.save(macro("inside"), folder="Tests")
        renamed = self.library.rename(path, "still inside")
        self.assertEqual(renamed.parent, self.root / "Tests")

    def test_renaming_onto_an_existing_name_is_refused(self):
        self.library.save(macro("taken"))
        path = self.library.save(macro("other"))
        with self.assertRaises(MacroExists):
            self.library.rename(path, "taken")
        self.assertTrue(path.exists())

    def test_nothing_outside_the_macro_folder_can_be_touched(self):
        outside = Path(self.tmp.name) / "elsewhere.macro.json"
        outside.write_text("{}", encoding="utf-8")
        for call in (lambda: self.library.delete(outside), lambda: self.library.load(outside),
                     lambda: self.library.move(outside, ""), lambda: self.library.rename(outside, "x")):
            with self.assertRaises(LibraryError):
                call()
        self.assertTrue(outside.exists())

    def test_a_file_inside_a_folder_is_owned_and_can_be_deleted(self):
        self.library.create_folder("Tests")
        path = self.library.save(macro("doomed"), folder="Tests")
        self.library.delete(path)
        self.assertFalse(path.exists())

    def test_an_unreadable_file_is_listed_with_its_error_not_hidden(self):
        self.library.create_folder("Tests")
        (self.root / "Tests" / "broken.macro.json").write_text("{not json", encoding="utf-8")
        info = next(i for i in self.library.list() if i.name == "broken")
        self.assertFalse(info.valid)
        self.assertEqual(info.folder, "Tests")


class Meta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "meta.json"
        self.meta = MacroMeta(self.path)

    def test_favourites_survive_a_restart(self):
        self.assertFalse(self.meta.is_favorite("a.macro.json"))
        self.assertTrue(self.meta.toggle_favorite("a.macro.json"))
        self.assertEqual(MacroMeta(self.path).favorites(), frozenset({"a.macro.json"}))

    def test_unstarring_removes_the_entry_entirely(self):
        self.meta.set_favorite("a", True)
        self.meta.set_favorite("a", False)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {})

    def test_descriptions_round_trip_and_are_bounded(self):
        self.meta.set_description("a", "  Collects the daily rewards.  ")
        self.assertEqual(MacroMeta(self.path).description("a"), "Collects the daily rewards.")
        self.meta.set_description("a", "x" * 5000)
        self.assertEqual(len(self.meta.description("a")), 500)

    def test_renaming_a_macro_carries_its_metadata_with_it(self):
        self.meta.set_favorite("old.macro.json", True)
        self.meta.set_description("old.macro.json", "note")
        self.meta.rename_key("old.macro.json", "Tests/new.macro.json")
        self.assertTrue(self.meta.is_favorite("Tests/new.macro.json"))
        self.assertEqual(self.meta.description("Tests/new.macro.json"), "note")
        self.assertFalse(self.meta.is_favorite("old.macro.json"))

    def test_metadata_for_deleted_macros_is_dropped(self):
        self.meta.set_favorite("a", True)
        self.meta.set_favorite("b", True)
        self.meta.keep_only(frozenset({"a"}))
        self.assertEqual(self.meta.favorites(), frozenset({"a"}))

    def test_a_damaged_file_reads_as_empty_instead_of_crashing(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(MacroMeta(self.path).favorites(), frozenset())

    def test_without_a_path_nothing_touches_disk(self):
        memory = MacroMeta(None)
        memory.set_favorite("a", True)
        self.assertTrue(memory.is_favorite("a"))
        self.assertFalse(self.path.exists())


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.json"
        self.history = RunHistory(self.path)

    def add(self, name: str, outcome: str, at: float, key: str = "") -> RunRecord:
        return self.history.record(macro=name, macro_key=key or f"{name}.macro.json", outcome=outcome,
                                   targets=("Target 1",), actions_total=10, completed=10, at=at)

    def test_runs_are_listed_newest_first_and_survive_a_restart(self):
        self.add("first", COMPLETED, 1000.0)
        self.add("second", FAILED, 2000.0)
        self.assertEqual([r.macro for r in RunHistory(self.path).list()], ["second", "first"])

    def test_a_record_keeps_what_the_user_needs_and_hides_the_technical_detail(self):
        record = self.history.record(macro="m", macro_key="m.macro.json", outcome=STOPPED,
                                     targets=("A", "B", "C"), duration_s=12.5, actions_total=42, completed=14,
                                     detail="PlaybackError: target_not_found")
        self.assertEqual((record.target_count, record.completed, record.actions_total), (3, 14, 42))
        self.assertIn("target_not_found", record.detail)             # kept, but only for "show details"

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            self.history.record(macro="m", macro_key="k", outcome="exploded")

    def test_runs_of_one_macro_can_be_found_again(self):
        self.add("a", COMPLETED, 1000.0)
        self.add("b", COMPLETED, 1500.0)
        self.add("a", PARTIAL, 2000.0)
        runs = self.history.for_macro("a.macro.json")
        self.assertEqual([r.outcome for r in runs], [PARTIAL, COMPLETED])
        self.assertEqual(len(self.history.for_macro("a.macro.json", limit=1)), 1)

    def test_last_run_times_report_the_most_recent_run_per_macro(self):
        self.add("a", COMPLETED, 1000.0)
        self.add("a", COMPLETED, 3000.0)
        self.add("b", COMPLETED, 2000.0)
        self.assertEqual(self.history.last_run_times(),
                         {"a.macro.json": 3000.0, "b.macro.json": 2000.0})

    def test_renaming_a_macro_keeps_its_past_runs(self):
        self.add("a", COMPLETED, 1000.0)
        self.history.rename_key("a.macro.json", "Tests/a.macro.json")
        self.assertEqual(len(self.history.for_macro("Tests/a.macro.json")), 1)

    def test_the_file_is_capped_so_it_cannot_grow_without_end(self):
        for i in range(MAX_RECORDS + 25):
            self.add("m", COMPLETED, float(i))
        self.assertEqual(len(self.history.list()), MAX_RECORDS)
        self.assertEqual(self.history.list()[-1].at, 25.0)           # the oldest fell off

    def test_damaged_entries_are_dropped_and_never_invent_a_run(self):
        self.path.write_text(json.dumps([
            {"at": 1.0, "macro": "good", "macro_key": "g", "outcome": COMPLETED, "targets": ["A"]},
            {"at": 2.0, "macro": "bad outcome", "outcome": "nonsense"},
            {"macro": "no time", "outcome": COMPLETED},
            "not even an object"]), encoding="utf-8")
        self.assertEqual([r.macro for r in RunHistory(self.path).list()], ["good"])

    def test_clearing_empties_the_file(self):
        self.add("a", COMPLETED, 1.0)
        self.history.clear()
        self.assertEqual(RunHistory(self.path).list(), [])

    def test_runs_are_grouped_by_local_day(self):
        now = time.time()
        self.assertEqual(day_key(now), time.strftime("%Y-%m-%d", time.localtime(now)))
        self.assertNotEqual(day_key(now), day_key(now - 24 * 3600))


class ListRule(unittest.TestCase):
    def info(self, name, *, folder="", modified=0.0, actions=1, key=None) -> MacroInfo:
        key = key or (f"{folder}/{name}.macro.json" if folder else f"{name}.macro.json")
        return MacroInfo(Path(key), name, "windows", "t", actions * 2, actions, "", folder, modified, key)

    def setUp(self):
        self.alpha = self.info("alpha", modified=300.0, actions=5)
        self.beta = self.info("beta", folder="Tests", modified=200.0, actions=30)
        self.gamma = self.info("gamma", folder="Tests", modified=100.0, actions=1)
        self.all = [self.alpha, self.beta, self.gamma]
        self.favorites = frozenset({self.beta.key})
        self.runs = {self.gamma.key: 5000.0, self.alpha.key: 1000.0}

    def show(self, flt: Filter):
        return [i.name for i in visible(self.all, flt, favorites=self.favorites, last_runs=self.runs)]

    def test_all_is_everything_newest_first_by_default(self):
        self.assertEqual(self.show(Filter()), ["alpha", "beta", "gamma"])

    def test_a_folder_shows_only_that_folder(self):
        self.assertEqual(self.show(Filter().with_scope(Scope.FOLDER, "Tests")), ["beta", "gamma"])

    def test_favourites_is_a_view_over_the_same_files(self):
        self.assertEqual(self.show(Filter().with_scope(Scope.FAVORITES)), ["beta"])

    def test_recent_ignores_the_sort_because_it_would_no_longer_be_recent(self):
        self.assertEqual(self.show(Filter(scope=Scope.RECENT, sort=Sort.NAME_DESC)),
                         ["alpha", "beta", "gamma"])

    def test_search_matches_the_name_or_the_folder_ignoring_case(self):
        self.assertEqual(self.show(Filter(query="ALPH")), ["alpha"])
        self.assertEqual(self.show(Filter(query="tests")), ["beta", "gamma"])
        self.assertEqual(self.show(Filter(query="nothing here")), [])

    def test_search_applies_inside_a_folder_too(self):
        self.assertEqual(self.show(Filter(scope=Scope.FOLDER, folder="Tests", query="gam")), ["gamma"])

    def test_every_sort_order(self):
        self.assertEqual(self.show(Filter(sort=Sort.NAME)), ["alpha", "beta", "gamma"])
        self.assertEqual(self.show(Filter(sort=Sort.NAME_DESC)), ["gamma", "beta", "alpha"])
        self.assertEqual(self.show(Filter(sort=Sort.MODIFIED)), ["alpha", "beta", "gamma"])
        self.assertEqual(self.show(Filter(sort=Sort.MODIFIED_OLD)), ["gamma", "beta", "alpha"])
        self.assertEqual(self.show(Filter(sort=Sort.ACTIONS)), ["beta", "alpha", "gamma"])
        self.assertEqual(self.show(Filter(sort=Sort.LAST_RUN)), ["gamma", "alpha", "beta"])   # never-run last

    def test_switching_scope_forgets_a_stale_folder(self):
        self.assertEqual(Filter(scope=Scope.FOLDER, folder="Tests").with_scope(Scope.ALL).folder, "")

    def test_clearing_returns_to_everything(self):
        cleared = Filter(scope=Scope.FOLDER, folder="Tests", query="x", sort=Sort.NAME).cleared()
        self.assertEqual((cleared.scope, cleared.folder, cleared.query), (Scope.ALL, "", ""))

    def test_counts_feed_the_navigation(self):
        totals = counts(self.all, self.favorites)
        self.assertEqual((totals["all"], totals["favorites"], totals["Tests"]), (3, 1, 2))


if __name__ == "__main__":
    unittest.main()
