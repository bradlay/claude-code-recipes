#!/usr/bin/env python3
# SubagentStart hook: inject project context (CLAUDE.md excerpt,
# .claude/rules/*.md headings, recent git activity, top-level
# directory listing) into Plan and Explore subagents so they boot with
# the same project awareness as the parent session.
#
# Fail-open: any error returns an empty pass-through so the subagent
# starts without the injection rather than failing to start at all.
# This hook is informational, not a security gate.

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io  # noqa: E402

# Total budget for the injected context. Subagents have their own context
# windows and don't need (or want) the entire project blob.
TOTAL_BUDGET = 12000

# Per-section budgets. Sum is below TOTAL_BUDGET to leave room for
# section headers and the trailing marker.
_BUDGET_CLAUDE_MD = 8000
_BUDGET_RULES = 800
_BUDGET_GIT = 800
_BUDGET_STRUCTURE = 800


def _capped(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... (truncated)"


def _read_claude_md(cwd: Path, max_chars: int) -> str:
    path = cwd / "CLAUDE.md"
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return "(no CLAUDE.md found in project root)"
    return _capped(content, max_chars)


def _list_rules(cwd: Path) -> str:
    rules_dir = cwd / ".claude" / "rules"
    if not rules_dir.is_dir():
        return "(no .claude/rules/ directory)"

    lines: list[str] = []
    try:
        for entry in sorted(rules_dir.iterdir()):
            if not entry.name.endswith(".md"):
                continue
            try:
                with entry.open() as f:
                    first_line = f.readline().strip().lstrip("# ").strip()
                lines.append(f"- {entry.name}: {first_line}" if first_line else f"- {entry.name}")
            except (OSError, UnicodeDecodeError):
                lines.append(f"- {entry.name}")
    except OSError:
        return "(unable to read .claude/rules/)"
    return "\n".join(lines) if lines else "(empty)"


def _git_context(cwd: Path) -> str:
    parts: list[str] = []

    git_bin = shutil.which("git")
    if git_bin is None:
        return "(git not on PATH)"

    # Each git probe is best-effort. Subprocess failure (timeout, missing
    # binary mid-call, OS error) is treated as "skip this section" since
    # this hook is informational, not a security gate.
    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
        branch = subprocess.run(
            [git_bin, "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            parts.append(f"Branch: {branch.stdout.strip()}")

    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
        status = subprocess.run(
            [git_bin, "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if status.returncode == 0:
            short = status.stdout.strip()
            if short:
                parts.append("Working tree:\n" + short)
            else:
                parts.append("Working tree: clean")

    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
        log = subprocess.run(
            [git_bin, "log", "--oneline", "-5"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if log.returncode == 0 and log.stdout.strip():
            parts.append("Last commits:\n" + log.stdout.strip())

    return "\n".join(parts) if parts else "(not a git repo)"


def _top_level_dirs(cwd: Path) -> str:
    try:
        entries = sorted(cwd.iterdir(), key=lambda p: p.name)
    except OSError:
        return "(unable to list)"

    dirs = [e.name for e in entries if e.is_dir() and not e.name.startswith(".")]
    files = [e.name for e in entries if e.is_file() and not e.name.startswith(".")]
    result: list[str] = []
    if dirs:
        result.append("Dirs: " + ", ".join(dirs[:20]))
    if files:
        result.append("Files: " + ", ".join(files[:15]))
    return "\n".join(result) if result else "(empty)"


def main() -> int:
    inv = _io.parse_stdin(__file__)

    try:
        cwd = inv.cwd if inv.cwd.is_dir() else Path.cwd()

        claude_md = _capped(_read_claude_md(cwd, _BUDGET_CLAUDE_MD), _BUDGET_CLAUDE_MD)
        rules = _capped(_list_rules(cwd), _BUDGET_RULES)
        git_state = _capped(_git_context(cwd), _BUDGET_GIT)
        structure = _capped(_top_level_dirs(cwd), _BUDGET_STRUCTURE)

        context_parts = [
            "=== PROJECT CONTEXT (auto-injected by subagent-context-injector) ===",
            f"## CLAUDE.md\n{claude_md}",
            f"## Rules (.claude/rules/*.md)\n{rules}",
            f"## Git state\n{git_state}",
            f"## Top-level structure\n{structure}",
            "=== END PROJECT CONTEXT ===",
        ]

        context = "\n\n".join(context_parts)
        if len(context) > TOTAL_BUDGET:
            context = context[:TOTAL_BUDGET] + "\n... (context truncated to TOTAL_BUDGET)"

        _io.log(inv, f"injected {len(context)} chars of context for subagent")
        return _io.emit_subagent_start_context(context)
    except Exception as exc:
        _io.log(inv, f"subagent-context error (failing open): {exc}", level="error")
        return _io.emit_continue()


if __name__ == "__main__":
    sys.exit(main())
