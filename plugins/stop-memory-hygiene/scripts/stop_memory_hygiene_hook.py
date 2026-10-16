#!/usr/bin/env python3
# Stop hook: advisory hygiene check for Claude Code's auto-memory index.
#
# Auto-memory has no pruning mechanism, so MEMORY.md grows until it is too
# large to load usefully. This hook measures it when a session stops and
# suggests a prune as a non-blocking systemMessage, at most once per interval
# per project. It never blocks a stop: any error returns {"continue": true}.

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

_DEFAULT_SIZE_KB = 110
_DEFAULT_LONG_LINES = 60
_DEFAULT_ORPHANS = 25
_DEFAULT_INTERVAL_SECONDS = 24 * 3600
_LONG_LINE_CHARS = 200
_INDEX_NAME = "MEMORY.md"
_STAMP_NAME = ".hygiene-last-notice"

# The project directory is the path up to and including the slug after
# ".claude/projects/". Either separator is accepted so a Windows-style
# transcript path resolves too.
_PROJECT_DIR_RE = re.compile(r"^(.*?[\\/]\.claude[\\/]projects[\\/][^\\/]+)[\\/]")
_MD_LINK_RE = re.compile(r"\]\(([^)#]+\.md)\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        with contextlib.suppress(ValueError):
            return max(0, int(raw))
    return default


def limits() -> dict[str, int]:
    """Thresholds, each overridable by an environment variable."""
    return {
        "size_bytes": _int_env("CLAUDE_MEMORY_HYGIENE_SIZE_KB", _DEFAULT_SIZE_KB) * 1024,
        "long_lines": _int_env("CLAUDE_MEMORY_HYGIENE_LONG_LINES", _DEFAULT_LONG_LINES),
        "orphans": _int_env("CLAUDE_MEMORY_HYGIENE_ORPHANS", _DEFAULT_ORPHANS),
        "interval": _int_env("CLAUDE_MEMORY_HYGIENE_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS),
    }


def memory_dir(transcript_path: str) -> Path | None:
    """Return <project>/memory for a transcript under .claude/projects/<slug>/."""
    match = _PROJECT_DIR_RE.match(transcript_path)
    if match is None:
        return None
    mem = Path(match.group(1)) / "memory"
    return mem if mem.is_dir() else None


def stamp_path(mem: Path) -> Path:
    """Rate-limit stamp: in the plugin data dir when available, so the hook
    does not write into the user's memory directory."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data) / f"{mem.parent.name}.last-notice"
    return mem / _STAMP_NAME


def stats(mem: Path) -> tuple[int, int, int]:
    """(index size in bytes, index lines over the length limit, unlinked topic files)."""
    index = mem / _INDEX_NAME
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (0, 0, 0)
    size = len(text.encode("utf-8"))
    long_lines = sum(1 for line in text.splitlines() if len(line) > _LONG_LINE_CHARS)
    linked = {Path(p).name for p in _MD_LINK_RE.findall(text)}
    linked |= {f"{name}.md" for name in _WIKI_LINK_RE.findall(text)}
    orphans = 0
    with contextlib.suppress(OSError):
        for entry in mem.iterdir():
            if entry.suffix == ".md" and entry.name != _INDEX_NAME and entry.name not in linked:
                orphans += 1
    return (size, long_lines, orphans)


def problems(measured: tuple[int, int, int], lim: dict[str, int]) -> list[str]:
    size, long_lines, orphans = measured
    found: list[str] = []
    if size > lim["size_bytes"]:
        found.append(f"{_INDEX_NAME} is {size // 1024}KB (budget ~{lim['size_bytes'] // 1024}KB)")
    if long_lines > lim["long_lines"]:
        found.append(f"{long_lines} index lines exceed {_LONG_LINE_CHARS} chars")
    if orphans > lim["orphans"]:
        found.append(f"{orphans} topic files are not linked from the index")
    return found


def _recently_notified(stamp: Path, interval: int) -> bool:
    try:
        return time.time() - stamp.stat().st_mtime < interval
    except OSError:
        return False


def _touch(stamp: Path) -> None:
    with contextlib.suppress(OSError):
        stamp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp.touch(mode=0o600)


def _read_payload() -> dict[str, object]:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        parsed = json.loads(sys.stdin.read() or "{}")
        if isinstance(parsed, dict):
            return parsed
    return {}


def decide(payload: dict[str, object]) -> dict[str, object]:
    transcript = payload.get("transcript_path")
    mem = memory_dir(transcript) if isinstance(transcript, str) else None
    if mem is None:
        return {"continue": True}

    lim = limits()
    stamp = stamp_path(mem)
    if _recently_notified(stamp, lim["interval"]):
        return {"continue": True}

    found = problems(stats(mem), lim)
    if not found:
        return {"continue": True}

    _touch(stamp)
    return {
        "systemMessage": (
            "Memory hygiene (advisory): "
            + "; ".join(found)
            + ". Consider a prune pass: trim over-long index lines to pointers "
            "(keep the detail in topic files first), archive superseded orphans "
            "to memory/archive/, and dedupe."
        )
    }


def main() -> int:
    try:
        result = decide(_read_payload())
    except Exception:  # advisory hook: never break a stop
        result = {"continue": True}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
