"""Tests for the quality_report tool: log loading, pairing, agreement metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

# quality_report lives one level above _lib (in scripts/, not scripts/_lib/).
_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "plan-review-loop"
    / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import quality_report as qr  # noqa: E402


def _write_log(
    log_dir: Path,
    *,
    name: str,
    provider: str,
    plan_path: str,
    iteration: int,
    findings: list[dict],
    shadow: bool = False,
    primary_provider: str | None = None,
    elapsed: float = 1.0,
    error: str | None = None,
    returncode: int = 0,
    timestamp: str = "2026-05-03T12:00:00+0000",
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / name
    p.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "provider": provider,
                "plan_path": plan_path,
                "iteration": iteration,
                "findings": findings,
                "findings_count": len(findings),
                "elapsed_seconds": elapsed,
                "returncode": returncode,
                "error": error,
                "shadow": shadow,
                "primary_provider": primary_provider,
            }
        )
    )
    return p


class TestCycleLog:
    def test_blocking_titles_only_p0_p1(self, tmp_path: Path) -> None:
        p = _write_log(
            tmp_path,
            name="x.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[
                {"severity": "P0", "title": "Crit one"},
                {"severity": "P1", "title": "High two"},
                {"severity": "P2", "title": "Should be ignored"},
            ],
        )
        log = qr._load_cycle_log(p)
        assert log is not None
        assert log.blocking_titles == {"crit one", "high two"}
        assert log.has_blocking is True

    def test_no_blocking_when_only_p2(self, tmp_path: Path) -> None:
        p = _write_log(
            tmp_path,
            name="x.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P2", "title": "advisory"}],
        )
        log = qr._load_cycle_log(p)
        assert log is not None
        assert log.has_blocking is False

    def test_malformed_log_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert qr._load_cycle_log(p) is None


class TestSummarize:
    def test_collects_per_provider_stats(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="a.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "x"}],
            elapsed=10.0,
        )
        _write_log(
            tmp_path,
            name="b.json",
            provider="codex",
            plan_path="/p.md",
            iteration=2,
            findings=[],
            elapsed=20.0,
        )
        _write_log(
            tmp_path,
            name="c.json",
            provider="local",
            plan_path="/p.md",
            iteration=1,
            findings=[],
            elapsed=5.0,
            error="boom",
            returncode=1,
        )
        logs = qr._load_logs(tmp_path)
        s = qr._summarize_providers(logs)
        assert s["codex"].runs == 2
        assert s["codex"].mean_elapsed == 15.0
        assert s["codex"].block_rate == 0.5
        assert s["codex"].errors == 0
        assert s["local"].errors == 1
        assert s["local"].error_rate == 1.0


class TestJaccard:
    def test_identical_sets_score_1(self) -> None:
        assert qr._jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets_score_0(self) -> None:
        assert qr._jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self) -> None:
        # |intersect| = 1 (a), |union| = 3 (a,b,c) => 1/3
        assert qr._jaccard({"a", "b"}, {"a", "c"}) == pytest.approx(1 / 3)

    def test_two_empty_sets_score_1(self) -> None:
        # Both clean = perfect agreement
        assert qr._jaccard(set(), set()) == 1.0


class TestPairLogs:
    def test_pairs_primary_with_shadow_by_plan_iteration(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="primary.json",
            provider="codex",
            plan_path="/the-plan.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "Crit"}],
        )
        _write_log(
            tmp_path,
            name="shadow.json",
            provider="local",
            plan_path="/the-plan.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "Crit"}],
            shadow=True,
            primary_provider="codex",
        )
        logs = qr._load_logs(tmp_path)
        pairs = qr._pair_logs(logs)
        assert len(pairs) == 1
        p = pairs[0]
        assert p.primary.provider == "codex"
        assert p.shadow.provider == "local"
        assert p.decision_match is True
        assert p.jaccard == 1.0

    def test_decision_disagreement_detected(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="primary.json",
            provider="codex",
            plan_path="/plan.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "Crit"}],
        )
        _write_log(
            tmp_path,
            name="shadow.json",
            provider="local",
            plan_path="/plan.md",
            iteration=1,
            findings=[],  # shadow says clean
            shadow=True,
            primary_provider="codex",
        )
        pairs = qr._pair_logs(qr._load_logs(tmp_path))
        assert pairs[0].decision_match is False
        assert pairs[0].jaccard == 0.0

    def test_only_blocking_findings_count_for_jaccard(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="p.json",
            provider="codex",
            plan_path="/x.md",
            iteration=1,
            findings=[
                {"severity": "P0", "title": "Same"},
                {"severity": "P2", "title": "Different P2 should not affect"},
            ],
        )
        _write_log(
            tmp_path,
            name="s.json",
            provider="local",
            plan_path="/x.md",
            iteration=1,
            findings=[
                {"severity": "P0", "title": "Same"},
                {"severity": "P2", "title": "Another different P2"},
            ],
            shadow=True,
            primary_provider="codex",
        )
        pairs = qr._pair_logs(qr._load_logs(tmp_path))
        assert pairs[0].jaccard == 1.0

    def test_skips_when_no_shadow(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="primary.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "x"}],
        )
        pairs = qr._pair_logs(qr._load_logs(tmp_path))
        assert pairs == []

    def test_primary_provider_pointer_used_when_multiple_primaries(
        self, tmp_path: Path
    ) -> None:
        # Both codex and gemini ran (codex won then gemini was a fallback),
        # then a local shadow ran with primary_provider=codex. The pair
        # should bind to codex, not gemini.
        _write_log(
            tmp_path,
            name="codex.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "codex-finding"}],
        )
        _write_log(
            tmp_path,
            name="gemini.json",
            provider="gemini",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "gemini-finding"}],
        )
        _write_log(
            tmp_path,
            name="shadow.json",
            provider="local",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "codex-finding"}],
            shadow=True,
            primary_provider="codex",
        )
        pairs = qr._pair_logs(qr._load_logs(tmp_path))
        assert len(pairs) == 1
        assert pairs[0].primary.provider == "codex"
        assert pairs[0].jaccard == 1.0


class TestFilters:
    def test_plan_glob(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="a.json",
            provider="codex",
            plan_path="/plans/hotspot.md",
            iteration=1,
            findings=[],
        )
        _write_log(
            tmp_path,
            name="b.json",
            provider="codex",
            plan_path="/plans/other.md",
            iteration=1,
            findings=[],
        )
        logs = qr._load_logs(tmp_path, plan_glob="*hotspot*")
        assert len(logs) == 1
        assert "hotspot" in logs[0].plan_path

    def test_provider_filter(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="a.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[],
        )
        _write_log(
            tmp_path,
            name="b.json",
            provider="local",
            plan_path="/p.md",
            iteration=1,
            findings=[],
        )
        logs = qr._load_logs(tmp_path, provider_filter="local")
        assert len(logs) == 1
        assert logs[0].provider == "local"


class TestRendering:
    def test_text_includes_provider_table(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="a.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[],
        )
        logs = qr._load_logs(tmp_path)
        summaries = qr._summarize_providers(logs)
        pairs = qr._pair_logs(logs)
        args = argparse.Namespace(
            since=None, plan=None, provider=None, show_disagreements=False, limit=20
        )
        out = qr._render_text(summaries, pairs, log_dir=tmp_path, args=args)
        assert "codex" in out
        assert "Per-provider" in out

    def test_json_output_round_trips(self, tmp_path: Path) -> None:
        _write_log(
            tmp_path,
            name="primary.json",
            provider="codex",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "x"}],
        )
        _write_log(
            tmp_path,
            name="shadow.json",
            provider="local",
            plan_path="/p.md",
            iteration=1,
            findings=[{"severity": "P0", "title": "x"}],
            shadow=True,
            primary_provider="codex",
        )
        logs = qr._load_logs(tmp_path)
        out = qr._render_json(qr._summarize_providers(logs), qr._pair_logs(logs))
        parsed = json.loads(out)
        assert {p["provider"] for p in parsed["providers"]} == {"codex", "local"}
        assert len(parsed["pairs"]) == 1
        assert parsed["pairs"][0]["decision_match"] is True
