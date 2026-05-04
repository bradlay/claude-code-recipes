"""Tests for the preflight `_compute_shadow_health` helper.

Severity logic and config-signature scope filter. The fixtures
write real cycle-log files into a tmp `review-log/` dir and let
the helper read them through the public `_load_logs` path so the
filter contract (skip out-of-signature, skip `result_status:
unknown`) is verified end-to-end rather than mocked.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest import mock

import preflight


def _write_record(
    log_dir: Path,
    *,
    name: str,
    result_status: str,
    signature: str,
    age_seconds: float = 60.0,
    shadow: bool = True,
) -> None:
    """Write a minimal cycle-log record at the given age (seconds before now)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    # ISO timestamp drives event_time_epoch; mtime drives _load_logs's
    # since_days filter. Set both to the same point in the past.
    ts = time.time() - age_seconds
    record: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
        "provider": "local",
        "elapsed_seconds": 1.0,
        "returncode": 0,
        "error": None,
        "shadow": shadow,
        "result_status": result_status,
        "shadow_config_signature": signature,
        "iteration": 1,
        "plan_path": "/tmp/x.md",
    }
    p = log_dir / name
    p.write_text(json.dumps(record))
    os.utime(p, (ts, ts))


class TestComputeShadowHealth:
    def test_zero_records_is_warming(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        with mock.patch.dict(os.environ, env, clear=False):
            health = preflight._compute_shadow_health()
        assert health["severity"] == "warming"
        assert health["total_24h"] == 0

    def test_all_ok_is_ok(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            for i in range(10):
                _write_record(
                    log_dir,
                    name=f"r{i}_shadow.json",
                    result_status="ok",
                    signature=sig,
                )
            health = preflight._compute_shadow_health()
        assert health["severity"] == "ok"
        assert health["total_24h"] == 10
        assert health["failed_24h"] == 0

    def test_all_failed_is_critical(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            for i in range(10):
                _write_record(
                    log_dir,
                    name=f"r{i}_shadow.json",
                    result_status="error",
                    signature=sig,
                )
            health = preflight._compute_shadow_health()
        assert health["severity"] == "critical"
        assert health["failed_24h"] == 10

    def test_high_fail_rate_below_critical_threshold_is_degraded(
        self, tmp_path: Path
    ) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            # 8 errors + 2 ok over 10 records = 80% fail rate, below
            # the 95% critical threshold (which requires >=95%).
            # Newest is ok so consecutive_failures stays at 0.
            _write_record(
                log_dir,
                name="ok0_shadow.json",
                result_status="ok",
                signature=sig,
                age_seconds=10,
            )
            _write_record(
                log_dir,
                name="ok1_shadow.json",
                result_status="ok",
                signature=sig,
                age_seconds=20,
            )
            for i in range(8):
                _write_record(
                    log_dir,
                    name=f"err{i}_shadow.json",
                    result_status="error",
                    signature=sig,
                    age_seconds=100 + i,
                )
            health = preflight._compute_shadow_health()
        assert health["severity"] == "degraded"
        assert health["failed_24h"] == 8
        assert health["consecutive_failures"] == 0

    def test_three_consecutive_failures_promotes_to_degraded(
        self, tmp_path: Path
    ) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            # 3 newest errors followed by older oks — total volume
            # under the rate-based threshold but consecutive streak
            # alone bumps severity to degraded.
            for i in range(3):
                _write_record(
                    log_dir,
                    name=f"err{i}_shadow.json",
                    result_status="error",
                    signature=sig,
                    age_seconds=10 + i,
                )
            for i in range(5):
                _write_record(
                    log_dir,
                    name=f"ok{i}_shadow.json",
                    result_status="ok",
                    signature=sig,
                    age_seconds=200 + i,
                )
            health = preflight._compute_shadow_health()
        assert health["severity"] == "degraded"
        assert health["consecutive_failures"] == 3

    def test_five_consecutive_failures_is_critical(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            for i in range(5):
                _write_record(
                    log_dir,
                    name=f"err{i}_shadow.json",
                    result_status="error",
                    signature=sig,
                    age_seconds=10 + i,
                )
            for i in range(5):
                _write_record(
                    log_dir,
                    name=f"ok{i}_shadow.json",
                    result_status="ok",
                    signature=sig,
                    age_seconds=200 + i,
                )
            health = preflight._compute_shadow_health()
        assert health["severity"] == "critical"
        assert health["consecutive_failures"] == 5

    def test_records_under_other_signature_excluded(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            # 10 errors under a stale signature — must NOT be counted.
            for i in range(10):
                _write_record(
                    log_dir,
                    name=f"old{i}_shadow.json",
                    result_status="error",
                    signature="0123456789abcdef",
                )
            # 1 ok under current signature.
            _write_record(
                log_dir,
                name="new0_shadow.json",
                result_status="ok",
                signature=sig,
            )
            health = preflight._compute_shadow_health()
        assert health["total_24h"] == 1
        assert health["failed_24h"] == 0
        # 1 < 5 so no rate threshold triggers, and consecutive streak
        # is 0 — severity stays ok.
        assert health["severity"] == "ok"

    def test_unknown_status_records_excluded(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        with mock.patch.dict(os.environ, env, clear=False):
            from _lib._shadow_signature import current_shadow_config_signature

            sig = current_shadow_config_signature()
            # Pre-A.0 records may classify as "unknown" via the legacy
            # fallback when stdout is unavailable. They MUST be excluded
            # from health, because guessing would manufacture severity.
            (log_dir).mkdir(parents=True, exist_ok=True)
            ts = time.time() - 60
            for i in range(5):
                p = log_dir / f"old{i}_shadow.json"
                p.write_text(
                    json.dumps(
                        {
                            "timestamp": time.strftime(
                                "%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)
                            ),
                            "shadow": True,
                            "shadow_config_signature": sig,
                            "returncode": 0,
                            "findings_count": 0,
                            # No result_status, no stdout/stdout_size →
                            # legacy classifier returns "unknown".
                        }
                    )
                )
                os.utime(p, (ts, ts))
            health = preflight._compute_shadow_health()
        assert health["total_24h"] == 0
        assert health["severity"] == "warming"

    def test_unavailable_when_log_dir_unreadable(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}

        def _raise(_path: Path, **_kwargs: Any) -> Any:
            raise OSError("permission denied")

        monkeypatch.setattr(preflight, "_load_logs", _raise)
        with mock.patch.dict(os.environ, env, clear=False):
            health = preflight._compute_shadow_health()
        assert health["severity"] == "unavailable"
        assert "permission denied" in health["read_error"]

    def test_signature_stable_across_calls_under_same_env(
        self, tmp_path: Path
    ) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        with mock.patch.dict(os.environ, env, clear=False):
            h1 = preflight._compute_shadow_health()
            h2 = preflight._compute_shadow_health()
        assert h1["config_signature"] == h2["config_signature"]
        assert len(h1["config_signature"]) == 16

    def test_signature_rotates_when_env_changes(self, tmp_path: Path) -> None:
        env_base = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        env_a = {**env_base, "CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS": "4096"}
        env_b = {**env_base, "CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS": "131072"}
        with mock.patch.dict(os.environ, env_a, clear=False):
            sig_a = preflight._compute_shadow_health()["config_signature"]
        with mock.patch.dict(os.environ, env_b, clear=False):
            sig_b = preflight._compute_shadow_health()["config_signature"]
        assert sig_a != sig_b


class TestFormatContextShadowHealth:
    """Renderer assertions for shadow severity branches."""

    def _base_report(self, **shadow_health: Any) -> dict[str, Any]:
        return {
            "chain": ["codex"],
            "shadow": ["local"],
            "ok": [],
            "missing": [],
            "findings": [],
            "shadow_health": shadow_health if shadow_health else None,
        }

    def test_ok_severity_renders_no_section(self) -> None:
        out = preflight._format_context(
            self._base_report(severity="ok", total_24h=10, failed_24h=0)
        )
        assert "shadow ok" not in out
        assert "shadow degraded" not in out

    def test_warming_renders_explicit_message(self) -> None:
        out = preflight._format_context(
            self._base_report(severity="warming", total_24h=0, failed_24h=0)
        )
        assert "shadow warming" in out
        assert "trigger one ExitPlanMode" in out

    def test_degraded_renders_counts_and_streak(self) -> None:
        out = preflight._format_context(
            self._base_report(
                severity="degraded",
                total_24h=10,
                failed_24h=8,
                fail_rate=0.8,
                consecutive_failures=3,
            )
        )
        assert "shadow degraded" in out
        assert "failed 8/10" in out
        assert "3 consecutive failures" in out

    def test_critical_renders_counts(self) -> None:
        out = preflight._format_context(
            self._base_report(
                severity="critical",
                total_24h=10,
                failed_24h=10,
                fail_rate=1.0,
                consecutive_failures=5,
            )
        )
        assert "shadow critical" in out
        assert "failed 10/10" in out

    def test_unavailable_renders_read_error(self) -> None:
        out = preflight._format_context(
            self._base_report(
                severity="unavailable", read_error="permission denied"
            )
        )
        assert "shadow health unavailable" in out
        assert "permission denied" in out


class TestReportSignatureShadowHealth:
    def _base_report(self, severity: str, **extra: Any) -> dict[str, Any]:
        return {
            "chain": ["codex"],
            "shadow": ["local"],
            "ok": [],
            "missing": [],
            "findings": [],
            "probes": [],
            "shadow_health": {
                "severity": severity,
                "total_24h": extra.get("total_24h", 0),
                "failed_24h": extra.get("failed_24h", 0),
                "fail_rate": extra.get("fail_rate", 0.0),
                "consecutive_failures": extra.get("consecutive_failures", 0),
                "config_signature": extra.get("config_signature", "abc"),
                "read_error": extra.get("read_error", ""),
            },
        }

    def test_severity_change_rotates_signature(self) -> None:
        sig_ok = preflight._report_signature(self._base_report("ok"))
        sig_deg = preflight._report_signature(self._base_report("degraded"))
        assert sig_ok != sig_deg

    def test_config_signature_change_rotates_signature(self) -> None:
        sig_a = preflight._report_signature(
            self._base_report("ok", config_signature="aaa")
        )
        sig_b = preflight._report_signature(
            self._base_report("ok", config_signature="bbb")
        )
        assert sig_a != sig_b

    def test_same_inputs_same_signature(self) -> None:
        sig_a = preflight._report_signature(self._base_report("ok"))
        sig_b = preflight._report_signature(self._base_report("ok"))
        assert sig_a == sig_b
