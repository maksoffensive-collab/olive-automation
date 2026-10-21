"""Provider-neutral OCR and AI contracts (`app.ocr.provider`, `app.ai.provider`). No local engine, no cloud API:
every test here uses a fake/in-memory provider. No network access, no API keys, no vendor SDK.

What this proves, concretely:
  * the OCR and AI contracts work end to end with a fake provider, satisfying the runtime-checkable Protocol
    without subclassing anything;
  * two different fake providers can be swapped behind the SAME calling code, unchanged;
  * the core (core/providers/playback/automation/gui/bootstrap) has zero import dependency on either package, so
    the application keeps working with neither configured;
  * AI output is inert, structured data: it can never carry something callable, so an AiProvider cannot execute
    anything on its own, however it is implemented.
"""
import ast
import threading
import unittest
from pathlib import Path

from app.ai.provider import AiProvider, AiRequest, AiResponse
from app.ocr.provider import OcrProvider, OcrResult, OcrTextBlock
from app.vision.bitmap import Bitmap
from app.vision.frame import Frame
from app.vision.roi import Roi
from tests.test_vision import frame_of  # the real verified-target -> AdbSession -> Frame pipeline, fakes only

ROOT = Path(__file__).resolve().parents[1] / "app"


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def a_frame() -> Frame:
    return frame_of(Bitmap.solid(20, 10, (10, 20, 30)))


class FakeOcrProvider:
    """A deterministic stand-in: 'reads' whatever text was baked in at construction time."""

    def __init__(self, blocks=()):
        self.blocks = blocks
        self.calls = []

    def read_text(self, frame: Frame, *, roi: Roi | None = None, language: str = "",
                  cancel: threading.Event | None = None) -> OcrResult:
        self.calls.append((frame, roi))
        return OcrResult(tuple(self.blocks))


class OtherFakeOcrProvider:
    """A second, differently-shaped fake, to prove substitutability rather than one hard-coded shape."""

    def read_text(self, frame: Frame, *, roi: Roi | None = None, language: str = "",
                  cancel: threading.Event | None = None) -> OcrResult:
        return OcrResult((OcrTextBlock("OTHER", Roi(0, 0, 1, 1), 1.0),))


class FakeAiProvider:
    def __init__(self, response: AiResponse):
        self.response = response
        self.requests = []

    def ask(self, request: AiRequest) -> AiResponse:
        self.requests.append(request)
        return self.response


class OcrProviderContract(unittest.TestCase):
    def test_a_fake_provider_satisfies_the_protocol_without_subclassing_it(self):
        fake = FakeOcrProvider()
        self.assertIsInstance(fake, OcrProvider)

    def test_reads_text_from_a_real_frame_and_returns_structured_blocks(self):
        block = OcrTextBlock("Play", Roi(5, 5, 40, 10), 0.9)
        fake = FakeOcrProvider([block])
        frame = a_frame()
        result = fake.read_text(frame, roi=Roi(0, 0, 10, 10))
        self.assertEqual(result.blocks, (block,))
        self.assertEqual(result.text, "Play")
        self.assertEqual(fake.calls, [(frame, Roi(0, 0, 10, 10))])

    def test_empty_result_means_no_text_found_not_an_error(self):
        result = OcrResult()
        self.assertEqual((result.blocks, result.text), ((), ""))

    def test_multiple_blocks_join_one_per_line_in_order(self):
        blocks = (OcrTextBlock("Start", Roi(0, 0, 1, 1), 1.0), OcrTextBlock("Cancel", Roi(1, 1, 1, 1), 1.0))
        self.assertEqual(OcrResult(blocks).text, "Start\nCancel")

    def test_text_block_validation(self):
        good = Roi(0, 0, 1, 1)
        with self.assertRaises(ValueError):
            OcrTextBlock(123, good, 1.0)  # pyright: ignore[reportArgumentType]  (deliberately not a string)
        with self.assertRaises(ValueError):
            OcrTextBlock("x", (0, 0, 1, 1), 1.0)  # pyright: ignore[reportArgumentType]  (a bare tuple, not a Roi)
        for bad in (-0.1, 1.1, True, "0.5"):
            with self.assertRaises(ValueError):
                OcrTextBlock("x", good, bad)  # pyright: ignore[reportArgumentType]  (deliberately invalid)

    def test_provider_never_executes_anything_it_only_reads(self):
        """OCR answers 'where is this text', nothing else: the Protocol has exactly one method."""
        public = {n for n in dir(OcrProvider) if not n.startswith("_")}
        self.assertEqual(public, {"read_text"})


