"""app.bootstrap.main: provider selection for the application entry point. No real LDPlayer install or real
window enumeration is exercised here -- WindowsServices.create_default() itself only builds thin Win32 backend
wrappers (no discovery happens at construction time), and LDPlayerServices.create_default() is patched to
control whether "LDPlayer is installed" without touching the filesystem or spawning any process."""
import unittest
from unittest.mock import patch

from app.bootstrap.composite_services import CompositeAutomationServices
from app.bootstrap.main import _build_services, _explicit_provider_from
from app.bootstrap.windows_services import WindowsServices
from app.core.cancel import EmergencyStop
from app.core.errors import ConfigError


class ExplicitProviderFlag(unittest.TestCase):
    def test_no_flag_means_none_the_normal_combined_launch(self):
        self.assertIsNone(_explicit_provider_from([]))
        self.assertIsNone(_explicit_provider_from(["--something-else"]))

    def test_explicit_windows_or_ldplayer_is_returned_as_is(self):
        self.assertEqual(_explicit_provider_from(["--provider", "windows"]), "windows")
        self.assertEqual(_explicit_provider_from(["--provider", "ldplayer"]), "ldplayer")

    def test_an_unknown_value_is_rejected_up_front_not_guessed(self):
        with self.assertRaises(ValueError):
            _explicit_provider_from(["--provider", "android"])

    def test_a_trailing_flag_with_no_value_is_ignored_not_a_crash(self):
        self.assertIsNone(_explicit_provider_from(["--provider"]))


class BuildServices(unittest.TestCase):
    def test_explicit_windows_builds_only_the_windows_provider(self):
        services = _build_services("windows", EmergencyStop())
        self.assertIsInstance(services, WindowsServices)

    def test_explicit_ldplayer_that_is_not_installed_is_a_reported_hard_failure(self):
        with patch("app.bootstrap.ldplayer_services.LDPlayerServices.create_default",
                  side_effect=ConfigError("LDPlayer was not found.")):
            self.assertIsNone(_build_services("ldplayer", EmergencyStop()))

    def test_explicit_ldplayer_that_is_installed_builds_only_the_ldplayer_provider(self):
        from app.bootstrap.ldplayer_services import LDPlayerServices
        sentinel = object()
        with patch("app.bootstrap.ldplayer_services.LDPlayerServices.create_default", return_value=sentinel):
            self.assertIs(_build_services("ldplayer", EmergencyStop()), sentinel)

    def test_default_launch_with_no_ldplayer_installed_degrades_to_windows_only(self):
        """The default (no --provider) path must never refuse to start just because LDPlayer is not installed --
        that is an ordinary environment, not a startup failure (contrast with the explicit-ldplayer case above)."""
        with patch("app.bootstrap.ldplayer_services.LDPlayerServices.create_default",
                  side_effect=ConfigError("LDPlayer was not found.")):
            services = _build_services(None, EmergencyStop())
        self.assertIsInstance(services, WindowsServices)
        self.assertNotIsInstance(services, CompositeAutomationServices)

    def test_default_launch_with_ldplayer_installed_combines_both_providers(self):
        """A minimal double standing in for "LDPlayer is installed": create_default() succeeding is all
        _build_services actually inspects, so nothing about the real provider needs to be faked here."""
        class FakeLDPlayerServices:
            def __init__(self, stop):
                self.provider_id = "ldplayer-adb"
                self.stop = stop

            def discover(self):
                return []

        stop = EmergencyStop()
        with patch("app.bootstrap.ldplayer_services.LDPlayerServices.create_default",
                  side_effect=lambda s: FakeLDPlayerServices(s)):
            services = _build_services(None, stop)
        self.assertIsInstance(services, CompositeAutomationServices)
        self.assertEqual(services.provider_id, "ldplayer-adb + windows")


