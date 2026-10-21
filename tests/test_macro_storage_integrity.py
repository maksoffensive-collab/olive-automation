"""Macro storage integrity (second bug-fix stage): how a macro is named, written and read back.

  C-17  the recording / macro JSON loaders must refuse duplicate keys (last-wins would silently pick one);
  C-12  macro names are turned into file names without losing or colliding non-ASCII names;
  C-08  a save never leaves a temporary file behind and never overwrites an existing macro unless allowed,
        even if the file appears between the existence check and the write.
Deterministic; only temporary folders are touched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.gui import library as library_module
from app.gui.library import LibraryError, MacroExists, MacroLibrary, slugify
from app.playback.format import MacroFormatError, macro_from_json, macro_to_json
from app.playback.macro import Macro
from app.providers.recording import RecordedEvent as E, RecordedEventKind as K, Recording
from app.providers.recording_format import RecordingFormatError, recording_from_json, recording_to_json


def recording(x=1):
    return Recording("windows", "win-1", (E(K.POINTER_MOVE, 0, x, 2), E(K.POINTER_MOVE, 5, x + 1, 2)))


def macro(name, x=1):
    return Macro(recording(x), name)


class DuplicateKeys(unittest.TestCase):
    """C-17."""

    def test_valid_files_still_load(self):
        self.assertEqual(recording_from_json(recording_to_json(recording())), recording())
        self.assertEqual(macro_from_json(macro_to_json(macro("ok"))), macro("ok"))

    def test_recording_top_level_duplicate_is_refused(self):
        text = recording_to_json(recording()).replace('"end_reason": "stopped"',
                                                      '"end_reason": "bogus", "end_reason": "stopped"')
        self.assertIn('"bogus"', text)
        with self.assertRaises(RecordingFormatError) as cm:
            recording_from_json(text)
        self.assertIn("duplicate", str(cm.exception))

    def test_recording_duplicate_inside_an_event_is_refused(self):
        text = recording_to_json(recording()).replace('"kind": "pointer_move"',
                                                      '"kind": "pointer_move", "kind": "pointer_move"', 1)
        with self.assertRaises(RecordingFormatError):
            recording_from_json(text)

    def test_macro_duplicate_name_is_refused_instead_of_last_wins(self):
        text = macro_to_json(macro("keep me")).replace('"name": "keep me"', '"name": "a", "name": "keep me"')
        with self.assertRaises(MacroFormatError) as cm:
            macro_from_json(text)
        self.assertIn("duplicate", str(cm.exception))

    def test_macro_duplicate_recording_and_nested_events_are_refused(self):
        good = json.loads(macro_to_json(macro("m")))
        inner = json.dumps(good["recording"])
        text = '{"schema": "automation-macro", "version": 1, "name": "m", "recording": %s, "recording": %s}' % (inner, inner)
        with self.assertRaises(MacroFormatError):
            macro_from_json(text)
        nested = macro_to_json(macro("m")).replace('"x": 1', '"x": 1, "x": 99', 1)
        with self.assertRaises(MacroFormatError):
            macro_from_json(nested)

    def test_the_library_lists_such_a_file_as_invalid_and_refuses_to_load_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            bad = Path(tmp) / f"dup{library_module.SUFFIX}"
            bad.write_text(macro_to_json(macro("m")).replace('"name": "m"', '"name": "a", "name": "m"'), encoding="utf-8")
            (info,) = lib.list()
            self.assertFalse(info.valid)
            self.assertIn("duplicate", info.error)
            with self.assertRaises(LibraryError):
                lib.load(bad)


class FileNames(unittest.TestCase):
    """C-12."""

    def test_non_ascii_names_keep_their_letters(self):
        self.assertEqual(slugify("\u00c4ventyr"), "\u00e4ventyr")
        self.assertEqual(slugify("\u0424\u0430\u0440\u043c 1"), "\u0444\u0430\u0440\u043c_1")
        self.assertEqual(slugify("\u9f8d \u6e2c\u8a66"), "\u9f8d_\u6e2c\u8a66")

    def test_different_names_do_not_collapse_to_the_same_file(self):
        self.assertNotEqual(slugify("\u00c51"), slugify("\u00c41"))
        self.assertNotEqual(slugify("\u00c51"), "1")
        self.assertNotEqual(slugify("\u0424\u0430\u0440\u043c"), slugify("\u0411\u043e\u0439"))

    def test_existing_ascii_behaviour_is_unchanged(self):
        self.assertEqual(slugify(" Farm / Dev #1 "), "farm_dev_1")
        self.assertEqual(slugify("../../x"), "x")
        self.assertEqual(slugify("a" * 200), "a" * library_module.MAX_SLUG)
        for nothing in ("", "   ", "???", "...", "//", "_-_"[:0]):
            self.assertEqual(slugify(nothing), "")

    def test_composed_and_decomposed_forms_are_one_name(self):
        self.assertEqual(slugify("\u00e5"), slugify("a\u030a"))

    def test_a_slug_can_never_address_another_folder(self):
        for name in ("..\\evil", "a/b", "c:\\x", "\u00e4/../\u00f6", "a:b*c?d"):
            slug = slugify(name)
            self.assertFalse(set(slug) & set('\\/:*?"<>|.'), (name, slug))

    def test_names_that_are_only_punctuation_are_still_refused_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LibraryError) as cm:
                MacroLibrary(tmp).path_for("???")
            self.assertIn("name", str(cm.exception))

    def test_non_ascii_macros_save_load_and_stay_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            names = ["\u00c51", "\u00c41", "\u0424\u0430\u0440\u043c", "Farm"]
            paths = [lib.save(macro(n, x=i + 1)) for i, n in enumerate(names)]
            self.assertEqual(len({p.name for p in paths}), 4)
            for n, p in zip(names, paths):
                self.assertEqual(lib.load(p).name, n)
            self.assertEqual(sorted(i.name for i in lib.list()), sorted(names))
            with self.assertRaises(MacroExists):                              # a genuine same-name save still asks
                lib.save(macro("\u00c51"))


class SafeSave(unittest.TestCase):
    """C-08."""

    def test_a_failed_save_leaves_no_temporary_file_and_keeps_the_old_macro(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.save(macro("keep me", x=1))
            before = path.read_bytes()
            with mock.patch.object(library_module.os, "replace", side_effect=PermissionError(13, "locked")):
                with self.assertRaises(LibraryError):
                    lib.save(macro("keep me", x=50), overwrite=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(os.listdir(tmp)), [path.name])           # no *.tmp left behind

    def test_a_failure_while_writing_leaves_nothing_either(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            with mock.patch.object(library_module, "macro_to_json", side_effect=OSError(28, "disk full")):
                with self.assertRaises(LibraryError):
                    lib.save(macro("new"))
            self.assertEqual(os.listdir(tmp), [])

    def test_a_file_that_appears_after_the_existence_check_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.path_for("race")
            path.write_text("someone else's macro", encoding="utf-8")         # created "after" the check
            with mock.patch.object(Path, "exists", return_value=False):       # the check saw nothing
                with self.assertRaises(MacroExists):
                    lib.save(macro("race"))
            self.assertEqual(path.read_text(encoding="utf-8"), "someone else's macro")
            self.assertEqual(sorted(os.listdir(tmp)), [path.name])

    def test_overwrite_true_replaces_atomically_and_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.save(macro("m", x=1))
            lib.save(macro("m", x=7), overwrite=True)
            self.assertEqual(lib.load(path).recording.events[0].x, 7)
            self.assertEqual(sorted(os.listdir(tmp)), [path.name])

    def test_two_saves_use_distinct_temporary_names(self):
        seen = []
        real = library_module.tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real(*args, **kwargs)
            seen.append(Path(name).name)
            return fd, name

        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            with mock.patch.object(library_module.tempfile, "mkstemp", side_effect=spy):
                lib.save(macro("a"))
                lib.save(macro("a"), overwrite=True)
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])
            self.assertTrue(all(n.endswith(".tmp") for n in seen))

    def test_saved_bytes_are_unchanged_by_the_new_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = macro("same bytes")
            path = MacroLibrary(tmp).save(m)
            self.assertEqual(path.read_bytes(), macro_to_json(m).encode("utf-8"))

    def test_directory_creation_failure_is_a_clean_library_error(self):
        """mkdir() is the FIRST thing save() does; if it fails, tmp_name is still None, so the finally block must
        not attempt to clean up a temp file that was never created."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(Path(tmp) / "macros")     # the subdirectory does not exist yet
            with mock.patch.object(library_module.Path, "mkdir", side_effect=OSError(13, "Permission denied")):
                with self.assertRaises(LibraryError):
                    lib.save(macro("x"))
            self.assertFalse((Path(tmp) / "macros").exists())

    def test_temporary_file_creation_failure_is_a_clean_library_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            with mock.patch.object(library_module.tempfile, "mkstemp",
                                   side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(LibraryError):
                    lib.save(macro("x"))
            self.assertEqual(os.listdir(tmp), [])

    def test_a_non_overwrite_link_failure_other_than_file_exists_is_a_clean_library_error(self):
        """os.link is the never-overwrite mechanism; a filesystem that cannot create hard links at all (not just
        'the name exists') must still fail cleanly, not crash, and must not leave a temp file behind."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            with mock.patch.object(library_module.os, "link", side_effect=OSError(1, "Incorrect function")):
                with self.assertRaises(LibraryError):
                    lib.save(macro("x"))
            self.assertEqual(os.listdir(tmp), [])


class SlugCollision(unittest.TestCase):
    """Two DIFFERENT names can legitimately produce the SAME slug (unlike the FileNames tests above, which prove
    the opposite: that distinct-looking unicode names do NOT collapse). When they do collide, the second save must
    be refused exactly like a same-name save -- never a silent overwrite -- and overwrite=True must still work."""

    def test_punctuation_only_differences_collide_and_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.save(macro("Farm!", x=1))
            self.assertEqual(path.name, "farm.macro.json")
            with self.assertRaises(MacroExists):
                lib.save(macro("Farm?", x=2))                 # different name, same slug: still protected
            self.assertEqual(lib.load(path).name, "Farm!")    # untouched
            lib.save(macro("Farm?", x=2), overwrite=True)      # explicit overwrite still works
            self.assertEqual(lib.load(path).name, "Farm?")

    def test_case_only_differences_collide_and_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.save(macro("Farm"))
            with self.assertRaises(MacroExists):
                lib.save(macro("farm"))
            with self.assertRaises(MacroExists):
                lib.save(macro("FARM"))
            self.assertEqual(lib.load(path).name, "Farm")

    def test_a_name_within_the_length_limit_that_still_collides_after_slug_truncation_is_protected(self):
        """Macro names may be up to 100 characters (app.playback.macro.MAX_NAME_LENGTH) but slugs are truncated at
        MAX_SLUG (60): two distinct, valid names sharing the same first 60 slug characters must not silently
        overwrite one another."""
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            prefix = "x" * library_module.MAX_SLUG
            path = lib.save(macro(prefix + " one", x=1))
            self.assertEqual(path.name, "x" * library_module.MAX_SLUG + ".macro.json")
            with self.assertRaises(MacroExists):
                lib.save(macro(prefix + " two", x=2))
            self.assertEqual(lib.load(path).name, prefix + " one")


class ReservedAndDotOnlyNames(unittest.TestCase):
    """Windows reserves bare device names (CON, NUL, COM1, ...) but the library always appends `.macro.json`, so
    the on-disk name is never bare. Verified empirically on this real filesystem, not merely asserted."""

    def test_windows_device_names_save_and_load_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            for name in ("CON", "NUL", "PRN", "AUX", "COM1", "LPT1", "con", "Nul"):
                with self.subTest(name=name):
                    path = lib.save(macro(name), overwrite=True)
                    self.assertTrue(path.is_file())
                    self.assertEqual(lib.load(path).name, name)

    def test_dot_and_dot_dot_alone_are_refused_like_any_other_all_punctuation_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            for name in (".", ".."):
                with self.subTest(name=name):
                    with self.assertRaises(LibraryError):
                        lib.save(macro(name))


class PathEscapeIsRefused(unittest.TestCase):
    """The library must never load or delete a file outside its own configured directory, however the path to it
    is spelled. Proven directly here (not merely inferred from the slug-safety tests above)."""

    def test_a_path_outside_the_directory_is_refused_by_load_and_delete(self):
        with tempfile.TemporaryDirectory() as outer:
            lib_dir = Path(outer) / "macros"
            lib_dir.mkdir()
            lib = MacroLibrary(lib_dir)
            outside = Path(outer) / "secret.macro.json"
            outside.write_text("not yours", encoding="utf-8")
            for attempt in (outside, lib_dir / ".." / "secret.macro.json"):
                with self.subTest(attempt=str(attempt)):
                    with self.assertRaises(LibraryError):
                        lib.load(attempt)
                    with self.assertRaises(LibraryError):
                        lib.delete(attempt)
            self.assertEqual(outside.read_text(encoding="utf-8"), "not yours")

    def test_a_file_inside_the_directory_but_with_the_wrong_suffix_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            other = Path(tmp) / "notes.txt"
            other.write_text("hi", encoding="utf-8")
            with self.assertRaises(LibraryError):
                lib.load(other)
            with self.assertRaises(LibraryError):
                lib.delete(other)
            self.assertTrue(other.is_file())


class DirectoryContentsAreHandledSafely(unittest.TestCase):
    """`list()` globs `*.macro.json`; anything else in the folder must be invisible to it, and even a matching
    NAME that is not an ordinary readable file must be reported as invalid, never crash the scan."""

    def test_unrelated_files_and_leftover_temp_files_are_ignored_by_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            lib.save(macro("real"))
            (Path(tmp) / "notes.txt").write_text("hi", encoding="utf-8")
            (Path(tmp) / "save-abc123.tmp").write_text("leftover", encoding="utf-8")
            names = sorted(i.name for i in lib.list())
            self.assertEqual(names, ["real"])

    def test_a_directory_entry_matching_the_suffix_is_listed_as_invalid_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            (Path(tmp) / "weird.macro.json").mkdir()
            infos = lib.list()
            self.assertEqual(len(infos), 1)
            self.assertFalse(infos[0].valid)
            with self.assertRaises(LibraryError):
                lib.load(infos[0].path)
            with self.assertRaises(LibraryError):
                lib.delete(infos[0].path)


class RefreshAndRepeatedOperations(unittest.TestCase):
    def test_list_reflects_external_filesystem_changes_with_no_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            self.assertEqual(lib.list(), [])
            path = lib.save(macro("a"))
            self.assertEqual([i.name for i in lib.list()], ["a"])
            path.unlink()                                       # simulate an external change, not through the library
            self.assertEqual(lib.list(), [])

    def test_many_macros_are_all_listed_correctly_and_sorted_by_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            for i in range(150):
                lib.save(macro(f"m{i:03d}", x=i))
            infos = lib.list()
            self.assertEqual(len(infos), 150)
            self.assertEqual([i.name for i in infos], sorted(i.name for i in infos))
            self.assertTrue(all(i.valid for i in infos))

    def test_repeated_save_list_load_delete_cycles_leave_no_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            for i in range(20):
                path = lib.save(macro(f"cycle{i}"))
                self.assertEqual(lib.load(path).name, f"cycle{i}")
                lib.delete(path)
                self.assertEqual(lib.list(), [])
            self.assertEqual(os.listdir(tmp), [])

    def test_deleting_the_same_macro_twice_fails_cleanly_the_second_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            path = lib.save(macro("once"))
            lib.delete(path)
            with self.assertRaises(LibraryError):
                lib.delete(path)

    def test_loading_a_nonexistent_but_well_formed_path_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = MacroLibrary(tmp)
            with self.assertRaises(LibraryError):
                lib.load(Path(tmp) / "never_saved.macro.json")


if __name__ == "__main__":
    unittest.main()
