"""Unit tests for the backend registry — the single source of truth for
backend keys, models, argv, and legacy-alias normalization."""

from __future__ import annotations

import os
from unittest import mock

from _lib import backends


class TestRegistryShape:
    def test_expected_keys(self) -> None:
        assert set(backends.REGISTRY) == {"opus", "sonnet", "codex", "gemini", "local"}

    def test_online_keys_exclude_local(self) -> None:
        assert backends.ONLINE_KEYS == ["opus", "sonnet", "codex", "gemini"]
        assert "local" not in backends.ONLINE_KEYS

    def test_self_review_keys(self) -> None:
        assert frozenset({"opus", "sonnet"}) == backends.SELF_REVIEW_KEYS


class TestArgvConstruction:
    def test_opus_argv(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert backends.REGISTRY["opus"].run_argv() == [
                "claude",
                "--print",
                "--model",
                "claude-opus-4-8",
            ]

    def test_sonnet_argv(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert backends.REGISTRY["sonnet"].run_argv() == [
                "claude",
                "--print",
                "--model",
                "claude-sonnet-4-6",
            ]

    def test_gemini_runs_agy_binary(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            argv = backends.REGISTRY["gemini"].run_argv()
            assert argv == ["agy", "-p", "", "--model", "Gemini 3.1 Pro (High)"]
            assert backends.REGISTRY["gemini"].binary == "agy"

    def test_codex_argv_uses_xhigh(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            argv = backends.REGISTRY["codex"].run_argv()
            assert argv[0] == "codex"
            assert any("xhigh" in a for a in argv)
            assert any(a == 'model="gpt-5.5"' for a in argv)

    def test_codex_probe_uses_low_effort(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert any("low" in a for a in backends.REGISTRY["codex"].probe_argv())

    def test_local_argv_invokes_provider(self) -> None:
        argv = backends.REGISTRY["local"].run_argv()
        assert "python" in argv[0]
        assert argv[-1].endswith("local_provider.py")


class TestModelOverrides:
    def test_opus_model_env_override(self) -> None:
        with mock.patch.dict(
            os.environ, {"CLAUDE_PLAN_REVIEW_OPUS_MODEL": "claude-opus-9"}, clear=True
        ):
            assert backends.REGISTRY["opus"].model() == "claude-opus-9"

    def test_agy_model_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_AGY_MODEL": "Gemini X"}, clear=True):
            assert backends.REGISTRY["gemini"].run_argv()[-1] == "Gemini X"

    def test_blank_override_falls_back_to_default(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_CODEX_MODEL": "  "}, clear=True):
            assert backends.REGISTRY["codex"].model() == "gpt-5.5"


class TestNormalizeKey:
    def test_canonical_keys_pass_through(self) -> None:
        for key in ("opus", "sonnet", "codex", "gemini", "local"):
            assert backends.normalize_key(key) == key

    def test_legacy_claude_maps_to_sonnet(self) -> None:
        assert backends.normalize_key("claude") == "sonnet"

    def test_legacy_agy_maps_to_gemini(self) -> None:
        assert backends.normalize_key("agy") == "gemini"

    def test_whitespace_tolerated(self) -> None:
        assert backends.normalize_key("  codex ") == "codex"

    def test_unknown_returns_none(self) -> None:
        assert backends.normalize_key("bogus") is None
        assert backends.is_known("bogus") is False
        assert backends.is_known("gemini") is True
