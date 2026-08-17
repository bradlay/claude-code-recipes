# Run a subprocess in its own process group and reap the whole group on
# timeout.
#
# Why this module exists
# -----------------------
# ``subprocess.run(argv, timeout=...)`` kills only the DIRECT child.
# Anything that child spawned reparents to init and keeps running. A
# hook-invoked helper that shells out (a nested CLI call, a linter, a
# formatter) can leave a tree of orphaned grandchildren behind it once the
# timeout fires on the parent alone — for example a worker shell and its
# whole-home `find` descendant, still running long after the hook that
# spawned them has already returned.
#
# The termination guarantee (deliberately NARROW, do not widen this text)
# -----------------------------------------------------------------------
# Children are spawned with ``start_new_session=True``, so each child leads
# its own process group (pgid == child pid). On timeout we group-signal
# that pgid: ``killpg(SIGTERM)`` then a bounded grace then ``killpg(SIGKILL)``.
#
# * **Termination**: every descendant *remaining in the child's process
#   group* is terminated. That is exactly what ``killpg`` gives us, no more.
# * **Reaping**: ONLY our own direct child is reaped. Without subreaper
#   support (unavailable on macOS) this process is not the parent of a
#   grandchild and therefore cannot ``wait()`` on one, even one still
#   inside the group. So reap telemetry is defined ONLY over direct
#   children.
# * **Outside the guarantee**: a descendant that double-forks AND calls
#   ``setsid`` leaves the process group entirely. We do not claim to
#   terminate it, and we do not claim to detect it. It survives.
#
# Safety: the recorded pgid is asserted to be a *new* group (``pgid ==
# child.pid``) and never the caller's (``os.getpgrp()``) before any group
# signal is sent. Otherwise a group-kill would take out the caller, its
# hook, or the operator's interactive shell. If that assertion fails we
# downgrade to signalling the direct child only and record
# ``pgid_safe: false``.

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Grace between the group TERM and the group KILL.
DEFAULT_KILL_GRACE_S = 1.5
# Bounded drain of a killed child's pipes: never let cleanup itself hang.
DRAIN_TIMEOUT_S = 1.0
# Bounded `ps` enumeration used to count process-group members. Still
# bounded, so cleanup can never hang, but well clear of a loaded machine.
PS_TIMEOUT_S = 5.0
# System locations for `ps`, tried in order before any PATH lookup, so a
# shim directory or a planted binary earlier on PATH cannot shadow the
# real one. /bin/ps on macOS and Debian, /usr/bin/ps where /bin is a
# symlink or absent.
_PS_CANDIDATES = ("/bin/ps", "/usr/bin/ps")


@dataclass
class GroupCleanup:
    """Measurable facts about a timeout cleanup, no claim beyond what we did.

    ``in_group_alive_after_grace`` is the count AFTER the TERM grace and
    BEFORE the KILL (a signal-resistant child is legitimately non-zero
    here). ``in_group_alive_after_kill`` is the count after the full
    TERM/grace/KILL sequence; that is the number the termination guarantee
    is measured on. Both are ``None`` when the group could not be
    enumerated (``ps`` failed); "unknown" is never reported as zero.
    """

    caller_pgid: int
    child_pgid: int | None = None
    pgid_safe: bool = False
    group_kill_issued: bool = False
    members_signalled: int | None = None
    in_group_alive_after_grace: int | None = None
    in_group_alive_after_kill: int | None = None
    escalated_to_kill: bool = False
    direct_child_reaped: bool = False
    direct_child_returncode: int | None = None
    unsafe_reason: str | None = None

    def as_fields(self) -> dict[str, Any]:
        """Flatten for a structured telemetry record."""
        return {
            "caller_pgid": self.caller_pgid,
            "child_pgid": self.child_pgid,
            "pgid_safe": self.pgid_safe,
            "group_kill_issued": self.group_kill_issued,
            "members_signalled": self.members_signalled,
            "in_group_alive_after_grace": self.in_group_alive_after_grace,
            "in_group_alive_after_kill": self.in_group_alive_after_kill,
            "escalated_to_kill": self.escalated_to_kill,
            "direct_child_reaped": self.direct_child_reaped,
            "direct_child_returncode": self.direct_child_returncode,
            "unsafe_reason": self.unsafe_reason,
        }


