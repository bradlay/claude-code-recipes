"""Unit tests for the runner's lock and state machinery.

The full review_plan() flow shells out to provider CLIs which we don't
exercise in unit tests; those run in the CI smoke job. These tests
cover the deterministic, in-process bits: lock keying, sentinel
detection, atomic writes, and state file naming.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Generator
from pathlib import Path
from unittest import mock

import pytest
from _lib import paths, runner


@pytest.fixture
def tmp_data(tmp_path: Path) -> Generator[Path, None, None]:
    """Point CLAUDE_PLUGIN_DATA at a tmp dir for the duration of the test."""
    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(tmp_path)}):
        yield tmp_path


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    p = tmp_path / "plan.md"
    p.write_text("# Tiny plan with at least one hundred chars padded out " * 4)
    return p


class TestKeying:
    def test_lock_key_is_deterministic(self, plan_file: Path) -> None:
        k1 = runner._path_lock_key(plan_file)
        k2 = runner._path_lock_key(plan_file)
        assert k1 == k2

    def test_lock_key_independent_of_content(self, plan_file: Path) -> None:
        original_key = runner._path_lock_key(plan_file)
        plan_file.write_text("# different content " * 20)
        new_key = runner._path_lock_key(plan_file)
        assert original_key == new_key, "lock key must depend on path only"

    def test_state_key_changes_with_content(self, plan_file: Path) -> None:
        sk1 = runner._state_key(plan_file, "hash_one")
        sk2 = runner._state_key(plan_file, "hash_two")
        assert sk1 != sk2, "state key must include content hash"

    def test_lock_path_in_state_dir(self, plan_file: Path, tmp_data: Path) -> None:
        lp = runner._lock_path(plan_file)
        assert lp.parent == paths.review_state_dir()
        assert lp.name.endswith(".lock")


class TestAtomicWrite:
    def test_basic_write(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        runner._atomic_write(target, '{"key": "value"}')
        assert target.read_text() == '{"key": "value"}'

    def test_chmod_to_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        runner._atomic_write(target, "x")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600

    def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "out.json"
        runner._atomic_write(target, "x")
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == [], f"tempfiles remain: {leftover}"

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "out.json"
        runner._atomic_write(target, "x")
        assert target.exists()


class TestPidLiveness:
    def test_self_pid_is_alive(self) -> None:
        assert runner._is_pid_alive(os.getpid()) is True

    def test_almost_certainly_dead_pid(self) -> None:
        # PID 1 always exists on linux; pick a high pid that won't
        # collide with anything reasonable.
        # We can't reliably assert "dead" without forking, but the
        # function should return False (or True only for pid 1) — pick
        # a random uncommon pid and skip if it happens to exist.
        unlikely_pid = 999999
        try:
            os.kill(unlikely_pid, 0)
        except ProcessLookupError:
            assert runner._is_pid_alive(unlikely_pid) is False
        else:
            pytest.skip(f"pid {unlikely_pid} happens to be alive on this host")


class TestPathLock:
    def test_acquire_release_roundtrip(self, plan_file: Path, tmp_data: Path) -> None:
        lock = runner._PathLock(plan_file)
        assert lock.acquire() is None
        # Sentinel present while held
        assert runner._sentinel_path(plan_file).exists()
        lock.release()
        # Sentinel removed on release
        assert not runner._sentinel_path(plan_file).exists()

    def test_second_acquire_same_process_busy(self, plan_file: Path, tmp_data: Path) -> None:
        first = runner._PathLock(plan_file)
        assert first.acquire() is None
        try:
            second = runner._PathLock(plan_file)
            busy = second.acquire()
            assert busy is not None
            assert "already in progress" in busy
        finally:
            first.release()

    def test_release_after_failed_acquire_is_safe(self, plan_file: Path, tmp_data: Path) -> None:
        first = runner._PathLock(plan_file)
        assert first.acquire() is None
        try:
            second = runner._PathLock(plan_file)
            second.acquire()  # busy
            second.release()  # must not raise, must not unlink first's sentinel
            assert runner._sentinel_path(plan_file).exists()
        finally:
            first.release()

    def test_stale_sentinel_recovery(self, plan_file: Path, tmp_data: Path) -> None:
        """Sentinel for a dead pid is swept and lock is acquired."""
        # Write a sentinel claiming a dead pid; do NOT hold flock.
        sentinel = runner._sentinel_path(plan_file)
        sentinel.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        sentinel.write_text(json.dumps({"pid": 999999, "start_time": time.time() - 60}))
        # No flock held so LOCK_NB succeeds; the stale sentinel cleanup
        # runs via _is_pid_alive returning False, but we never enter the
        # BlockingIOError branch because the lock is free. Sentinel just
        # persists from the prior holder (would be cleaned on next
        # release). Test that acquire still succeeds.
        lock = runner._PathLock(plan_file)
        assert lock.acquire() is None
        lock.release()


class TestStateLifecycle:
    def test_load_default_when_absent(self, plan_file: Path, tmp_data: Path) -> None:
        state = runner._load_state(plan_file, "abc123")
        assert state["iteration"] == 0
        assert state["plan_hash"] == "abc123"
        assert state["last_review_status"] == "not_reviewed"

    def test_save_and_reload(self, plan_file: Path, tmp_data: Path) -> None:
        runner._save_state(
            plan_file,
            "abc123",
            {
                "iteration": 3,
                "previous_findings": [{"severity": "P1", "title": "t"}],
                "plan_hash": "abc123",
                "last_reviewed_at": "2026-01-01T00:00:00Z",
                "last_review_status": "blocking",
                "last_review_findings_count": 1,
            },
        )
        reloaded = runner._load_state(plan_file, "abc123")
        assert reloaded["iteration"] == 3
        assert reloaded["last_review_status"] == "blocking"

    def test_clean_state_for_plan_removes_all_content_hashes(
        self, plan_file: Path, tmp_data: Path
    ) -> None:
        runner._save_state(plan_file, "hash_a", {"iteration": 1})
        runner._save_state(plan_file, "hash_b", {"iteration": 2})
        runner._save_state(plan_file, "hash_c", {"iteration": 3})

        # All three exist
        prefix = f"_state_{runner._path_lock_key(plan_file)}_"
        before = list(paths.review_state_dir().glob(f"{prefix}*.json"))
        assert len(before) == 3

        runner._clean_state_for_plan(plan_file)
        after = list(paths.review_state_dir().glob(f"{prefix}*.json"))
        assert after == [], "clean must remove every content-hash variant"

    def test_state_file_mode_is_0600(self, plan_file: Path, tmp_data: Path) -> None:
        runner._save_state(plan_file, "abc123", {"iteration": 1})
        sp = runner._state_path(plan_file, "abc123")
        mode = sp.stat().st_mode & 0o777
        assert mode == 0o600


class TestReviewPlanShortCircuits:
    def test_too_short_returns_status_without_locking(self, tmp_path: Path, tmp_data: Path) -> None:
        plan = tmp_path / "tiny.md"
        plan.write_text("# small")  # well under 100 chars
        outcome = runner.review_plan(plan)
        assert outcome.status == "plan_too_short"
        # Lock file should not exist; runner short-circuits before locking
        assert not runner._lock_path(plan).exists()
