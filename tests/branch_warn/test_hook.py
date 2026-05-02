"""Unit tests for branch-warn."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import time
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "branch-warn"
    / "scripts"
    / "branch_warn_hook.py"
)
_spec = importlib.util.spec_from_file_location("branch_warn_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _stdin(cwd: Path) -> io.StringIO:
    return io.StringIO(f'{{"hook_event_name":"UserPromptSubmit","cwd":"{cwd}"}}')


class TestThrottleSeconds:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_BRANCH_WARN_THROTTLE_SECONDS", raising=False)
        assert hook._throttle_seconds() == 3600

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_BRANCH_WARN_THROTTLE_SECONDS", "120")
        assert hook._throttle_seconds() == 120

    def test_invalid_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_BRANCH_WARN_THROTTLE_SECONDS", "not-a-number")
        assert hook._throttle_seconds() == 3600


class TestProtectedBranches:
    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_BRANCH_WARN_PROTECTED", raising=False)
        assert hook._protected_branches() == {"main", "master"}

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_BRANCH_WARN_PROTECTED", "trunk, prod, ")
        assert hook._protected_branches() == {"trunk", "prod"}


class TestThrottleBehavior:
    def test_no_marker_first_run_is_not_throttled(self, tmp_path: Path) -> None:
        marker = tmp_path / "warned"
        assert hook._was_warned_recently(marker, 3600) is False

    def test_recent_marker_is_throttled(self, tmp_path: Path) -> None:
        marker = tmp_path / "warned"
        marker.touch()
        assert hook._was_warned_recently(marker, 3600) is True

    def test_old_marker_is_not_throttled(self, tmp_path: Path) -> None:
        marker = tmp_path / "warned"
        marker.touch()
        # Backdate to 2 hours ago.
        old = time.time() - 7200
        os.utime(marker, (old, old))
        assert hook._was_warned_recently(marker, 3600) is False


class TestMainEndToEnd:
    def test_no_git_repo_emits_pass_through(
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
        # Either empty pass-through or "On branch:" — but no git in
        # tmp_path means current_branch returns None and we pass through.
        d = json.loads(out)
        assert "systemMessage" not in d

    def test_throttle_silences_second_run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        # Pre-touch the marker so the next call is throttled.
        marker = tmp_path / "warned"
        marker.touch()
        monkeypatch.setattr("sys.stdin", _stdin(tmp_path))
        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        d = json.loads(out)
        assert d == {}
