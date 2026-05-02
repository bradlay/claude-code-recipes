"""Unit tests for the posttooluse-bash-audit hook."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "posttooluse-bash-audit"
    / "scripts"
    / "audit_hook.py"
)
_spec = importlib.util.spec_from_file_location("audit_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _payload(tool_name: str = "Bash", command: str = "ls", session_id: str = "test-sess") -> str:
    return (
        '{"hook_event_name":"PostToolUse",'
        f'"tool_name":"{tool_name}",'
        f'"tool_input":{{"command":"{command}"}},'
        f'"session_id":"{session_id}"}}'
    )


class TestAuditLogging:
    def test_appends_a_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr("sys.stdin", io.StringIO(_payload(command="ls -la")))
        rc = hook.main()
        assert rc == 0
        log = (tmp_path / "audit.log").read_text()
        assert "ls -la" in log
        assert "test-sess" in log

    def test_appends_multiple_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))

        for cmd in ("echo a", "echo b", "echo c"):
            monkeypatch.setattr("sys.stdin", io.StringIO(_payload(command=cmd)))
            assert hook.main() == 0

        log_lines = (tmp_path / "audit.log").read_text().strip().splitlines()
        assert len(log_lines) == 3
        assert log_lines[0].endswith("echo a")
        assert log_lines[2].endswith("echo c")

    def test_truncates_long_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        long_cmd = "x" * 500
        monkeypatch.setattr("sys.stdin", io.StringIO(_payload(command=long_cmd)))
        hook.main()
        log = (tmp_path / "audit.log").read_text()
        assert "..." in log
        # Total line should be much shorter than the original command
        assert len(log) < 400

    def test_collapses_newlines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        # Payload uses \\n in JSON to encode a real newline.
        payload = (
            '{"hook_event_name":"PostToolUse",'
            '"tool_name":"Bash",'
            '"tool_input":{"command":"line1\\nline2"},'
            '"session_id":"sess"}'
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        hook.main()
        log = (tmp_path / "audit.log").read_text()
        assert "\\n" not in log  # no escaped newlines either
        assert "line1 line2" in log


class TestNonBashSkipped:
    def test_edit_tool_does_not_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(_payload(tool_name="Edit", command="should not log")),
        )
        rc = hook.main()
        assert rc == 0
        # No audit log file should have been created
        assert not (tmp_path / "audit.log").exists()


class TestEmptyCommand:
    def test_empty_string_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr("sys.stdin", io.StringIO(_payload(command="")))
        rc = hook.main()
        assert rc == 0
        assert not (tmp_path / "audit.log").exists()


class TestFilePermissions:
    def test_audit_log_is_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        monkeypatch.setattr("sys.stdin", io.StringIO(_payload(command="touch x")))
        hook.main()
        log_path = tmp_path / "audit.log"
        assert log_path.exists()
        mode = log_path.stat().st_mode & 0o777
        assert mode == 0o600
