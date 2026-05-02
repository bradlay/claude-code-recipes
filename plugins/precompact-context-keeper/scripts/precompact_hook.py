#!/usr/bin/env python3
# PreCompact hook: just before conversation compaction, inject CLAUDE.md
# + a tiny git-state summary as a systemMessage so the post-compaction
# model still has the project framing.
#
# The host CLI compaction step strips conversation history. Without this
# hook the model wakes up on the other side without the project context
# it had at the start of the session. Emitting a systemMessage on
# PreCompact threads that context through the compaction boundary.
#
# Fail-open: any error returns a pass-through so compaction proceeds
# without the injection.

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

# Compaction is rare and the post-compaction model has plenty of room;
# but no need to dump the entire CLAUDE.md. 1500 chars is plenty for the
# first-page of "what is this project."
_MAX_CLAUDE_MD = 1500
_MAX_STATUS_LINES = 15


def _read_claude_md(cwd: Path) -> str:
    path = cwd / "CLAUDE.md"
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    if len(content) > _MAX_CLAUDE_MD:
        return content[:_MAX_CLAUDE_MD] + "\n... (truncated)"
    return content


def _git_work_state(cwd: Path) -> str:
    git_bin = shutil.which("git")
    if git_bin is None:
        return ""

    parts: list[str] = []

    # Each git probe is best-effort; PreCompact must never fail-closed.
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
        if status.returncode == 0 and status.stdout.strip():
            lines = status.stdout.strip().splitlines()
            if len(lines) > _MAX_STATUS_LINES:
                summary = (
                    "\n".join(lines[:_MAX_STATUS_LINES])
                    + f"\n... ({len(lines) - _MAX_STATUS_LINES} more)"
                )
            else:
                summary = status.stdout.strip()
            parts.append("Uncommitted:\n" + summary)

    return "\n".join(parts)


def main() -> int:
    inv = _io.parse_stdin(__file__)

    try:
        cwd = inv.cwd if inv.cwd.is_dir() else Path.cwd()
        claude_md = _read_claude_md(cwd)
        work_state = _git_work_state(cwd)

        if not claude_md and not work_state:
            _io.log(inv, "nothing to inject; passing through")
            return _io.emit_continue()

        parts = ["PRESERVE ACROSS COMPACTION"]
        if claude_md:
            parts.append(f"## Project Config (CLAUDE.md)\n{claude_md}")
        if work_state:
            parts.append(f"## Current Work State\n{work_state}")

        msg = "\n\n---\n\n".join(parts)
        _io.log(inv, f"injected {len(msg)} chars of pre-compaction context")
        return _io.emit_system_message(msg)
    except Exception as exc:
        _io.log(inv, f"precompact-keeper error (failing open): {exc}", level="error")
        return _io.emit_continue()


if __name__ == "__main__":
    sys.exit(main())
