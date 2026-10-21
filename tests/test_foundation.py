"""Foundation smoke test: the package tree exists and imports. Tests no feature."""
import importlib
import unittest

PACKAGES = [
    "app", "app.core", "app.adb", "app.ldplayer", "app.instances", "app.macros",
    "app.recorder", "app.automation", "app.vision", "app.ocr", "app.ai",
    "app.recovery", "app.logging", "app.gui", "app.config",
]


class FoundationTests(unittest.TestCase):
    def test_all_packages_import(self):
        for name in PACKAGES:
            with self.subTest(package=name):
                self.assertIsNotNone(importlib.import_module(name))

    def test_app_logging_does_not_shadow_stdlib_logging(self):
        import logging
        self.assertNotEqual(logging.__name__, "app.logging")
        self.assertTrue(hasattr(logging, "getLogger"))


if __name__ == "__main__":
    unittest.main()
