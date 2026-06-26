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

from _lib import _io, backends, paths  # noqa: E402
from _lib._shadow_signature import current_shadow_config_signature  # noqa: E402
from _lib.chain import PROVIDER_CMDS, _shadow_from_env, resolve_chain  # noqa: E402
from _lib.probes import ProbeResult, probe_provider  # noqa: E402
from quality_report import _load_logs  # noqa: E402


def _compute_shadow_health() -> dict[str, Any]:
    """24h shadow-runner health under the CURRENT config signature.

    Records under prior signatures (= prior config) drop out of the
    in-scope set because a config change rotates the signature, so an
    old failure window doesn't keep alarming after the operator
    actually fixes the problem.

    Returns a dict with severity ∈ {ok, warming, degraded, critical,
    unavailable}. Keys are stable across severities so the renderer
    and signature hash can index them without conditionals.
    """
    cur_sig = current_shadow_config_signature()
    log_dir = paths.review_log_dir()
    try:
        all_logs = _load_logs(log_dir, since_days=1)
    except OSError as e:
        return {
            "severity": "unavailable",
            "read_error": str(e),
            "config_signature": cur_sig,
            "total_24h": 0,
            "failed_24h": 0,
            "fail_rate": 0.0,
            "consecutive_failures": 0,
        }

    in_scope = [
        log
        for log in all_logs
        if log.is_shadow
        and log.shadow_config_signature == cur_sig
        and log.result_status != "unknown"
    ]
    total = len(in_scope)
    failed = sum(1 for log in in_scope if log.result_status != "ok")
    fail_rate = (failed / total) if total else 0.0

    in_scope.sort(key=lambda log: log.event_time_epoch, reverse=True)
    consecutive = 0
    # Bounded streak walk. 20 is generous given the 24h window and
    # default volumes — if every recent run failed we'll catch it.
    for log in in_scope[:20]:
        if log.result_status != "ok":
            consecutive += 1
        else:
            break

    if total == 0:
        # Fresh signature rotation OR a freshly-installed shadow runner
        # that hasn't been triggered yet. Surface explicitly so the
        # operator doesn't read "ok" and assume the fix landed.
        severity = "warming"
    else:
        severity = "ok"
        if total >= 10 and fail_rate >= 0.95:
            severity = "critical"
        elif total >= 5 and fail_rate >= 0.8:
            severity = "degraded"
        if consecutive >= 5:
            severity = "critical"
        elif consecutive >= 3 and severity == "ok":
            severity = "degraded"

    return {
        "severity": severity,
        "total_24h": total,
        "failed_24h": failed,
        "fail_rate": round(fail_rate, 3),
        "consecutive_failures": consecutive,
        "config_signature": cur_sig,
    }


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
    chain = resolve_chain()
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

    # Warm probes for every online picker backend (+ local) whose CLI is
    # installed, not just the active chain — so the interactive picker reads a
    # fresh cache and SessionStart surfaces each backend's health.
    for prov in [*backends.ONLINE_KEYS, "local"]:
        if prov in probe_targets:
            continue
        if prov == "local" or shutil.which(PROVIDER_CMDS.get(prov, [prov])[0]):
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

    shadow_health = _compute_shadow_health() if shadow else None

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chain": chain,
        "shadow": shadow,
        "ok": ok,
        "missing": missing,
        "findings": findings,
        "probes": [r.to_dict() for r in probe_results],
        "shadow_health": shadow_health,
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
    h = report.get("shadow_health") or {}
    shadow_health_for_sig = {
        "severity": h.get("severity", ""),
        "total_24h": h.get("total_24h", 0),
        "failed_24h": h.get("failed_24h", 0),
        "fail_rate": h.get("fail_rate", 0.0),
        "consecutive_failures": h.get("consecutive_failures", 0),
        "config_signature": h.get("config_signature", ""),
        "read_error": h.get("read_error", ""),
    }
    sig_input = json.dumps(
        {
            "chain": report["chain"],
            "shadow": report.get("shadow", []),
            "ok": report["ok"],
            "missing": report.get("missing", []),
            "findings": report["findings"],
            "probes": probe_summary,
            "shadow_health": shadow_health_for_sig,
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
    h = report.get("shadow_health") or {}
    sev = h.get("severity")
    if sev == "unavailable":
        lines.append("  shadow health unavailable:")
        lines.append(
            f"    - could not read review-log/: {h.get('read_error', 'unknown error')}",
        )
    elif sev == "warming":
        lines.append("  shadow warming:")
        lines.append(
            "    - 0 in-scope shadow runs under current config; "
            "trigger one ExitPlanMode to validate.",
        )
    elif sev in ("degraded", "critical"):
        lines.append(f"  shadow {sev}:")
        lines.append(
            f"    - failed {h['failed_24h']}/{h['total_24h']} of last 24h "
            f"shadow runs ({h['fail_rate']:.0%}); "
            f"{h['consecutive_failures']} consecutive failures. "
            f"`plan-review-shadow stats` for details.",
        )
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
    # Recursion guard (defense-in-depth; bin/preflight catches this first).
    # A SessionStart fired inside a plugin-spawned `claude --print` (probe or
    # review) must not re-probe, or each level spawns another claude and
    # fork-bombs. The launchers export CLAUDE_PLAN_REVIEW_NESTED on the child.
    if os.environ.get("CLAUDE_PLAN_REVIEW_NESTED"):
        return 0
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
    # OR if shadow health is anything other than ok (so degraded state
    # is surfaced every session until resolved).
    h = report.get("shadow_health") or {}
    shadow_alarming = h.get("severity") in (
        "warming",
        "degraded",
        "critical",
        "unavailable",
    )
    if new_sig != cached_sig or report["findings"] or shadow_alarming:
        return _io.emit_session_start_context(_format_context(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
