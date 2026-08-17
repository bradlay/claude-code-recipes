# Bounded transcript JSONL parser: session-scoped edited-file attribution.
#
# The checklist's "uncommitted files" check is repo-wide by default, which
# means its nudge can point at another concurrent session's in-flight work
# in a shared checkout. This module reads the session's own transcript (the
# `transcript_path` the host CLI hands the hook) and extracts the set of
# files THIS session actually edited through an attributable tool
# (Edit/Write/MultiEdit/NotebookEdit), so a caller can scope its nudge to
# files the session actually owns.
#
# `None` vs `set()` matters: `None` means the transcript could not be read
# (missing path, IOError, path outside the confined transcript root) and the
# caller must fail open (fall back to the unscoped, repo-wide check) rather
# than treat "unreadable" as "nothing edited". `set()` means the transcript
# parsed cleanly and the session edited no files through an attributable
# tool (e.g. a conversation-only session).

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Tools whose tool_use call carries a file path the session actually
# modified. Bash is excluded — it can touch anything and attribution to a
# single path is not reliable.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# `transcript_path` arrives from hook stdin JSON. Confine reads to the
# client-owned transcript tree so malformed/hostile stdin can't make the
# hook read an arbitrary file on disk.
_TRANSCRIPT_ROOT = (Path.home() / ".claude" / "projects").resolve()

# Bound the amount of transcript read: transcripts are append-only and can
# grow large over a long session, but only the tail is needed for recent
# edit attribution.
_MAX_READ_BYTES = 16 * 1024 * 1024


def _confined_transcript_path(transcript_path: str | None) -> Path | None:
    """Resolve `transcript_path`, or None if it's missing or escapes the
    confined transcript root (caller degrades safely to unscoped)."""
    if not transcript_path:
        return None
    try:
        resolved = Path(transcript_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_relative_to(_TRANSCRIPT_ROOT):
        return None
    return resolved


def _resolve_edited_path(raw: str, cwd: Path) -> str:
    """Resolve one transcript-attributed edit path without requiring it to
    exist on disk (the file may have since been deleted or moved)."""
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _edit_paths_from_row(row: dict[str, Any], cwd: Path) -> set[str]:
    """Extract explicit Edit/Write/MultiEdit/NotebookEdit paths from one
    transcript row."""
    message = row.get("message")
    if not isinstance(message, dict):
        return set()
    content = message.get("content")
    if not isinstance(content, list):
        return set()

    paths: set[str] = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "tool_use" or item.get("name") not in EDIT_TOOLS:
            continue
        tool_input = item.get("input")
        if not isinstance(tool_input, dict):
            continue
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(raw, str) and raw:
            paths.add(_resolve_edited_path(raw, cwd))
    return paths


def edited_files(transcript_path: str | None, cwd: Path | None = None) -> set[str] | None:
    """Absolute paths of files THIS session edited through an attributable
    tool, read from a bounded tail of its transcript.

    Returns None when the transcript could not be read at all (missing
    path, IOError, path outside the confined transcript root) — the caller
    MUST treat that as "unknown" and fall back to unscoped behavior, never
    as "no files edited".
    """
    path = _confined_transcript_path(transcript_path)
    if path is None:
        return None
    current_cwd = cwd or Path.cwd()
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > _MAX_READ_BYTES:
            with path.open("rb") as f:
                f.seek(size - _MAX_READ_BYTES)
                f.readline()  # discard partial first line
                blob = f.read().decode("utf-8", errors="replace")
        else:
            blob = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    files: set[str] = set()
    for line in blob.splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # One malformed/schema-drifted row is skipped, not escalated to
            # "unreadable" — a single bad line must not poison the whole
            # signal, since the host CLI's transcript schema can shift.
            continue
        if not isinstance(row, dict):
            continue
        files.update(_edit_paths_from_row(row, current_cwd))
    return files


__all__ = ["EDIT_TOOLS", "edited_files"]
