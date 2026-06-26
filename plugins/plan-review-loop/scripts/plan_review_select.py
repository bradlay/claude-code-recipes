#!/usr/bin/env python3
# Persist / clear / re-probe the per-session plan-review backend selection.
#
# Run by Claude after AskUserQuestion (the ExitPlanMode hook prints the exact,
# self-contained command), or by the /plan-review-backend command.
#
# Usage:
#   plan-review-select --session <id> <key>            # persist a choice
#   plan-review-select --session <id> --reprobe <key>  # probe first, then persist
#   plan-review-select --session <id> --clear          # forget the choice (re-ask)
#
# Exit codes:
#   0  ok
#   1  --reprobe failed (backend not reachable)
#   2  invalid arguments / unknown backend

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import backends, paths, picker  # noqa: E402
from _lib.probes import probe_provider  # noqa: E402


def _latest_session_id() -> str | None:
    """Newest session id from the hook archive — lets /plan-review-backend
    re-pick without the hook handing it the id."""
    path = paths.hooks_raw_jsonl()
    if not path.exists():
        return None
    latest: str | None = None
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                record = json.loads(line)
                sid = record.get("session_id")
                if isinstance(sid, str) and sid and sid != "unknown":
                    latest = sid
    return latest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plan-review-select")
    parser.add_argument("--session", help="session id from the hook")
    parser.add_argument(
        "--latest-session",
        action="store_true",
        help="resolve the session id from the most recent hook invocation",
    )
    parser.add_argument("--clear", action="store_true", help="forget the selection")
    parser.add_argument(
        "--reprobe",
        action="store_true",
        help="force a fresh probe before persisting",
    )
    parser.add_argument("backend", nargs="?", help="backend key to select")
    args = parser.parse_args(argv)

    session = args.session
    if not session and args.latest_session:
        session = _latest_session_id()
    if not session:
        print(
            "error: provide --session <id> or --latest-session (no recent hook session found).",
            file=sys.stderr,
        )
        return 2

    if args.clear:
        picker.clear_selection(session)
        print("plan-review backend selection cleared; next ExitPlanMode will ask again.")
        return 0

    allowed = ", ".join(backends.ONLINE_KEYS)
    if not args.backend:
        print(f"error: backend key required. Choose one of: {allowed}", file=sys.stderr)
        return 2

    key = backends.normalize_key(args.backend)
    if key is None or key not in backends.ONLINE_KEYS:
        print(f"error: unknown backend {args.backend!r}. Choose one of: {allowed}", file=sys.stderr)
        return 2

    if args.reprobe:
        result = probe_provider(key, force=True)
        if not result.ok:
            print(f"error: backend {key!r} failed its probe: {result.detail}", file=sys.stderr)
            return 1

    try:
        picker.write_selection(session, key)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"plan-review backend set to {key} ({backends.REGISTRY[key].label}) for this session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
