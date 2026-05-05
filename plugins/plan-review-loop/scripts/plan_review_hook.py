#!/usr/bin/env python3
# PreToolUse(ExitPlanMode) hook entry point.
#
# Resolves the plan file from the hook invocation, calls review_plan(),
# and translates the outcome to a hook decision:
#   blocking findings (P0/P1) : deny + findings as additionalContext
#   P2-only findings          : allow + advisory context
#   clean review              : allow
#   lock-busy                 : deny with "review already in progress"
#   missing plan / chain fail : fail-closed by default; bypass via
#                               CLAUDE_PLAN_REVIEW_FAIL_OPEN=1
#
# health.json is written on every invocation under ${CLAUDE_PLUGIN_DATA}.

from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, paths  # noqa: E402
from _lib.runner import ReviewOutcome, review_plan  # noqa: E402


def _write_health(outcome_status: str, *, provider: str = "", error: str = "") -> None:
    with contextlib.suppress(OSError):
        record = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": outcome_status,
            "last_provider": provider,
            "last_error": error,
        }
        path = paths.health_file()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(record, indent=2) + "\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _format_findings(findings: list[dict[str, Any]], provider: str) -> str:
    lines = [f"Plan review findings (via {provider}):\n"]
    counts: dict[str, int] = {}

    for f in findings:
        sev = f.get("severity", "P2")
        title = f.get("title", "Untitled")
        desc = f.get("description", "")
        rec = f.get("recommendation", "")
        counts[sev] = counts.get(sev, 0) + 1
        lines.append(f"  {sev}: {title}")
        if desc:
            lines.append(f"    {desc}")
        if rec:
            lines.append(f"    Recommendation: {rec}")
        lines.append("")

    summary = ", ".join(f"{counts.get(s, 0)} {s}" for s in ("P0", "P1", "P2") if counts.get(s))
    lines.insert(1, f"  ({summary})\n")
    lines.append(
        "BLOCKING: Address ALL findings above (P0, P1, and P2) in the plan, "
        "then call ExitPlanMode again for re-review.",
    )
    return "\n".join(lines)


def _format_questions(questions: list[str], provider: str) -> str:
    lines = [f"Questions from {provider} (use AskUserQuestion to ask the user):\n"]
    for i, q in enumerate(questions, 1):
        lines.append(f"  {i}. {q}")
    lines.append("")
    lines.append(
        "ACTION REQUIRED: Use AskUserQuestion to ask the user these questions. "
        "Incorporate their answers into the plan before calling ExitPlanMode again.",
    )
    return "\n".join(lines)


