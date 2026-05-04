#!/usr/bin/env python3
# Shadow-runner inspector. Lists, shows, and aggregates the shadow
# review records produced by chain._save_cycle_log (post-decision
# parallel reviews emitted under `${CLAUDE_PLUGIN_DATA}/review-log/
# *_shadow.json`).
#
# Usage:
#   plan-review-shadow list [--limit N] [--since DAYS] [--status ...] [--json]
#   plan-review-shadow show [PATH | latest] [--json]
#   plan-review-shadow stats [--since DAYS] [--scope ...] [--json]
#
# Exit codes:
#   0  command succeeded (empty results are NOT failure)
#   1  operational failure (show against missing path, OSError on log dir)
#   2  invalid arguments (argparse default)

from __future__ import annotations

import argparse
import contextlib
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import paths  # noqa: E402
from preflight import _compute_shadow_health  # noqa: E402
from quality_report import CycleLog, _load_cycle_log, _load_logs  # noqa: E402

# Strip ANSI escape sequences (CSI, OSC) and C0/C1 control bytes from
# untrusted record content before rendering to a human terminal — model
# output occasionally contains escapes that would otherwise reflow our
# table or smuggle hyperlinks. JSON output bypasses this.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize_for_terminal(text: str) -> str:
    return _CTRL_RE.sub("", _ANSI_RE.sub("", text))


def _truncate(text: str, width: int) -> str:
    text = _sanitize_for_terminal(text)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _shadow_logs(since_days: int | None) -> list[CycleLog]:
    log_dir = paths.review_log_dir()
    logs = _load_logs(log_dir, since_days=since_days)
    return [log for log in logs if log.is_shadow]


def _record_dict(log: CycleLog) -> dict[str, Any]:
    """Render a stable dict for `--json` output. Underlying record may
    or may not include legacy fields, so we always emit the same keys."""
    plan_title = log.data.get("plan_title") or log.data.get("plan_filename") or log.plan_path or ""
    return {
        "path": str(log.path),
        "timestamp": log.timestamp,
        "event_time_epoch": log.event_time_epoch,
        "provider": log.provider,
        "primary_provider": log.primary_provider,
        "plan_path": log.plan_path,
        "plan_title": str(plan_title),
        "iteration": log.iteration,
        "elapsed_seconds": log.elapsed_seconds,
        "result_status": log.result_status,
        "shadow_config_signature": log.shadow_config_signature,
        "returncode": log.returncode,
        "error": log.error,
        "parse_error": (str(log.data.get("parse_error")) if log.data.get("parse_error") else None),
        "findings_count": int(log.data.get("findings_count") or 0),
    }


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    logs = _shadow_logs(args.since)
    if args.status == "ok":
        logs = [log for log in logs if log.result_status == "ok"]
    elif args.status == "fail":
        logs = [log for log in logs if log.result_status not in ("ok", "unknown")]
    logs.sort(key=lambda log: log.event_time_epoch, reverse=True)
    logs = logs[: args.limit]

    if args.json:
        print(json.dumps([_record_dict(log) for log in logs], indent=2))
        return 0

    if not logs:
        print(
            "(no shadow records — adjust --since/--status, "
            "or trigger an ExitPlanMode under the current chain.)"
        )
        return 0

    # Tabular human output — newest first.
    header = f"{'TIMESTAMP':<25}  {'PLAN':<32}  ITER  STATUS  ELAPSED  FNDS  ERROR"
    print(header)
    print("-" * len(header))
    for log in logs:
        rec = _record_dict(log)
        status_glyph = "ok " if rec["result_status"] == "ok" else f"{rec['result_status'][:6]:<6}"
        err = rec["error"] or rec["parse_error"] or ""
        print(
            f"{_truncate(rec['timestamp'], 25):<25}  "
            f"{_truncate(rec['plan_title'], 32):<32}  "
            f"{rec['iteration']:>4}  "
            f"{status_glyph:<6}  "
            f"{rec['elapsed_seconds']:>6.2f}s  "
            f"{rec['findings_count']:>4}  "
            f"{_truncate(err, 60)}"
        )
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _resolve_show_target(target: str) -> Path | None:
    if target == "latest":
        logs = _shadow_logs(since_days=None)
        if not logs:
            return None
        logs.sort(key=lambda log: log.event_time_epoch, reverse=True)
        return logs[0].path
    p = Path(target)
    if not p.is_absolute():
        # Resolve relative to the review-log dir for convenience.
        candidate = paths.review_log_dir() / target
        if candidate.exists():
            return candidate
    if p.exists():
        return p
    return None


