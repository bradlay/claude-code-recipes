#!/usr/bin/env python3
# Stop hook entry point: when the session is about to end, build a
# completion checklist (commit / push / test / deploy hints) and either
# allow the stop with a summary message OR block with a reason if there
# are blocking items (uncommitted changes, unpushed commits).
#
# Fail-open: any error allows the stop. The hook never wedges the user.
#
# stop_hook_active: when this hook fires recursively (we already
# blocked once), we allow the stop unconditionally so the user is never
# trapped.

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

    # If we already blocked once and the host CLI is asking again, let
    # the session stop. Avoids wedging the user behind a recursive
    # block.
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

        message = format_checklist(data)
        blocking = [i for i in data["items"] if i["status"] == "todo"]
        if blocking:
            return _io.emit_block(message)
        return _io.emit_continue_with_message(message)
    except Exception as exc:
        _io.log(inv, f"stop-check error (failing open): {exc}", level="error")
        return _io.emit_continue_with_message()


if __name__ == "__main__":
    sys.exit(main())
