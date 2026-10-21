"""Tests for `app.ldplayer.keymap.android_keycode`: the neutral `Key` -> Android `KeyEvent` code bridge used by
`app.ldplayer.playback._LDPlayerInput.key_press` (see that module for why this lives in app.ldplayer, not app.adb)."""
import unittest

from app.ldplayer.keymap import android_keycode
from app.providers.input import Key


class Keymap(unittest.TestCase):
    def test_total_over_key_and_free_of_ambiguity(self):
        codes = {k: android_keycode(k) for k in Key}
        self.assertEqual(len(codes), len(Key))
        self.assertEqual(len(set(codes.values())), len(Key))              # no two keys share a code
        self.assertEqual((codes[Key.A], codes[Key.Z], codes[Key.DIGIT_0], codes[Key.DIGIT_9]), (29, 54, 7, 16))
        self.assertEqual((codes[Key.F1], codes[Key.F12]), (131, 142))
        self.assertEqual((codes[Key.ENTER], codes[Key.ESCAPE], codes[Key.SPACE], codes[Key.TAB]), (66, 111, 62, 61))
        self.assertEqual((codes[Key.BACKSPACE], codes[Key.DELETE]), (67, 112))
        self.assertEqual((codes[Key.LEFT], codes[Key.UP], codes[Key.RIGHT], codes[Key.DOWN]), (21, 19, 22, 20))
        self.assertEqual((codes[Key.SHIFT], codes[Key.CTRL], codes[Key.ALT]), (59, 113, 57))
        for bad in ("a", 66, None):
            with self.assertRaises(ValueError):
                android_keycode(bad)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
