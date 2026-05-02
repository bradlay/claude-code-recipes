# Shared I/O for hook scripts in this plugin: stdin parsing, plan-file
# resolution across host-CLI version drift, hook decision emission, and
# per-event logging. CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 turns the fail-closed
# default into fail-open. Raw hook archive at 0600.

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

_RAW_JSONL_MAX_BYTES = 20 * 1024 * 1024  # 20 MB; rotated to .1 on overflow
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
        "source",  # SessionStart: "startup", "compact", "clear", ...
        "permission_mode",  # PreToolUse includes the active permission mode
        "tool_use_id",  # PreToolUse includes the originating tool_use id
        "planFilePath",
        "plan_file_path",
    },
)


@dataclasses.dataclass(frozen=True)
class HookInvocation:
    event: str
    tool_name: str | None
    session_id: str
    cwd: Path
    tool_input: dict[str, Any]
    tool_response: dict[str, Any] | None
    transcript_path: Path | None
    raw: dict[str, Any]
    hook_script: str


_SCRIPT_PREFIX_TO_EVENT: tuple[tuple[str, str], ...] = (
    ("plan_review_hook", "PreToolUse"),
    ("preflight", "SessionStart"),
)


def _infer_event_from_script(hook_script: str) -> str:
    name = Path(hook_script).stem.lower()
    for prefix, event in _SCRIPT_PREFIX_TO_EVENT:
        if name.startswith(prefix):
            return event
    return "Unknown"


def parse_stdin(hook_script: str) -> HookInvocation:
    try:
        payload = sys.stdin.read()
    except OSError as exc:
        _log_event(_infer_event_from_script(hook_script), f"stdin read failed: {exc}")
        payload = ""

    raw: dict[str, Any] = {}
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                raw = parsed
            else:
                _log_event(
                    _infer_event_from_script(hook_script),
                    f"stdin not a JSON object: {type(parsed).__name__}",
                )
        except json.JSONDecodeError as exc:
            _log_event(
                _infer_event_from_script(hook_script),
                f"stdin JSON decode failed: {exc}; first 120 chars: {payload[:120]!r}",
            )

    event = raw.get("hook_event_name") or _infer_event_from_script(hook_script)
    tool_name = raw.get("tool_name")
    session_id = str(raw.get("session_id", "unknown"))
    cwd_raw = raw.get("cwd") or str(Path.cwd())
    transcript_raw = raw.get("transcript_path")

    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_response = raw.get("tool_response")
    if not isinstance(tool_response, dict):
        tool_response = None

    inv = HookInvocation(
        event=str(event),
        tool_name=str(tool_name) if tool_name else None,
        session_id=session_id,
        cwd=Path(cwd_raw),
        tool_input=tool_input,
        tool_response=tool_response,
        transcript_path=Path(transcript_raw) if transcript_raw else None,
        raw=raw,
        hook_script=hook_script,
    )

    _archive_raw(inv)
    _warn_on_unknown_keys(inv)
    return inv


def plans_dir() -> Path:
    return paths.claude_plans_dir()


def resolve_plan_file(inv: HookInvocation) -> Path | None:
    """Find the ExitPlanMode plan file across host-CLI version shapes.

    Fallback order:
      1. tool_input.planFilePath / plan_file_path
      2. raw.planFilePath
      3. $CLAUDE_PLAN_FILE env var
      4. Newest *.md in plans_dir() within
         CLAUDE_PLAN_REVIEW_PLAN_MAX_AGE_SECONDS (default 3600)
    """
    for key in ("planFilePath", "plan_file_path"):
        cand = inv.tool_input.get(key)
        if cand and Path(cand).exists():
            log(inv, f"plan from tool_input.{key}: {cand}")
            return Path(cand)
        if cand:
            log(inv, f"tool_input.{key} not usable ({cand!r})", level="warn")

    top = inv.raw.get("planFilePath")
    if top and Path(top).exists():
        log(inv, f"plan from raw.planFilePath: {top}")
        return Path(top)

    env_path = os.environ.get("CLAUDE_PLAN_FILE")
    if env_path and Path(env_path).exists():
        log(inv, f"plan from $CLAUDE_PLAN_FILE: {env_path}")
        return Path(env_path)

    try:
        max_age = float(os.environ.get("CLAUDE_PLAN_REVIEW_PLAN_MAX_AGE_SECONDS", "3600"))
    except ValueError:
        max_age = 3600.0

    pdir = plans_dir()
    if pdir.is_dir():
        now = time.time()
        candidates = [p for p in pdir.glob("*.md") if (now - p.stat().st_mtime) <= max_age]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            chosen = candidates[0]
            age = now - chosen.stat().st_mtime
            log(inv, f"plan from {pdir} (newest, age={age:.0f}s): {chosen}")
            return chosen
        log(inv, f"no recent *.md in {pdir} (max_age={max_age:.0f}s)", level="warn")
    else:
        log(inv, f"plans dir does not exist: {pdir}", level="warn")

    log(inv, f"raw keys: {sorted(inv.raw.keys())}", level="warn")
    if inv.tool_input:
        log(inv, f"tool_input keys: {sorted(inv.tool_input.keys())}", level="warn")
    return None


