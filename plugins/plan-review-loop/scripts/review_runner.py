#!/usr/bin/env python3
# Ad-hoc CLI for plan review.
#
# Same code path as the hook (calls _lib.runner.review_plan), so iteration
# state and locking are consistent. Use this for debugging or to re-review
# without entering plan mode.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib.runner import review_plan  # noqa: E402


def _format_findings(findings: list[dict]) -> str:
    if not findings:
        return "(no findings)"
    counts: dict[str, int] = {}
    lines: list[str] = []
    for f in findings:
        sev = f.get("severity", "P2")
        counts[sev] = counts.get(sev, 0) + 1
        lines.append(f"  {sev}: {f.get('title', 'Untitled')}")
        if f.get("description"):
            lines.append(f"    {f['description']}")
        if f.get("recommendation"):
            lines.append(f"    Recommendation: {f['recommendation']}")
        lines.append("")
    summary = ", ".join(f"{counts[s]} {s}" for s in ("P0", "P1", "P2") if counts.get(s))
    return f"({summary})\n" + "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the plan-review loop on a plan file. "
        "Same code path as the PreToolUse hook.",
    )
    parser.add_argument("plan_path", type=Path, help="Path to plan markdown file")
    parser.add_argument(
        "--chain",
        default=None,
        help="Comma-separated provider chain (overrides CLAUDE_PLAN_REVIEW_CHAIN). Default: codex.",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Print JSON to stdout. Exit 1 if blocking findings.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset iteration state for this plan before reviewing.",
    )
    args = parser.parse_args()

    if not args.plan_path.exists():
        print(f"error: plan file not found: {args.plan_path}", file=sys.stderr)
        return 2

    chain = [c.strip() for c in args.chain.split(",")] if args.chain else None

    outcome = review_plan(args.plan_path, chain=chain, reset=args.reset)

    if args.json_output:
        out = {
            "status": outcome.status,
            "provider": outcome.provider,
            "iteration": outcome.iteration,
            "elapsed_seconds": outcome.elapsed_seconds,
            "findings": outcome.findings,
            "questions": outcome.questions,
            "busy_reason": outcome.busy_reason,
            "error": outcome.error,
        }
        json.dump(out, sys.stdout, indent=2)
        print()
        return 1 if outcome.status == "blocking" else 0

    if outcome.status == "busy":
        print(f"BUSY: {outcome.busy_reason}", file=sys.stderr)
        return 3
    if outcome.status == "plan_too_short":
        print(f"SKIPPED: {outcome.error}", file=sys.stderr)
        return 0
    if outcome.status == "blocking":
        print(
            f"Iteration {outcome.iteration} ({outcome.provider}, "
            f"{outcome.elapsed_seconds:.1f}s): BLOCKING",
        )
        print(_format_findings(outcome.findings))
        return 1
    if outcome.status == "p2_only":
        print(
            f"Iteration {outcome.iteration} ({outcome.provider}, "
            f"{outcome.elapsed_seconds:.1f}s): P2 advisory only",
        )
        print(_format_findings(outcome.findings))
        return 0
    if outcome.status == "clean":
        print(
            f"Iteration {outcome.iteration} ({outcome.provider}, "
            f"{outcome.elapsed_seconds:.1f}s): clean",
        )
        return 0
    # provider_failed
    print("All configured providers failed.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
