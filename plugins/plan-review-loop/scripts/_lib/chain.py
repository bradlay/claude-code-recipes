# Provider chain executor for plan review.
#
# Default chain: codex first, gemini and claude as fallbacks. Override
# with CLAUDE_PLAN_REVIEW_CHAIN (comma-separated; same allowed names).
#
# Per-cycle log files capture full prompt, stdout, stderr, findings, and
# metadata for every iteration so the loop is auditable and reproducible.
# Set CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY=1 to drop the content fields
# and keep only timestamps, sizes, and findings counts. Files are 0600
# in 0700 dirs; the dir is pruned to the 50 most recent entries.

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import paths

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SECURE_FILE_MODE = 0o600

GEMINI_MODEL = "auto-gemini-3"
CLAUDE_SONNET_MODEL = "claude-sonnet-4-6"
CODEX_MODEL = "gpt-5.4"


def _review_log_file() -> Path:
    return paths.data_dir() / "review-chain.log"


def _review_log_dir() -> Path:
    return paths.review_log_dir()


MAX_PROMPT_LOG = 512_000
MAX_STDOUT_LOG = 2_097_152
MAX_STDERR_LOG = 512_000

_LOG_RETAIN = 50  # prune oldest entries beyond this count


def _metadata_only_logs() -> bool:
    return os.environ.get("CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _file_log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with contextlib.suppress(OSError):
        path = _review_log_file()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = path.exists()
        with path.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
        if not existed:
            with contextlib.suppress(OSError):
                path.chmod(_SECURE_FILE_MODE)


def _prune_log_dir(directory: Path, retain: int = _LOG_RETAIN) -> None:
    """Keep only the most recent `retain` JSON files."""
    with contextlib.suppress(OSError):
        entries = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in entries[retain:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def _save_cycle_log(
    provider: str,
    prompt: str,
    stdout: str,
    stderr: str,
    returncode: int | None,
    elapsed: float,
    findings: list[dict[str, Any]] | None,
    *,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    meta: dict[str, Any] = dict(metadata) if metadata else {}
    plan_path_raw = meta.get("plan_path") or ""
    plan_filename = meta.get("plan_filename", "") or ""
    plan_stem = plan_filename.removesuffix(".md") or "unknown"
    if plan_path_raw:
        path_hash = hashlib.sha256(str(plan_path_raw).encode()).hexdigest()[:12]
        plan_stem = f"{path_hash}_{plan_stem}"
    iteration = meta.get("iteration", 0)
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{plan_stem}_{iteration}_{ts}_{provider}.json"

    try:
        log_dir = _review_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = log_dir / filename

        log_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "provider": provider,
            "elapsed_seconds": round(elapsed, 2),
            "returncode": returncode,
            "error": error,
            "plan_path": meta.get("plan_path"),
            "plan_filename": meta.get("plan_filename"),
            "plan_title": meta.get("plan_title"),
            "iteration": iteration,
            "project": meta.get("project"),
            "findings_count": len(findings) if findings else 0,
        }

        if not _metadata_only_logs():
            log_data.update(
                {
                    "prompt_size": len(prompt),
                    "prompt": prompt[:MAX_PROMPT_LOG],
                    "stdout_size": len(stdout),
                    "stdout": stdout[:MAX_STDOUT_LOG],
                    "stderr_size": len(stderr),
                    "stderr": stderr[:MAX_STDERR_LOG],
                    "findings": findings,
                },
            )

        log_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False) + "\n")
        with contextlib.suppress(OSError):
            log_path.chmod(_SECURE_FILE_MODE)
        _file_log(f"cycle log saved: {log_path}")
        _prune_log_dir(log_dir)
        return log_path
    except OSError as e:
        _file_log(f"failed to save cycle log: {e}")
        return None


PROVIDER_CMDS: dict[str, list[str]] = {
    "codex": [
        "codex",
        "exec",
        "-c",
        f'model="{CODEX_MODEL}"',
        "-c",
        'model_reasoning_effort="xhigh"',
        "--skip-git-repo-check",
    ],
    "gemini": ["gemini", "--model", GEMINI_MODEL, "-p"],
    "claude": ["claude", "--print", "--model", CLAUDE_SONNET_MODEL],
}

# Default chain: codex first (gpt-5.4 xhigh); gemini and claude as fallbacks.
# Override with CLAUDE_PLAN_REVIEW_CHAIN (see README).
DEFAULT_CHAINS: dict[str, list[str]] = {
    "plan": ["codex", "gemini", "claude"],
}

