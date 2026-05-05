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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths
from .chain import ChainResult, run_chain

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


# Hard safety net to prevent true runaway in edge cases (provider
# bug, plan that genuinely has unfixable issues). Not the primary
# stop signal — convergence detection in `_assess_progress` is.
# Set to a generous value because real loops should never reach it
# under healthy convergence.
_DEFAULT_SAFETY_MAX_ITERATIONS = 10
_HISTORY_INACTIVITY_DAYS = 30
# Convergence thresholds. At iteration ≥ 2 the runner compares the
# current findings' IDs against the PRIOR iteration's findings. If
# the overlap (Jaccard similarity) is below this threshold, the
# reviewer is finding mostly-new issues each round — drift, not
# convergence. Exit with `divergence_detected` (advisory allow).
_CONVERGENCE_MIN_OVERLAP = 0.5
# Drift must be observed twice in a row before exiting, so a single
# unrelated edit doesn't kill an otherwise-converging loop.
_DRIFT_STREAK_TO_EXIT = 2


def _safety_max_iterations() -> int:
    """Hard safety cap. Convergence detection is the primary exit;
    this exists so a buggy provider can't loop forever.
    """
    raw = os.environ.get("CLAUDE_PLAN_REVIEW_MAX_ITERATIONS", "").strip()
    if not raw:
        return _DEFAULT_SAFETY_MAX_ITERATIONS
    try:
        value = int(raw)
        return value if value > 0 else _DEFAULT_SAFETY_MAX_ITERATIONS
    except ValueError:
        return _DEFAULT_SAFETY_MAX_ITERATIONS


def _path_lock_key(plan_path: Path) -> str:
    """Path-only key. Locks serialize per plan path, regardless of edits."""
    abs_path = str(plan_path.resolve())
    path_hash = hashlib.sha256(abs_path.encode()).hexdigest()[:12]
    return f"{path_hash}_{plan_path.stem}"


def _state_key(plan_path: Path, content_hash: str) -> str:
    """Path-only state key. Iteration history must accumulate across
    plan rewrites — including the content hash made every Write
    create a fresh state file with iteration=0 so the safety cap
    never fired on actively-iterating plans. The content_hash
    parameter is retained for signature compatibility with callers
    that already compute it; it is unused.
    """
    del content_hash
    return _path_lock_key(plan_path)


def _state_path(plan_path: Path, content_hash: str) -> Path:
    return paths.review_state_dir() / f"_state_{_state_key(plan_path, content_hash)}.json"


def _history_path(plan_path: Path) -> Path:
    """Path-only history file. Survives plan edits — the runaway-loop
    fix's central mechanism. Iteration counter, accumulated resolved
    finding IDs, and prior plan content all persist here so the
    reviewer can see what's already been addressed AND the cap
    actually fires after N rounds regardless of edit churn.

    Reset triggers:
      * explicit `--reset` flag.
      * inactivity > _HISTORY_INACTIVITY_DAYS (abandoned-plan cleanup
        in `_prune_state_dir()`, NOT an active-plan reset).
      * clean review (the loop closed; next ExitPlanMode starts at
        iteration 1).
    """
    return paths.review_state_dir() / f"_history_{_path_lock_key(plan_path)}.json"