def main() -> int:
    inv = _io.parse_stdin(__file__)
    _io.log(inv, "plan review hook triggered")

    plan_file = _io.resolve_plan_file(inv)
    if plan_file is None:
        _write_health("misconfigured", error="no plan path resolved")
        return _io.fail_open_or_closed_pretooluse(
            inv,
            "no plan path could be resolved from the hook invocation",
        )

    plan_path = str(plan_file)
    try:
        plan_size = plan_file.stat().st_size
    except OSError:
        plan_size = 0
    _io.log(inv, f"reviewing: {plan_path} ({plan_size} bytes)")

    start = time.monotonic()
    try:
        outcome: ReviewOutcome = review_plan(plan_file)
    except Exception as exc:
        elapsed = time.monotonic() - start
        _io.log(inv, f"review_plan unexpected error after {elapsed:.1f}s: {exc}", level="error")
        _write_health("error", error=f"review_plan exception: {exc}")
        return _io.fail_open_or_closed_pretooluse(
            inv,
            f"plan review crashed unexpectedly: {exc}",
        )

    _io.log(
        inv,
        f"outcome: status={outcome.status} provider={outcome.provider} "
        f"iteration={outcome.iteration} findings={len(outcome.findings)} "
        f"elapsed={outcome.elapsed_seconds:.1f}s",
    )

    if outcome.status == "busy":
        _write_health("busy", error=outcome.busy_reason)
        return _io.emit_pretooluse_deny(
            outcome.busy_reason,
            additional_context=(
                f"Lock-busy: another session is reviewing this plan. {outcome.busy_reason}"
            ),
        )

    if outcome.status == "plan_too_short":
        _write_health("plan_too_short", error=outcome.error)
        return _io.emit_pretooluse_allow(
            additional_context="Plan review skipped (plan <100 chars)."
        )

    if outcome.status == "blocking":
        _write_health("blocking", provider=outcome.provider)
        parts: list[str] = [_format_findings(outcome.findings, outcome.provider)]
        if outcome.questions:
            parts.append(_format_questions(outcome.questions, outcome.provider))
        context = "\n\n".join(parts)
        reason = (
            f"Plan review ({outcome.provider}, iteration {outcome.iteration}) found "
            f"blocking issues. Revise the plan."
        )
        return _io.emit_pretooluse_deny(reason=reason, additional_context=context)

    if outcome.status == "p2_only":
        _write_health("p2_only", provider=outcome.provider)
        parts = [_format_findings(outcome.findings, outcome.provider)]
        parts.append(
            "NOTE: These are P2 (advisory) findings only, no blocking P0/P1 issues. "
            "Consider addressing during implementation but you may proceed.",
        )
        if outcome.questions:
            parts.append(_format_questions(outcome.questions, outcome.provider))
        return _io.emit_pretooluse_allow(additional_context="\n\n".join(parts))

    if outcome.status == "max_iterations_with_unresolved_p0":
        # Loop terminated with unresolved P0 — either reviewer
        # diverged (still raising mostly-new issues) or the safety
        # cap fired. Block with the runner's explanation; operator
        # can override with CLAUDE_PLAN_REVIEW_FAIL_OPEN=1.
        _write_health("max_iterations_with_unresolved_p0", provider=outcome.provider)
        parts = [_format_findings(outcome.findings, outcome.provider)]
        if outcome.error:
            parts.append(f"BLOCKED: {outcome.error}")
        else:
            parts.append(
                f"BLOCKED: review loop terminated after {outcome.iteration} "
                f"rounds with unresolved P0. Address the P0(s) or set "
                f"CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 to override.",
            )
        return _io.emit_pretooluse_deny(
            reason=f"plan-review terminated with unresolved P0 ({outcome.iteration} rounds)",
            additional_context="\n\n".join(parts),
        )

    if outcome.status == "max_iterations_reached":
        # Loop terminated with no P0 outstanding — convergence
        # detection saw drift (or safety cap fired) and exited
        # advisory. Surface findings and the runner's explanation.
        _write_health("max_iterations_reached", provider=outcome.provider)
        parts = []
        if outcome.findings:
            parts.append(_format_findings(outcome.findings, outcome.provider))
        if outcome.error:
            parts.append(outcome.error)
        else:
            parts.append(
                f"Review loop exited after {outcome.iteration} rounds. "
                f"No P0 outstanding so the plan can proceed; remaining "
                f"findings above are advisory.",
            )
        if outcome.questions:
            parts.append(_format_questions(outcome.questions, outcome.provider))
        return _io.emit_pretooluse_allow(additional_context="\n\n".join(parts))

    if outcome.status == "clean":
        _write_health("clean", provider=outcome.provider)
        msg = f"Plan review ({outcome.provider}): no issues found."
        return _io.emit_pretooluse_allow(additional_context=msg)

    # status == "provider_failed": no provider returned a clean answer.
    _write_health("provider_failed", error="all configured providers failed")
    return _io.fail_open_or_closed_pretooluse(
        inv,
        "no configured plan-review provider was available "
        "(all providers in CLAUDE_PLAN_REVIEW_CHAIN failed or are not on PATH)",
    )


if __name__ == "__main__":
    sys.exit(main())
