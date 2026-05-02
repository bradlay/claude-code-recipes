# Minimal I/O for the PreCompact hook: stdin parsing, raw archive,
# per-event log, and the systemMessage envelope. Fail-open by default
# since this hook is informational.

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
        "trigger",  # PreCompact: "manual" or "auto"
        "custom_instructions",  # PreCompact: user's compact instructions, if any
        "permission_mode",
        "tool_use_id",
    },
)


@dataclasses.dataclass(frozen=True)
class HookInvocation:
    event: str
    cwd: Path
    raw: dict[str, Any]
    hook_script: str


def parse_stdin(hook_script: str) -> HookInvocation:
    try:
        payload = sys.stdin.read()
    except OSError:
        payload = ""

    # Failure to decode stdin JSON is silent: the hook is informational
    # and a malformed payload from a host-CLI version drift shouldn't
    # block compaction. The empty raw dict propagates downstream.
    raw: dict[str, Any] = {}
    if payload:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                raw = parsed

    cwd_raw = raw.get("cwd") or str(Path.cwd())
    inv = HookInvocation(
        event=str(raw.get("hook_event_name") or "PreCompact"),
        cwd=Path(cwd_raw),
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
        print(f"[precompact-keeper:{inv.event}] {msg}", file=sys.stderr)
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


def emit_system_message(message: str) -> int:
    """PreCompact hooks emit a top-level systemMessage that the
    post-compaction model sees as a system note."""
    _write_stdout({"systemMessage": message})
    return 0


def emit_continue() -> int:
    """Pass-through emit when there's nothing to inject."""
    _write_stdout({})
    return 0
