"""Unit tests for the provider chain executor."""

from __future__ import annotations

import os
from typing import Any
from unittest import mock

from _lib import chain


class TestChainFromEnv:
    def test_unset_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert chain._chain_from_env() is None

    def test_empty_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_CHAIN": ""}, clear=True):
            assert chain._chain_from_env() is None

    def test_single_provider(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_CHAIN": "codex"}, clear=True):
            assert chain._chain_from_env() == ["codex"]

    def test_multiple_with_whitespace(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_CHAIN": "codex, gemini ,claude"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert chain._chain_from_env() == ["codex", "gemini", "claude"]

    def test_unknown_provider_dropped(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_CHAIN": "codex,bogus,gemini"}
        with mock.patch.dict(os.environ, env, clear=True):
            # bogus is rejected; valid entries kept
            assert chain._chain_from_env() == ["codex", "gemini"]

    def test_all_unknown_returns_none(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_CHAIN": "fake1,fake2"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert chain._chain_from_env() is None


class TestParseResponseJson:
    def test_empty(self) -> None:
        result = chain._parse_response_json("")
        assert result.parse_ok is False
        assert result.findings is None
        assert result.parse_error == "empty"

    def test_whitespace_only(self) -> None:
        result = chain._parse_response_json("   \n  ")
        assert result.parse_ok is False
        assert result.findings is None

    def test_no_json_at_all(self) -> None:
        result = chain._parse_response_json("just narrative text, no braces")
        assert result.parse_ok is False
        assert result.findings is None
        assert result.parse_error == "no JSON object"

    def test_plain_json(self) -> None:
        raw = '{"findings": [{"severity": "P1", "title": "x"}], "questions": ["q1"]}'
        result = chain._parse_response_json(raw)
        assert result.parse_ok is True
        assert result.findings == [{"severity": "P1", "title": "x"}]
        assert result.questions == ["q1"]

    def test_clean_review_with_empty_findings(self) -> None:
        # `findings: []` is a legitimate clean review and MUST be
        # parse_ok=True (codex P1#3 — the old `or None` collapse
        # made empty-list indistinguishable from parse failure,
        # corrupting result_status classification).
        raw = '{"findings": [], "questions": []}'
        result = chain._parse_response_json(raw)
        assert result.parse_ok is True
        assert result.findings == []
        assert result.questions == []

    def test_markdown_fenced_clean_review(self) -> None:
        raw = '```json\n{"findings": [], "questions": []}\n```'
        result = chain._parse_response_json(raw)
        assert result.parse_ok is True
        assert result.findings == []

    def test_narrative_wrapper(self) -> None:
        raw = (
            "Here is my review:\n\n"
            '{"findings": [{"severity": "P0", "title": "boom"}], "questions": []}\n\n'
            "Hope this helps."
        )
        result = chain._parse_response_json(raw)
        assert result.parse_ok is True
        assert result.findings == [{"severity": "P0", "title": "boom"}]

    def test_malformed_json(self) -> None:
        # Need closing brace so the regex matches the JSON-shape
        # token; json.loads then fails.
        raw = '{"findings": [oops, broken}'
        result = chain._parse_response_json(raw)
        assert result.parse_ok is False
        assert result.findings is None
        assert result.parse_error and result.parse_error.startswith("json decode")

    def test_findings_null_is_unparseable(self) -> None:
        # A model that emitted JSON but with `findings: null`
        # should NOT be classified as ok.
        result = chain._parse_response_json('{"findings": null}')
        assert result.parse_ok is False
        assert result.parse_error == "missing/non-list findings"

    def test_findings_missing_key_is_unparseable(self) -> None:
        result = chain._parse_response_json('{"foo": "bar"}')
        assert result.parse_ok is False
        assert result.parse_error == "missing/non-list findings"


class TestProviderRegistry:
    def test_default_chain_is_codex_first(self) -> None:
        assert chain.DEFAULT_CHAINS["plan"][0] == "codex"

    def test_default_chain_includes_fallbacks(self) -> None:
        assert chain.DEFAULT_CHAINS["plan"] == ["codex", "gemini", "claude"]

    def test_local_provider_registered_but_not_default(self) -> None:
        # `local` is a generic OpenAI-compat client (vLLM/Ollama/llama.cpp);
        # it ships registered so users can opt in via CLAUDE_PLAN_REVIEW_CHAIN
        # or CLAUDE_PLAN_REVIEW_SHADOW, but is NOT in the default chain so
        # users without a local backend are unaffected.
        assert "local" in chain.PROVIDER_CMDS
        assert "local" not in chain.DEFAULT_CHAINS["plan"]
        # Local provider invokes the in-tree runner via the current python.
        local_cmd = chain.PROVIDER_CMDS["local"]
        assert local_cmd[0].endswith("python") or "python" in local_cmd[0]
        assert local_cmd[-1].endswith("local_provider.py")

    def test_codex_uses_xhigh_reasoning(self) -> None:
        codex_cmd = chain.PROVIDER_CMDS["codex"]
        assert any("xhigh" in arg for arg in codex_cmd)


class TestMetadataOnlyLogs:
    def test_default_writes_full_content(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert chain._metadata_only_logs() is False

    def test_opt_out(self) -> None:
        for val in ("1", "true", "yes", "on"):
            env = {"CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY": val}
            with mock.patch.dict(os.environ, env, clear=True):
                assert chain._metadata_only_logs() is True

    def test_falsy_values_keep_full_content(self) -> None:
        for val in ("0", "false", "", "no"):
            env = {"CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY": val}
            with mock.patch.dict(os.environ, env, clear=True):
                assert chain._metadata_only_logs() is False


class TestFormatFindings:
    def _result(self, findings: list[dict[str, Any]]) -> chain.ChainResult:
        return chain.ChainResult(
            provider="codex",
            findings=findings,
            questions=None,
            raw_output="",
            elapsed_seconds=1.0,
        )

    def test_empty_returns_empty_string(self) -> None:
        assert self._result([]).format_findings() == ""

    def test_includes_severity_summary(self) -> None:
        out = self._result(
            [
                {"severity": "P0", "title": "crit"},
                {"severity": "P1", "title": "high"},
                {"severity": "P1", "title": "high2"},
            ]
        ).format_findings()
        assert "1 P0" in out
        assert "2 P1" in out
        assert "BLOCKING" in out


import json
import threading
import time
from pathlib import Path

import pytest


class TestSaveCycleLogFields:
    """Round-trip + result_status classification at write time."""

    def _meta(self, tmp_path: Path) -> dict[str, Any]:
        return {
            "plan_path": str(tmp_path / "plan.md"),
            "plan_filename": "plan.md",
            "plan_title": "Test plan",
            "iteration": 1,
            "project": None,
        }

    def _read(self, log_path: Path) -> dict[str, Any]:
        return json.loads(log_path.read_text())

    def test_ok_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain, "_review_log_dir", lambda: tmp_path)
        path = chain._save_cycle_log(
            "codex",
            "prompt",
            '{"findings": [], "questions": []}',
            "",
            0,
            0.1,  # elapsed
            [],
            metadata=self._meta(tmp_path),
            shadow=True,
        )
        assert path is not None
        data = self._read(path)
        assert data["result_status"] == "ok"
        assert data["parse_error"] is None
        assert data["shadow_config_signature"]
        # Signature is 16-char hex.
        assert len(data["shadow_config_signature"]) == 16

    def test_error_record_from_returncode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain, "_review_log_dir", lambda: tmp_path)
        path = chain._save_cycle_log(
            "codex",
            "prompt",
            "",
            "boom",
            returncode=1,
            elapsed=0.1,
            findings=None,
            error="rc=1",
            metadata=self._meta(tmp_path),
            shadow=True,
        )
        assert path is not None
        data = self._read(path)
        assert data["result_status"] == "error"

    def test_empty_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain, "_review_log_dir", lambda: tmp_path)
        path = chain._save_cycle_log(
            "codex",
            "prompt",
            "",  # zero stdout, no error, rc=0
            "",
            0,
            0.1,  # elapsed
            None,
            metadata=self._meta(tmp_path),
            shadow=True,
        )
        assert path is not None
        data = self._read(path)
        assert data["result_status"] == "empty"

    def test_unparseable_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain, "_review_log_dir", lambda: tmp_path)
        path = chain._save_cycle_log(
            "codex",
            "prompt",
            "garbage that isn't JSON",
            "",
            0,
            0.1,  # elapsed
            None,
            metadata=self._meta(tmp_path),
            shadow=True,
        )
        assert path is not None
        data = self._read(path)
        assert data["result_status"] == "unparseable"
        assert data["parse_error"]


class TestAtomicWrite:
    """Atomic-write contract via deterministic test seam."""

    def test_no_partial_json_visible_during_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain, "_review_log_dir", lambda: tmp_path)
        gate = threading.Event()
        observed: dict[str, Any] = {"json_during_pause": []}

        def hook() -> None:
            # We're between fsync(file) and replace(); the .json file
            # should NOT exist yet, only the .tmp.
            for p in tmp_path.glob("*.json"):
                if not p.name.startswith("."):
                    observed["json_during_pause"].append(p.name)
            gate.wait()

        monkeypatch.setattr(chain, "_ATOMIC_REPLACE_HOOK", hook)

        meta = {
            "plan_path": str(tmp_path / "plan.md"),
            "plan_filename": "plan.md",
            "plan_title": "atomic",
            "iteration": 1,
            "project": None,
        }
        result_holder: dict[str, Any] = {}

        def writer() -> None:
            result_holder["path"] = chain._save_cycle_log(
                "codex",
                "prompt",
                '{"findings": []}',
                "",
                0,
                0.1,  # elapsed
                [],
                metadata=meta,
                shadow=True,
            )

        t = threading.Thread(target=writer)
        t.start()
        # Give the writer time to reach the hook. 1s is generous and
        # not timing-sensitive — the hook GATES until we release.
        time.sleep(0.2)
        # Verify no incomplete .json visible during the pause.
        assert observed["json_during_pause"] == [], (
            f"saw partial json during write: {observed['json_during_pause']}"
        )
        gate.set()
        t.join(timeout=5.0)
        assert not t.is_alive()
        # Now the .json file is present and complete.
        json_files = [p for p in tmp_path.glob("*.json") if not p.name.startswith(".")]
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        assert data["result_status"] == "ok"


