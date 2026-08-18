"""Unit tests for the budget-capped, cached quota advisory."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from _lib import quota_advisory as qa


class TestParsePct:
    def test_parses_remaining_minutes(self) -> None:
        stdout = json.dumps({"blocks": [{"projection": {"remainingMinutes": 60}}]})
        pct = qa._parse_pct(stdout)
        assert pct is not None
        assert 79 < pct < 81  # (300-60)/300 = 80%

    def test_empty_blocks_returns_none(self) -> None:
        assert qa._parse_pct(json.dumps({"blocks": []})) is None

    def test_malformed_json_returns_none(self) -> None:
        assert qa._parse_pct("not json") is None

    def test_missing_remaining_minutes_returns_none(self) -> None:
        assert qa._parse_pct(json.dumps({"blocks": [{"projection": {}}]})) is None


class TestCache:
    def test_fresh_cache_served_without_subprocess(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"pct": 42.0, "at": time.time()}))
        # A command that would fail loudly if actually invoked.
        pct = qa.block_usage_pct(
            timeout=5.0, usage_cmd=("/no/such/binary",), cache_path=cache_path
        )
        assert pct == 42.0

    def test_negative_cache_returns_none_without_retry(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"pct": None, "at": time.time()}))
        pct = qa.block_usage_pct(
            timeout=5.0, usage_cmd=("/no/such/binary",), cache_path=cache_path
        )
        assert pct is None

    def test_stale_cache_triggers_refresh(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"pct": 42.0, "at": time.time() - qa.CACHE_TTL_S - 1}))
        script = "import json,sys; print(json.dumps({'blocks':[{'projection':{'remainingMinutes':30}}]}))"
        pct = qa.block_usage_pct(
            timeout=5.0, usage_cmd=(sys.executable, "-c", script), cache_path=cache_path
        )
        assert pct is not None
        assert pct > 89  # (300-30)/300 = 90%


class TestBudgetExhausted:
    def test_zero_budget_returns_none_without_running(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        pct = qa.block_usage_pct(
            timeout=0.0, usage_cmd=("/no/such/binary",), cache_path=cache_path
        )
        assert pct is None

    def test_a_slow_usage_cli_times_out_and_negative_caches(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        script = "import time; time.sleep(5)"
        pct = qa.block_usage_pct(
            timeout=0.2, usage_cmd=(sys.executable, "-c", script), cache_path=cache_path
        )
        assert pct is None
        raw = json.loads(cache_path.read_text())
        assert raw["pct"] is None  # negative-cached so a later Stop doesn't retry immediately

    def test_missing_binary_is_reported_not_raised(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        pct = qa.block_usage_pct(
            timeout=5.0, usage_cmd=("/no/such/binary-xyz",), cache_path=cache_path
        )
        assert pct is None


class TestCompactAdvisoryText:
    def test_below_threshold_returns_none(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"pct": 10.0, "at": time.time()}))
        assert qa.compact_advisory_text(cache_path=cache_path) is None

    def test_above_threshold_returns_advisory_text(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"pct": 85.0, "at": time.time()}))
        text = qa.compact_advisory_text(cache_path=cache_path)
        assert text is not None
        assert "85" in text
        assert "/compact" in text
