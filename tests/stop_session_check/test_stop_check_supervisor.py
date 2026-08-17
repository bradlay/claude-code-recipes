"""Unit tests for the shared-deadline / process-group-safe subprocess
supervisor."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from _lib import stop_check_supervisor as sup


class TestBudget:
    def test_start_gives_full_remaining(self) -> None:
        b = sup.Budget.start(5.0)
        assert 4.9 <= b.remaining() <= 5.0

    def test_expired_true_within_margin(self) -> None:
        b = sup.Budget.start(0.1)
        time.sleep(0.15)
        assert b.expired() is True

    def test_allot_caps_by_remaining(self) -> None:
        b = sup.Budget.start(1.0)
        assert b.allot(10.0) <= 1.0

    def test_from_env_stale_deadline_falls_back_to_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sup.ENV_DEADLINE_TS, "1")  # long in the past
        b = sup.Budget.from_env(3.0)
        assert b.remaining() > 2.0

    def test_from_env_adopts_future_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        future = time.time() + 42
        monkeypatch.setenv(sup.ENV_DEADLINE_TS, str(future))
        b = sup.Budget.from_env(1.0)
        assert 40 < b.remaining() <= 42


class TestPhaseTimer:
    def test_records_phase_duration(self) -> None:
        t = sup.PhaseTimer()
        with t.phase("x"):
            time.sleep(0.01)
        assert t.timings["x"] >= 10

    def test_skip_records_zero(self) -> None:
        t = sup.PhaseTimer()
        t.skip("y")
        assert t.timings["y"] == 0
        assert "y" in t.skipped


class TestLogEvent:
    def test_writes_structured_jsonl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        sup.log_event("test_event", foo="bar")
        log_path = tmp_path / "hooks" / "stop-check.jsonl"
        rec = json.loads(log_path.read_text().splitlines()[-1])
        assert rec["event"] == "test_event"
        assert rec["foo"] == "bar"

    def test_never_raises_on_unwritable_log_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Path:
            raise OSError("no")

        monkeypatch.setattr(sup.paths, "hooks_log_dir", _boom)
        sup.log_event("test_event")  # must not raise


class TestSupervisedRun:
    def test_zero_budget_is_skipped_not_run(self) -> None:
        result = sup.supervised_run([sys.executable, "-c", "print(1)"], timeout=0, phase="x")
        assert result.skipped is True

    def test_fast_command_completes(self) -> None:
        result = sup.supervised_run(
            [sys.executable, "-c", "print('hi')"], timeout=5.0, phase="x", log=False
        )
        assert result.ok
        assert "hi" in result.stdout

    def test_deadline_exceeded_kills_the_whole_group(self) -> None:
        # A child that spawns a grandchild sleeping longer than the child's
        # own timeout: the whole group must be terminated, not just the
        # direct child, and the caller must get a `timed_out` result back
        # rather than hanging past the deadline.
        script = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)"
        )
        start = time.monotonic()
        result = sup.supervised_run(
            [sys.executable, "-c", script], timeout=0.5, phase="x", log=False
        )
        elapsed = time.monotonic() - start
        assert result.timed_out is True
        # Bounded by timeout + kill_grace + drain, well under the 30s sleep.
        assert elapsed < 10.0
        assert result.cleanup is not None
        assert result.cleanup.pgid_safe is True

    def test_spawn_failure_reported_not_raised(self) -> None:
        result = sup.supervised_run(
            ["/no/such/executable-xyz"], timeout=1.0, phase="x", log=False
        )
        assert result.spawn_failed is True
