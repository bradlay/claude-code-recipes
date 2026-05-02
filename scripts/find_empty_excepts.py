#!/usr/bin/env python3
"""Find every `try/except <X>: pass` (or with a single-line `pass`-only
body) across the plugin scripts. Exit 1 if any are found.

CodeQL's "Empty except" rule flags these, and we keep tripping it. The
canonical replacement is `with contextlib.suppress(<X>): ...`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def empty_excepts(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, exception-spec) for each except handler whose body
    is exactly a single `pass` (or just docstring + pass)."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, OSError):
        return []

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = [s for s in node.body if not isinstance(s, ast.Expr)]
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            spec = ast.unparse(node.type) if node.type else "Exception"
            found.append((node.lineno, spec))
    return found


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    targets = sorted(repo_root.glob("plugins/*/scripts/**/*.py"))
    bad: list[tuple[Path, int, str]] = []
    for f in targets:
        for lineno, spec in empty_excepts(f):
            bad.append((f.relative_to(repo_root), lineno, spec))

    if not bad:
        print("OK: no `except <X>: pass` patterns found")
        return 0

    print(f"FAIL: {len(bad)} empty-except handlers (use contextlib.suppress instead):")
    for p, lineno, spec in bad:
        print(f"  {p}:{lineno}  except {spec}: pass")
    return 1


if __name__ == "__main__":
    sys.exit(main())
