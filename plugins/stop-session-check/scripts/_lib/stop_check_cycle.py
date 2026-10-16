# Cycle-break state for a blocking Stop hook.
#
# Prevents the infinite loop: Stop fires and blocks -> the model responds
# -> Stop fires again with the same blocker set -> ... Each Stop call
# records a fingerprint of the current blocker set. After
# CYCLE_BREAK_THRESHOLD consecutive fires with the same fingerprint the
# caller should downgrade a `block` decision to an advisory message so the
# host CLI can exit instead of looping forever on an unresolvable blocker.

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from _lib import paths

CYCLE_BREAK_THRESHOLD = 3
RESET_AFTER_SECONDS = 1800  # 30 min


def _state_dir() -> Path:
    return paths.data_dir() / "stop-check-cycle"


def blocker_fingerprint(blocking_items: list[dict[str, Any]]) -> str:
    """Stable hash of the current blocker set (sorted so order doesn't
    matter)."""
    keys = sorted(f"{i.get('label', '')}|{i.get('detail', '')}" for i in blocking_items)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def record_strike(repo_name: str, fingerprint: str) -> int:
    """Increment (or reset) the strike count for this repo+fingerprint.

    Returns the count after this call.
    State JSON: {"fingerprint": "<hex>", "strikes": <int>, "last_at": <epoch>}
    """
    now = time.time()
    try:
        state_dir = _state_dir()
        path = state_dir / f"{repo_name}.json"
    except OSError:
        return 1  # state location unavailable — every Stop is a fresh strike

    prev_fingerprint = ""
    prev_strikes = 0
    prev_last_at = 0.0

    if path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                prev_fingerprint = str(raw.get("fingerprint", "") or "")
                try:
                    prev_strikes = int(raw.get("strikes", 0) or 0)
                except (TypeError, ValueError):
                    prev_strikes = 0
                try:
                    prev_last_at = float(raw.get("last_at", 0) or 0)
                except (TypeError, ValueError):
                    prev_last_at = 0.0

    if prev_fingerprint == fingerprint and now - prev_last_at < RESET_AFTER_SECONDS:
        strikes = prev_strikes + 1
    else:
        strikes = 1

    # Best-effort: if the state cannot be persisted, every Stop is a fresh strike.
    with contextlib.suppress(OSError):
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fingerprint": fingerprint, "strikes": strikes, "last_at": now})
        )

    return strikes


__all__ = ["CYCLE_BREAK_THRESHOLD", "RESET_AFTER_SECONDS", "blocker_fingerprint", "record_strike"]
