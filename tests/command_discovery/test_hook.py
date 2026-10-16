"""End-to-end tests for the command-discovery PostToolUse hook entry point."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "command-discovery"
    / "scripts"
    / "command_discovery_hook.py"
)
_spec = importlib.util.spec_from_file_location("command_discovery_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


class TestFailOpen:
    def test_malformed_stdin_never_raises(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json at all {{{"))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}

    def test_empty_stdin_never_raises(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}

    def test_evaluate_error_never_breaks_the_hook(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(hook, "evaluate", _boom)
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "mycli x"}})),
        )
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}


class TestSuggestionEmission:
    def test_no_suggestion_emits_empty_payload(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hook, "evaluate", lambda payload: None)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}

    def test_suggestion_becomes_additional_context(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(hook, "evaluate", lambda payload: "try `mycli status` instead")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert parsed["hookSpecificOutput"]["additionalContext"] == "try `mycli status` instead"
