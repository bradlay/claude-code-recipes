#!/usr/bin/env python3
# Manual probe runner. Verifies that each chain (and shadow) provider's
# required model is reachable and returns a sane response — i.e. the
# auth tier actually grants access to the model the chain expects, not
# a silently-substituted free-tier fallback.
#
# Usage:
#   plan-review-probe                    # probe all chain + shadow providers
#   plan-review-probe --force            # bypass the 24h TTL cache
#   plan-review-probe --provider codex   # probe a single provider
#   plan-review-probe --json             # machine-readable output
#
# Exit codes:
#   0  every probed provider passed
#   1  at least one failed
#   2  invalid arguments / unknown provider

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib.chain import (  # noqa: E402
    PROVIDER_CMDS,
    _chain_from_env,
    _shadow_from_env,
)
from _lib.probes import probe_provider  # noqa: E402
from _lib.runner import default_chain  # noqa: E402


def _resolve_targets(provider: str | None) -> list[str]:
    if provider:
        if provider not in PROVIDER_CMDS:
            print(
                f"unknown provider: {provider!r}. "
                f"Allowed: {', '.join(PROVIDER_CMDS)}",
                file=sys.stderr,
            )
            sys.exit(2)
        return [provider]
    chain = _chain_from_env() or default_chain()
    shadow = _shadow_from_env()
    targets: list[str] = []
    for name in (*chain, *shadow):
        if name in targets:
            continue
        # Skip CLI providers that aren't installed; "local" never has a
        # PATH binary so always include it.
        if name == "local" or shutil.which(PROVIDER_CMDS.get(name, [name])[0]):
            targets.append(name)
    return targets


def _format_human(targets: list[str], results: list) -> str:
    chain = _chain_from_env() or default_chain()
    shadow = _shadow_from_env()
    lines: list[str] = []
    lines.append(f"chain: {' -> '.join(chain)}")
    if shadow:
        lines.append(f"shadow: {', '.join(shadow)}")
    lines.append("")
    width = max((len(name) for name in targets), default=8)
    for r in results:
        scope = []
        if r.name in chain:
            scope.append("chain")
        if r.name in shadow:
            scope.append("shadow")
        scope_str = "+".join(scope) or "?"
        cache_marker = " [cached]" if r.cached else ""
        status = "ok " if r.ok else "FAIL"
        lines.append(
            f"  {status}  {r.name.ljust(width)}  ({scope_str})  "
            f"model={r.model!r}{cache_marker}",
        )
        if not r.ok:
            lines.append(f"        {r.detail}")
    if any(not r.ok for r in results):
        lines.append("")
        lines.append("Some probes failed. Common fixes:")
        lines.append("  - codex:  `codex login status` / "
                     "`printenv OPENAI_API_KEY | codex login --with-api-key`")
        lines.append("  - gemini: re-auth via `gemini` first run, "
                     "or set GEMINI_API_KEY")
        lines.append("  - claude: `claude --version`; refresh Claude Code login")
        lines.append("  - local:  ensure CLAUDE_PLAN_REVIEW_LOCAL_URL backend "
                     "(e.g. autosre vLLM) is up and serving the expected model")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe plan-review-loop providers for auth + model access.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 24h TTL cache; always probe live.",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_CMDS),
        help="Probe a single provider instead of the full chain+shadow set.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    args = parser.parse_args()

    targets = _resolve_targets(args.provider)
    if not targets:
        print("no providers selected (chain + shadow are empty)", file=sys.stderr)
        return 2

    results = [probe_provider(name, force=args.force) for name in targets]

    if args.as_json:
        print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
    else:
        print(_format_human(targets, results))

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
