"""End-to-end tests for the stop-session-check hook entry point."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "stop-session-check"
    / "scripts"
    / "stop_check_hook.py"
)
_spec = importlib.util.spec_from_file_location("stop_check_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _stdin(cwd: Path, stop_hook_active: bool = False) -> io.StringIO:
    return io.StringIO(
        f'{{"hook_event_name":"Stop","cwd":"{cwd}",'
        f'"stop_hook_active":{str(stop_hook_active).lower()},'
        f'"session_id":"sess-1"}}'
    )


class TestStopHookActiveBypass:
    def test_recursive_invocation_allows_stop(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr("sys.stdin", _stdin(tmp_path, stop_hook_active=True))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        d = json.loads(out)
        # Empty pass-through (no decision, no message) lets the stop go.
        assert d == {}


class TestNotInGitRepo:
    def test_emits_pass_through(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr("sys.stdin", _stdin(tmp_path))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        d = json.loads(out)
        # Not a git repo => pass through, allow stop.
        assert d == {}
