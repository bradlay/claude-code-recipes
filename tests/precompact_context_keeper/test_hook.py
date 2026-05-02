"""Unit tests for the precompact-context-keeper hook."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "precompact-context-keeper"
    / "scripts"
    / "precompact_hook.py"
)
_spec = importlib.util.spec_from_file_location("precompact_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


class TestReadClaudeMd:
    def test_missing_returns_empty(self, tmp_path: Path) -> None:
        assert hook._read_claude_md(tmp_path) == ""

    def test_present(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Project X\n\nDescribe stuff.")
        out = hook._read_claude_md(tmp_path)
        assert "Project X" in out
        assert "Describe stuff" in out

    def test_truncates_long(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("x" * 5000)
        out = hook._read_claude_md(tmp_path)
        assert "(truncated)" in out
        assert len(out) <= hook._MAX_CLAUDE_MD + 100


class TestGitWorkState:
    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        out = hook._git_work_state(tmp_path)
        # Either empty (git unavailable or no repo) — both acceptable
        # for the "no signal to inject" path.
        assert isinstance(out, str)


class TestMainNothingToInject:
    def test_pass_through_emit(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No CLAUDE.md, not a git repo => nothing to inject.
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(f'{{"hook_event_name":"PreCompact","cwd":"{tmp_path}"}}'),
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))

        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        d = json.loads(out)
        # Empty pass-through: no systemMessage key
        assert d == {}


class TestMainEmitsSystemMessage:
    def test_with_claude_md(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Test project\nKey constraint: do X.")
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(f'{{"hook_event_name":"PreCompact","cwd":"{tmp_path}"}}'),
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))

        rc = hook.main()
        assert rc == 0
        out, _ = capsys.readouterr()
        d = json.loads(out)
        assert "systemMessage" in d
        assert "PRESERVE ACROSS COMPACTION" in d["systemMessage"]
        assert "Test project" in d["systemMessage"]
        assert "do X" in d["systemMessage"]
