# Quota-aware advisory text for the Stop hook.
#
# When the active usage-tracking block crosses a high-water mark, the Stop
# hook prepends an advisory line nudging you to run `/compact` to free
# context budget. Best-effort: if the usage CLI isn't available (or is too
# slow for the run's remaining budget), returns None and the caller
# suppresses the line.
#
# WHY THIS IS BUDGETED AND CACHED
# --------------------------------
# `ccusage blocks --active --json` (the default usage CLI this queries) can
# take several seconds — it rescans the whole transcript tree on every
# call. A synchronous, unbounded call for a single optional, cosmetic
# advisory line risks consuming a large share of a hook's own timeout
# budget.
#
# So: the value is CACHED (a multi-hour usage block does not move
# meaningfully minute to minute) and the refresh is BUDGET-CAPPED — the
# caller passes what it can afford, and a refresh that doesn't fit is
# skipped, not run-and-hoped.
#
# HONEST LIMIT: because the refresh only runs when the remaining budget
# covers it, a Stop run on a machine where the usage CLI is slow will NOT
# refresh a cold cache — the advisory is simply absent there. That is the
# correct trade (a cosmetic nudge must never break the Stop gate).

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from _lib import paths

# Emit the advisory once block usage crosses this percentage.
COMPACT_THRESHOLD_PCT = 70

# Usage block length in minutes (ccusage blocks `endTime - startTime`).
BLOCK_TOTAL_MINUTES = 5 * 60

CACHE_TTL_S = 300.0  # a multi-hour block is unchanged, for advisory purposes, in 5 min

# Default advisory budget (s): deliberately small — the advisory is a
# cosmetic prefix line and must never jeopardise the Stop gate it rides
# along with.
DEFAULT_ADVISORY_DEADLINE_S = 2.0

DEFAULT_USAGE_CMD = ("ccusage", "blocks", "--active", "--json")

_MISS = object()  # cache absent/stale -> a refresh may be attempted
_UNAVAILABLE = object()  # negative cache -> the usage CLI recently failed; do NOT retry


def _read_cache(cache_path: Path) -> float | object:
    """Cached block-usage %, `_UNAVAILABLE` (negative cache), or `_MISS`.

    The negative entry matters: a refresh that cannot fit the budget costs
    real time to discover (a full subprocess spawn attempt). Without it,
    EVERY Stop run would pay that cost to learn the same thing. With it,
    at most one run per TTL does.
    """
    try:
        raw = json.loads(cache_path.read_text())
        if time.time() - float(raw["at"]) > CACHE_TTL_S:
            return _MISS
        pct = raw["pct"]
        return _UNAVAILABLE if pct is None else float(pct)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _MISS


def _write_cache(cache_path: Path, pct: float | None) -> None:
    """Cache a value, or `None` (the negative entry meaning "don't retry
    yet")."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"pct": pct, "at": time.time()}))
    except OSError:  # best-effort - a cold cache just means no advisory
        pass


def _parse_pct(stdout: str) -> float | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    blocks = data.get("blocks") or []
    if not blocks:
        return None
    proj = blocks[0].get("projection") or {}
    remaining = proj.get("remainingMinutes")
    if remaining is None:
        return None
    try:
        consumed = max(0.0, BLOCK_TOTAL_MINUTES - float(remaining))
    except (TypeError, ValueError):
        return None
    return min(100.0, 100.0 * consumed / BLOCK_TOTAL_MINUTES)


def block_usage_pct(
    timeout: float | None = None,
    usage_cmd: tuple[str, ...] = DEFAULT_USAGE_CMD,
    cache_path: Path | None = None,
) -> float | None:
    """% of the active usage block consumed, or None when unavailable.

    Serves a fresh cached value for free. Refreshes only when `timeout`
    (the caller's affordable budget) is enough to run the usage CLI —
    otherwise returns None rather than blowing the caller's budget.
    """
    cache_path = cache_path or paths.cache_file()
    cached = _read_cache(cache_path)
    if cached is _UNAVAILABLE:
        return None
    if isinstance(cached, float):
        return cached

    budget = DEFAULT_ADVISORY_DEADLINE_S if timeout is None else timeout
    if budget <= 0:
        return None

    try:
        result = subprocess.run(  # noqa: S603 (fixed argv, never a shell string)
            list(usage_cmd),
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _write_cache(cache_path, None)  # negative cache: don't re-pay this cost every Stop
        return None

    if result.returncode != 0:
        _write_cache(cache_path, None)
        return None

    pct = _parse_pct(result.stdout)
    _write_cache(cache_path, pct)  # a None here is also negative-cached (unparseable output)
    return pct


def compact_advisory_text(
    timeout: float | None = None,
    usage_cmd: tuple[str, ...] = DEFAULT_USAGE_CMD,
    cache_path: Path | None = None,
) -> str | None:
    """The advisory text if the block is over the compact threshold, else
    None."""
    pct = block_usage_pct(timeout, usage_cmd, cache_path)
    if pct is None or pct < COMPACT_THRESHOLD_PCT:
        return None
    return (
        f"[quota-advisory] Active usage block is ~{int(pct)}% consumed. "
        "Consider running /compact to free context budget."
    )


__all__ = [
    "BLOCK_TOTAL_MINUTES",
    "COMPACT_THRESHOLD_PCT",
    "CACHE_TTL_S",
    "DEFAULT_ADVISORY_DEADLINE_S",
    "DEFAULT_USAGE_CMD",
    "block_usage_pct",
    "compact_advisory_text",
]