@dataclass
class ReapedResult:
    """Outcome of one reaped call."""

    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_failed: bool = False
    skipped: bool = False
    spawn_ms: int = 0
    wait_ms: int = 0
    cleanup_ms: int = 0
    cleanup: GroupCleanup | None = None

    @property
    def ok(self) -> bool:
        """True only when the child ran to completion with exit 0."""
        return self.returncode == 0 and not self.timed_out


def group_members(pgid: int) -> list[int] | None:
    """PIDs currently in ``pgid``, or None if the group could not be
    enumerated.

    ``None`` is a real value here: an unreadable group is UNKNOWN, and
    unknown must never be reported as zero survivors.

    ``ps`` is taken from a fixed list of system locations FIRST, and only
    falls back to a PATH lookup when none of them exists — a shim or a
    planted ``ps`` earlier on PATH must not shadow the real one.
    """
    ps_path = next(
        (p for p in _PS_CANDIDATES if os.path.exists(p)),
        None,
    ) or shutil.which("ps")
    if ps_path is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            [ps_path, "-A", "-o", "pid=,pgid="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    members: list[int] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, gid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if gid == pgid:
            members.append(pid)
    return members


def resolve_pgid(proc: subprocess.Popen[str], caller_pgid: int) -> GroupCleanup:
    """Record the child's pgid and decide whether it is safe to
    group-signal.

    Safe ONLY when the child leads a brand-new group (pgid == child pid)
    that is not the caller's. Anything else (setsid failed, group reused,
    lookup raced a fast exit) downgrades to direct-child signalling: we
    never risk killing the wrapper, the hook, or the operator's shell.
    """
    cleanup = GroupCleanup(caller_pgid=caller_pgid)
    try:
        pgid = os.getpgid(proc.pid)
    except OSError as exc:  # child already reaped/exited, no group to signal
        cleanup.unsafe_reason = f"getpgid failed: {exc.__class__.__name__}"
        return cleanup
    cleanup.child_pgid = pgid
    if pgid == caller_pgid:
        cleanup.unsafe_reason = "child pgid == caller pgid (start_new_session did not take)"
        return cleanup
    if pgid <= 0:
        cleanup.unsafe_reason = "non-positive pgid"
        return cleanup
    if pgid != proc.pid:
        cleanup.unsafe_reason = "child does not lead its own group"
        return cleanup
    cleanup.pgid_safe = True
    return cleanup


def terminate_group(proc: subprocess.Popen[str], cleanup: GroupCleanup, grace: float) -> None:
    """TERM then bounded grace then KILL, group-wide when the pgid is safe.

    Reaps ONLY ``proc`` (our direct child). Grandchildren are terminated by
    the group signal but cannot be reaped here: we are not their parent
    (see the module docstring; this limit is deliberate and NOT worked
    around).
    """
    pgid = cleanup.child_pgid

    if cleanup.pgid_safe and pgid is not None:
        members = group_members(pgid)
        cleanup.members_signalled = None if members is None else len(members)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
            cleanup.group_kill_issued = True
    else:
        # Unsafe pgid: signal the direct child ONLY. Never killpg the caller's group.
        with contextlib.suppress(OSError):
            proc.terminate()

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)

    if cleanup.pgid_safe and pgid is not None:
        alive = group_members(pgid)
        cleanup.in_group_alive_after_grace = None if alive is None else len(alive)
        if alive is None or alive:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
                cleanup.escalated_to_kill = True
    elif proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()
            cleanup.escalated_to_kill = True

    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=DRAIN_TIMEOUT_S)

    if cleanup.pgid_safe and pgid is not None and cleanup.escalated_to_kill:
        # SIGKILL is not synchronous; give the kernel a moment before counting.
        deadline = time.monotonic() + DRAIN_TIMEOUT_S
        remaining_members = group_members(pgid)
        while remaining_members and time.monotonic() < deadline:
            time.sleep(0.02)
            remaining_members = group_members(pgid)
        cleanup.in_group_alive_after_kill = (
            None if remaining_members is None else len(remaining_members)
        )
    elif cleanup.pgid_safe and pgid is not None:
        cleanup.in_group_alive_after_kill = cleanup.in_group_alive_after_grace

    cleanup.direct_child_returncode = proc.poll()
    cleanup.direct_child_reaped = cleanup.direct_child_returncode is not None


