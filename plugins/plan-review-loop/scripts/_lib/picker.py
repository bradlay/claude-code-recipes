# Interactive backend selection for the ExitPlanMode hook.
#
# A Claude Code hook is a non-interactive subprocess (no TTY), so it cannot
# render a menu. Instead, when online and no backend has been chosen for the
# session, the hook denies ExitPlanMode with an instruction telling Claude
# to ask the user via AskUserQuestion and persist the choice through
# `bin/plan-review-select`. The selection is sticky per session.
#
# Untrusted input: `session_id` arrives from the hook JSON and `--session`
# from the Claude-run command, so it is hashed (never used raw) in the state
# filename, and the chosen backend key is validated against the registry.

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

from . import backends, paths
from .probes import ProbeResult

_SECURE_FILE_MODE = 0o600
# After this many denied attempts with no selection written, the hook stops
# looping and fails closed (or honors CLAUDE_PLAN_REVIEW_AUTOSELECT).
MAX_PICKER_ATTEMPTS = 2


def _safe_session(session_id: str) -> str:
    """Hash the (untrusted) session id to a fixed safe filename token."""
    sid = (session_id or "unknown").strip() or "unknown"
    return hashlib.sha256(sid.encode()).hexdigest()[:16]


def _selection_path(session_id: str) -> Path:
    return paths.review_state_dir() / f"backend-{_safe_session(session_id)}.json"


def _load(session_id: str) -> dict[str, Any]:
    path = _selection_path(session_id)
    if not path.exists():
        return {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data: dict[str, Any] = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    return {}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with contextlib.suppress(OSError):
            Path(tmp).chmod(_SECURE_FILE_MODE)
        Path(tmp).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


def read_selection(session_id: str) -> dict[str, Any] | None:
    """Return the chosen backend record for this session, or None. The stored
    key is re-validated against the registry so a stale/removed key never
    flows through to a review."""
    data = _load(session_id)
    key = data.get("backend_key")
    if not isinstance(key, str) or key not in backends.REGISTRY:
        return None
    return data


def write_selection(session_id: str, backend_key: str) -> None:
    """Persist the chosen backend. Raises ValueError on an unknown key so the
    select command surfaces a clear error rather than writing garbage."""
    # Validate against the full registry, NOT picker_keys: the select command
    # runs as its own subprocess that may not inherit CLAUDE_PLAN_REVIEW_LOCAL_URL,
    # so gating `local` here would reject a backend the picker legitimately
    # offered. The offer gate lives in the picker; a `local` pick with no
    # reachable endpoint self-heals at the pre-review re-probe in the hook.
    canonical = backends.normalize_key(backend_key)
    if canonical is None or canonical not in backends.REGISTRY:
        allowed = ", ".join(backends.REGISTRY)
        raise ValueError(f"unknown backend {backend_key!r}; choose one of: {allowed}")
    _atomic_write(
        _selection_path(session_id),
        {"backend_key": canonical, "chain": [canonical], "ts": time.time(), "attempts": 0},
    )


def clear_selection(session_id: str) -> None:
    with contextlib.suppress(OSError):
        _selection_path(session_id).unlink()


def record_attempt(session_id: str) -> int:
    """Increment and return the per-session picker-attempt counter (tracked
    on the same state file before any selection is written)."""
    data = _load(session_id)
    attempts = int(data.get("attempts", 0)) + 1
    data["attempts"] = attempts
    data["ts"] = time.time()
    with contextlib.suppress(OSError):
        _atomic_write(_selection_path(session_id), data)
    return attempts


def _age_label(result: ProbeResult, now: float) -> str:
    if result.last_probed <= 0:
        return "just probed"
    age = int(result.age_seconds(now))
    if age < 90:
        return "verified just now"
    return f"verified {age // 60}m ago"


def build_picker_instruction(
    session_id: str,
    available: list[ProbeResult],
    *,
    select_bin: str,
    data_dir: str,
) -> str:
    """The additionalContext returned on the picker deny: tells Claude to ask
    the user which verified backend to use, then persist it and retry.

    The persist command is fully self-contained (absolute select-script path +
    the resolved CLAUDE_PLUGIN_DATA) because a plain Bash tool call does not
    inherit the plugin env vars the hook runs with, so the selection must land
    in the same data dir the hook reads back."""
    now = time.time()
    # Multi-line strings are built as parenthesized variables (not as adjacent
    # literals inside the list literal) so the implicit concatenation is
    # unambiguous and not a possible-missing-comma footgun.
    header = (
        "Plan-review backend not selected for this session. Only backends "
        "whose auth/model probe currently passes are offered:"
    )
    lines = [header, ""]
    for result in available:
        backend = backends.REGISTRY[result.name]
        lines.append(
            f"  - {result.name}: {backend.label} [model {result.model}; {_age_label(result, now)}]"
        )
    cmd = (
        f"CLAUDE_PLUGIN_DATA={shlex.quote(data_dir)} {shlex.quote(select_bin)} "
        f"--session {shlex.quote(session_id)} <key>"
    )
    step1 = (
        "1. STOP. Do not run the command below and do not choose a backend on "
        "the user's behalf. FIRST call the AskUserQuestion tool to ask the user "
        "which backend to review this plan with (one option per backend key "
        "listed above)."
    )
    step2 = (
        "2. Persist their choice by running this exact command, replacing "
        "<key> with the chosen backend key:"
    )
    step3 = (
        "3. Call ExitPlanMode again — the review will run against the chosen "
        "backend. The choice is remembered for the rest of this session "
        "(use /plan-review-loop:plan-review-backend to change it)."
    )
    lines.extend(
        ["", "ACTION REQUIRED (do NOT pick a backend yourself):", step1, step2, f"   {cmd}", step3]
    )
    return "\n".join(lines)
