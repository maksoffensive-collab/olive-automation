"""Round-trip regression tests against COPIES of the user's real .record files.

Originals are only read (copied); they are never opened for writing. Each test hashes
every original before and after to prove nothing changed. The parser is run on the
temporary copy. If the LDPlayer folders do not exist (e.g. another machine) the tests skip.
"""
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from app.macros.analysis import analyze
from app.macros.compare import structural_diff
from app.macros.model import MacroFormatError
from app.macros.parser import parse_file
from app.macros.serializer import serialize

REAL_DIRS = [
    Path(r"C:\LDPlayer\LDPlayer14\vms\operationRecords"),
    Path(r"C:\LDPlayer\LDPlayer14\GEternal"),
]


def real_files() -> list[Path]:
    return sorted(p for d in REAL_DIRS if d.is_dir() for p in d.glob("*.record"))


def fingerprint(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns


class RealFileRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = real_files()
        if not cls.files:
            raise unittest.SkipTest("no real .record files found on this machine")
        cls.before = {p: fingerprint(p) for p in cls.files}
        cls._tmp = tempfile.TemporaryDirectory()
        cls.copies = []
        for i, src in enumerate(cls.files):
            dst = Path(cls._tmp.name) / f"{i:03d}.record"
            shutil.copyfile(src, dst)
            cls.copies.append((src, dst))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_tmp", None):
            cls._tmp.cleanup()
        # Final guarantee after every test in this class has run.
        for src in cls.files:
            if fingerprint(src) != cls.before[src]:
                raise AssertionError(f"ORIGINAL MODIFIED: {src}")

    def test_originals_never_modified(self):
        """Originals still have the same hash, size and mtime as before the copies were made."""
        for src in self.files:
            with self.subTest(file=src.name):
                self.assertEqual(fingerprint(src), self.before[src])
        self.assertEqual(real_files(), self.files, "file set changed")

    def test_every_real_file_parses(self):
        for src, dst in self.copies:
            with self.subTest(file=src.name):
                try:
                    macro = parse_file(dst)
                except MacroFormatError as exc:  # pragma: no cover - reported as failure
                    self.fail(f"cannot parse: {exc}")
                self.assertEqual([i for i in macro.issues if i.severity == "error"], [])

    def test_structural_round_trip(self):
        for src, dst in self.copies:
            with self.subTest(file=src.name):
                original = dst.read_bytes()
                out = serialize(parse_file(dst))
                diffs = structural_diff(json.loads(original.decode("utf-8-sig")),
                                        json.loads(out.decode("utf-8-sig")))
                self.assertEqual(diffs, [])

    def test_byte_exact_round_trip(self):
        """Observed 40/40 on 2026-09-21. A failure here means a formatting quirk, not data loss."""
        for src, dst in self.copies:
            with self.subTest(file=src.name):
                self.assertEqual(serialize(parse_file(dst)), dst.read_bytes())

    def test_reparse_of_serialized_output_is_identical(self):
        for src, dst in self.copies:
            with self.subTest(file=src.name):
                first = serialize(parse_file(dst))
                p = Path(self._tmp.name) / "reparse.tmp"
                p.write_bytes(first)
                self.assertEqual(serialize(parse_file(p)), first)

    def test_no_unknown_structure_in_real_files(self):
        """Characterization: keys/ids seen on 2026-09-21. New unknowns are reported, not hidden."""
        for src, dst in self.copies:
            with self.subTest(file=src.name):
                r = analyze(parse_file(dst))
                self.assertEqual(r.unknown_top_level_keys, [])
                self.assertEqual(r.record_info_keys_unknown, [])
                self.assertEqual(r.record_info_keys_missing, [])
                self.assertEqual(r.unknown_operation_ids, [])
                self.assertEqual(r.unknown_operation_keys, {})
                self.assertEqual(r.unknown_point_keys, [])
                self.assertEqual(r.unknown_merged_entry_keys, [])

    def test_parsing_a_read_only_copy_works(self):
        """A read-only file can be parsed and serialized: the parser never needs write access."""
        src, dst = self.copies[0]
        ro = Path(self._tmp.name) / "readonly.record"
        shutil.copyfile(dst, ro)
        os.chmod(ro, stat.S_IREAD)
        try:
            before = fingerprint(ro)
            self.assertEqual(serialize(parse_file(ro)), ro.read_bytes())
            self.assertEqual(fingerprint(ro), before)
        finally:
            os.chmod(ro, stat.S_IWRITE | stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
