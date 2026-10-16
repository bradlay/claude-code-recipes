# Phase timing + process-group-safe subprocess supervision, reusable by any
# hook step that needs to shell out under a shared deadline.
#
# Why this module exists
# -----------------------
# A hook wrapper has a hard host-enforced timeout (Claude Code kills a slow
# hook outright). If a step inside the hook shells out with its own
# independent timeout, several such calls can each be individually "under
# budget" while their sum blows the wrapper's whole allowance — and when
# that happens the wrapper's timeout carries no information about WHERE the
# time went, only that it overran.
#
# This module provides the two things that fixes that class of failure:
#
# 1. **A budget**, one absolute deadline the wrapper can hand to a step (or a
#    child process, via ``STOP_CHECK_DEADLINE_TS``-style env handover),
#    plus per-phase deadlines. Every subprocess gets
#    ``min(phase_deadline, remaining_total)``, so an expensive optional step
#    degrades (returns "unknown") instead of blowing the total budget.
# 2. **Phase telemetry**, ``PhaseTimer`` records ms per phase so a slow run
#    is attributable to a phase rather than inferred from an absent line.
#
# The process-group machinery itself lives in this plugin's ``reaped_run``
# module; ``supervised_run`` is the hook-shaped wrapper around it — the
# budget semantics (``timeout <= 0`` means "skipped, not run") and the
# structured telemetry are what live here.

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from _lib import paths
from _lib.reaped_run import GroupCleanup, ReapedResult, group_members, reaped_run

# Handover of the wrapper's absolute deadline (epoch seconds, monotonic-free
# so it survives the process boundary) and its spawn timestamp, so a child
# process can attribute its own startup cost, often the largest unmeasured
# phase.
ENV_DEADLINE_TS = "STOP_CHECK_DEADLINE_TS"
ENV_SPAWN_TS = "STOP_CHECK_SPAWN_TS"

SupervisedResult = ReapedResult


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default``."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# --- Deadlines (named constants, all env-tunable) ---------------------------
#
# TOTAL is the wrapper's whole budget. Everything else is a phase budget that
# is strictly less than TOTAL so the wrapper keeps headroom for its own
# cleanup (TERM -> grace -> KILL) and still emits a valid response. A step
# budgets ITSELF against the same absolute deadline, so it aborts optional
# work rather than being killed with no chance to report anything.


def total_deadline_s() -> float:
    """Whole wrapper budget (s)."""
    return _env_float("STOP_CHECK_TOTAL_DEADLINE_S", 10.0)


def subprocess_deadline_s() -> float:
    """Budget (s) for one out-of-process call made by a hook step."""
    return _env_float("STOP_CHECK_SUBPROCESS_DEADLINE_S", 4.0)


def git_deadline_s() -> float:
    """Budget (s) for one `git` invocation."""
    return _env_float("STOP_CHECK_GIT_DEADLINE_S", 3.0)


def advisory_deadline_s() -> float:
    """Budget (s) for an OPTIONAL, cosmetic advisory step.

    Deliberately small: an advisory line must never jeopardise the gate it
    rides along with.
    """
    return _env_float("STOP_CHECK_ADVISORY_DEADLINE_S", 2.0)


def kill_grace_s() -> float:
    """Grace (s) between the group TERM and the group KILL."""
    return _env_float("STOP_CHECK_KILL_GRACE_S", 1.5)


# ============================================================================
# Logging (single writer, structured fields only — never raw stdout/stderr,
# so nothing free-text/secret-shaped reaches the log)
# ============================================================================


def log_event(event: str, **fields: Any) -> None:
    """Append one JSONL line to the stop-check diagnostic log (best-effort).

    Diagnostics must never break a Stop hook, so every failure is
    swallowed. Only structured, caller-supplied fields are written — this
    function never logs a subprocess's stdout/stderr, so nothing free-text
    or secret-shaped from a supervised command reaches the log.
    """
    with contextlib.suppress(Exception):
        log_path = paths.hooks_log_dir() / "stop-check.jsonl"
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with log_path.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")


# ============================================================================
# Budget + phase timing
# ============================================================================


