# Minimal I/O for the PostToolUse(Bash) audit hook: stdin parsing, raw
# archive, per-event log, and a pass-through emit. Fail-open by default
# since the audit log is a convenience, not a gate.

from __future__ import annotations

import contextlib
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

_RAW_JSONL_MAX_BYTES = 20 * 1024 * 1024
_SECURE_FILE_MODE = 0o600

_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "session_id",
        "transcript_path",
        "cwd",
        "hook_event_name",
        "tool_name",
        "tool_input",
        "tool_response",
        "prompt",
        "matcher",
        "message",
        "stop_hook_active",
        "source",
        "permission_mode",
        "tool_use_id",
    },
)


@dataclasses.dataclass(frozen=True)
class HookInvocation:
    event: str
    cwd: Path
    session_id: str
    tool_name: str | None
    tool_input: dict[str, Any]
    raw: dict[str, Any]
    hook_script: str


def parse_stdin(hook_script: str) -> HookInvocation:
    try:
        payload = sys.stdin.read()
    except OSError:
        payload = ""

    raw: dict[str, Any] = {}
    if payload:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                raw = parsed

    cwd_raw = raw.get("cwd") or str(Path.cwd())
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_name_raw = raw.get("tool_name")
    inv = HookInvocation(
        event=str(raw.get("hook_event_name") or "PostToolUse"),
        cwd=Path(cwd_raw),
        session_id=str(raw.get("session_id", "unknown")),
        tool_name=str(tool_name_raw) if tool_name_raw else None,
        tool_input=tool_input,
        raw=raw,
        hook_script=hook_script,
    )

    _archive_raw(inv)
    return inv


def _event_log_path(event: str) -> Path:
    return paths.hooks_log_dir() / f"{event}.log"


def log(inv: HookInvocation, msg: str, *, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    with contextlib.suppress(OSError):
        print(f"[bash-audit:{inv.event}] {msg}", file=sys.stderr)
    with contextlib.suppress(OSError):
        path = _event_log_path(inv.event)
        existed = path.exists()
        with path.open("a") as f:
            f.write(line + "\n")
        if not existed:
            with contextlib.suppress(OSError):
                path.chmod(_SECURE_FILE_MODE)


def _archive_raw(inv: HookInvocation) -> None:
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": inv.event,
        "hook_script": inv.hook_script,
        "raw": inv.raw,
    }
    with contextlib.suppress(OSError):
        path = paths.hooks_raw_jsonl()
        existed = path.exists()
        if existed and path.stat().st_size > _RAW_JSONL_MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
            existed = False
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if not existed:
            with contextlib.suppress(OSError):
                path.chmod(_SECURE_FILE_MODE)

    unknown = [k for k in inv.raw if k not in _KNOWN_TOP_LEVEL_KEYS]
    if unknown:
        log(
            inv,
            f"unknown top-level keys from host CLI (update _io.py): {sorted(unknown)}",
            level="warn",
        )


def _write_stdout(payload: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()


def emit_continue() -> int:
    """Pass-through. PostToolUse hooks don't need to emit anything to
    let the tool result through; an empty JSON object is the standard
    no-op envelope."""
    _write_stdout({})
    return 0
