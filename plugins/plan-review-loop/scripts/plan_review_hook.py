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
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, backends, paths, picker, probes  # noqa: E402
from _lib._io import HookInvocation  # noqa: E402
from _lib.runner import ReviewOutcome, review_plan  # noqa: E402


@dataclass
class ChainDecision:
    """How the hook should source the review backend for this invocation.

    kind:
      proceed -> run review_plan(chain=chain) (chain=None uses the default
                 resolver / tier / explicit CLAUDE_PLAN_REVIEW_CHAIN)
      deny    -> emit a deny with reason+context (picker prompt or loop-guard)
      fail    -> fail-open-or-closed per CLAUDE_PLAN_REVIEW_FAIL_OPEN
    """

    kind: str
    chain: list[str] | None = None
    reason: str = ""
    context: str = ""
    health: str = ""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _decide_chain(inv: HookInvocation) -> ChainDecision:
    """Resolve which backend reviews this plan, surfacing the interactive
    picker when online and unselected. Non-interactive contexts (nested,
    autoswe, explicit chain/tier/autoselect) never prompt."""
    # Nested (probe/review child) — keep default behavior, never prompt.
    if _env("CLAUDE_PLAN_REVIEW_NESTED"):
        return ChainDecision("proceed", chain=None)

    # autoswe: local qwen only, proven reachable synchronously, no prompt.
    # Headless runs may never have warmed the SessionStart probe cache and a
    # stale positive would hang the run, so force a fresh reachability check.
    if _env("AUTOSWE_RUN_ID"):
        result = probes.probe_provider("local", force=True)
        if result.ok:
            return ChainDecision("proceed", chain=["local"])
        url = _env("CLAUDE_PLAN_REVIEW_LOCAL_URL") or "(unset)"
        return ChainDecision(
            "fail",
            reason=(
                f"autoswe plan review: local vLLM unreachable at {url} "
                f"({result.detail}). autoswe reviews run only against the local "
                "model and do not fall back to a cloud backend."
            ),
            health="autoswe_local_unreachable",
        )

    # Explicit chain or tier preset — non-interactive operator intent.
    if _env("CLAUDE_PLAN_REVIEW_CHAIN") or _env("CLAUDE_PLAN_REVIEW_TIER"):
        return ChainDecision("proceed", chain=None)

    # Deterministic, log-visible auto-selection (opt-in).
    autoselect = _env("CLAUDE_PLAN_REVIEW_AUTOSELECT")
    if autoselect:
        key = backends.normalize_key(autoselect)
        if key is not None:
            return ChainDecision("proceed", chain=[key])

    # Sticky per-session selection — re-probe the chosen backend right before
    # the review so an auth that expired since the pick is caught here.
    selection = picker.read_selection(inv.session_id)
    if selection is not None:
        key = str(selection["backend_key"])
        if probes.probe_provider(key, force=True).ok:
            return ChainDecision("proceed", chain=[key])
        picker.clear_selection(inv.session_id)  # stale auth — re-ask below

    # No usable selection: surface only backends verified working right now.
    available = probes.available_backends()
    if not available:
        return ChainDecision(
            "fail",
            reason=(
                "no plan-review backend is verified working (every online "
                "backend probe failed). Fix auth/model access for one of: "
                f"{', '.join(backends.ONLINE_KEYS)}, or set "
                "CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 to bypass."
            ),
            health="no_backend_available",
        )

    attempts = picker.record_attempt(inv.session_id)
    if attempts > picker.MAX_PICKER_ATTEMPTS:
        # Loop guard: fail closed (deliberate-choice requirement preserved);
        # never silently auto-pick a default.
        return ChainDecision(
            "deny",
            reason="plan-review backend still not selected; failing closed.",
            context=(
                f"No backend was selected after {attempts - 1} prompt(s). Pick "
                "one with the /plan-review-backend command, or set "
                "CLAUDE_PLAN_REVIEW_AUTOSELECT=<key> (or "
                "CLAUDE_PLAN_REVIEW_CHAIN=<key>) for a non-interactive default. "
                f"Backends: {', '.join(backends.ONLINE_KEYS)}."
            ),
            health="picker_unselected",
        )

    select_bin = str(_THIS_DIR.parent / "bin" / "plan-review-select")
    return ChainDecision(
        "deny",
        reason="Select a plan-review backend before exiting plan mode.",
        context=picker.build_picker_instruction(
            inv.session_id,
            available,
            select_bin=select_bin,
            data_dir=str(paths.data_dir()),
        ),
        health="picker_prompt",
    )


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

    decision = _decide_chain(inv)
    if decision.kind == "deny":
        _write_health(decision.health)
        _io.log(inv, f"backend decision: deny ({decision.health})")
        return _io.emit_pretooluse_deny(decision.reason, additional_context=decision.context)
    if decision.kind == "fail":
        _write_health(decision.health)
        _io.log(inv, f"backend decision: fail ({decision.health})", level="warn")
        return _io.fail_open_or_closed_pretooluse(inv, decision.reason)
    _io.log(inv, f"backend decision: proceed chain={decision.chain}")

    start = time.monotonic()
    try:
        outcome: ReviewOutcome = review_plan(plan_file, chain=decision.chain)
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
