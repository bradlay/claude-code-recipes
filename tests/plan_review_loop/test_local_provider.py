"""Unit tests for the local OpenAI-compat plan-review provider."""

from __future__ import annotations

import io
import os
import urllib.error
from unittest import mock

import pytest
from _lib import local_provider


class TestStripThinkBlocks:
    def test_no_blocks_unchanged(self) -> None:
        assert local_provider._strip_think_blocks("plain text") == "plain text"

    def test_single_block_removed(self) -> None:
        text = "before<think>reasoning</think>after"
        assert local_provider._strip_think_blocks(text) == "beforeafter"

    def test_multiline_block_removed(self) -> None:
        text = "x\n<think>\nline1\nline2\n</think>\ny"
        assert local_provider._strip_think_blocks(text) == "x\n\ny"

    def test_multiple_blocks_all_removed(self) -> None:
        text = "a<think>1</think>b<think>2</think>c"
        assert local_provider._strip_think_blocks(text) == "abc"

    def test_non_greedy_across_blocks(self) -> None:
        # Greedy match would eat the middle "}}" — non-greedy must not.
        text = "<think>x</think>{}<think>y</think>"
        assert local_provider._strip_think_blocks(text) == "{}"


class TestEnvHelpers:
    def test_env_str_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert local_provider._env_str("X", "fallback") == "fallback"

    def test_env_str_set(self) -> None:
        with mock.patch.dict(os.environ, {"X": "value"}, clear=True):
            assert local_provider._env_str("X", "fallback") == "value"

    def test_env_str_empty_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"X": "  "}, clear=True):
            assert local_provider._env_str("X", "fallback") == "fallback"

    def test_env_int_invalid_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"X": "not-a-number"}, clear=True):
            assert local_provider._env_int("X", 42) == 42

    def test_env_int_valid(self) -> None:
        with mock.patch.dict(os.environ, {"X": "100"}, clear=True):
            assert local_provider._env_int("X", 42) == 100

    def test_env_float_invalid_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"X": "abc"}, clear=True):
            assert local_provider._env_float("X", 0.5) == 0.5

    def test_env_float_valid(self) -> None:
        with mock.patch.dict(os.environ, {"X": "0.7"}, clear=True):
            assert local_provider._env_float("X", 0.5) == 0.7


class TestBaseUrl:
    def test_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert local_provider.base_url() == "http://localhost:8010"

    def test_override(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_LOCAL_URL": "http://my-vllm:9999"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert local_provider.base_url() == "http://my-vllm:9999"

    def test_strips_trailing_slash(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_LOCAL_URL": "http://x:1/"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert local_provider.base_url() == "http://x:1"


class TestResolveModel:
    def test_explicit_env_wins(self) -> None:
        env = {"CLAUDE_PLAN_REVIEW_LOCAL_MODEL": "Qwen/Custom-Model"}
        # Should NOT hit the network at all.
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            local_provider, "_http_get_json"
        ) as m:
            assert local_provider.resolve_model("http://x:1") == "Qwen/Custom-Model"
            m.assert_not_called()

    def test_auto_discovery(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "_http_get_json",
            return_value={"data": [{"id": "auto-discovered-model"}]},
        ):
            assert local_provider.resolve_model("http://x:1") == "auto-discovered-model"

    def test_empty_data_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "_http_get_json",
            return_value={"data": []},
        ), pytest.raises(local_provider.LocalProviderError, match="no models"):
            local_provider.resolve_model("http://x:1")

    def test_malformed_data_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "_http_get_json",
            return_value={"data": [{"name": "no-id-field"}]},
        ), pytest.raises(local_provider.LocalProviderError, match="malformed"):
            local_provider.resolve_model("http://x:1")

    def test_http_error_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "_http_get_json",
            side_effect=urllib.error.URLError("connection refused"),
        ), pytest.raises(local_provider.LocalProviderError, match="failed"):
            local_provider.resolve_model("http://x:1")


class TestCallModel:
    def test_happy_path(self) -> None:
        response = {
            "choices": [{"message": {"content": '{"findings": [], "questions": []}'}}],
        }
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider, "_http_post_json", return_value=response
        ) as m:
            out = local_provider.call_model("http://x:1", "model-x", "the prompt")
        assert out == '{"findings": [], "questions": []}'
        # Verify payload shape
        url, payload = m.call_args[0]
        assert url == "http://x:1/v1/chat/completions"
        assert payload["model"] == "model-x"
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["content"] == "the prompt"
        assert payload["temperature"] == 0.1
        assert payload["max_tokens"] == 4096
        assert payload["priority"] == 20

    def test_env_overrides_apply(self) -> None:
        env = {
            "CLAUDE_PLAN_REVIEW_LOCAL_TEMPERATURE": "0.5",
            "CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS": "100",
            "CLAUDE_PLAN_REVIEW_LOCAL_PRIORITY": "5",
        }
        response = {"choices": [{"message": {"content": "ok"}}]}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            local_provider, "_http_post_json", return_value=response
        ) as m:
            local_provider.call_model("http://x:1", "m", "p")
        payload = m.call_args[0][1]
        assert payload["temperature"] == 0.5
        assert payload["max_tokens"] == 100
        assert payload["priority"] == 5

    def test_http_error_translates_to_local_error(self) -> None:
        http_err = urllib.error.HTTPError(
            "http://x:1", 500, "Server Error", {}, io.BytesIO(b"upstream broke")
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider, "_http_post_json", side_effect=http_err
        ), pytest.raises(local_provider.LocalProviderError, match="500"):
            local_provider.call_model("http://x:1", "m", "p")

    def test_url_error_translates(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "_http_post_json",
            side_effect=urllib.error.URLError("no route"),
        ), pytest.raises(local_provider.LocalProviderError, match="failed"):
            local_provider.call_model("http://x:1", "m", "p")

    def test_malformed_response_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider, "_http_post_json", return_value={"foo": "bar"}
        ), pytest.raises(local_provider.LocalProviderError, match="unexpected"):
            local_provider.call_model("http://x:1", "m", "p")


class TestMain:
    def test_empty_prompt_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = local_provider.main([""])
        assert rc == 2
        assert "empty prompt" in capsys.readouterr().err

    def test_happy_path_strips_think_and_writes_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider, "resolve_model", return_value="m"
        ), mock.patch.object(
            local_provider,
            "call_model",
            return_value='<think>reasoning</think>{"findings": []}',
        ):
            rc = local_provider.main(["the prompt"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip() == '{"findings": []}'

    def test_provider_error_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            local_provider,
            "resolve_model",
            side_effect=local_provider.LocalProviderError("backend down"),
        ):
            rc = local_provider.main(["the prompt"])
        assert rc == 1
        assert "backend down" in capsys.readouterr().err