PROVIDER_TIMEOUTS: dict[str, int] = {
    "codex": 900,
    "gemini": 180,
    "claude": 180,
}
DEFAULT_PROVIDER_TIMEOUT = 300

MAX_CHAIN_SECONDS = 1200


def _chain_from_env() -> list[str] | None:
    raw = os.environ.get("CLAUDE_PLAN_REVIEW_CHAIN")
    if not raw:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    valid = [n for n in names if n in PROVIDER_CMDS]
    invalid = [n for n in names if n not in PROVIDER_CMDS]
    if invalid:
        logger.warning(
            "chain_env_unknown_providers: invalid=%r allowed=%r",
            invalid,
            list(PROVIDER_CMDS),
        )
    return valid or None


@dataclass
class ProviderAttempt:
    provider: str
    success: bool
    elapsed_seconds: float
    returncode: int | None = None
    stdout_size: int = 0
    stderr_size: int = 0
    error: str | None = None
    findings_count: int | None = None
    raw_stdout: str = ""


@dataclass
class ChainResult:
    provider: str
    findings: list[dict[str, Any]] | None
    questions: list[str] | None
    raw_output: str
    elapsed_seconds: float
    attempts: list[ProviderAttempt] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def format_findings(self) -> str:
        if not self.findings:
            return ""

        lines = [f"Plan review findings (via {self.provider}):\n"]
        counts: dict[str, int] = {}

        for f in self.findings:
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

        summary_parts = [f"{counts.get(s, 0)} {s}" for s in ("P0", "P1", "P2") if counts.get(s)]
        lines.insert(1, f"  ({', '.join(summary_parts)})\n")

        lines.append(
            "BLOCKING: Address ALL findings above (P0, P1, and P2) in the plan, "
            "then call ExitPlanMode again for re-review.",
        )
        return "\n".join(lines)


def run_chain(
    prompt: str,
    *,
    chain: list[str] | None = None,
    mode: str = "plan",
    metadata: dict[str, Any] | None = None,
) -> ChainResult:
    if chain is None:
        chain = _chain_from_env() or DEFAULT_CHAINS.get(mode, DEFAULT_CHAINS["plan"])

    meta: dict[str, Any] = dict(metadata) if metadata else {}
    plan_name = meta.get("plan_filename", "").replace(".md", "") or "unknown"

    total_start = time.monotonic()
    attempts: list[ProviderAttempt] = []

    _file_log("=" * 60)
    _file_log(
        f"chain start: mode={mode}, chain={chain}, prompt_size={len(prompt)}, "
        f"plan={plan_name}, budget={MAX_CHAIN_SECONDS}s",
    )
    logger.info(
        "chain_start: mode=%r chain=%r prompt_size=%d plan=%r",
        mode,
        chain,
        len(prompt),
        plan_name,
    )

    for provider_name in chain:
        chain_elapsed = time.monotonic() - total_start
        if chain_elapsed >= MAX_CHAIN_SECONDS:
            _file_log(
                f"chain budget exhausted ({chain_elapsed:.0f}s >= {MAX_CHAIN_SECONDS}s), "
                f"skipping {provider_name}",
            )
            break

        if provider_name not in PROVIDER_CMDS:
            _file_log(f"skipping unknown provider: {provider_name}")
            attempts.append(
                ProviderAttempt(
                    provider=provider_name,
                    success=False,
                    elapsed_seconds=0,
                    error=f"unknown provider (allowed: {', '.join(PROVIDER_CMDS)})",
                ),
            )
            continue

        cmd_prefix = PROVIDER_CMDS[provider_name]
        cli_binary = cmd_prefix[0]

        if not shutil.which(cli_binary):
            _file_log(f"skipping {provider_name} ({cli_binary} not on PATH)")
            attempts.append(
                ProviderAttempt(
                    provider=provider_name,
                    success=False,
                    elapsed_seconds=0,
                    error=f"{cli_binary} not on PATH",
                ),
            )
            continue

        provider_timeout = PROVIDER_TIMEOUTS.get(provider_name, DEFAULT_PROVIDER_TIMEOUT)
        remaining = MAX_CHAIN_SECONDS - (time.monotonic() - total_start)
        effective_timeout = min(provider_timeout, int(remaining))

        attempt = _try_provider(provider_name, prompt, timeout=effective_timeout, metadata=meta)
        attempts.append(attempt)

        if attempt.success and attempt.findings_count is not None:
            elapsed = time.monotonic() - total_start
            _file_log(
                f"chain complete: provider={provider_name}, "
                f"findings={attempt.findings_count}, elapsed={elapsed:.1f}s",
            )

            raw = attempt.raw_stdout
            findings, questions = _parse_response_json(raw)

            return ChainResult(
                provider=provider_name,
                findings=findings,
                questions=questions,
                raw_output=raw,
                elapsed_seconds=elapsed,
                attempts=attempts,
            )

    elapsed = time.monotonic() - total_start
    _file_log(f"chain exhausted: all providers failed or clean, elapsed={elapsed:.1f}s")

    return ChainResult(
        provider="",
        findings=None,
        questions=None,
        raw_output="",
        elapsed_seconds=elapsed,
        attempts=attempts,
    )