class AiProviderContract(unittest.TestCase):
    def test_a_fake_provider_satisfies_the_protocol_without_subclassing_it(self):
        fake = FakeAiProvider(AiResponse("do nothing"))
        self.assertIsInstance(fake, AiProvider)

    def test_answers_a_request_and_keeps_the_context_intact(self):
        fake = FakeAiProvider(AiResponse("tap the play button", actions=({"op": "tap", "x": 10, "y": 20},)))
        req = AiRequest("what should I do next?", context={"ocr_text": "Play\nCancel"})
        resp = fake.ask(req)
        self.assertEqual(resp.text, "tap the play button")
        self.assertEqual(resp.actions, ({"op": "tap", "x": 10, "y": 20},))
        self.assertEqual(fake.requests, [req])

    def test_request_requires_a_non_empty_prompt(self):
        for bad in ("", "   ", 123, None):
            with self.assertRaises(ValueError):
                AiRequest(bad)
        with self.assertRaises(ValueError):
            AiRequest("ok", context=["not", "a", "dict"])

    def test_actions_must_be_inert_plain_dicts_never_callables(self):
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap"}, "not a dict"))
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "callback": lambda: None},))   # nothing executable may ride along

    def test_a_callable_nested_inside_a_dict_is_rejected(self):
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "meta": {"cb": lambda: None}},))

    def test_a_callable_nested_inside_a_list_is_rejected(self):
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "steps": [1, 2, lambda: None]},))

    def test_a_callable_nested_several_levels_deep_is_rejected_in_dicts_and_lists(self):
        deep_in_dicts = {"op": "tap", "a": {"b": {"c": {"cb": lambda: None}}}}
        deep_in_lists = {"op": "tap", "a": [[[lambda: None]]]}
        mixed_dict_then_list = {"op": "tap", "a": {"b": [{"c": lambda: None}]}}
        mixed_list_then_dict = {"op": "tap", "a": [{"b": {"c": [lambda: None]}}]}
        for bad in (deep_in_dicts, deep_in_lists, mixed_dict_then_list, mixed_list_then_dict):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                AiResponse("x", actions=(bad,))

    def test_a_bound_method_and_a_custom_object_are_also_rejected_not_only_lambdas(self):
        class Thing:
            def method(self):
                pass

        thing = Thing()
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "cb": thing.method},))          # a bound method
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "obj": thing},))                # an arbitrary custom object

    def test_a_deeply_nested_inert_structure_is_still_accepted(self):
        """The validator must not become so strict that ordinary AI response data is rejected."""
        action = {
            "op": "type_text",
            "text": "hello",
            "count": 3,
            "ratio": 0.5,
            "enabled": True,
            "note": None,
            "targets": [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
            "nested": {"a": [1, 2, {"b": (3, 4, {"c": ["d", "e", None, False, 1.5]})}]},
        }
        resp = AiResponse("ok", actions=(action,))
        self.assertEqual(resp.actions, (action,))

    def test_a_callable_nested_inside_a_tuple_is_rejected(self):
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "a": ((1, 2, (lambda: None,)),)},))

    def test_a_non_string_dict_key_is_rejected_at_top_level_and_nested(self):
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", 1: "x"},))
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "nested": {True: "x"}},))

    def test_empty_containers_are_accepted(self):
        action = {"op": "tap", "list": [], "dict": {}, "tuple": ()}
        resp = AiResponse("ok", actions=(action,))
        self.assertEqual(resp.actions, (action,))

    def test_a_wide_but_shallow_structure_is_accepted_quickly(self):
        """Breadth is not depth: a single level with many items must not be confused with the pathological-depth
        case and must not be slow enough to be its own denial-of-service vector."""
        import time

        action = {"op": "tap", "many": list(range(200_000))}
        start = time.perf_counter()
        resp = AiResponse("ok", actions=(action,))
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertEqual(resp.actions, (action,))

    def test_the_exact_depth_boundary_is_precise(self):
        """Empirically located (not assumed) boundary of _MAX_INERT_DEPTH through this exact nesting shape: 18
        levels is accepted, 19 is refused. This pins the exact behavior so a future change to the limit, or to how
        depth is counted, is a deliberate, visible edit to this test, not a silent shift."""
        def nested(n):
            d = {"v": "leaf"}
            for _ in range(n):
                d = {"x": d}
            return d

        AiResponse("ok", actions=({"op": "tap", "n": nested(18)},))         # must not raise
        with self.assertRaises(ValueError):
            AiResponse("ok", actions=({"op": "tap", "n": nested(19)},))

    def test_a_pathologically_deep_structure_is_rejected_not_a_recursionerror_crash(self):
        """The inertness check must itself be safe against untrusted-shaped AI output: a provider returning an
        absurdly deep (accidental or adversarial) structure must be cleanly refused, not crash the validator that
        exists specifically to make AI output safe to hold."""
        deep: dict = {}
        cursor = deep
        for _ in range(2000):
            cursor["x"] = {}
            cursor = cursor["x"]
        with self.assertRaises(ValueError):
            AiResponse("x", actions=({"op": "tap", "nested": deep},))

    def test_provider_can_only_return_data_it_has_no_execution_method(self):
        """AI answers 'what should happen', never runs it: the Protocol has exactly one method, `ask`."""
        public = {n for n in dir(AiProvider) if not n.startswith("_")}
        self.assertEqual(public, {"ask"})


