"""Unit tests for the Stop-hook cycle-break fingerprint state."""

from __future__ import annotations

import pytest
from _lib import stop_check_cycle as cycle


class TestBlockerFingerprint:
    def test_order_independent(self) -> None:
        a = [{"label": "x", "detail": "1"}, {"label": "y", "detail": "2"}]
        b = [{"label": "y", "detail": "2"}, {"label": "x", "detail": "1"}]
        assert cycle.blocker_fingerprint(a) == cycle.blocker_fingerprint(b)

    def test_different_blockers_different_fingerprint(self) -> None:
        a = [{"label": "x", "detail": "1"}]
        b = [{"label": "x", "detail": "2"}]
        assert cycle.blocker_fingerprint(a) != cycle.blocker_fingerprint(b)


class TestRecordStrike:
    def test_first_call_is_strike_one(self, tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        assert cycle.record_strike("repo", "fp-a") == 1

    def test_same_fingerprint_increments(
        self, tmp_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        cycle.record_strike("repo", "fp-a")
        cycle.record_strike("repo", "fp-a")
        assert cycle.record_strike("repo", "fp-a") == 3

    def test_reaching_threshold_prevents_repeat_block(
        self, tmp_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This is the core failure mode the module exists to prevent: a
        # Stop hook that blocks on the same fingerprint forever. After
        # CYCLE_BREAK_THRESHOLD strikes the caller downgrades to advisory.
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        strikes = 0
        for _ in range(cycle.CYCLE_BREAK_THRESHOLD):
            strikes = cycle.record_strike("repo", "fp-loop")
        assert strikes >= cycle.CYCLE_BREAK_THRESHOLD

    def test_different_fingerprint_resets_strikes(
        self, tmp_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        cycle.record_strike("repo", "fp-a")
        cycle.record_strike("repo", "fp-a")
        assert cycle.record_strike("repo", "fp-b") == 1

    def test_stale_strike_resets_after_window(
        self, tmp_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        cycle.record_strike("repo", "fp-a")
        state_file = cycle._state_dir() / "repo.json"
        import json
        import time

        raw = json.loads(state_file.read_text())
        raw["last_at"] = time.time() - cycle.RESET_AFTER_SECONDS - 1
        state_file.write_text(json.dumps(raw))
        assert cycle.record_strike("repo", "fp-a") == 1

    def test_unwritable_state_dir_degrades_to_fresh_strike(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> "cycle.Path":
            raise OSError("no")

        monkeypatch.setattr(cycle, "_state_dir", _boom)
        # record_strike must not raise even if state can't be located/persisted.
        assert cycle.record_strike("repo", "fp-a") == 1