@dataclass
class Budget:
    """One absolute deadline, shared across a wrapper -> step (or child
    process) boundary."""

    deadline_ts: float
    spawn_ts: float

    @classmethod
    def start(cls, seconds: float, spawn_ts: float | None = None) -> Budget:
        """A budget of ``seconds`` starting now."""
        now = time.time()
        return cls(deadline_ts=now + seconds, spawn_ts=spawn_ts if spawn_ts else now)

    @classmethod
    def from_env(cls, fallback_seconds: float) -> Budget:
        """Adopt the wrapper's absolute deadline if it handed one over via
        the environment.

        A stale/garbage value degrades to a fresh local budget rather than
        an instantly-expired one (a hook must never be wedged by bad
        telemetry env).
        """
        raw_deadline = _env_float(ENV_DEADLINE_TS, 0.0)
        raw_spawn = _env_float(ENV_SPAWN_TS, 0.0)
        now = time.time()
        if raw_deadline > now:
            return cls(deadline_ts=raw_deadline, spawn_ts=raw_spawn or now)
        return cls.start(fallback_seconds)

    def remaining(self) -> float:
        """Seconds left before the total deadline (never negative)."""
        return max(0.0, self.deadline_ts - time.time())

    def expired(self, margin: float = 0.25) -> bool:
        """True when too little budget remains to start another
        out-of-process call."""
        return self.remaining() <= margin

    def allot(self, phase_deadline: float) -> float:
        """The timeout for one call: the phase deadline, capped by what's
        left."""
        return max(0.0, min(phase_deadline, self.remaining()))

    def startup_ms(self) -> int | None:
        """ms between the wrapper's spawn and now: a child's own startup
        cost."""
        if not self.spawn_ts:
            return None
        return round((time.time() - self.spawn_ts) * 1000)


class PhaseTimer:
    """Records ms per named phase; emits them as one log record."""

    def __init__(self) -> None:
        self.timings: dict[str, int] = {}
        self.skipped: list[str] = []
        self._t0 = time.perf_counter()

    @contextlib.contextmanager
    def phase(self, name: str) -> Any:
        """Time a phase; the entry is written even if the body raises."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = round((time.perf_counter() - t0) * 1000)

    def skip(self, name: str) -> None:
        """Record that a phase was skipped because the budget was
        exhausted."""
        self.skipped.append(name)
        self.timings.setdefault(name, 0)

    def total_ms(self) -> int:
        """ms since this timer was created."""
        return round((time.perf_counter() - self._t0) * 1000)

    def emit(self, event: str, **fields: Any) -> None:
        """Write the phase table to the stop-check log."""
        log_event(
            event,
            phases_ms=dict(self.timings),
            phases_skipped=list(self.skipped),
            total_ms=self.total_ms(),
            **fields,
        )


# ============================================================================
# Process-group-safe supervision
# ============================================================================


def supervised_run(
    cmd: list[str],
    *,
    timeout: float,
    phase: str,
    cwd: str | None = None,
    stdin_input: str | None = None,
    env: dict[str, str] | None = None,
    log: bool = True,
) -> SupervisedResult:
    """Run ``cmd`` in its OWN process group under a hard timeout, with
    telemetry.

    On timeout the whole group is terminated (TERM -> grace -> KILL) so a
    slow grandchild cannot outlive the deadline or keep our pipes from
    closing.

    Guarantees are exactly those in ``_lib.reaped_run``: group TERMINATION
    for in-group descendants, reaping for the direct child only, and NO
    claim over a descendant that double-forks + setsids out of the group.

    ``timeout <= 0`` means "no budget left": the call is skipped, not run.
    """
    if timeout <= 0:
        result = SupervisedResult()
        result.skipped = True
        if log:
            log_event(
                "supervised", phase=phase, cmd=cmd[0], skipped=True, reason="budget_exhausted"
            )
        return result

    def _log_cancelled(cancelled: SupervisedResult) -> None:
        if log:
            log_event(
                "supervised",
                phase=phase,
                cmd=cmd[0],
                cancelled=True,
                **(cancelled.cleanup.as_fields() if cancelled.cleanup else {}),
            )

    result = reaped_run(
        cmd,
        timeout=timeout,
        cwd=cwd,
        stdin_input=stdin_input,
        env=env,
        kill_grace=kill_grace_s(),
        on_cancel=_log_cancelled,
    )

    if result.spawn_failed:
        if log:
            log_event(
                "supervised",
                phase=phase,
                cmd=cmd[0],
                spawn_failed=True,
                error=result.stderr.split(":", 1)[0],
                spawn_ms=result.spawn_ms,
            )
        return result

    if log:
        log_event(
            "supervised",
            phase=phase,
            cmd=cmd[0],
            timed_out=result.timed_out,
            returncode=result.returncode,
            spawn_ms=result.spawn_ms,
            wait_ms=result.wait_ms,
            cleanup_ms=result.cleanup_ms,
            **(result.cleanup.as_fields() if result.timed_out and result.cleanup else {}),
        )
    return result


__all__ = [
    "ENV_DEADLINE_TS",
    "ENV_SPAWN_TS",
    "Budget",
    "GroupCleanup",
    "PhaseTimer",
    "SupervisedResult",
    "advisory_deadline_s",
    "git_deadline_s",
    "group_members",
    "kill_grace_s",
    "log_event",
    "subprocess_deadline_s",
    "supervised_run",
    "total_deadline_s",
]
