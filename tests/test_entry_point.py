"""app/__main__.py: `python -m app` starts the application, importing the module never does.

None of these tests can start the real application. The static test only parses the file. The two behaviour tests
run in a separate interpreter that first replaces app.bootstrap.main with a stand-in whose main() only records the
call, and replaces tkinter with a module that refuses to be used; so even a regression would call the stand-in."""
import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "app" / "__main__.py"
PRELUDE = """
import json, sys, types
calls = []
fake = types.ModuleType("app.bootstrap.main")
def main(argv=None):
    calls.append(argv)
    return 7
fake.main = main
sys.modules["app.bootstrap.main"] = fake
class NoGui(types.ModuleType):
    def __getattr__(self, name):
        raise RuntimeError("tkinter must not be used here")
sys.modules["tkinter"] = NoGui("tkinter")
"""


def is_main_guard(node) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators))


class EntryPointStatic(unittest.TestCase):
    def test_module_level_code_only_imports_or_is_behind_the_main_guard(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        for node in tree.body:
            with self.subTest(line=node.lineno):
                self.assertTrue(isinstance(node, (ast.Import, ast.ImportFrom)) or is_main_guard(node)
                                or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)),
                                f"line {node.lineno} runs when the module is imported")

    def test_the_main_guard_exits_with_the_result_of_main(self):
        tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
        guards = [n for n in tree.body if is_main_guard(n)]
        self.assertEqual(len(guards), 1)
        calls = [n for n in ast.walk(guards[0]) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "main"]
        self.assertTrue(calls)


class EntryPointBehaviour(unittest.TestCase):
    def run_child(self, body: str) -> list:
        proc = subprocess.run([sys.executable, "-B", "-c", PRELUDE + body], cwd=ROOT, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_importing_the_entry_point_does_not_call_main(self):
        result = self.run_child("import importlib\n"
                                "try:\n"
                                "    importlib.import_module('app.__main__')\n"
                                "    outcome = 'imported'\n"
                                "except SystemExit:\n"
                                "    outcome = 'exited'\n"
                                "print(json.dumps([outcome, len(calls)]))\n")
        self.assertEqual(result, ["imported", 0])

    def test_running_the_package_calls_main_once_and_exits_with_its_result(self):
        result = self.run_child("import runpy\n"
                                "try:\n"
                                "    runpy.run_module('app', run_name='__main__', alter_sys=True)\n"
                                "    code = None\n"
                                "except SystemExit as exc:\n"
                                "    code = exc.code\n"
                                "print(json.dumps([len(calls), code]))\n")
        self.assertEqual(result, [1, 7])


if __name__ == "__main__":
    unittest.main()
