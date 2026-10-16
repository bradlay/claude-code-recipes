"""End-to-end tests for the quota-advisory Stop hook entry point."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "quota-advisory"
    / "scripts"
    / "quota_advisory_hook.py"
)
_spec = importlib.util.spec_from_file_location("quota_advisory_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


class TestStopHookActiveBypass:
    def test_recursive_invocation_is_pass_through(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"stop_hook_active": True}))
        )
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}


class TestFailOpen:
    def test_advisory_lookup_error_never_breaks_the_stop(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(hook, "compact_advisory_text", _boom)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}

    def test_no_advisory_emits_empty_payload(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr(hook, "compact_advisory_text", lambda: None)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {}

    def test_advisory_present_becomes_system_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr(hook, "compact_advisory_text", lambda: "consider /compact")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {"systemMessage": "consider /compact"}