def reaped_run(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    stdin_input: str | None = None,
    env: dict[str, str] | None = None,
    kill_grace: float = DEFAULT_KILL_GRACE_S,
    on_cancel: Callable[[ReapedResult], None] | None = None,
) -> ReapedResult:
    """Run ``cmd`` in its OWN process group under a hard timeout.

    On timeout the whole group is terminated (TERM, grace, KILL) so a slow
    grandchild cannot outlive the deadline or keep our pipes from closing.

    Guarantees are exactly those in the module docstring: group
    TERMINATION for in-group descendants, reaping for the direct child
    only, and NO claim over a descendant that double-forks and setsids out
    of the group.

    ``timeout <= 0`` means "no budget left": the call is skipped, not run.
    A spawn failure is reported as ``spawn_failed`` rather than raised, so
    a caller can classify it alongside a non-zero exit. ``on_cancel``
    fires when the CALLER is cancelled (SIGINT, harness cancellation)
    after the group has been cleaned up and before the exception is
    re-raised.
    """
    result = ReapedResult()
    if timeout <= 0:
        result.skipped = True
        return result

    caller_pgid = os.getpgrp()
    t_spawn = time.perf_counter()
    try:
        proc = subprocess.Popen(  # noqa: S603 (fixed argv, never a shell string)
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if stdin_input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group -> safe to killpg
        )
    except (OSError, ValueError) as exc:
        result.spawn_failed = True
        result.spawn_ms = round((time.perf_counter() - t_spawn) * 1000)
        result.stderr = f"{exc.__class__.__name__}: {exc}"
        return result

    result.spawn_ms = round((time.perf_counter() - t_spawn) * 1000)
    cleanup = resolve_pgid(proc, caller_pgid)
    result.cleanup = cleanup

    t_wait = time.perf_counter()
    try:
        stdout, stderr = proc.communicate(input=stdin_input, timeout=timeout)
        result.stdout, result.stderr = stdout or "", stderr or ""
        result.returncode = proc.returncode
        cleanup.direct_child_reaped = True
        cleanup.direct_child_returncode = proc.returncode
    except subprocess.TimeoutExpired:
        result.timed_out = True
        t_cleanup = time.perf_counter()
        terminate_group(proc, cleanup, kill_grace)
        # Bounded drain: collect whatever the child managed to emit. If the
        # pipes are still held open (only possible from OUTSIDE the group,
        # i.e. an escaped descendant), give up rather than hang the caller.
        try:
            stdout, stderr = proc.communicate(timeout=DRAIN_TIMEOUT_S)
            result.stdout, result.stderr = stdout or "", stderr or ""
        except (subprocess.TimeoutExpired, ValueError, OSError):
            result.stderr = "(output unavailable: pipes still held after group kill)"
        result.returncode = proc.poll()
        result.cleanup_ms = round((time.perf_counter() - t_cleanup) * 1000)
    except BaseException:
        # The CALLER was cancelled (SIGINT/SIGTERM -> KeyboardInterrupt,
        # harness cancellation, ...). Clean up the group before the
        # exception escapes: a cancelled caller must not leak a running
        # process tree.
        terminate_group(proc, cleanup, kill_grace)
        if on_cancel is not None:
            on_cancel(result)
        raise
    finally:
        result.wait_ms = round((time.perf_counter() - t_wait) * 1000)

    return result


__all__ = [
    "DEFAULT_KILL_GRACE_S",
    "DRAIN_TIMEOUT_S",
    "PS_TIMEOUT_S",
    "GroupCleanup",
    "ReapedResult",
    "group_members",
    "reaped_run",
    "resolve_pgid",
    "terminate_group",
]
