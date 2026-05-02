# Plan review entry point used by both the hook and the CLI.
#
# Concurrency: lock is path-only (sha256(abs_path)[:12]_<stem>.lock) so all
# reviews for the same plan path serialize, regardless of edits between
# them. fcntl.flock(LOCK_EX | LOCK_NB) with deny-if-busy semantics. Never
# proceeds without the lock. Stale-lock detection via os.kill(pid, 0).
#
# State: file key includes a content hash so plan edits start a fresh
# iteration counter. Writes are atomic (tempfile + os.replace) and 0600.
# On clean review (no P0/P1), all state files for the plan path are
# unlinked so the next ExitPlanMode starts at iteration 1.

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .chain import DEFAULT_CHAINS, ChainResult, run_chain

logger = logging.getLogger(__name__)

_SECURE_FILE_MODE = 0o600
_STATE_RETAIN = 50
_LOCK_BUSY_RC = 3


@dataclass
class ReviewOutcome:
    """Result of a review call. The hook translates this to a hook decision;
    the CLI pretty-prints it."""

    status: str
    """One of: clean, p2_only, blocking, provider_failed, busy, plan_too_short"""
    findings: list[dict[str, Any]]
    questions: list[str]
    provider: str
    iteration: int
    elapsed_seconds: float
    busy_reason: str = ""
    error: str = ""


def _path_lock_key(plan_path: Path) -> str:
    """Path-only key. Locks serialize per plan path, regardless of edits."""
    abs_path = str(plan_path.resolve())
    path_hash = hashlib.sha256(abs_path.encode()).hexdigest()[:12]
    return f"{path_hash}_{plan_path.stem}"


def _state_key(plan_path: Path, content_hash: str) -> str:
    """State key includes content hash so edits invalidate iteration history."""
    return f"{_path_lock_key(plan_path)}_{content_hash[:12]}"


def _state_path(plan_path: Path, content_hash: str) -> Path:
    return paths.review_state_dir() / f"_state_{_state_key(plan_path, content_hash)}.json"


def _lock_path(plan_path: Path) -> Path:
    return paths.review_state_dir() / f"{_path_lock_key(plan_path)}.lock"


def _sentinel_path(plan_path: Path) -> Path:
    return paths.review_state_dir() / f"{_path_lock_key(plan_path)}.in-progress"


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    Path(tmp.name).replace(path)
    with contextlib.suppress(OSError):
        path.chmod(_SECURE_FILE_MODE)


def _load_state(plan_path: Path, content_hash: str) -> dict[str, Any]:
    sp = _state_path(plan_path, content_hash)
    if sp.exists():
        try:
            loaded: dict[str, Any] = json.loads(sp.read_text())
            return loaded
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "iteration": 0,
        "previous_findings": [],
        "plan_hash": content_hash,
        "last_reviewed_at": "",
        "last_review_status": "not_reviewed",
        "last_review_findings_count": 0,
    }


