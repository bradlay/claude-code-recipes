"""Unit tests for the per-session backend picker state + instruction."""

from __future__ import annotations

import re

import pytest
from _lib import picker
from _lib.probes import ProbeResult


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))


class TestSessionSanitization:
    def test_session_id_is_hashed_not_raw(self) -> None:
        # A path-traversal-ish session id must not leak into the filename.
        path = picker._selection_path("../../etc/passwd")
        assert "/" not in path.name.removeprefix("backend-").removesuffix(".json")
        assert re.fullmatch(r"backend-[0-9a-f]{16}\.json", path.name)

    def test_distinct_sessions_distinct_files(self) -> None:
        assert picker._selection_path("a") != picker._selection_path("b")

    def test_same_session_stable(self) -> None:
        assert picker._selection_path("s1") == picker._selection_path("s1")

    def test_empty_session_handled(self) -> None:
        assert picker._selection_path("").name == picker._selection_path("unknown").name


class TestSelectionRoundtrip:
    def test_write_then_read(self) -> None:
        picker.write_selection("sess", "opus")
        sel = picker.read_selection("sess")
        assert sel is not None
        assert sel["backend_key"] == "opus"
        assert sel["chain"] == ["opus"]

    def test_write_normalizes_legacy_alias(self) -> None:
        picker.write_selection("sess", "claude")  # legacy -> sonnet
        sel = picker.read_selection("sess")
        assert sel is not None
        assert sel["backend_key"] == "sonnet"

    def test_write_rejects_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="unknown backend"):
            picker.write_selection("sess", "bogus")

    def test_write_accepts_local_any_env(self) -> None:
        # write validates against the full registry (the select subprocess may
        # not see LOCAL_URL); the offer gate lives in the picker, and an
        # unreachable local pick self-heals at the pre-review re-probe.
        picker.write_selection("sess", "local")
        sel = picker.read_selection("sess")
        assert sel is not None
        assert sel["backend_key"] == "local"

    def test_read_rejects_stale_key(self) -> None:
        picker._atomic_write(picker._selection_path("sess"), {"backend_key": "removed"})
        assert picker.read_selection("sess") is None

    def test_no_selection_returns_none(self) -> None:
        assert picker.read_selection("never-set") is None

    def test_clear(self) -> None:
        picker.write_selection("sess", "codex")
        picker.clear_selection("sess")
        assert picker.read_selection("sess") is None


class TestAttempts:
    def test_record_attempt_increments(self) -> None:
        assert picker.record_attempt("sess") == 1
        assert picker.record_attempt("sess") == 2

    def test_attempt_then_select_resets(self) -> None:
        picker.record_attempt("sess")
        picker.write_selection("sess", "opus")
        sel = picker.read_selection("sess")
        assert sel is not None
        assert sel["attempts"] == 0


class TestInstruction:
    def test_instruction_is_self_contained(self) -> None:
        available = [
            ProbeResult(name="opus", ok=True, model="claude-opus-4-8", last_probed=0.0),
            ProbeResult(name="codex", ok=True, model="gpt-5.5", last_probed=0.0),
        ]
        text = picker.build_picker_instruction(
            "sess-123",
            available,
            select_bin="/abs/bin/plan-review-select",
            data_dir="/abs/data",
        )
        # Lists only the verified backends.
        assert "opus" in text and "codex" in text
        assert "sonnet" not in text and "gemini" not in text
        # Self-contained command: absolute bin + explicit data dir + session.
        assert "CLAUDE_PLUGIN_DATA=/abs/data" in text
        assert "/abs/bin/plan-review-select" in text
        assert "--session sess-123" in text
        assert "AskUserQuestion" in text


def test_write_accepts_local_when_url_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_PLAN_REVIEW_LOCAL_URL", "http://x:8010")
    picker.write_selection("sess", "local")
    sel = picker.read_selection("sess")
    assert sel is not None
    assert sel["backend_key"] == "local"


def test_write_accepts_local_without_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Accepted even without LOCAL_URL: reachability is checked at review time,
    # not at selection time (the select subprocess may not inherit the env).
    monkeypatch.delenv("CLAUDE_PLAN_REVIEW_LOCAL_URL", raising=False)
    picker.write_selection("sess", "local")
    sel = picker.read_selection("sess")
    assert sel is not None and sel["backend_key"] == "local"


def test_instruction_notes_local_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    available = [
        ProbeResult(name="local", ok=True, model="qwen", last_probed=0.0),
        ProbeResult(name="opus", ok=True, model="claude-opus-4-8", last_probed=0.0),
    ]
    text = picker.build_picker_instruction("s", available, select_bin="/b", data_dir="/d")
    assert "local" in text
    assert "default" in text.lower()
