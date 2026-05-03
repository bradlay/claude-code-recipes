#!/usr/bin/env python3
# Quality report for plan-review providers.
#
# Walks the cycle-log dir produced by chain._save_cycle_log, pairs primary
# runs with their shadow runs (by plan_path x iteration), and prints
# agreement statistics:
#
#   - "decision agreement": did the shadow provider produce P0/P1 findings
#     iff the primary did? (binary block-vs-allow match)
#   - "finding overlap": jaccard similarity between primary and shadow
#     P0/P1 findings, scored on case-folded title.
#   - per-provider summary: count, mean elapsed, mean findings, error rate.
#
# Output is plain text. Use --json for machine-readable output.

from __future__ import annotations

import argparse
import collections
import contextlib
import fnmatch
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import paths  # noqa: E402

P0_P1 = ("P0", "P1")


@dataclass
class CycleLog:
    path: Path
    data: dict[str, Any]

    @property
    def provider(self) -> str:
        return str(self.data.get("provider", ""))

    @property
    def plan_path(self) -> str:
        return str(self.data.get("plan_path", ""))

    @property
    def iteration(self) -> int:
        return int(self.data.get("iteration", 0))

    @property
    def is_shadow(self) -> bool:
        return bool(self.data.get("shadow", False))

    @property
    def primary_provider(self) -> str | None:
        v = self.data.get("primary_provider")
        return str(v) if v else None

    @property
    def timestamp(self) -> str:
        return str(self.data.get("timestamp", ""))

    @property
    def elapsed_seconds(self) -> float:
        try:
            return float(self.data.get("elapsed_seconds", 0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def findings(self) -> list[dict[str, Any]]:
        v = self.data.get("findings") or []
        return [f for f in v if isinstance(f, dict)]

    @property
    def has_blocking(self) -> bool:
        return any(f.get("severity") in P0_P1 for f in self.findings)

    @property
    def returncode(self) -> int | None:
        v = self.data.get("returncode")
        return int(v) if isinstance(v, int) else None

    @property
    def error(self) -> str | None:
        v = self.data.get("error")
        return str(v) if v else None

    @property
    def blocking_titles(self) -> set[str]:
        """Case-folded P0/P1 finding titles, used for jaccard overlap."""
        out: set[str] = set()
        for f in self.findings:
            if f.get("severity") in P0_P1:
                title = str(f.get("title", "")).strip().casefold()
                if title:
                    out.add(title)
        return out


def _load_cycle_log(p: Path) -> CycleLog | None:
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return CycleLog(path=p, data=data)


def _load_logs(
    log_dir: Path,
    *,
    since_days: int | None = None,
    plan_glob: str | None = None,
    provider_filter: str | None = None,
) -> list[CycleLog]:
    if not log_dir.is_dir():
        return []
    cutoff = time.time() - (since_days * 86400) if since_days else None
    out: list[CycleLog] = []
    for p in log_dir.glob("*.json"):
        with contextlib.suppress(OSError):
            if cutoff is not None and p.stat().st_mtime < cutoff:
                continue
        log = _load_cycle_log(p)
        if log is None:
            continue
        if plan_glob and not fnmatch.fnmatch(log.plan_path, plan_glob):
            continue
        if provider_filter and log.provider != provider_filter:
            continue
        out.append(log)
    return out


@dataclass
class ProviderSummary:
    provider: str
    runs: int = 0
    errors: int = 0
    total_elapsed: float = 0.0
    total_findings: int = 0
    blocking_runs: int = 0

    @property
    def error_rate(self) -> float:
        return self.errors / self.runs if self.runs else 0.0

    @property
    def mean_elapsed(self) -> float:
        return self.total_elapsed / self.runs if self.runs else 0.0

    @property
    def mean_findings(self) -> float:
        return self.total_findings / self.runs if self.runs else 0.0

    @property
    def block_rate(self) -> float:
        return self.blocking_runs / self.runs if self.runs else 0.0


def _summarize_providers(logs: list[CycleLog]) -> dict[str, ProviderSummary]:
    out: dict[str, ProviderSummary] = {}
    for log in logs:
        s = out.setdefault(log.provider, ProviderSummary(provider=log.provider))
        s.runs += 1
        s.total_elapsed += log.elapsed_seconds
        s.total_findings += len(log.findings)
        if log.error or (log.returncode is not None and log.returncode != 0):
            s.errors += 1
        if log.has_blocking:
            s.blocking_runs += 1
    return out


@dataclass
class PairedComparison:
    plan_path: str
    iteration: int
    primary: CycleLog
    shadow: CycleLog
    decision_match: bool
    jaccard: float
    primary_only_titles: list[str] = field(default_factory=list)
    shadow_only_titles: list[str] = field(default_factory=list)
    common_titles: list[str] = field(default_factory=list)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _pair_logs(logs: list[CycleLog]) -> list[PairedComparison]:
    """Pair non-shadow logs with shadow logs by (plan_path, iteration).

    For each shadow run, find the primary log it points at via primary_provider,
    or — if primary_provider isn't set — the most recent non-shadow log for the
    same plan_path x iteration.
    """
    by_pair: dict[tuple[str, int], list[CycleLog]] = collections.defaultdict(list)
    for log in logs:
        by_pair[(log.plan_path, log.iteration)].append(log)

    out: list[PairedComparison] = []
    for (plan_path, iteration), bucket in by_pair.items():
        primaries = [b for b in bucket if not b.is_shadow]
        shadows = [b for b in bucket if b.is_shadow]
        if not primaries or not shadows:
            continue
        # Latest primary wins (in case of retries).
        primaries.sort(key=lambda x: x.timestamp, reverse=True)
        for shadow in shadows:
            primary: CycleLog | None = None
            if shadow.primary_provider:
                for p in primaries:
                    if p.provider == shadow.primary_provider:
                        primary = p
                        break
            if primary is None:
                primary = primaries[0]

            primary_titles = primary.blocking_titles
            shadow_titles = shadow.blocking_titles
            out.append(
                PairedComparison(
                    plan_path=plan_path,
                    iteration=iteration,
                    primary=primary,
                    shadow=shadow,
                    decision_match=primary.has_blocking == shadow.has_blocking,
                    jaccard=_jaccard(primary_titles, shadow_titles),
                    primary_only_titles=sorted(primary_titles - shadow_titles),
                    shadow_only_titles=sorted(shadow_titles - primary_titles),
                    common_titles=sorted(primary_titles & shadow_titles),
                ),
            )
    return out


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_text(
    summaries: dict[str, ProviderSummary],
    pairs: list[PairedComparison],
    *,
    log_dir: Path,
    args: argparse.Namespace,
) -> str:
    lines: list[str] = []
    scope = []
    if args.since:
        scope.append(f"last {args.since}d")
    if args.plan:
        scope.append(f"plan~{args.plan}")
    if args.provider:
        scope.append(f"provider={args.provider}")
    scope_str = "; ".join(scope) if scope else "all"
    lines.append(f"Plan-review quality report  ({scope_str})")
    lines.append(f"  log dir: {log_dir}")
    lines.append("")

    if not summaries:
        lines.append("No cycle logs found.")
        return "\n".join(lines)

    lines.append("Per-provider:")
    lines.append(
        f"  {'provider':<14} {'runs':>5} {'err%':>6} {'block%':>7} "
        f"{'mean-s':>7} {'mean-find':>10}",
    )
    for provider in sorted(summaries):
        s = summaries[provider]
        lines.append(
            f"  {provider:<14} {s.runs:>5} {s.error_rate * 100:>5.0f}% "
            f"{s.block_rate * 100:>6.0f}% {s.mean_elapsed:>7.1f} "
            f"{s.mean_findings:>10.1f}",
        )
    lines.append("")

    if not pairs:
        lines.append(
            "No primary/shadow pairs found. Set CLAUDE_PLAN_REVIEW_SHADOW=local "
            "(or another provider) to start collecting comparison data.",
        )
        return "\n".join(lines)

    by_combo: dict[tuple[str, str], list[PairedComparison]] = collections.defaultdict(
        list,
    )
    for p in pairs:
        by_combo[(p.primary.provider, p.shadow.provider)].append(p)

    lines.append(f"Primary vs shadow agreement  (n={len(pairs)} pairs):")
    lines.append(
        f"  {'primary':<10} {'shadow':<10} {'pairs':>6} "
        f"{'decision%':>10} {'jaccard':>8}",
    )
    for (primary, shadow), bucket in sorted(by_combo.items()):
        decision_pct = sum(1 for p in bucket if p.decision_match) / len(bucket) * 100
        jaccard_mean = sum(p.jaccard for p in bucket) / len(bucket)
        lines.append(
            f"  {primary:<10} {shadow:<10} {len(bucket):>6} "
            f"{decision_pct:>9.0f}% {jaccard_mean:>8.2f}",
        )
    lines.append("")

    if args.show_disagreements:
        disagreements = [p for p in pairs if not p.decision_match or p.jaccard < 1.0]
        disagreements.sort(key=lambda p: p.primary.timestamp, reverse=True)
        if disagreements:
            lines.append(f"Disagreements ({len(disagreements)}):")
            for p in disagreements[: args.limit]:
                plan_label = Path(p.plan_path).name if p.plan_path else "?"
                lines.append(
                    f"  iter {p.iteration:>2} {plan_label}  "
                    f"{p.primary.provider} vs {p.shadow.provider}  "
                    f"decision={'match' if p.decision_match else 'DIFFER'}  "
                    f"jaccard={p.jaccard:.2f}",
                )
                if p.common_titles:
                    lines.append(
                        f"      common: {', '.join(_truncate(t, 50) for t in p.common_titles)}",
                    )
                if p.primary_only_titles:
                    lines.append(
                        f"      primary-only ({p.primary.provider}): "
                        f"{', '.join(_truncate(t, 50) for t in p.primary_only_titles)}",
                    )
                if p.shadow_only_titles:
                    lines.append(
                        f"      shadow-only ({p.shadow.provider}): "
                        f"{', '.join(_truncate(t, 50) for t in p.shadow_only_titles)}",
                    )
                lines.append("")
        else:
            lines.append("No disagreements — all pairs match on decision and findings.")

    return "\n".join(lines)


def _render_json(
    summaries: dict[str, ProviderSummary],
    pairs: list[PairedComparison],
) -> str:
    out: dict[str, Any] = {
        "providers": [
            {
                "provider": s.provider,
                "runs": s.runs,
                "errors": s.errors,
                "error_rate": s.error_rate,
                "blocking_runs": s.blocking_runs,
                "block_rate": s.block_rate,
                "mean_elapsed_seconds": s.mean_elapsed,
                "mean_findings": s.mean_findings,
            }
            for s in summaries.values()
        ],
        "pairs": [
            {
                "plan_path": p.plan_path,
                "iteration": p.iteration,
                "primary_provider": p.primary.provider,
                "shadow_provider": p.shadow.provider,
                "decision_match": p.decision_match,
                "jaccard": p.jaccard,
                "common_titles": p.common_titles,
                "primary_only_titles": p.primary_only_titles,
                "shadow_only_titles": p.shadow_only_titles,
            }
            for p in pairs
        ],
    }
    return json.dumps(out, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality report for plan-review providers (compares primary vs shadow runs).",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=None,
        metavar="DAYS",
        help="Only include logs modified within the last N days.",
    )
    parser.add_argument(
        "--plan",
        default=None,
        help="Glob pattern to filter on plan_path (e.g. '*hotspot*').",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Only include logs from this provider.",
    )
    parser.add_argument(
        "--show-disagreements",
        action="store_true",
        help="List individual pairs where primary and shadow disagreed.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max disagreements to list (default 20).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Override the cycle-log directory (default: plugin's review-log/).",
    )
    args = parser.parse_args(argv)

    log_dir = args.log_dir or paths.review_log_dir()
    logs = _load_logs(
        log_dir,
        since_days=args.since,
        plan_glob=args.plan,
        provider_filter=args.provider,
    )
    summaries = _summarize_providers(logs)
    pairs = _pair_logs(logs)

    if args.json:
        sys.stdout.write(_render_json(summaries, pairs) + "\n")
    else:
        sys.stdout.write(
            _render_text(summaries, pairs, log_dir=log_dir, args=args) + "\n",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