def _save_state(plan_path: Path, content_hash: str, state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        _atomic_write(_state_path(plan_path, content_hash), json.dumps(state, indent=2) + "\n")


def _prune_state_dir() -> None:
    with contextlib.suppress(OSError):
        entries = sorted(
            paths.review_state_dir().glob("_state_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in entries[_STATE_RETAIN:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def _clean_state_for_plan(plan_path: Path) -> None:
    """On clean review, sweep all state files for this plan path."""
    prefix = f"_state_{_path_lock_key(plan_path)}_"
    with contextlib.suppress(OSError):
        for path in paths.review_state_dir().glob(f"{prefix}*.json"):
            with contextlib.suppress(OSError):
                path.unlink()


def _read_sentinel(plan_path: Path) -> dict[str, Any] | None:
    sp = _sentinel_path(plan_path)
    if not sp.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(sp.read_text())
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


INITIAL_REVIEW_PROMPT = """\
Review this implementation plan. Identify issues by severity:
- P0 (Critical): Will cause data loss, security vulnerability, or system outage
- P1 (High): Will cause incorrect behavior or significant technical debt
- P2 (Medium): Design improvements or missing edge cases

Focus on: completeness, correctness, dependency ordering, security, risk.
Only flag genuine issues with concrete recommendations.

If you need clarification on any aspect of the plan before you can fully review it,
include those as questions in your response.

PLAN:
{plan_content}

Respond as JSON: {{"findings": [{{"severity": "P0|P1|P2", "title": "...", "description": "...", "recommendation": "..."}}], "questions": ["question 1", "question 2"]}}
Note: "questions" array is optional. Only include if you genuinely need clarification."""

RE_REVIEW_PROMPT = """\
This is review iteration {iteration} of an implementation plan. The previous review \
found {prev_count} issues that the author has attempted to address in the updated plan below.

Your task is focused:
1. Verify each previously flagged issue was adequately addressed in the updated plan
2. Check that the fixes did not introduce NEW issues (regressions)
3. Do NOT raise entirely new concerns unrelated to the previous findings; \
the goal is convergence, not expanding scope

Previous findings that should now be resolved:
{previous_findings_text}

UPDATED PLAN:
{plan_content}

For each previous finding, check if it was fixed. If a fix introduced a regression, flag it.
Only flag genuinely NEW P0/P1 issues if they are critical and directly caused by the plan changes.

Respond as JSON: {{"findings": [{{"severity": "P0|P1|P2", "title": "...", "description": "...", "recommendation": "..."}}], "questions": ["question 1", "question 2"]}}
Note: "questions" array is optional."""


def _extract_title(content: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else "Untitled"


def _detect_project(content: str) -> str | None:
    match = re.search(r"\*\*Project\*\*:\s*`([^`]+)`", content)
    return match.group(1).strip() if match else None


class _PathLock:
    """Acquire a path-only flock with deny-if-busy semantics + stale detection."""

    def __init__(self, plan_path: Path) -> None:
        self.plan_path = plan_path
        self.lock_path = _lock_path(plan_path)
        self.sentinel_path = _sentinel_path(plan_path)
        self._fd: int | None = None

    def acquire(self) -> str | None:
        """Returns None on success, or a busy-reason string on failure."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        for attempt in range(2):
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                # Check if holder is alive via sentinel
                sentinel = _read_sentinel(self.plan_path)
                if sentinel and "pid" in sentinel:
                    pid = int(sentinel["pid"])
                    start_time = float(sentinel.get("start_time", time.time()))
                    age = max(0, int(time.time() - start_time))
                    if _is_pid_alive(pid):
                        return (
                            f"Plan review already in progress for this plan "
                            f"(started {age}s ago, pid {pid}). Try ExitPlanMode "
                            f"again when it completes."
                        )
                    # Stale lock: holder dead. Sweep sentinel and retry once.
                    if attempt == 0:
                        with contextlib.suppress(OSError):
                            self.sentinel_path.unlink(missing_ok=True)
                        continue
                # No sentinel and we couldn't lock: generic contention.
                return "Plan review lock contention. Please retry."
            else:
                self._fd = fd
                self._write_sentinel()
                return None

        return "Plan review lock contention. Please retry."

    def _write_sentinel(self) -> None:
        with contextlib.suppress(OSError):
            _atomic_write(
                self.sentinel_path,
                json.dumps({"pid": os.getpid(), "start_time": time.time()}),
            )

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            self.sentinel_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None


def review_plan(
    plan_path: Path,
    *,
    chain: list[str] | None = None,
    reset: bool = False,
) -> ReviewOutcome:
    """Acquire path lock, run review, return outcome.

    The hook calls this and emits hook-decision JSON; the CLI pretty-prints.
    Lock-busy returns immediately with status="busy". Callers must not
    proceed without the lock.
    """
    plan_content = plan_path.read_text()
    if len(plan_content.strip()) < 100:
        return ReviewOutcome(
            status="plan_too_short",
            findings=[],
            questions=[],
            provider="",
            iteration=0,
            elapsed_seconds=0.0,
            error="plan too short (<100 chars)",
        )

    content_hash = hashlib.sha256(plan_content.encode()).hexdigest()
    plan_title = _extract_title(plan_content)
    project = _detect_project(plan_content)

    lock = _PathLock(plan_path)
    busy = lock.acquire()
    if busy is not None:
        return ReviewOutcome(
            status="busy",
            findings=[],
            questions=[],
            provider="",
            iteration=0,
            elapsed_seconds=0.0,
            busy_reason=busy,
        )

    try:
        state = _load_state(plan_path, content_hash)
        if reset or state.get("plan_hash", "") == "":
            state = {
                "iteration": 0,
                "previous_findings": [],
                "plan_hash": content_hash,
                "last_reviewed_at": "",
                "last_review_status": "not_reviewed",
                "last_review_findings_count": 0,
            }

        state["iteration"] += 1
        iteration = state["iteration"]
        previous_findings = state.get("previous_findings", [])

        metadata = {
            "plan_path": str(plan_path),
            "plan_filename": plan_path.name,
            "plan_title": plan_title,
            "iteration": iteration,
            "project": project,
            "plan_hash": content_hash,
        }

        if iteration <= 2 or not previous_findings:
            prompt = INITIAL_REVIEW_PROMPT.format(plan_content=plan_content)
        else:
            prev_lines: list[str] = []
            for i, f in enumerate(previous_findings, 1):
                sev = f.get("severity", "P2")
                title = f.get("title", "Untitled")
                desc = f.get("description", "")
                prev_lines.append(f"  {i}. [{sev}] {title}: {desc[:200]}")

            prompt = RE_REVIEW_PROMPT.format(
                iteration=iteration,
                prev_count=len(previous_findings),
                previous_findings_text="\n".join(prev_lines),
                plan_content=plan_content,
            )

        logger.info(
            "plan_review_start: plan=%r plan_size=%d iteration=%d chain=%r",
            str(plan_path),
            len(plan_content),
            iteration,
            chain,
        )

        result: ChainResult = run_chain(prompt, chain=chain, mode="plan", metadata=metadata)

        findings = result.findings or []
        p0_p1 = [f for f in findings if f.get("severity") in ("P0", "P1")]
        p2_only = len(p0_p1) == 0 and len(findings) > 0
        has_blocking = len(p0_p1) > 0

        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state["previous_findings"] = findings
        state["plan_hash"] = content_hash
        state["last_reviewed_at"] = now
        state["last_review_findings_count"] = len(findings)

        if has_blocking:
            state["last_review_status"] = "blocking"
            status = "blocking"
        elif p2_only:
            state["last_review_status"] = "p2_only"
            status = "p2_only"
        elif result.provider:
            state["last_review_status"] = "clean"
            status = "clean"
        else:
            state["last_review_status"] = "provider_failed"
            status = "provider_failed"

        if status == "clean":
            # Loop closed. Sweep all state files for this plan path so
            # the next ExitPlanMode starts fresh at iteration 1.
            _clean_state_for_plan(plan_path)
        else:
            _save_state(plan_path, content_hash, state)
            _prune_state_dir()

        return ReviewOutcome(
            status=status,
            findings=findings,
            questions=result.questions or [],
            provider=result.provider,
            iteration=iteration,
            elapsed_seconds=result.elapsed_seconds,
        )
    finally:
        lock.release()


def default_chain() -> list[str]:
    return list(DEFAULT_CHAINS["plan"])