def _try_provider(
    name: str,
    prompt: str,
    *,
    timeout: int,
    metadata: dict[str, Any] | None = None,
) -> ProviderAttempt:
    if name not in PROVIDER_CMDS:
        raise ValueError(f"Unknown provider: {name}. Allowed: {', '.join(PROVIDER_CMDS)}")
    cmd_prefix = PROVIDER_CMDS[name]
    cmd = [*cmd_prefix, prompt]

    _file_log(f"trying {name}: timeout={timeout}s")

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed = time.monotonic() - start

        _file_log(
            f"{name} completed: rc={result.returncode}, "
            f"stdout={len(result.stdout)} chars, stderr={len(result.stderr)} chars, "
            f"elapsed={elapsed:.1f}s",
        )

        if result.returncode != 0:
            stderr_preview = result.stderr[:500]
            _file_log(f"{name} FAILED stderr: {stderr_preview}")
            _save_cycle_log(
                name,
                prompt,
                result.stdout,
                result.stderr,
                result.returncode,
                elapsed,
                None,
                error=stderr_preview[:200],
                metadata=metadata,
            )

            return ProviderAttempt(
                provider=name,
                success=False,
                elapsed_seconds=elapsed,
                returncode=result.returncode,
                stdout_size=len(result.stdout),
                stderr_size=len(result.stderr),
                error=f"rc={result.returncode}: {stderr_preview[:200]}",
            )

        findings = _parse_findings_json(result.stdout)
        findings_count = len(findings) if findings else 0

        if findings_count > 0:
            _file_log(f"{name} returned {findings_count} findings")
        else:
            _file_log(f"{name} returned no findings (clean or unparseable)")

        _save_cycle_log(
            name,
            prompt,
            result.stdout,
            result.stderr,
            0,
            elapsed,
            findings,
            metadata=metadata,
        )

        return ProviderAttempt(
            provider=name,
            success=True,
            elapsed_seconds=elapsed,
            returncode=0,
            stdout_size=len(result.stdout),
            stderr_size=len(result.stderr),
            findings_count=findings_count,
            raw_stdout=result.stdout,
        )

    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        _file_log(f"{name} TIMED OUT after {elapsed:.1f}s (limit={timeout}s)")
        _save_cycle_log(
            name,
            prompt,
            "",
            "",
            None,
            elapsed,
            None,
            error=f"timed out after {elapsed:.1f}s",
            metadata=metadata,
        )
        return ProviderAttempt(
            provider=name,
            success=False,
            elapsed_seconds=elapsed,
            error=f"timed out after {elapsed:.1f}s",
        )

    except (FileNotFoundError, OSError) as e:
        elapsed = time.monotonic() - start
        _file_log(f"{name} OS ERROR: {e}")
        _save_cycle_log(name, prompt, "", "", None, elapsed, None, error=str(e), metadata=metadata)
        return ProviderAttempt(
            provider=name,
            success=False,
            elapsed_seconds=elapsed,
            error=str(e),
        )


def _parse_response_json(
    raw: str,
) -> tuple[list[dict[str, Any]] | None, list[str] | None]:
    if not raw or not raw.strip():
        return None, None

    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return None, None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None, None

    findings = data.get("findings", []) or None
    questions = data.get("questions", []) or None
    return findings, questions


def _parse_findings_json(raw: str) -> list[dict[str, Any]] | None:
    findings, _ = _parse_response_json(raw)
    return findings
