"""Unit tests for stop-memory-hygiene."""

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
    / "stop-memory-hygiene"
    / "scripts"
    / "stop_memory_hygiene_hook.py"
)
_spec = importlib.util.spec_from_file_location("stop_memory_hygiene_hook", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def _project(tmp_path: Path, slug: str = "proj") -> tuple[Path, str]:
    """Create <tmp>/.claude/projects/<slug>/memory and return (memory dir, transcript path)."""
    mem = tmp_path / ".claude" / "projects" / slug / "memory"
    mem.mkdir(parents=True)
    transcript = str(tmp_path / ".claude" / "projects" / slug / "session.jsonl")
    return mem, transcript


def _run(
    transcript: str | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    payload = "{}" if transcript is None else json.dumps({"transcript_path": transcript})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert hook.main() == 0
    out, _ = capsys.readouterr()
    result: dict[str, object] = json.loads(out)
    return result


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CLAUDE_MEMORY_HYGIENE_SIZE_KB",
        "CLAUDE_MEMORY_HYGIENE_LONG_LINES",
        "CLAUDE_MEMORY_HYGIENE_ORPHANS",
        "CLAUDE_MEMORY_HYGIENE_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))


class TestMemoryDir:
    def test_posix_path(self, tmp_path: Path) -> None:
        mem, transcript = _project(tmp_path)
        assert hook.memory_dir(transcript) == mem

    def test_missing_memory_dir(self, tmp_path: Path) -> None:
        transcript = str(tmp_path / ".claude" / "projects" / "x" / "s.jsonl")
        assert hook.memory_dir(transcript) is None

    def test_unrelated_path(self) -> None:
        assert hook.memory_dir("/var/tmp/session.jsonl") is None


class TestLimits:
    def test_defaults(self) -> None:
        lim = hook.limits()
        assert lim == {
            "size_bytes": 110 * 1024,
            "long_lines": 60,
            "orphans": 25,
            "interval": 86400,
        }

    def test_env_override_and_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_MEMORY_HYGIENE_SIZE_KB", "1")
        monkeypatch.setenv("CLAUDE_MEMORY_HYGIENE_ORPHANS", "not-a-number")
        lim = hook.limits()
        assert lim["size_bytes"] == 1024
        assert lim["orphans"] == 25


class TestStats:
    def test_orphans_respect_both_link_styles(self, tmp_path: Path) -> None:
        mem, _ = _project(tmp_path)
        (mem / "MEMORY.md").write_text("- [A](a.md)\n- [[b]]\n", encoding="utf-8")
        for name in ("a.md", "b.md", "c.md", "d.md"):
            (mem / name).write_text("x", encoding="utf-8")
        _, _, orphans = hook.stats(mem)
        assert orphans == 2

    def test_long_lines_counted(self, tmp_path: Path) -> None:
        mem, _ = _project(tmp_path)
        (mem / "MEMORY.md").write_text("y" * 201 + "\nshort\n", encoding="utf-8")
        _, long_lines, _ = hook.stats(mem)
        assert long_lines == 1


class TestMainEndToEnd:
    def test_no_transcript_passes_through(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _run(None, capsys, monkeypatch) == {"continue": True}

    def test_malformed_stdin_passes_through(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert hook.main() == 0
        out, _ = capsys.readouterr()
        assert json.loads(out) == {"continue": True}

    def test_small_index_is_silent(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mem, transcript = _project(tmp_path)
        (mem / "MEMORY.md").write_text("- [A](a.md)\n", encoding="utf-8")
        assert _run(transcript, capsys, monkeypatch) == {"continue": True}

    def test_over_budget_notifies_once(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mem, transcript = _project(tmp_path)
        (mem / "MEMORY.md").write_text("z\n" * 60_000, encoding="utf-8")

        first = _run(transcript, capsys, monkeypatch)
        message = first.get("systemMessage")
        assert isinstance(message, str)
        assert "MEMORY.md is 117KB" in message

        stamp = tmp_path / "plugin-data" / "proj.last-notice"
        assert stamp.exists()
        assert not (mem / ".hygiene-last-notice").exists()

        second = _run(transcript, capsys, monkeypatch)
        assert second == {"continue": True}

    def test_interval_expiry_notifies_again(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mem, transcript = _project(tmp_path)
        (mem / "MEMORY.md").write_text("z\n" * 60_000, encoding="utf-8")
        stamp = tmp_path / "plugin-data" / "proj.last-notice"
        stamp.parent.mkdir(parents=True)
        stamp.touch()
        old = time.time() - 2 * 86400
        os.utime(stamp, (old, old))
        assert "systemMessage" in _run(transcript, capsys, monkeypatch)

    def test_stamp_falls_back_to_memory_dir(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        mem, transcript = _project(tmp_path)
        (mem / "MEMORY.md").write_text("z\n" * 60_000, encoding="utf-8")
        assert "systemMessage" in _run(transcript, capsys, monkeypatch)
        assert (mem / ".hygiene-last-notice").exists()
