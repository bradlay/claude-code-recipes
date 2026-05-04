#!/usr/bin/env python3
# SessionStart preflight.
#
# Catches misconfiguration before the user hits ExitPlanMode and gets a
# denial. Emits additionalContext on the new session listing what's
# missing. Caches the report in preflight.json so subsequent sessions
# don't re-emit unchanged status. Writes health.json so the hook can
# read the most recent prereq state without re-running checks.

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, paths  # noqa: E402
from _lib.chain import PROVIDER_CMDS, _chain_from_env, _shadow_from_env  # noqa: E402
from _lib.probes import ProbeResult, probe_provider  # noqa: E402
from _lib.runner import default_chain  # noqa: E402


def _check_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = directory / f".preflight-probe-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _build_report() -> dict[str, Any]:
    chain = _chain_from_env() or default_chain()
    shadow = _shadow_from_env()

    findings: list[str] = []
    ok: list[str] = []
    missing: list[str] = []
    probe_results: list[ProbeResult] = []

    py = sys.executable or shutil.which("python3")
    if py:
        ok.append(f"python3 ({py})")
    else:
        findings.append("python3 not on PATH")

    available_providers: list[str] = []
    for prov in chain:
        cmd = PROVIDER_CMDS.get(prov, [prov])[0]
        if shutil.which(cmd):
            available_providers.append(prov)
            ok.append(f"{prov} ({cmd})")
        else:
            missing.append(f"{prov} ({cmd})")

    # Only a blocker when the entire chain is unreachable.
    if not available_providers:
        findings.append(
            "no provider in CLAUDE_PLAN_REVIEW_CHAIN is on PATH "
            f"(chain={','.join(chain)}). Install at least one of "
            f"[{', '.join(PROVIDER_CMDS.keys())}], or set "
            "CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 to bypass."
        )

    # Auth + model-access probes for every available chain provider AND every
    # shadow provider. Probes are TTL-cached (24h) and invalidated by
    # credential mtime changes — running here on every SessionStart is cheap
    # when warm, and re-validates auth before the user hits ExitPlanMode.
    probe_targets: list[str] = list(available_providers)
    for prov in shadow:
        if prov not in probe_targets and (
            prov == "local" or shutil.which(PROVIDER_CMDS.get(prov, [prov])[0])
        ):
            probe_targets.append(prov)

    chain_probe_failures: list[str] = []
    for prov in probe_targets:
        result = probe_provider(prov)
        probe_results.append(result)
        cache_marker = " [cached]" if result.cached else ""
        is_chain = prov in available_providers
        scope_marker = "" if is_chain else " (shadow)"
        if result.ok:
            ok.append(
                f"{prov}{scope_marker} model {result.model!r} reachable{cache_marker}",
            )
        else:
            msg = (
                f"{prov}{scope_marker} model {result.model!r} probe "
                f"failed{cache_marker}: {result.detail[:200]}"
            )
            missing.append(msg)
            if is_chain:
                chain_probe_failures.append(msg)

    # Block only when EVERY chain provider's probe failed — a single working
    # chain link is enough; the chain falls through naturally on bad probes.
    healthy_chain = [r for r in probe_results if r.ok and r.name in available_providers]
    if available_providers and not healthy_chain:
        findings.append(
            "every chain provider failed its model-access probe — the chain "
            "would error at ExitPlanMode. Fix auth/model access or narrow "
            "CLAUDE_PLAN_REVIEW_CHAIN to a working provider, or set "
            "CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 to bypass.\n"
            "    failures: " + " | ".join(chain_probe_failures),
        )

    data_root = paths.data_dir()
    if _check_writable(data_root):
        ok.append(f"writable {data_root}")
    else:
        findings.append(f"data dir not writable: {data_root}")

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain": chain,
        "shadow": shadow,
        "ok": ok,
        "missing": missing,
        "findings": findings,
        "probes": [r.to_dict() for r in probe_results],
        "healthy": not findings,
    }


def _report_signature(report: dict[str, Any]) -> str:
    """Hash the materially-relevant fields so unchanged reports don't re-emit."""
    # Probe outcomes (ok per provider + cred signature) are part of the
    # signature so a flipped auth state forces a fresh SessionStart context.
    probe_summary = [
        {
            "name": p["name"],
            "ok": p["ok"],
            "model": p.get("model", ""),
            "cred": p.get("cred_signature", ""),
        }
        for p in report.get("probes", [])
    ]
    sig_input = json.dumps(
        {
            "chain": report["chain"],
            "shadow": report.get("shadow", []),
            "ok": report["ok"],
            "missing": report.get("missing", []),
            "findings": report["findings"],
            "probes": probe_summary,
        },
        sort_keys=True,
    )
    return hashlib.sha256(sig_input.encode()).hexdigest()


def _format_context(report: dict[str, Any]) -> str:
    lines = ["plan-review-loop preflight:"]
    lines.append(f"  chain: {' -> '.join(report['chain'])}")
    shadow = report.get("shadow") or []
    if shadow:
        lines.append(f"  shadow: {', '.join(shadow)}")
    if report["ok"]:
        lines.append("  ok:")
        for line in report["ok"]:
            lines.append(f"    - {line}")
    if report.get("missing"):
        lines.append("  degraded (informational; chain falls through):")
        for line in report["missing"]:
            lines.append(f"    - {line}")
    if report["findings"]:
        lines.append("  blocking issues:")
        for line in report["findings"]:
            lines.append(f"    - {line}")
        lines.append(
            "  ExitPlanMode will be denied until these are resolved, unless "
            "CLAUDE_PLAN_REVIEW_FAIL_OPEN=1 is set.",
        )
    return "\n".join(lines)


def _write_health(report: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        record = {
            "timestamp": report["timestamp"],
            "status": "preflight_ok" if report["healthy"] else "preflight_degraded",
            "chain": report["chain"],
            "findings": report["findings"],
        }
        path = paths.health_file()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(record, indent=2) + "\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def main() -> int:
    # Parse stdin so the hook archive captures the SessionStart invocation.
    inv = _io.parse_stdin(__file__)
    _io.log(inv, "preflight running")

    report = _build_report()
    _write_health(report)

    cache_path = paths.preflight_cache_file()
    new_sig = _report_signature(report)

    cached_sig = ""
    if cache_path.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            cached = json.loads(cache_path.read_text())
            cached_sig = cached.get("signature", "")

    with contextlib.suppress(OSError):
        cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_path.write_text(
            json.dumps({"signature": new_sig, "report": report, "ts": time.time()}, indent=2) + "\n"
        )
        with contextlib.suppress(OSError):
            cache_path.chmod(0o600)

    # Re-emit if the report changed OR if there are any current findings
    # (so degraded state is surfaced every session until resolved).
    if new_sig != cached_sig or report["findings"]:
        return _io.emit_session_start_context(_format_context(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
