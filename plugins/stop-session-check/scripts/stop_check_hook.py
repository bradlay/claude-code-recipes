#!/usr/bin/env python3
# Stop hook entry point: when the session is about to end, build a
# completion checklist (commit / push / test / deploy hints) and always
# allow the stop, attaching the checklist as an advisory message.
#
# The hook does NOT block. Repo state (uncommitted files, ahead-of-
# remote count) is shared across every Claude session running in the
# same working tree, so blocking on it traps parallel sessions behind
# work they did not author. We surface the checklist as a nudge to
# commit/push *your own* in-flight work, but never force it.
#
# Fail-open: any error allows the stop. The hook never wedges the user.

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io  # noqa: E402
from _lib.checklist import (  # noqa: E402
    build_checklist,
    detect_repo,
    format_checklist,
)


def main() -> int:
    inv = _io.parse_stdin(__file__)

    if inv.stop_hook_active:
        return _io.emit_continue_with_message()

    try:
        cwd = inv.cwd if inv.cwd.is_dir() else Path.cwd()
        repo_name, repo_path = detect_repo(cwd)
        if not repo_name or not repo_path:
            return _io.emit_continue_with_message()

        data = build_checklist(repo_name, repo_path)

        non_done = [i for i in data["items"] if i["status"] != "done"]
        if not non_done:
            return _io.emit_continue_with_message(
                f"[session-end] {repo_name} ({data['repo_type']}): all clear."
            )

        return _io.emit_continue_with_message(format_checklist(data))
    except Exception as exc:
        _io.log(inv, f"stop-check error (failing open): {exc}", level="error")
        return _io.emit_continue_with_message()


if __name__ == "__main__":
    sys.exit(main())