class TestShadowRetention:
    """Time-based shadow pruner: keeps records < 8d, plus count floor."""

    def _make_shadow_file(
        self,
        log_dir: Path,
        name: str,
        mtime_seconds_ago: float,
    ) -> Path:
        path = log_dir / f"{name}_shadow.json"
        path.write_text(
            json.dumps({"shadow": True, "filename_test": True}),
        )
        now = time.time()
        os_mtime = now - mtime_seconds_ago
        import os as _os

        _os.utime(path, (os_mtime, os_mtime))
        return path

    def test_keeps_all_recent_regardless_of_count(
        self, tmp_path: Path
    ) -> None:
        # 250 records all in last hour — exceeds COUNT_FLOOR (200).
        # Time-based rule keeps all of them.
        for i in range(250):
            self._make_shadow_file(tmp_path, f"abc_p_{i}_run_codex", 60.0)
        chain._prune_shadow_log_dir(tmp_path)
        survivors = list(tmp_path.glob("*_shadow.json"))
        assert len(survivors) == 250

    def test_drops_old_records_beyond_floor(
        self, tmp_path: Path
    ) -> None:
        # 100 old records (30 days), then 5 recent records.
        # Count floor (200) keeps everything; nothing pruned.
        for i in range(100):
            self._make_shadow_file(
                tmp_path, f"abc_p_{i}_run_codex", 30 * 86400,
            )
        for i in range(5):
            self._make_shadow_file(
                tmp_path, f"abc_p_recent_{i}_run_codex", 60.0,
            )
        chain._prune_shadow_log_dir(tmp_path)
        survivors = list(tmp_path.glob("*_shadow.json"))
        # 100 old + 5 recent = 105, all kept by COUNT_FLOOR.
        assert len(survivors) == 105

    def test_drops_old_when_count_exceeds_floor(
        self, tmp_path: Path
    ) -> None:
        # 250 old records (30 days). Count floor keeps 200; rest
        # pruned because they're past the time floor.
        for i in range(250):
            self._make_shadow_file(
                tmp_path, f"abc_p_{i}_run_codex", 30 * 86400,
            )
        chain._prune_shadow_log_dir(tmp_path)
        survivors = list(tmp_path.glob("*_shadow.json"))
        assert len(survivors) == chain.SHADOW_RETAIN_COUNT_FLOOR

    def test_non_shadow_files_untouched(self, tmp_path: Path) -> None:
        # Non-shadow file in same dir, ancient mtime — pruner ignores.
        non_shadow = tmp_path / "abc_p_1_old_codex.json"
        non_shadow.write_text("{}")
        import os as _os

        old = time.time() - 100 * 86400
        _os.utime(non_shadow, (old, old))
        chain._prune_shadow_log_dir(tmp_path)
        assert non_shadow.exists()
