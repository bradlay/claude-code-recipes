"""Unit tests for the subagent-context-injector hook helpers.

Drives _capped, _read_claude_md, _list_rules, _git_context, and
_top_level_dirs against synthetic directories.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

# The hook is a __main__ script, not a package. Load it directly via
# importlib so we can call its helper functions without running main().
_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "subagent-context-injector"
    / "scripts"
    / "subagent_context_hook.py"
)
_spec = importlib.util.spec_from_file_location("subagent_context_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


class TestCapped:
    def test_short_passes_through(self) -> None:
        assert hook._capped("hello", 100) == "hello"

    def test_truncates_with_marker(self) -> None:
        out = hook._capped("a" * 200, 50)
        assert out.startswith("a" * 50)
        assert "(truncated)" in out


class TestReadClaudeMd:
    def test_missing_file(self, tmp_path: Path) -> None:
        out = hook._read_claude_md(tmp_path, max_chars=2000)
        assert "no CLAUDE.md" in out

    def test_present_file(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Hello\nthis is the project")
        out = hook._read_claude_md(tmp_path, max_chars=2000)
        assert "Hello" in out
        assert "this is the project" in out

    def test_truncates_long_file(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("x" * 5000)
        out = hook._read_claude_md(tmp_path, max_chars=100)
        assert "(truncated)" in out
        assert len(out) <= 200


class TestListRules:
    def test_no_rules_dir(self, tmp_path: Path) -> None:
        out = hook._list_rules(tmp_path)
        assert "no .claude/rules" in out

    def test_empty_rules_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        out = hook._list_rules(tmp_path)
        assert out == "(empty)"

    def test_lists_md_files_with_first_line(self, tmp_path: Path) -> None:
        rules = tmp_path / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "a.md").write_text("# Rule A\nbody...")
        (rules / "b.md").write_text("# Rule B header")
        (rules / "ignored.txt").write_text("not md")
        out = hook._list_rules(tmp_path)
        assert "a.md: Rule A" in out
        assert "b.md: Rule B header" in out
        assert "ignored.txt" not in out


class TestTopLevelDirs:
    def test_lists_dirs_and_files(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "README.md").write_text("x")
        (tmp_path / ".hidden").mkdir()  # should be skipped
        out = hook._top_level_dirs(tmp_path)
        assert "src" in out
        assert "tests" in out
        assert "README.md" in out
        assert ".hidden" not in out


class TestGitContext:
    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        out = hook._git_context(tmp_path)
        # Either "not a git repo" if git is on PATH and tmp_path isn't
        # a repo, or "git not on PATH" if git is missing entirely.
        assert out in ("(not a git repo)", "(git not on PATH)")


class TestMain:
    def test_full_run_emits_subagent_start(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text("# test project\nrules here")
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(f'{{"hook_event_name":"SubagentStart","cwd":"{tmp_path}"}}'),
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))

        rc = hook.main()
        assert rc == 0

        out, _ = capsys.readouterr()
        d = json.loads(out)
        assert d["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
        assert "test project" in d["hookSpecificOutput"]["additionalContext"]