class ProviderSubstitutability(unittest.TestCase):
    def run_ocr_pipeline(self, provider: OcrProvider) -> str:
        """Stand-in for 'higher-level automation logic': it only ever calls the Protocol's own method."""
        return provider.read_text(a_frame()).text

    def test_two_different_ocr_providers_work_through_the_same_calling_code(self):
        self.assertEqual(self.run_ocr_pipeline(FakeOcrProvider([OcrTextBlock("A", Roi(0, 0, 1, 1), 1.0)])), "A")
        self.assertEqual(self.run_ocr_pipeline(OtherFakeOcrProvider()), "OTHER")

    def run_ai_pipeline(self, provider: AiProvider, prompt: str) -> str:
        return provider.ask(AiRequest(prompt)).text

    def test_two_different_ai_providers_work_through_the_same_calling_code(self):
        self.assertEqual(self.run_ai_pipeline(FakeAiProvider(AiResponse("first")), "go"), "first")

        class OtherFakeAiProvider:
            def ask(self, request: AiRequest) -> AiResponse:
                return AiResponse(f"second: {request.prompt}")

        self.assertEqual(self.run_ai_pipeline(OtherFakeAiProvider(), "go"), "second: go")


COMPOSITION_FILE = Path("bootstrap") / "main.py"          # relative to the app root: the ONE exempt file
COMPOSITION_FUNCTION = "_build_ocr"                         # ... and the one exempt top-level function in it


