#!/usr/bin/env python3
# PostToolUse(Bash) audit hook: append every executed Bash command to a
# local audit log under ${CLAUDE_PLUGIN_DATA}/audit.log. Always
# fail-open; the log is a convenience for "what did Claude run last
# week?", not a correctness gate.
#
# The log line is one row per command:
#   <iso-timestamp> | <session_id> | <command-summary>
#
# Command summary: first 200 chars, newlines collapsed to spaces, ellipsis
# appended if truncated.

from __future__ import annotations

import contextlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, paths  # noqa: E402

_SUMMARY_MAX = 200
_SECURE_FILE_MODE = 0o600


def main() -> int:
    inv = _io.parse_stdin(__file__)

    if inv.tool_name != "Bash":
        return _io.emit_continue()

    command = inv.tool_input.get("command", "")
    if not isinstance(command, str) or not command:
        return _io.emit_continue()

    summary = command[:_SUMMARY_MAX].replace("\n", " ").strip()
    if len(command) > _SUMMARY_MAX:
        summary += "..."

    session_id = (
        inv.session_id
        if inv.session_id != "unknown"
        else os.environ.get("CLAUDE_SESSION_ID", "unknown")
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log_path = paths.audit_log()
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = log_path.exists()
        with log_path.open("a") as f:
            f.write(f"{timestamp} | {session_id} | {summary}\n")
        if not existed:
            with contextlib.suppress(OSError):
                log_path.chmod(_SECURE_FILE_MODE)

    return _io.emit_continue()


if __name__ == "__main__":
    sys.exit(main())