def _cmd_show(args: argparse.Namespace) -> int:
    target = _resolve_show_target(args.target)
    if target is None:
        if args.target == "latest":
            print("no shadow records found.", file=sys.stderr)
        else:
            print(f"not found: {args.target}", file=sys.stderr)
        return 1
    log = _load_cycle_log(target)
    if log is None:
        print(f"failed to parse {target}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(log.data, indent=2, ensure_ascii=False))
        return 0

    rec = _record_dict(log)
    print(f"path:                {rec['path']}")
    print(f"timestamp:           {rec['timestamp']}")
    print(f"provider:            {rec['provider']}")
    print(f"primary_provider:    {rec['primary_provider']}")
    print(f"plan_path:           {rec['plan_path']}")
    print(f"plan_title:          {_sanitize_for_terminal(rec['plan_title'])}")
    print(f"iteration:           {rec['iteration']}")
    print(f"result_status:       {rec['result_status']}")
    print(f"config_signature:    {rec['shadow_config_signature']}")
    print(f"elapsed_seconds:     {rec['elapsed_seconds']:.2f}")
    print(f"returncode:          {rec['returncode']}")
    print(f"findings_count:      {rec['findings_count']}")
    if rec["error"]:
        print(f"error:               {_sanitize_for_terminal(rec['error'])}")
    if rec["parse_error"]:
        print(f"parse_error:         {_sanitize_for_terminal(rec['parse_error'])}")
    findings = log.findings
    if findings:
        print("\nfindings:")
        for f in findings:
            sev = str(f.get("severity", "?"))
            title = _sanitize_for_terminal(str(f.get("title", "")))
            print(f"  [{sev}] {title}")
    return 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def _history_stats(logs: list[CycleLog]) -> dict[str, Any]:
    total = len(logs)
    if total == 0:
        return {
            "total": 0,
            "by_status": {},
            "ok": 0,
            "fail": 0,
            "elapsed_mean": 0.0,
            "elapsed_median": 0.0,
            "error_types": {},
        }
    by_status = Counter(log.result_status for log in logs)
    elapsed = [log.elapsed_seconds for log in logs if log.elapsed_seconds]

    def _short_error(log: CycleLog) -> str:
        raw = log.error or log.data.get("parse_error") or ""
        if not raw:
            return ""
        first_line = str(raw).splitlines()[0] if str(raw).splitlines() else ""
        return first_line[:80]

    error_types = Counter(
        _short_error(log) for log in logs if log.result_status not in ("ok", "unknown")
    )
    error_types.pop("", None)
    return {
        "total": total,
        "by_status": dict(by_status),
        "ok": by_status.get("ok", 0),
        "fail": sum(c for s, c in by_status.items() if s not in ("ok", "unknown")),
        "elapsed_mean": round(statistics.fmean(elapsed), 3) if elapsed else 0.0,
        "elapsed_median": round(statistics.median(elapsed), 3) if elapsed else 0.0,
        "error_types": dict(error_types),
    }


def _cmd_stats(args: argparse.Namespace) -> int:
    out: dict[str, Any] = {}
    if args.scope in ("history", "both"):
        out["history"] = _history_stats(_shadow_logs(args.since))
    if args.scope in ("current", "both"):
        out["current"] = _compute_shadow_health()

    if args.json:
        if args.scope == "history":
            print(json.dumps(out["history"], indent=2))
        elif args.scope == "current":
            print(json.dumps(out["current"], indent=2))
        else:
            print(json.dumps(out, indent=2))
        return 0

    if "history" in out:
        h = out["history"]
        print(f"history (last {args.since}d, all signatures):")
        print(f"  total runs:     {h['total']}")
        print(f"  ok:             {h['ok']}")
        print(f"  fail:           {h['fail']}")
        if h["by_status"]:
            print("  by status:")
            for status, count in sorted(h["by_status"].items()):
                print(f"    {status:<14} {count}")
        if h["total"]:
            print(f"  elapsed mean:   {h['elapsed_mean']:.2f}s")
            print(f"  elapsed median: {h['elapsed_median']:.2f}s")
        if h["error_types"]:
            print("  error types:")
            for msg, count in sorted(h["error_types"].items(), key=lambda t: t[1], reverse=True)[
                :10
            ]:
                print(f"    [{count}] {_truncate(msg, 80)}")
    if "current" in out:
        c = out["current"]
        if "history" in out:
            print()
        print("current (in-scope, current config_signature):")
        print(f"  severity:               {c.get('severity', '?')}")
        print(f"  total (24h):            {c.get('total_24h', 0)}")
        print(f"  failed (24h):           {c.get('failed_24h', 0)}")
        print(f"  fail_rate:              {c.get('fail_rate', 0.0):.0%}")
        print(f"  consecutive_failures:   {c.get('consecutive_failures', 0)}")
        print(f"  config_signature:       {c.get('config_signature', '')}")
        if c.get("read_error"):
            print(f"  read_error:             {c['read_error']}")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan-review-shadow",
        description=(
            "Inspect shadow-runner cycle logs. Use `list` for the recent "
            "tail, `show` for full record detail, `stats` for aggregates."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent shadow runs")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument(
        "--since",
        type=int,
        default=7,
        help="filter by record mtime; days (default: 7)",
    )
    p_list.add_argument(
        "--status",
        choices=("ok", "fail", "all"),
        default="all",
    )
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="show one shadow record")
    p_show.add_argument(
        "target",
        nargs="?",
        default="latest",
        help="path to a *_shadow.json record, or 'latest' (default).",
    )
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=_cmd_show)

    p_stats = sub.add_parser("stats", help="aggregate counts and 24h health")
    p_stats.add_argument(
        "--since",
        type=int,
        default=7,
        help="history window in days (default: 7)",
    )
    p_stats.add_argument(
        "--scope",
        choices=("history", "current", "both"),
        default="both",
    )
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except OSError as e:
        # Catch the case where _load_logs's caller can't open the dir.
        # _load_logs itself returns [] for missing dirs, but a permission
        # flip mid-call could surface here.
        print(f"could not read review-log dir: {e}", file=sys.stderr)
        return 1
    return int(result) if result is not None else 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
