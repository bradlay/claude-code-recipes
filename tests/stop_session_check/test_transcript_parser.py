"""Unit tests for the bounded session-scoped transcript parser."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from _lib import transcript_parser


def _row(tool_name: str, file_path: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": tool_name, "input": {"file_path": file_path}}
                ]
            },
        }
    )


class TestConfinement:
    def test_missing_path_returns_none(self) -> None:
        assert transcript_parser.edited_files(None) is None

    def test_path_outside_confined_root_returns_none(self, tmp_path: Path) -> None:
        outside = tmp_path / "transcript.jsonl"
        outside.write_text(_row("Edit", str(tmp_path / "a.py")) + "\n")
        assert transcript_parser.edited_files(str(outside)) is None

    def test_nonexistent_confined_path_returns_none(self) -> None:
        fake = Path.home() / ".claude" / "projects" / "does-not-exist.jsonl"
        assert transcript_parser.edited_files(str(fake)) is None


class TestEditAttribution:
    def _confined_transcript(self, tmp_path: Path, lines: list[str]) -> Path:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("\n".join(lines) + "\n")
        return transcript

    def test_extracts_edit_and_write_paths(self, tmp_path: Path) -> None:
        transcript = self._confined_transcript(
            tmp_path,
            [_row("Edit", "/repo/a.py"), _row("Write", "/repo/b.py")],
        )
        with mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path):
            result = transcript_parser.edited_files(str(transcript))
        assert result == {"/repo/a.py", "/repo/b.py"}

    def test_bash_tool_not_attributed(self, tmp_path: Path) -> None:
        row = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf /repo"}}
                    ]
                },
            }
        )
        transcript = self._confined_transcript(tmp_path, [row])
        with mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path):
            result = transcript_parser.edited_files(str(transcript))
        assert result == set()

    def test_no_edits_returns_empty_set_not_none(self, tmp_path: Path) -> None:
        transcript = self._confined_transcript(tmp_path, [json.dumps({"type": "user"})])
        with mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path):
            result = transcript_parser.edited_files(str(transcript))
        assert result == set()

    def test_relative_path_resolved_against_cwd(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        transcript = self._confined_transcript(tmp_path, [_row("Edit", "a.py")])
        with mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path):
            result = transcript_parser.edited_files(str(transcript), cwd=cwd)
        assert result == {str((cwd / "a.py").resolve())}

    def test_malformed_row_is_skipped_not_escalated_to_none(self, tmp_path: Path) -> None:
        transcript = self._confined_transcript(
            tmp_path,
            ['{"type": "assistant", "message": not valid json "tool_use"', _row("Edit", "/repo/a.py")],
        )
        with mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path):
            result = transcript_parser.edited_files(str(transcript))
        assert result == {"/repo/a.py"}

    def test_oversized_transcript_is_bounded_not_unreadable(self, tmp_path: Path) -> None:
        transcript = tmp_path / "big.jsonl"
        filler = json.dumps({"type": "user", "message": {"content": "x" * 2000}})
        lines = [filler for _ in range(200)]
        lines.append(_row("Edit", "/repo/tail.py"))
        transcript.write_text("\n".join(lines) + "\n")
        with (
            mock.patch.object(transcript_parser, "_TRANSCRIPT_ROOT", tmp_path),
            mock.patch.object(transcript_parser, "_MAX_READ_BYTES", 4096),
        ):
            result = transcript_parser.edited_files(str(transcript))
        assert result == {"/repo/tail.py"}