def _event_log_path(event: str) -> Path:
    return paths.hooks_log_dir() / f"{event}.log"


def _log_event(event: str, msg: str, *, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    with contextlib.suppress(OSError):
        print(f"[plan-review-loop:{event}] {msg}", file=sys.stderr)
    with contextlib.suppress(OSError):
        path = _event_log_path(event)
        existed = path.exists()
        with path.open("a") as f:
            f.write(line + "\n")
        if not existed:
            with contextlib.suppress(OSError):
                path.chmod(_SECURE_FILE_MODE)


def log(inv: HookInvocation, msg: str, *, level: str = "info") -> None:
    _log_event(inv.event, f"[{inv.session_id}] {msg}", level=level)


def _archive_raw(inv: HookInvocation) -> None:
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": inv.event,
        "tool_name": inv.tool_name,
        "session_id": inv.session_id,
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

    dump_dir = os.environ.get("CLAUDE_PLAN_REVIEW_DUMP_DIR")
    if dump_dir:
        with contextlib.suppress(OSError):
            target_dir = Path(dump_dir)
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            fname = f"{inv.event}-{ts}-{os.getpid()}.json"
            target = target_dir / fname
            target.write_text(json.dumps(inv.raw, indent=2, default=str))
            with contextlib.suppress(OSError):
                target.chmod(_SECURE_FILE_MODE)


def _warn_on_unknown_keys(inv: HookInvocation) -> None:
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


def emit_pretooluse_allow(
    additional_context: str | None = None,
    *,
    system_message: str | None = None,
) -> int:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    }
    if additional_context:
        payload["hookSpecificOutput"]["additionalContext"] = additional_context
    if system_message:
        payload["systemMessage"] = system_message
    _write_stdout(payload)
    return 0


def emit_pretooluse_deny(
    reason: str,
    *,
    additional_context: str | None = None,
) -> int:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
    if additional_context:
        payload["hookSpecificOutput"]["additionalContext"] = additional_context
    _write_stdout(payload)
    return 0


def emit_session_start_context(context: str) -> int:
    """SessionStart hooks emit additionalContext for the new session."""
    _write_stdout(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            },
        },
    )
    return 0


def fail_open_pretooluse(inv: HookInvocation, reason: str) -> int:
    """Allow + advisory context + visible systemMessage explaining the bypass."""
    log(inv, f"fail_open: {reason}", level="warn")
    return emit_pretooluse_allow(
        additional_context=f"{inv.event}: {reason}. Proceeding without review.",
        system_message=(
            f"[plan-review-loop] {inv.event} bypass via CLAUDE_PLAN_REVIEW_FAIL_OPEN=1: {reason}"
        ),
    )


def fail_closed_pretooluse(inv: HookInvocation, reason: str, *, remediation: str = "") -> int:
    """Deny with a clear remediation message."""
    log(inv, f"fail_closed: {reason}", level="error")
    full = reason if not remediation else f"{reason} {remediation}"
    return emit_pretooluse_deny(
        full,
        additional_context=(
            f"plan-review-loop denied ExitPlanMode: {reason}\n\n"
            f"To bypass: set CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 in your shell. "
            f"To disable: /plugin disable plan-review-loop@claude-code-recipes."
        ),
    )


def fail_open_or_closed_pretooluse(inv: HookInvocation, reason: str) -> int:
    """Honor CLAUDE_PLAN_REVIEW_FAIL_OPEN: open if =1, else closed (default)."""
    if os.environ.get("CLAUDE_PLAN_REVIEW_FAIL_OPEN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return fail_open_pretooluse(inv, reason)
    return fail_closed_pretooluse(inv, reason)
