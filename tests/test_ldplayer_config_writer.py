"""The one change Olive makes to an LDPlayer instance's file: ADB debugging on (the person's decision of 2026-10-27).

Temporary folders only: no real LDPlayer file is read or written.
"""
import json
import tempfile
import unittest
from pathlib import Path

from app.ldplayer.config_reader import ADB_DEBUG_KEY
from app.ldplayer.config_writer import ConfigWriteRefused, config_path, enable_adb_debugging


def ldplayer_form(data: dict) -> bytes:
    """A file as LDPlayer writes one ([VERIFIED] 2026-10-27): four spaces, CRLF, no newline at the end."""
    return json.dumps(data, indent=4, ensure_ascii=False).replace("\n", "\r\n").encode("utf-8")


FILE = {"propertySettings.phoneIMEI": "010305020974078", "statusSettings.playerName": "Olive-ADB-Test",
        "basicSettings.verticalSync": False, "advancedSettings.resolution": {"width": 1280, "height": 720},
        "hotkeySettings.backKey": {"modifiers": 0, "key": 27}}


class EnableAdbDebugging(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ld = self.root / "LDPlayer14"
        (self.ld / "vms" / "config").mkdir(parents=True)
        self.backups = self.root / "data" / "ldplayer-backups"

    def write(self, raw: bytes, index: int = 18) -> Path:
        path = config_path(self.ld, index)
        path.write_bytes(raw)
        return path

    def test_the_one_setting_is_the_only_change_and_the_file_as_it_was_is_kept(self):
        for before in (0, None):                       # "Close", or not in the file (LDPlayer's default)
            with self.subTest(before=before):
                data = dict(FILE, **({ADB_DEBUG_KEY: before} if before is not None else {}))
                original = ldplayer_form(data)
                path = self.write(original)
                backup = enable_adb_debugging(self.ld, 18, "Olive-ADB-Test", self.backups)
                self.assertEqual(path.read_bytes(), ldplayer_form(dict(data, **{ADB_DEBUG_KEY: 1})))
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(backup.parent, self.backups)
                self.assertEqual(list(path.parent.iterdir()), [path])     # no temporary file left beside it

    def test_checking_only_writes_nothing_and_refuses_what_writing_would(self):
        original = ldplayer_form(dict(FILE, **{ADB_DEBUG_KEY: 0}))
        path = self.write(original)
        self.assertIsNone(enable_adb_debugging(self.ld, 18, "Olive-ADB-Test", self.backups, check_only=True))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.backups.exists())
        with self.assertRaises(ConfigWriteRefused):
            enable_adb_debugging(self.ld, 18, "LDPlayer-3", self.backups, check_only=True)

    def test_a_file_that_does_not_read_back_as_written_is_said_to_be_written(self):
        from unittest import mock

        import app.ldplayer.config_writer as module
        path = self.write(ldplayer_form(dict(FILE, **{ADB_DEBUG_KEY: 0})))
        real = module.os.replace

        def replace_then_spoil(source, target):
            real(source, target)
            Path(target).write_bytes(Path(target).read_bytes() + b" ")
        with mock.patch.object(module.os, "replace", replace_then_spoil):
            with self.assertRaises(ConfigWriteRefused) as cm:
                enable_adb_debugging(self.ld, 18, "Olive-ADB-Test", self.backups)
        self.assertTrue(cm.exception.written)
        self.assertTrue(path.exists())

    def test_on_already_writes_nothing(self):
        original = ldplayer_form(dict(FILE, **{ADB_DEBUG_KEY: 1}))
        path = self.write(original)
        self.assertIsNone(enable_adb_debugging(self.ld, 18, "Olive-ADB-Test", self.backups))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_a_file_olive_could_not_write_back_unchanged_is_refused_untouched(self):
        cases = {
            "two spaces": json.dumps(FILE, indent=2).encode(),
            "another order of spacing": json.dumps(FILE, indent=4, separators=(",", ":")).encode(),
            "not JSON": b"{ not json",
            "a list": ldplayer_form([1, 2]),            # type: ignore[arg-type]
            "not UTF-8": b"\xff\xfe{}",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                path = self.write(raw)
                with self.assertRaises(ConfigWriteRefused) as cm:
                    enable_adb_debugging(self.ld, 18, "Olive-ADB-Test", self.backups)
                self.assertTrue(cm.exception.said.get("ru"))
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(self.backups.exists())

    def test_a_file_that_names_another_instance_is_refused(self):
        path = self.write(ldplayer_form(FILE))
        with self.assertRaises(ConfigWriteRefused):
            enable_adb_debugging(self.ld, 18, "LDPlayer-3", self.backups)
        self.assertEqual(path.read_bytes(), ldplayer_form(FILE))

    def test_a_file_with_no_name_of_its_own_is_the_instance_of_its_number(self):
        """Older instances keep an empty name in the file; the file's number is then what ties it to the instance."""
        data = dict(FILE, **{"statusSettings.playerName": ""})
        path = self.write(ldplayer_form(data), index=13)
        enable_adb_debugging(self.ld, 13, "LDPlayer-13", self.backups)
        self.assertEqual(json.loads(path.read_bytes())[ADB_DEBUG_KEY], 1)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ConfigWriteRefused):
            enable_adb_debugging(self.ld, 99, "LDPlayer-99", self.backups)

    def test_lf_files_and_escaped_text_keep_their_form(self):
        data = dict(FILE, **{"statusSettings.playerName": "Игра"})
        for raw in (json.dumps(data, indent=4, ensure_ascii=True).encode(),
                    json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8") + b"\n"):
            with self.subTest(raw=raw[:20]):
                path = self.write(raw)
                enable_adb_debugging(self.ld, 18, "Игра", self.backups)
                after = path.read_bytes()
                self.assertNotIn(b"\r\n", after)
                self.assertEqual(after.endswith(b"\n"), raw.endswith(b"\n"))
                self.assertEqual(b"\\u0418" in after, b"\\u0418" in raw)


if __name__ == "__main__":
    unittest.main()
