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
        findings, questions = chain._parse_response_json("")
        assert findings is None
        assert questions is None

    def test_whitespace_only(self) -> None:
        findings, questions = chain._parse_response_json("   \n  ")
        assert findings is None
        assert questions is None

    def test_no_json_at_all(self) -> None:
        findings, questions = chain._parse_response_json("just narrative text, no braces")
        assert findings is None
        assert questions is None

    def test_plain_json(self) -> None:
        raw = '{"findings": [{"severity": "P1", "title": "x"}], "questions": ["q1"]}'
        findings, questions = chain._parse_response_json(raw)
        assert findings == [{"severity": "P1", "title": "x"}]
        assert questions == ["q1"]

    def test_markdown_fenced(self) -> None:
        raw = '```json\n{"findings": [], "questions": []}\n```'
        findings, questions = chain._parse_response_json(raw)
        # empty arrays canonicalize to None per `data.get("findings", []) or None`
        assert findings is None
        assert questions is None

    def test_narrative_wrapper(self) -> None:
        raw = (
            "Here is my review:\n\n"
            '{"findings": [{"severity": "P0", "title": "boom"}], "questions": []}\n\n'
            "Hope this helps."
        )
        findings, questions = chain._parse_response_json(raw)
        assert findings == [{"severity": "P0", "title": "boom"}]
        assert questions is None

    def test_malformed_json(self) -> None:
        raw = '{"findings": [oops, broken'
        findings, questions = chain._parse_response_json(raw)
        assert findings is None
        assert questions is None


class TestProviderRegistry:
    def test_default_chain_is_codex_first(self) -> None:
        assert chain.DEFAULT_CHAINS["plan"][0] == "codex"

    def test_default_chain_includes_fallbacks(self) -> None:
        assert chain.DEFAULT_CHAINS["plan"] == ["codex", "gemini", "claude"]

    def test_local_provider_dropped(self) -> None:
        # `local` provider was the gb10-specific runner; it must not ship
        # in the public plugin.
        assert "local" not in chain.PROVIDER_CMDS
        assert "local" not in chain.DEFAULT_CHAINS["plan"]

    def test_codex_uses_xhigh_reasoning(self) -> None:
        codex_cmd = chain.PROVIDER_CMDS["codex"]
        assert any('xhigh' in arg for arg in codex_cmd)


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