class DataDir(unittest.TestCase):
    """The person's data: in the project from the source tree, outside the program's folder when packaged -- so a
    newer version, which replaces the program's folder, never replaces their macros."""

    def test_from_the_source_tree_it_is_the_projects_data_folder(self):
        from app.bootstrap.main import DATA_DIR, PROJECT_ROOT, default_data_dir
        self.assertEqual(default_data_dir(frozen=False, env={"LOCALAPPDATA": r"C:\Elsewhere"}), PROJECT_ROOT / "data")
        self.assertEqual(DATA_DIR, PROJECT_ROOT / "data")          # these tests run from the source tree

    def test_packaged_it_is_olive_under_local_app_data(self):
        from pathlib import Path

        from app.bootstrap.main import default_data_dir
        self.assertEqual(default_data_dir(frozen=True, env={"LOCALAPPDATA": r"C:\Users\Анна\AppData\Local"}),
                         Path(r"C:\Users\Анна\AppData\Local") / "Olive" / "data")

    def test_packaged_without_local_app_data_it_falls_back_to_the_home_folder(self):
        from pathlib import Path

        from app.bootstrap.main import default_data_dir
        self.assertEqual(default_data_dir(frozen=True, env={}),
                         Path.home() / "AppData" / "Local" / "Olive" / "data")

    def test_packaged_it_is_never_inside_the_program(self):
        from app.bootstrap.main import PROJECT_ROOT, default_data_dir
        data = default_data_dir(frozen=True, env={"LOCALAPPDATA": str(PROJECT_ROOT / "x")})
        self.assertNotIn(PROJECT_ROOT / "data", [data, *data.parents])


class Version(unittest.TestCase):
    def test_the_package_and_pyproject_name_the_same_version(self):
        import tomllib
        from pathlib import Path

        from app import __version__
        project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8"))
        self.assertEqual(project["project"]["version"], __version__)


class SelfCheck(unittest.TestCase):
    """`--self-check PATH`: what a build can load, as JSON, with no window shown, no target and nothing sent."""

    def run_check(self, **patches):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock

        import app.bootstrap.composite_services  # noqa: F401
        import app.bootstrap.ldplayer_services  # noqa: F401
        import app.bootstrap.main as boot
        import app.gui.app  # noqa: F401
        import app.ocr.winrt  # noqa: F401
        import app.vision.roi  # noqa: F401
        import app.vision.template as template
        # Everything the check imports is imported BEFORE sys.modules is patched: leaving the patch restores
        # sys.modules as it was, which would drop a module first imported inside it -- and OpenCV cannot be
        # imported a second time in one process.
        template.fast_matcher()
        tk = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch("tkinter.Tk", tk), \
                patch.object(boot, "_build_services", side_effect=AssertionError("no services in a self-check")), \
                patch.object(boot, "_enable_dpi_awareness", side_effect=AssertionError("no DPI change either")), \
                patch.dict("sys.modules", patches):
            path = Path(tmp) / "report.json"
            code = boot.main(["--self-check", str(path)])
            return code, json.loads(path.read_text(encoding="utf-8")), tk

    def test_flag_is_read_only_with_its_path(self):
        from app.bootstrap.main import _self_check_path_from
        self.assertEqual(_self_check_path_from(["--self-check", "r.json"]), "r.json")
        self.assertIsNone(_self_check_path_from(["--self-check"]))
        self.assertIsNone(_self_check_path_from(["--provider", "windows"]))

    def test_everything_loadable_reports_ok_and_ends_with_zero(self):
        from app import __version__
        code, report, tk = self.run_check()
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["version"], __version__)
        self.assertFalse(report["frozen"])
        for name in ("window", "application", "text_recognition"):
            self.assertEqual(report["checks"][name], "ok")
        from app.vision.template import fast_matcher
        expected = "ok" if fast_matcher() is not None else "unavailable"   # a real search when OpenCV is here
        self.assertEqual(report["checks"]["fast_picture_search"], expected)

    def test_a_fast_search_that_answers_wrongly_is_reported_but_does_not_fail_the_check(self):
        import app.vision.template as template
        wrong = template.TemplateSearch((template.TemplateMatch(0, 0, 3, 3, 1.0),), None)
        with patch.object(template, "search_template", return_value=wrong):
            code, report, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("wrong answer", report["checks"]["fast_picture_search"])

    def test_the_window_is_hidden_before_it_could_be_drawn_and_then_closed(self):
        _, _, tk = self.run_check()
        root = tk.return_value
        self.assertEqual([c[0] for c in root.method_calls], ["withdraw", "destroy"])
        root.mainloop.assert_not_called()

    def test_a_part_that_cannot_be_loaded_is_reported_and_fails_the_check(self):
        code, report, _ = self.run_check(**{"app.ocr.winrt": None})
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(report["checks"]["text_recognition"].startswith("ImportError"))
        self.assertEqual(report["checks"]["window"], "ok")

    def test_the_fast_picture_search_missing_does_not_fail_the_check(self):
        import app.vision.template as template
        with patch.object(template, "fast_matcher", return_value=None):
            code, report, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(report["checks"]["fast_picture_search"], "unavailable")


if __name__ == "__main__":
    unittest.main()
