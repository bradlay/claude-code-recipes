"""Hook decision JSON shape and plan-file resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from _lib import _io


def _decode(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out, _err = capsys.readouterr()
    return json.loads(out) if out else {}


class TestEmitAllow:
    def test_minimal_allow(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_pretooluse_allow()
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_allow_with_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_pretooluse_allow(additional_context="ctx")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["additionalContext"] == "ctx"

    def test_allow_with_system_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_pretooluse_allow(system_message="msg")
        out = _decode(capsys)
        assert out["systemMessage"] == "msg"


class TestEmitDeny:
    def test_deny_with_reason(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_pretooluse_deny("blocked")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert out["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"

    def test_deny_with_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_pretooluse_deny("blocked", additional_context="why")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["additionalContext"] == "why"


class TestSessionStart:
    def test_emit_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        _io.emit_session_start_context("preflight info")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert out["hookSpecificOutput"]["additionalContext"] == "preflight info"


class TestFailOpenOrClosed:
    def _inv(self) -> _io.HookInvocation:
        return _io.HookInvocation(
            event="PreToolUse",
            tool_name="ExitPlanMode",
            session_id="test",
            cwd=Path.cwd(),
            tool_input={},
            tool_response=None,
            transcript_path=None,
            raw={},
            hook_script=__file__,
        )

    def test_default_is_fail_closed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            _io.fail_open_or_closed_pretooluse(self._inv(), "no provider")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_fail_open_env_bypasses(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_FAIL_OPEN": "1"}, clear=True):
            _io.fail_open_or_closed_pretooluse(self._inv(), "no provider")
        out = _decode(capsys)
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "bypass" in out["systemMessage"].lower()

    def test_truthy_values_for_bypass(self, capsys: pytest.CaptureFixture[str]) -> None:
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_FAIL_OPEN": val}, clear=True):
                _io.fail_open_or_closed_pretooluse(self._inv(), "x")
            out = _decode(capsys)
            assert out["hookSpecificOutput"]["permissionDecision"] == "allow", val


class TestPlanFileResolution:
    def _inv(
        self, tool_input: dict[str, Any], raw: dict[str, Any] | None = None
    ) -> _io.HookInvocation:
        return _io.HookInvocation(
            event="PreToolUse",
            tool_name="ExitPlanMode",
            session_id="test",
            cwd=Path.cwd(),
            tool_input=tool_input,
            tool_response=None,
            transcript_path=None,
            raw=raw if raw is not None else tool_input,
            hook_script=__file__,
        )

    def test_resolves_from_tool_input_planFilePath(self, tmp_path: Path) -> None:
        plan = tmp_path / "p.md"
        plan.write_text("x")
        inv = self._inv({"planFilePath": str(plan)})
        assert _io.resolve_plan_file(inv) == plan

    def test_resolves_from_tool_input_snake_case(self, tmp_path: Path) -> None:
        plan = tmp_path / "p.md"
        plan.write_text("x")
        inv = self._inv({"plan_file_path": str(plan)})
        assert _io.resolve_plan_file(inv) == plan

    def test_resolves_from_top_level_raw(self, tmp_path: Path) -> None:
        plan = tmp_path / "p.md"
        plan.write_text("x")
        inv = self._inv({}, raw={"planFilePath": str(plan)})
        assert _io.resolve_plan_file(inv) == plan

    def test_resolves_from_env(self, tmp_path: Path) -> None:
        plan = tmp_path / "p.md"
        plan.write_text("x")
        inv = self._inv({})
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_FILE": str(plan)}, clear=False):
            assert _io.resolve_plan_file(inv) == plan

    def test_returns_none_when_no_plan_anywhere(self, tmp_path: Path) -> None:
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": str(tmp_path)},
            clear=False,
        ):
            os.environ.pop("CLAUDE_PLAN_FILE", None)
            inv = self._inv({}, raw={})
            assert _io.resolve_plan_file(inv) is None
