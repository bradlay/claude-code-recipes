# quota-advisory

Stop hook that surfaces a one-line advisory when the active usage-tracking
block (queried via `ccusage blocks --active --json`, or a compatible CLI)
crosses a high-water mark, nudging you to run `/compact` to free context
budget.

- **Never blocks.** The advisory is a cosmetic prefix line; on any error or
  timeout it is simply omitted and the stop proceeds.
- **Cached and budget-capped.** The usage query is cached for 5 minutes and
  only re-run when the hook's own timeout budget can afford it, so a slow
  or missing usage CLI can never eat into the Stop hook's overall budget. A
  failed or unaffordable refresh is negative-cached so it isn't retried on
  every single Stop.
- **Threshold**: `COMPACT_THRESHOLD_PCT` in `scripts/_lib/quota_advisory.py`
  (default 70%).

## Requirements

An installed `ccusage`-compatible CLI on `PATH` that supports
`blocks --active --json` and reports `blocks[0].projection.remainingMinutes`.
Without it, the hook is a no-op (advisory never appears).
