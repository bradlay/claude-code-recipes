#!/usr/bin/env python3
# Stop hook entry point: when a usage-tracking block crosses a high-water
# mark, surface a one-line advisory nudging /compact. Never blocks the
# stop, and any error fails open (advisory absent, stop proceeds).

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib.quota_advisory import compact_advisory_text  # noqa: E402


def _write_stdout(payload: dict[str, object]) -> None:
    with contextlib.suppress(OSError):
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()


def main() -> int:
    try:
        payload = sys.stdin.read()
    except OSError:
        payload = ""

    stop_hook_active = False
    if payload:
        with contextlib.suppress(json.JSONDecodeError):
            raw = json.loads(payload)
            if isinstance(raw, dict):
                stop_hook_active = bool(raw.get("stop_hook_active", False))

    if stop_hook_active:
        _write_stdout({})
        return 0

    try:
        text = compact_advisory_text()
    except Exception:  # noqa: BLE001 (a cosmetic advisory must never break the stop)
        text = None

    _write_stdout({"systemMessage": text} if text else {})
    return 0


if __name__ == "__main__":
    sys.exit(main())
