"""Read-only .record inspector.

Usage (from the project root):
    .venv\\Scripts\\python.exe tools\\inspect_record.py <file-or-folder> [...]

Parses each file in binary read mode and prints a factual report. It never writes,
renames or deletes anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.macros.analysis import analyze, format_report  # noqa: E402
from app.macros.model import MacroFormatError  # noqa: E402
from app.macros.parser import parse_file  # noqa: E402


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        print(__doc__)
        return 2
    files: list[Path] = []
    for arg in argv:
        p = Path(arg)
        files += sorted(p.glob("*.record")) if p.is_dir() else [p]
    failures = 0
    for f in files:
        print("=" * 78)
        try:
            print(format_report(analyze(parse_file(f))))
        except (MacroFormatError, OSError) as exc:
            failures += 1
            print(f"File: {f}\nCANNOT PARSE: {exc}")
    print("=" * 78)
    print(f"{len(files)} file(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
