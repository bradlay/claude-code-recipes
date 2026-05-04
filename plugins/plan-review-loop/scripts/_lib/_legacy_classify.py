"""Read-side classification for cycle-log records that lack the
post-A.0 `result_status` field.

The pre-A.0 writer wrote `error`, `returncode`, `stdout`, `findings`,
etc. but no explicit semantic status. Reparsing stored `stdout` with
the new `_parse_response_json` rules gives us an unambiguous answer
in the common case (full-content logs). When `stdout` is absent
(METADATA_ONLY mode), we refuse to guess: the old writer collapsed
`findings: []` to `None`, so `findings_count == 0` does NOT
distinguish a clean review from an unparseable one.

Used by `quality_report.CycleLog.result_status` and any future
consumer that needs a single canonical legacy classifier.
"""

from __future__ import annotations

from typing import Any


def classify_legacy_record(data: dict[str, Any]) -> str:
    """Return result_status ∈ {ok, error, empty, unparseable, unknown}
    for a record that lacks an explicit `result_status` field."""
    error = data.get("error")
    rc = data.get("returncode")
    if error or (rc is not None and rc != 0):
        return "error"

    stdout_size = data.get("stdout_size")
    if stdout_size == 0:
        return "empty"

    raw_stdout = data.get("stdout")
    if isinstance(raw_stdout, str) and raw_stdout:
        # Lazy import to avoid import-cycle with chain.py at module
        # load (chain.py reaches into this module from _save_cycle_log;
        # importing chain at module load creates a cycle).
        from .chain import _parse_response_json  # noqa: PLC0415

        parse = _parse_response_json(raw_stdout)
        if parse.parse_ok:
            return "ok"
        return "unparseable"

    if isinstance(raw_stdout, str) and not raw_stdout.strip():
        # Explicit empty-string stdout that didn't get caught by
        # stdout_size==0 (e.g. older record without that field).
        return "empty"

    # No raw stdout available (METADATA_ONLY or older schema). We
    # refuse to infer from `findings_count` alone because the old
    # writer collapsed `findings: []` to None — `count == 0` is
    # ambiguous between "clean review" and "unparseable".
    return "unknown"
