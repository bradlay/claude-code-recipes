#!/usr/bin/env python3
# PostToolUse(Bash) hook entry point: correct a guessed/typo'd subcommand
# of a configured CLI vendor (advisory only). Reads the PostToolUse JSON
# payload from stdin. Emits an `additionalContext` block when a matching
# invocation failed because the subcommand name was wrong; otherwise
# emits nothing. Never blocks.

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib.discovery import evaluate  # noqa: E402


def _write_stdout(payload: dict[str, object]) -> None:
    with contextlib.suppress(OSError):
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""

    payload: dict[str, object] = {}
    if raw:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed

    try:
        context = evaluate(payload)
    except Exception:  # noqa: BLE001 - fail-silent: advisory must never break
        context = None

    if not context:
        _write_stdout({})
        return 0

    _write_stdout(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
