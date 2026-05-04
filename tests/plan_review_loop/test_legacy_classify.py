"""Tests for the legacy-record classifier.

Pre-A.0 cycle logs lack the explicit `result_status` field. The
shared classifier reparses stored `stdout` with the new parser
rules so the historical broken-shadow window stays visible to
operators (instead of disappearing into `unknown`).
"""

from __future__ import annotations

from typing import Any

from _lib._legacy_classify import classify_legacy_record


def test_error_field_set() -> None:
    data: dict[str, Any] = {"error": "boom", "returncode": 0}
    assert classify_legacy_record(data) == "error"


def test_returncode_nonzero() -> None:
    data: dict[str, Any] = {"returncode": 1, "error": None}
    assert classify_legacy_record(data) == "error"


def test_zero_stdout_size() -> None:
    data: dict[str, Any] = {"returncode": 0, "stdout_size": 0}
    assert classify_legacy_record(data) == "empty"


def test_stdout_clean_review_classified_ok() -> None:
    # The bug fix in action: pre-A.0 records with `findings: []`
    # collapsed to `findings: null` on disk. Reparsing the raw
    # stored stdout recovers the truth.
    data: dict[str, Any] = {
        "returncode": 0,
        "stdout": '{"findings": [], "questions": []}',
        "stdout_size": 30,
    }
    assert classify_legacy_record(data) == "ok"


def test_stdout_findings_list_classified_ok() -> None:
    data: dict[str, Any] = {
        "returncode": 0,
        "stdout": '{"findings": [{"severity": "P1", "title": "x"}]}',
        "stdout_size": 50,
    }
    assert classify_legacy_record(data) == "ok"


def test_stdout_garbage_classified_unparseable() -> None:
    data: dict[str, Any] = {
        "returncode": 0,
        "stdout": "garbage that isn't JSON",
        "stdout_size": 25,
    }
    assert classify_legacy_record(data) == "unparseable"


def test_stdout_findings_null_classified_unparseable() -> None:
    # Old writer's collapse: a model that emitted `findings: null`
    # would have stored that. The new parser correctly classifies
    # it as unparseable, not ok.
    data: dict[str, Any] = {
        "returncode": 0,
        "stdout": '{"findings": null}',
        "stdout_size": 20,
    }
    assert classify_legacy_record(data) == "unparseable"


def test_metadata_only_record_classified_unknown() -> None:
    # CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY=1 strips raw stdout.
    # Without it we can't distinguish clean from unparseable.
    data: dict[str, Any] = {
        "returncode": 0,
        "findings_count": 0,
        # No stdout key, no stdout_size = 0 either.
    }
    assert classify_legacy_record(data) == "unknown"


def test_post_a0_record_passes_through() -> None:
    # If a post-A.0 record's data dict somehow gets here without
    # going through CycleLog.result_status's shortcut (e.g. direct
    # call), we still classify it from legacy fields. Result_status
    # in data is honored by the property layer above; this layer
    # treats it as legacy.
    data: dict[str, Any] = {
        "returncode": 0,
        "stdout": '{"findings": [], "questions": []}',
        "stdout_size": 30,
    }
    assert classify_legacy_record(data) == "ok"