def _utc_now_iso() -> str:
    """Single timestamp format used in history records."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_finding_title(title: str) -> str:
    """Punctuation-insensitive, whitespace-collapsed lowercased title.

    Used as one input to `_finding_fingerprint`. Reviewer rephrasings
    that change punctuation/case won't generate a new fingerprint.
    Major rewordings still will — that's a known limitation; the
    reviewer is also asked to emit a stable `id` field to handle
    the rewording case (see `_load_or_init_history`'s round-trip
    of known IDs into the prompt).
    """
    title = re.sub(r"\s+", " ", title or "").strip().lower()
    title = re.sub(r"[^\w\s]", "", title)
    return title


def _finding_fingerprint(finding: dict[str, Any]) -> str:
    """Stable identity for a finding across iterations.

    Prefers a reviewer-supplied `id` if present (so the reviewer
    OWNS the identity decision when it's confident). Falls back to
    a runner-computed fingerprint of severity + normalized title +
    any `location.section` field. The fingerprint is short (16 hex)
    so it fits in the prompt without bloat.
    """
    explicit_id = finding.get("id")
    if isinstance(explicit_id, str) and explicit_id.strip():
        return explicit_id.strip()[:32]
    sev = finding.get("severity", "")
    title = _normalize_finding_title(finding.get("title", ""))
    loc = finding.get("location") or {}
    section = ""
    if isinstance(loc, dict):
        section = str(loc.get("section", "")).strip().lower()
    return hashlib.sha256(f"{sev}|{title}|{section}".encode()).hexdigest()[:16]


def _load_history(plan_path: Path) -> dict[str, Any]:
    """Load the path-keyed history record. Returns the default shape
    when the file is absent or unreadable — callers don't need to
    distinguish "fresh plan" from "corrupt history". A subsequent
    `_save_history` overwrites either case cleanly.
    """
    hp = _history_path(plan_path)
    if hp.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            data: dict[str, Any] = json.loads(hp.read_text())
            return data
    return {
        "first_seen_at": _utc_now_iso(),
        "last_reviewed_at": "",
        "iteration_total": 0,
        "completed_iterations": 0,
        "previous_findings": [],
        "resolved_finding_ids": [],
        "previous_plan_hash": "",
    }


def _save_history(plan_path: Path, history: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        _atomic_write(_history_path(plan_path), json.dumps(history, indent=2) + "\n")


def _clear_history(plan_path: Path) -> None:
    with contextlib.suppress(OSError):
        _history_path(plan_path).unlink(missing_ok=True)


def _assess_progress(
    iteration: int,
    current_findings: list[dict[str, Any]],
    previous_findings: list[dict[str, Any]],
    drift_streak: int,
) -> tuple[str, float, int]:
    """Convergence detection. The loop exits when the reviewer is
    finding mostly-new issues each round (drift) instead of verifying
    fixes to prior issues (convergence).

    Returns ``(status, overlap_ratio, new_drift_streak)``:

    * ``"converging"`` — current findings have ≥ _CONVERGENCE_MIN_OVERLAP
      Jaccard similarity with previous findings. Continue iterating.
    * ``"clean"`` — no findings at all. Loop closed.
    * ``"diverging"`` — overlap is below threshold AND we've now seen
      this for _DRIFT_STREAK_TO_EXIT consecutive iterations. Exit
      with divergence advisory.
    * ``"first_iteration"`` — iteration 1, nothing to compare against.

    The overlap ratio is the Jaccard similarity of the finding-ID
    sets: |current AND previous| / |current OR previous|. 0.5 means
    half the current findings are continuations of prior ones.
    """
    if not current_findings:
        return "clean", 1.0, 0
    if iteration <= 1 or not previous_findings:
        return "first_iteration", 0.0, 0

    current_ids = {_finding_fingerprint(f) for f in current_findings}
    prev_ids = {_finding_fingerprint(f) for f in previous_findings}
    union = current_ids | prev_ids
    if not union:
        return "first_iteration", 0.0, 0
    overlap = len(current_ids & prev_ids) / len(union)

    if overlap >= _CONVERGENCE_MIN_OVERLAP:
        # Reviewer is verifying prior findings, not fishing.
        return "converging", overlap, 0

    new_streak = drift_streak + 1
    if new_streak >= _DRIFT_STREAK_TO_EXIT:
        return "diverging", overlap, new_streak
    # First time seeing drift; could be a single unrelated edit.
    # Continue, but track the streak.
    return "drift_warning", overlap, new_streak


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
        # Corrupt state JSON or unreadable file is treated as "no state":
        # the loop falls through to the default fresh-iteration shape
        # below. The runner's flock has already been acquired so this
        # is the only reader; a partial/corrupt write is rare but
        # recoverable by overwriting on the next save.
        with contextlib.suppress(json.JSONDecodeError, OSError):
            loaded: dict[str, Any] = json.loads(sp.read_text())
            return loaded
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
    """On clean review, sweep state files for this plan path.

    Removes BOTH:
      * the new path-only `_state_<key>.json` (single file)
      * any legacy `_state_<key>_<hash>.json` left over from when the
        state filename rotated per content hash. Legacy files exist
        only on installs that ran an older version; new code never
        writes them.
    """
    state_dir = paths.review_state_dir()
    key = _path_lock_key(plan_path)
    with contextlib.suppress(OSError):
        (state_dir / f"_state_{key}.json").unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        for path in state_dir.glob(f"_state_{key}_*.json"):
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
This is iteration {iteration} of re-review for this implementation plan.

The loop exits via CONVERGENCE — when your findings overlap heavily
with the previous round (most issues are the SAME ones you raised
before), we treat the loop as productive and keep iterating until
clean. When your findings are mostly NEW issues two rounds in a row,
we treat that as drift / over-reaching and exit with the remaining
issues as advisory. Your job each iteration is to reuse `id`s on
prior issues you're re-flagging — that's how the runner detects
convergence vs. drift.

CONVERGENCE CONTRACT (the runner enforces these in code; ignoring them
makes your output dropped, not promoted):

1. Verify each previously flagged issue (list below) was addressed in
   the updated plan. If a fix introduced a regression, flag the
   regression — but as the SAME finding identity (reuse the `id` you
   were given for that issue, listed below). Do not re-mint a new id
   for an issue you already flagged.

2. Issues that have been RESOLVED in earlier iterations are listed by
   id. Do NOT re-flag any of them unless THE PLAN'S TEXT now
   contradicts the resolution. If you must re-flag, mark it as a
   regression by REUSING the same id, never as a brand-new finding.

3. After iteration 2, only P0 findings block exit. Carried-forward
   unresolved P1s (those raised in earlier iterations and not yet
   addressed) ALSO still block. Brand-new P1 findings raised for the
   first time at iteration {iteration} (where {iteration} > 2) will
   be classified as advisory, not blocking. Save them for advisory;
   late-discovery P1s shouldn't keep the loop spinning.

4. Be RUTHLESS about new findings at iteration > 2. Only raise a NEW
   issue (not in the prior list) if it is materially blocking or a
   real regression — not stylistic, not "would be nice", not a fresh
   reading of something you didn't comment on before. The loop's
   convergence detection treats a flood of new findings as drift and
   exits advisory; that's bad for everyone.

5. Respond with `id` per finding when re-flagging or when adding new
   findings. The runner uses `id` to track identity across iterations.

KNOWN FINDING IDs (any of these you raise must reuse the same id):
{known_ids_text}

PREVIOUS ITERATION'S FINDINGS (verify each addressed):
{previous_findings_text}

UPDATED PLAN:
{plan_content}

Respond as JSON: {{"findings": [{{"severity": "P0|P1|P2", "title": "...", "description": "...", "recommendation": "...", "id": "...optional but preferred..."}}], "questions": ["question 1", "question 2"]}}
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
        # Path-keyed history: survives plan edits. The runaway-loop
        # fix's central mechanism. `reset=True` (CLI `--reset`) wipes.
        if reset:
            _clear_history(plan_path)
        history = _load_history(plan_path)

        # The legacy per-content-hash state file is kept ONLY for the
        # immediate-previous-findings list (so the diff-style re-review
        # prompt has the last-iteration text). Iteration counter is
        # sourced from history exclusively.
        legacy_state = _load_state(plan_path, content_hash)

        # `iteration` reflects what the reviewer is ABOUT to do — it's
        # incremented BEFORE run_chain. The "completed_iterations"
        # field (incremented only after a successful review parse)
        # is what the cap actually counts against, so transient
        # provider failures don't burn iterations.
        iteration = int(history.get("completed_iterations", 0)) + 1
        previous_findings = list(history.get("previous_findings", []))
        resolved_finding_ids = list(history.get("resolved_finding_ids", []))

        metadata = {
            "plan_path": str(plan_path),
            "plan_filename": plan_path.name,
            "plan_title": plan_title,
            "iteration": iteration,
            "project": project,
            "plan_hash": content_hash,
        }

        max_iter = _safety_max_iterations()
        drift_streak = int(history.get("drift_streak", 0))

        # Build the prompt BEFORE the cap check (cap evaluation runs
        # AFTER the chain — never adjudicate the cap on stale state).
        if not previous_findings and not resolved_finding_ids:
            prompt = INITIAL_REVIEW_PROMPT.format(plan_content=plan_content)
        else:
            prev_lines: list[str] = []
            for i, f in enumerate(previous_findings, 1):
                fp = _finding_fingerprint(f)
                sev = f.get("severity", "P2")
                title = f.get("title", "Untitled")
                desc = f.get("description", "")
                prev_lines.append(
                    f"  {i}. [{sev}] id={fp} {title}: {desc[:200]}",
                )

            if resolved_finding_ids:
                ids_to_show = resolved_finding_ids[-100:]
                known_ids_text = "\n".join(f"  - {i}" for i in ids_to_show)
            else:
                known_ids_text = "  (none — first re-review)"

            prompt = RE_REVIEW_PROMPT.format(
                iteration=iteration,
                known_ids_text=known_ids_text,
                previous_findings_text="\n".join(prev_lines) or "  (none)",
                plan_content=plan_content,
            )

        logger.info(
            "plan_review_start: plan=%r plan_size=%d iteration=%d/%d chain=%r",
            str(plan_path),
            len(plan_content),
            iteration,
            max_iter,
            chain,
        )

        result: ChainResult = run_chain(
            prompt,
            chain=chain,
            mode="plan",
            metadata=metadata,
        )

        if not result.provider:
            # Chain returned no provider (all failed). Don't burn an
            # iteration on this — the per-content state file is saved
            # so a retry at the same content sees the same iteration
            # number. Nothing to advance in history.
            return ReviewOutcome(
                status="provider_failed",
                findings=[],
                questions=result.questions or [],
                provider="",
                iteration=iteration,
                elapsed_seconds=result.elapsed_seconds,
            )

        # We have a successful review. NOW it's safe to count the
        # iteration as completed.
        history["completed_iterations"] = iteration

        findings = list(result.findings or [])

        # Classify with carried-forward awareness. Past iteration 2:
        # P0 always blocks; P1 carried-forward (id in
        # resolved_finding_ids OR id in previous_findings) blocks;
        # newly-introduced P1 demotes to advisory. Iteration ≤ 2:
        # P1 always blocks regardless of id history.
        prev_ids = {_finding_fingerprint(f) for f in previous_findings}
        carry_set = prev_ids | set(resolved_finding_ids)

        blocking_findings: list[dict[str, Any]] = []
        advisory_findings: list[dict[str, Any]] = []
        for f in findings:
            sev = f.get("severity", "P2")
            fp = _finding_fingerprint(f)
            f.setdefault("id", fp)  # surface the runner's id back to the user
            if sev == "P0":
                blocking_findings.append(f)
            elif sev == "P1":
                if iteration <= 2 or fp in carry_set:
                    blocking_findings.append(f)
                else:
                    advisory_findings.append({**f, "demoted_from": "P1"})
            else:
                advisory_findings.append(f)

        has_p0 = any(f.get("severity") == "P0" for f in blocking_findings)

        # Update history. Resolved IDs accumulate the IDs of THIS
        # iteration's previous_findings that DON'T appear in the
        # current findings (i.e. they were addressed). Bound at 500
        # to keep the history file sane.
        new_resolved = list(resolved_finding_ids)
        current_ids = {_finding_fingerprint(f) for f in findings}
        for prev in previous_findings:
            pid = _finding_fingerprint(prev)
            if pid not in current_ids and pid not in new_resolved:
                new_resolved.append(pid)
        history["resolved_finding_ids"] = new_resolved[-500:]
        history["previous_findings"] = findings
        history["previous_plan_hash"] = content_hash
        history["last_reviewed_at"] = _utc_now_iso()

        # Convergence detection (primary exit signal). Compare the
        # current findings to the prior iteration's findings via
        # Jaccard similarity of finding-IDs. If the reviewer is
        # mostly verifying prior issues, we're converging — keep
        # iterating. If the reviewer is mostly finding NEW issues
        # for two rounds in a row, we have drift — exit advisory so
        # the operator can decide whether the divergent findings
        # are real.
        progress, overlap, new_drift_streak = _assess_progress(
            iteration,
            findings,
            previous_findings,
            drift_streak,
        )
        history["drift_streak"] = new_drift_streak

        logger.info(
            "plan_review_progress: plan=%r iteration=%d/%d (safety) "
            "progress=%s overlap=%.2f drift_streak=%d "
            "blocking=%d advisory=%d has_p0=%s",
            str(plan_path),
            iteration,
            max_iter,
            progress,
            overlap,
            new_drift_streak,
            len(blocking_findings),
            len(advisory_findings),
            has_p0,
        )

        # Hard safety cap (last-resort runaway protection). Real
        # convergence detection below handles the common case;
        # this is here only for the "provider is broken" edge case.
        if iteration >= max_iter:
            history["last_review_status"] = (
                "safety_cap_with_unresolved_p0" if has_p0 else "safety_cap_reached"
            )
            _save_history(plan_path, history)
            _save_state(plan_path, content_hash, legacy_state)
            _prune_state_dir()
            if has_p0:
                return ReviewOutcome(
                    status="max_iterations_with_unresolved_p0",
                    findings=blocking_findings + advisory_findings,
                    questions=result.questions or [],
                    provider=result.provider,
                    iteration=iteration,
                    elapsed_seconds=result.elapsed_seconds,
                    error=(
                        f"hit safety cap ({max_iter}) with unresolved P0; "
                        f"the reviewer hasn't converged after this many "
                        f"rounds — likely a buggy provider or a plan "
                        f"that needs human triage. Override with "
                        f"CLAUDE_PLAN_REVIEW_FAIL_OPEN=1."
                    ),
                )
            return ReviewOutcome(
                status="max_iterations_reached",
                findings=blocking_findings + advisory_findings,
                questions=result.questions or [],
                provider=result.provider,
                iteration=iteration,
                elapsed_seconds=result.elapsed_seconds,
                error=(
                    f"hit safety cap ({max_iter}); no P0 outstanding so "
                    f"allowing. Remaining findings are advisory."
                ),
            )

        # Divergence: reviewer is finding mostly-new issues each
        # round (drift), not converging on prior findings. Exit
        # with advisory so the operator can decide. P0 still
        # blocks even on divergence — a real P0 is a real P0
        # whether the reviewer is converging or not.
        if progress == "diverging":
            if has_p0:
                history["last_review_status"] = "diverging_with_p0"
                _save_history(plan_path, history)
                _save_state(plan_path, content_hash, legacy_state)
                _prune_state_dir()
                return ReviewOutcome(
                    status="max_iterations_with_unresolved_p0",
                    findings=blocking_findings + advisory_findings,
                    questions=result.questions or [],
                    provider=result.provider,
                    iteration=iteration,
                    elapsed_seconds=result.elapsed_seconds,
                    error=(
                        f"reviewer diverging (overlap={overlap:.2f}) AND "
                        f"unresolved P0 present. Address the P0 or "
                        f"override with CLAUDE_PLAN_REVIEW_FAIL_OPEN=1."
                    ),
                )
            history["last_review_status"] = "diverging_advisory"
            _save_history(plan_path, history)
            _clean_state_for_plan(plan_path)
            _clear_history(plan_path)
            _prune_state_dir()
            return ReviewOutcome(
                status="max_iterations_reached",
                findings=blocking_findings + advisory_findings,
                questions=result.questions or [],
                provider=result.provider,
                iteration=iteration,
                elapsed_seconds=result.elapsed_seconds,
                error=(
                    f"reviewer not converging after {iteration} rounds "
                    f"(overlap={overlap:.2f}); remaining findings are "
                    f"advisory. The loop exits when the reviewer keeps "
                    f"finding mostly-new issues instead of verifying "
                    f"prior fixes — this signals either drift or a plan "
                    f"that needs human triage rather than another "
                    f"automated round."
                ),
            )

        # Within the cap.
        if blocking_findings:
            status = "blocking"
            history["last_review_status"] = "blocking"
            findings_to_return = blocking_findings + advisory_findings
        elif advisory_findings:
            status = "p2_only"
            history["last_review_status"] = "p2_only"
            findings_to_return = advisory_findings
        else:
            status = "clean"
            history["last_review_status"] = "clean"
            findings_to_return = []

        if status == "clean":
            # Loop closed. Sweep history + legacy state files for
            # this plan path so the next ExitPlanMode starts fresh at
            # iteration 1.
            _clear_history(plan_path)
            _clean_state_for_plan(plan_path)
        else:
            _save_history(plan_path, history)
            _save_state(plan_path, content_hash, legacy_state)
            _prune_state_dir()

        return ReviewOutcome(
            status=status,
            findings=findings_to_return,
            questions=result.questions or [],
            provider=result.provider,
            iteration=iteration,
            elapsed_seconds=result.elapsed_seconds,
        )
    finally:
        lock.release()