def imports_outside_the_composition_point(path: Path, root: Path) -> set[str]:
    """Every module `path` imports, as absolute names -- relative imports resolved against the file's own package
    (`from ..ocr import winrt` is `app.ocr.winrt`), and `from app import ocr` counted as `app.ocr` -- except inside
    the one exempt function, and only when `path` IS the exempt file under `root` (compared as resolved paths, never
    by folder or file name alone)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if path.resolve() == (root / COMPOSITION_FILE).resolve():
        tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name == COMPOSITION_FUNCTION)]
    package = [root.name, *path.resolve().relative_to(root.resolve()).parent.parts]
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            base = package[:len(package) - (n.level - 1)] if n.level else []
            module = ".".join([*base, *([n.module] if n.module else [])])
            if module:
                out.add(module)
                out.update(f"{module}.{a.name}" for a in n.names if a.name != "*")
    return out


def composition_point_problems(source: str) -> list[str]:
    """What is wrong with the exempt function's shape: one `try` and nothing else, no else/finally, every handler
    ending in `return None` (optionally after reporting with print), and `app.ocr.winrt` the only AI/OCR import."""
    tree = ast.parse(source)
    points = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == COMPOSITION_FUNCTION]
    if len(points) != 1:
        return [f"expected exactly one top-level {COMPOSITION_FUNCTION}, found {len(points)}"]
    body = [n for n in points[0].body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.Try):
        return ["the engine must be built in one guarded try and nothing else"]
    guarded = body[0]
    problems = []
    if guarded.orelse or guarded.finalbody or not guarded.handlers:
        problems.append("the try must have handlers and no else/finally")
    for handler in guarded.handlers:
        *report, last = handler.body
        if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Constant) and last.value.value is None):
            problems.append("every handler must end in `return None`: a failure means 'no OCR', never a failed start")
        if not all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                   and n.value.func.id == "print" for n in report):
            problems.append("a handler may only report (print) before returning None")
    modules = {n.module for n in ast.walk(guarded) if isinstance(n, ast.ImportFrom) and n.module}
    modules |= {a.name for n in ast.walk(guarded) if isinstance(n, ast.Import) for a in n.names}
    if {m for m in modules if m.startswith(("app.ai", "app.ocr"))} != {"app.ocr.winrt"}:
        problems.append(f"only app.ocr.winrt may be imported here, not {sorted(modules)}")
    return problems


class CoreWorksWithoutAiOrOcr(unittest.TestCase):
    """The core must keep functioning with neither configured: it must not import either package at all.

    One OCR engine is now connected, and something has to construct it. That is the composition root's job (see
    `app/ocr/provider.py`), so exactly ONE point is exempt: the top-level function `_build_ocr` in the file
    `app/bootstrap/main.py` (matched by its exact path), which may import the engine module `app.ocr.winrt` lazily,
    inside a `try` that falls back to "no OCR". `composition_point_problems` holds that point to that shape;
    everywhere else -- the GUI included, every sub-package included -- the rule is unchanged (the GUI names the
    reader's type only through `app.observe`), and `tests.test_ocr_engine` proves the application still composes
    when the engine cannot load."""

    def test_no_core_or_provider_package_imports_ai_or_ocr(self):
        for pkg in ("core", "providers", "playback", "automation", "vision", "recovery", "gui", "bootstrap",
                   "windows", "adb", "ldplayer", "instances", "macros"):
            for path in sorted((ROOT / pkg).rglob("*.py")):          # sub-packages too (gui/view)
                bad = {m for m in imports_outside_the_composition_point(path, ROOT)
                       if m.startswith(("app.ai", "app.ocr"))}
                self.assertEqual(bad, set(), f"{path.relative_to(ROOT)} imports {bad}")

    def test_the_one_composition_point_chooses_only_the_ocr_engine_lazily_and_optionally(self):
        self.assertEqual(composition_point_problems((ROOT / COMPOSITION_FILE).read_text(encoding="utf-8")), [])


class CompositionPointGuard(unittest.TestCase):
    """The guard above, on synthetic trees: exempt in exactly one place, and nowhere that merely looks like it."""

    GOOD = (
        "def _build_ocr(stop):\n"
        "    '''doc'''\n"
        "    try:\n"
        "        from app.ocr.winrt import WindowsOcrProvider\n"
        "        return WindowsOcrProvider(stop)\n"
        "    except ImportError as exc:\n"
        "        print(exc)\n"
        "        return None\n")

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "app"

    def write(self, relative: str, source: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def ocr_imports(self, path: Path) -> set[str]:
        return {m for m in imports_outside_the_composition_point(path, self.root)
                if m.startswith(("app.ai", "app.ocr"))}

    def test_the_exact_file_is_exempt(self):
        self.assertEqual(self.ocr_imports(self.write("bootstrap/main.py", self.GOOD)), set())

    def test_a_look_alike_anywhere_else_is_not(self):
        for relative in ("gui/bootstrap/main.py", "bootstrap/sub/main.py", "bootstrap/other.py", "gui/main.py",
                         "bootstrap/bootstrap/main.py"):
            self.assertIn("app.ocr.winrt", self.ocr_imports(self.write(relative, self.GOOD)), relative)

    def test_relative_and_from_package_imports_are_seen_as_what_they_are(self):
        cases = {
            "gui/view/a.py": "from ...ocr import winrt\n",
            "gui/b.py": "from ..ocr.provider import OcrProvider\n",
            "gui/c.py": "from app import ocr\n",
            "gui/d.py": "from .. import ai\n",
            "bootstrap/e.py": "from ..ocr import winrt\n",
        }
        for relative, source in cases.items():
            self.assertTrue(self.ocr_imports(self.write(relative, source)), relative)
        self.assertEqual(self.ocr_imports(self.write("gui/f.py", "from . import flows\nfrom ..observe import x\n")),
                         set())

    def test_only_the_top_level_function_itself_is_exempt_in_the_exact_file(self):
        indented = "".join("    " + line + "\n" for line in self.GOOD.splitlines())
        cases = {
            "module level": self.GOOD + "import app.ocr.provider\n",
            "async def": self.GOOD.replace("def _build_ocr", "async def _build_ocr"),
            "nested def": "def outer():\n" + indented,
            "method": "class Composer:\n" + indented,
            "other function": self.GOOD.replace("_build_ocr", "_build_other"),
        }
        for name, source in cases.items():
            self.assertTrue(self.ocr_imports(self.write("bootstrap/main.py", source)), name)

    def test_the_exempt_function_must_keep_its_shape(self):
        self.assertEqual(composition_point_problems(self.GOOD), [])
        bad = {
            "returns an object": self.GOOD.replace("        return None", "        return object()"),
            "no return": self.GOOD.replace("        return None\n", ""),
            "finally": self.GOOD + "    finally:\n        pass\n",
            "imports ai": self.GOOD.replace("from app.ocr.winrt import", "from app.ai.provider import"),
            "another ocr module": self.GOOD.replace("app.ocr.winrt", "app.ocr.other"),
            "more than a try": self.GOOD.replace("    try:", "    x = 1\n    try:"),
            "does more than report": self.GOOD.replace("        print(exc)", "        exc.retry()"),
            "two of them": self.GOOD + self.GOOD,
        }
        for name, source in bad.items():
            self.assertTrue(composition_point_problems(source), name)


class AiAndOcrStaySeparate(unittest.TestCase):
    def test_ai_and_ocr_packages_do_not_import_each_other(self):
        """Kept conceptually separate on purpose: OCR reads text, AI decides what to do; neither needs the other."""
        for pkg, forbidden in (("ai", "app.ocr"), ("ocr", "app.ai")):
            for path in (ROOT / pkg).glob("*.py"):
                self.assertNotIn(forbidden, imports_of(path), f"{pkg}/{path.name}")

    def test_neither_package_imports_a_technology_or_execution_mechanism(self):
        for pkg in ("ai", "ocr"):
            for path in (ROOT / pkg).glob("*.py"):
                bad = {m for m in imports_of(path)
                      if m.startswith(("app.adb", "app.ldplayer", "app.instances", "app.windows", "app.automation"))
                      or m in ("subprocess", "socket", "urllib", "http", "requests")}
                self.assertEqual(bad, set(), f"{pkg}/{path.name} imports {bad}")


if __name__ == "__main__":
    unittest.main()
