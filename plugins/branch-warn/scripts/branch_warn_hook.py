#!/usr/bin/env python3
# UserPromptSubmit hook: emit a systemMessage like "On branch <X>" once
# per hour to nudge the user when they're working on an unexpected
# branch (typically: `main` instead of a feature branch).
#
# Throttled by mtime on a marker file under ${CLAUDE_PLUGIN_DATA}/.
# Fail-open: any error returns a pass-through and the prompt proceeds.

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, paths  # noqa: E402

# Default throttle: emit at most once per hour, configurable.
_DEFAULT_THROTTLE_SECONDS = 3600
# Default protected-branches list. Anything in this set triggers a
# louder "you're on <X>" warning. Anything else just shows "on branch
# <X>" hourly.
_DEFAULT_PROTECTED = {"main", "master"}


def _throttle_seconds() -> int:
    raw = os.environ.get("CLAUDE_BRANCH_WARN_THROTTLE_SECONDS")
    if raw:
        with contextlib.suppress(ValueError):
            return max(1, int(raw))
    return _DEFAULT_THROTTLE_SECONDS


def _protected_branches() -> set[str]:
    raw = os.environ.get("CLAUDE_BRANCH_WARN_PROTECTED")
    if raw:
        return {b.strip() for b in raw.split(",") if b.strip()}
    return _DEFAULT_PROTECTED


def _current_branch(cwd: Path) -> str | None:
    git_bin = shutil.which("git")
    if git_bin is None:
        return None

    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
        result = subprocess.run(
            [git_bin, "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    return None


def _touch(marker: Path) -> None:
    with contextlib.suppress(OSError):
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.touch(mode=0o600)


def _was_warned_recently(marker: Path, throttle: int) -> bool:
    if not marker.exists():
        return False
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    return age < throttle


def main() -> int:
    inv = _io.parse_stdin(__file__)

    try:
        cwd = inv.cwd if inv.cwd.is_dir() else Path.cwd()
        marker = paths.warned_marker()
        throttle = _throttle_seconds()

        if _was_warned_recently(marker, throttle):
            return _io.emit_systemmessage_or_passthrough()

        branch = _current_branch(cwd)
        if not branch:
            return _io.emit_systemmessage_or_passthrough()

        protected = _protected_branches()
        if branch in protected:
            _touch(marker)
            return _io.emit_systemmessage_or_passthrough(
                message=(
                    f"WARNING: on '{branch}' branch. "
                    "Consider a feature branch for work that might land in a PR."
                ),
            )

        # Non-protected branch: emit a quieter "on branch X" hint hourly.
        _touch(marker)
        return _io.emit_systemmessage_or_passthrough(message=f"On branch: {branch}")
    except Exception as exc:
        _io.log(inv, f"branch-warn error (failing open): {exc}", level="error")
        return _io.emit_systemmessage_or_passthrough()


if __name__ == "__main__":
    sys.exit(main())
