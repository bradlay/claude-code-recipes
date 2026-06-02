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
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from ._shadow_signature import current_shadow_config_signature

logger = logging.getLogger(__name__)

_SECURE_FILE_MODE = 0o600

# Model selection. Defaults are the highest-tier published alias per provider.
# Override centrally via env vars (export from ~/.bashrc, .envrc, etc.) so a
# single setting flows to every Claude Code session and every tool that reads
# these constants:
#
#   CLAUDE_PLAN_REVIEW_CODEX_MODEL    (default: "gpt-5.4")
#   CLAUDE_PLAN_REVIEW_GEMINI_MODEL   (default: "auto-gemini-3")
#   CLAUDE_PLAN_REVIEW_CLAUDE_MODEL   (default: "claude-sonnet-4-6")
#   CLAUDE_PLAN_REVIEW_LOCAL_MODEL    (handled in local_provider.py)
#
# auto-gemini-3 is Google's stable alias for the Gemini 3 family auto-router;
# specific IDs like gemini-3-pro do not resolve via the gemini CLI. gpt-5.4
# requires a paid OpenAI tier; the chain refuses to silently downgrade.
CODEX_MODEL = os.environ.get("CLAUDE_PLAN_REVIEW_CODEX_MODEL", "gpt-5.4")
GEMINI_MODEL = os.environ.get("CLAUDE_PLAN_REVIEW_GEMINI_MODEL", "auto-gemini-3")
CLAUDE_SONNET_MODEL = os.environ.get("CLAUDE_PLAN_REVIEW_CLAUDE_MODEL", "claude-sonnet-4-6")


def _review_log_file() -> Path:
    return paths.data_dir() / "review-chain.log"


def _review_log_dir() -> Path:
    return paths.review_log_dir()


MAX_PROMPT_LOG = 512_000
MAX_STDOUT_LOG = 2_097_152
MAX_STDERR_LOG = 512_000

_LOG_RETAIN = 50  # prune oldest entries beyond this count
# (applies to non-shadow logs only; shadow
# logs use the time-based pruner below).

# Shadow-log retention: time-based with a count floor for fresh
# installs. The 24h fail-rate window and 7-day stale check both
# require the actual record set to be intact, so a pure count
# floor would silently drop window data on any active install.
SHADOW_RETAIN_DAYS_DEFAULT = 8  # > 7d health window + slack
SHADOW_RETAIN_COUNT_FLOOR = 200  # protects fresh installs


def _shadow_retain_days() -> int:
    """Operator override; clamped at the health-window floor."""
    raw = os.environ.get("CLAUDE_PLAN_REVIEW_SHADOW_RETAIN_DAYS", "").strip()
    if not raw:
        return SHADOW_RETAIN_DAYS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return SHADOW_RETAIN_DAYS_DEFAULT
    return max(value, SHADOW_RETAIN_DAYS_DEFAULT)


# Test seam — pytest monkeypatches this to deterministically
# pause writers between tempfile flush/fsync and os.replace.
# Default no-op; production code never sets it.
def _atomic_replace_hook_default() -> None:
    return None


_ATOMIC_REPLACE_HOOK: Callable[[], None] = _atomic_replace_hook_default


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
    """Keep only the most recent `retain` JSON files. Applied to
    non-shadow records; shadow records use `_prune_shadow_log_dir`."""
    with contextlib.suppress(OSError):
        non_shadow = [p for p in directory.glob("*.json") if not p.stem.endswith("_shadow")]
        non_shadow.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in non_shadow[retain:]:
            with contextlib.suppress(OSError):
                stale.unlink()


def _prune_shadow_log_dir(directory: Path) -> None:
    """Time-based shadow retention: keep all records newer than
    SHADOW_RETAIN_DAYS regardless of count, plus the newest
    SHADOW_RETAIN_COUNT_FLOOR records as a sparse-history protection.

    Suppresses FileNotFoundError around stat() and unlink() to
    tolerate concurrent pruning/scanning races (benign — anything
    deleted by another process is, by definition, no longer a
    pruning concern).
    """
    cutoff = time.time() - _shadow_retain_days() * 86400
    entries: list[tuple[Path, float]] = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                if not e.name.endswith("_shadow.json"):
                    continue
                with contextlib.suppress(FileNotFoundError):
                    entries.append((Path(e.path), e.stat().st_mtime))
    except FileNotFoundError:
        return

    entries.sort(key=lambda t: t[1], reverse=True)
    keep_by_age = {p for p, m in entries if m >= cutoff}
    keep_by_count = {p for p, _ in entries[:SHADOW_RETAIN_COUNT_FLOOR]}
    keep = keep_by_age | keep_by_count

    for p, _ in entries:
        if p in keep:
            continue
        with contextlib.suppress(FileNotFoundError, PermissionError):
            p.unlink()


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
    shadow: bool = False,
    primary_provider: str | None = None,
) -> Path | None:
    """Write a per-cycle log file. Shadow runs (post-decision measurement
    runs of secondary providers) get `shadow=true` plus a `primary_provider`
    pointer so the quality-report tool can pair them back up."""
    meta: dict[str, Any] = dict(metadata) if metadata else {}
    plan_path_raw = meta.get("plan_path") or ""
    plan_filename = meta.get("plan_filename", "") or ""
    plan_stem = plan_filename.removesuffix(".md") or "unknown"
    if plan_path_raw:
        path_hash = hashlib.sha256(str(plan_path_raw).encode()).hexdigest()[:12]
        plan_stem = f"{path_hash}_{plan_stem}"
    iteration = meta.get("iteration", 0)
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = "_shadow" if shadow else ""
    filename = f"{plan_stem}_{iteration}_{ts}_{provider}{suffix}.json"

    # Compute result_status semantically. `error` set / non-zero rc
    # always wins. Empty stdout is `empty`. Otherwise we classify
    # via the parser's explicit parse_ok signal — `findings: []` is
    # a legitimate clean review and counts as ok.
    parse_error: str | None = None
    if error or (returncode is not None and returncode != 0):
        result_status = "error"
    elif not stdout or not stdout.strip():
        result_status = "empty"
    else:
        parse = _parse_response_json(stdout)
        if parse.parse_ok:
            result_status = "ok"
        else:
            result_status = "unparseable"
            parse_error = parse.parse_error

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
            "shadow": shadow,
            "primary_provider": primary_provider,
            # Post-A.0 fields (writer side).
            "result_status": result_status,
            "shadow_config_signature": current_shadow_config_signature(),
            "parse_error": parse_error,
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

        # Atomic write: tempfile in same dir, fsync content, replace,
        # best-effort dir fsync. Reader sees complete file or no file.
        payload = json.dumps(log_data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=str(log_dir),
            prefix=f".{filename}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(OSError):
                Path(tmp).chmod(_SECURE_FILE_MODE)
            _ATOMIC_REPLACE_HOOK()
            Path(tmp).replace(log_path)
            # Best-effort dir fsync (POSIX only) for crash-durability.
            if hasattr(os, "O_DIRECTORY"):
                with contextlib.suppress(OSError):
                    dir_fd = os.open(str(log_dir), os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

        _file_log(f"cycle log saved: {log_path}")
        if shadow:
            _prune_shadow_log_dir(log_dir)
        else:
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
    "gemini": ["gemini", "--model", GEMINI_MODEL, "-p", ""],
    "claude": ["claude", "--print", "--model", CLAUDE_SONNET_MODEL],
    # Local OpenAI-compat backend (vLLM, Ollama, llama.cpp server, ...).
    # Configure URL/model via CLAUDE_PLAN_REVIEW_LOCAL_* env vars; see
    # _lib/local_provider.py. Not in any default chain — opt in via
    # CLAUDE_PLAN_REVIEW_CHAIN or CLAUDE_PLAN_REVIEW_SHADOW.
    # Absolute path so the subprocess (fresh sys.path) can find it.
    "local": [sys.executable, str(Path(__file__).resolve().parent / "local_provider.py")],
}

# Default chain: codex first (gpt-5.4 xhigh); gemini and claude as fallbacks.
# Override with CLAUDE_PLAN_REVIEW_CHAIN (see README).
DEFAULT_CHAINS: dict[str, list[str]] = {
    "plan": ["codex", "gemini", "claude"],
}

# Tier presets selectable via CLAUDE_PLAN_REVIEW_TIER. `strict` is the
# default and tracks DEFAULT_CHAINS["plan"]; `fast` skips codex/gemini
# so routine plans don't pay the gpt-5.4 xhigh cost. Explicit
# CLAUDE_PLAN_REVIEW_CHAIN always wins; tier is the fallback.
_FAST_CHAIN: list[str] = ["claude"]

PROVIDER_TIMEOUTS: dict[str, int] = {
    "codex": 900,
    "gemini": 180,
    "claude": 180,
    "local": 600,
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


def _tier_from_env() -> list[str]:
    """Return the chain selected by CLAUDE_PLAN_REVIEW_TIER. Unknown
    tier values fall back to strict so a typo never silently
    downgrades to the cheap chain."""
    tier = os.environ.get("CLAUDE_PLAN_REVIEW_TIER", "").strip().lower()
    if tier == "fast":
        return list(_FAST_CHAIN)
    return list(DEFAULT_CHAINS["plan"])


def resolve_chain(mode: str = "plan") -> list[str]:
    """Select the provider chain. CLAUDE_PLAN_REVIEW_CHAIN wins when it
    parses to at least one valid provider. Otherwise CLAUDE_PLAN_REVIEW_TIER
    selects strict (default) or fast. Unknown tier values fall back to
    strict; an entirely-invalid CLAUDE_PLAN_REVIEW_CHAIN also falls
    through to the tier path so we never return an unusable list.
    `mode` is reserved for parity with DEFAULT_CHAINS but currently
    only `plan` is wired."""
    explicit = _chain_from_env()
    if explicit:
        return explicit
    return _tier_from_env()


def _shadow_from_env() -> list[str]:
    """Parse CLAUDE_PLAN_REVIEW_SHADOW. Returns the list of providers to run
    in shadow mode (after the primary chain returns) for measurement."""
    raw = os.environ.get("CLAUDE_PLAN_REVIEW_SHADOW")
    if not raw:
        return []
    names = [n.strip() for n in raw.split(",") if n.strip()]
    valid = [n for n in names if n in PROVIDER_CMDS]
    invalid = [n for n in names if n not in PROVIDER_CMDS]
    if invalid:
        logger.warning(
            "shadow_env_unknown_providers: invalid=%r allowed=%r",
            invalid,
            list(PROVIDER_CMDS),
        )
    return valid


_SHADOW_RUNNER_SCRIPT = str(Path(__file__).resolve().parent / "shadow_runner.py")


def _dispatch_shadow_runs(
    prompt: str,
    primary_provider: str,
    metadata: dict[str, Any],
) -> list[str]:
    """Spawn detached subprocesses to re-run the prompt against shadow
    providers for quality measurement. Returns the list of providers
    actually dispatched (for logging).

    Shadow runs:
      - Are skipped for the provider that just won the primary chain
        (would just duplicate the log we already wrote).
      - Run in a new session group (start_new_session=True) so the parent
        hook can exit immediately without killing them.
      - Write their own cycle-log file with shadow=true and a
        primary_provider pointer.
      - Have stdin/stdout/stderr redirected to DEVNULL — they are
        invisible to the user.

    Errors during dispatch are swallowed; shadow runs are best-effort
    measurement, not part of the decision path.
    """
    shadow_providers = [p for p in _shadow_from_env() if p != primary_provider]
    if not shadow_providers:
        return []

    job: dict[str, Any] = {
        "prompt": prompt,
        "primary_provider": primary_provider,
        "metadata": metadata,
        "providers": shadow_providers,
    }

    job_path: Path | None = None
    try:
        job_dir = paths.data_dir() / "shadow-jobs"
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        ts = time.strftime("%Y%m%d-%H%M%S")
        job_path = job_dir / f"job-{ts}-{os.getpid()}.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False))
        with contextlib.suppress(OSError):
            job_path.chmod(_SECURE_FILE_MODE)
    except OSError as exc:
        _file_log(f"shadow dispatch: failed to write job file: {exc}")
        return []

    try:
        # Disable shadow recursion in the child process and clear the
        # primary chain so the runner can't accidentally re-enter run_chain.
        child_env = dict(os.environ)
        child_env.pop("CLAUDE_PLAN_REVIEW_SHADOW", None)
        subprocess.Popen(
            [sys.executable, _SHADOW_RUNNER_SCRIPT, str(job_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=child_env,
        )
    except OSError as exc:
        _file_log(f"shadow dispatch: failed to spawn runner: {exc}")
        return []

    _file_log(
        f"shadow dispatch: providers={shadow_providers} primary={primary_provider} job={job_path}",
    )
    return shadow_providers


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
        chain = resolve_chain(mode)

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
            parse = _parse_response_json(raw)
            findings = parse.findings if parse.parse_ok else None
            questions = parse.questions if parse.parse_ok else None

            _dispatch_shadow_runs(prompt, provider_name, meta)

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

    # Even on chain exhaustion (no provider returned findings), still fire
    # shadows — measuring how a shadow provider would have responded when
    # all primaries failed is valuable signal.
    _dispatch_shadow_runs(prompt, "", meta)

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
    shadow: bool = False,
    primary_provider: str | None = None,
) -> ProviderAttempt:
    if name not in PROVIDER_CMDS:
        raise ValueError(f"Unknown provider: {name}. Allowed: {', '.join(PROVIDER_CMDS)}")
    cmd_prefix = PROVIDER_CMDS[name]
    # Pass prompt via stdin, not argv. Linux MAX_ARG_STRLEN caps a single
    # argv element at ~128KB regardless of ARG_MAX; large plans hit Errno 7
    # ("Argument list too long") on every provider exec.
    cmd = list(cmd_prefix)

    _file_log(f"trying {name}{' (shadow)' if shadow else ''}: timeout={timeout}s")

    start = time.monotonic()
    # Mark the child: a `claude` provider review spawns `claude --print`,
    # whose SessionStart preflight would otherwise probe claude and recurse.
    child_env = {**os.environ, "CLAUDE_PLAN_REVIEW_NESTED": "1"}
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
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
                shadow=shadow,
                primary_provider=primary_provider,
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
            shadow=shadow,
            primary_provider=primary_provider,
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
            shadow=shadow,
            primary_provider=primary_provider,
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
        _save_cycle_log(
            name,
            prompt,
            "",
            "",
            None,
            elapsed,
            None,
            error=str(e),
            metadata=metadata,
            shadow=shadow,
            primary_provider=primary_provider,
        )
        return ProviderAttempt(
            provider=name,
            success=False,
            elapsed_seconds=elapsed,
            error=str(e),
        )


@dataclass(frozen=True)
class ReviewParse:
    """Explicit parse state. parse_ok=True iff the model emitted
    valid JSON with a `findings` key shaped as a list (possibly
    empty — `findings: []` IS a legitimate clean review). The old
    `findings or None` collapsing made `[]` indistinguishable from
    "couldn't parse anything", which corrupted downstream
    classification."""

    findings: list[dict[str, Any]] | None
    questions: list[str] | None
    parse_ok: bool
    parse_error: str | None


def _parse_response_json(raw: str) -> ReviewParse:
    if not raw or not raw.strip():
        return ReviewParse(None, None, False, "empty")

    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return ReviewParse(None, None, False, "no JSON object")

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return ReviewParse(None, None, False, f"json decode: {e}")

    if not isinstance(data, dict):
        return ReviewParse(None, None, False, "top-level not object")

    findings_raw = data.get("findings")
    if not isinstance(findings_raw, list):
        return ReviewParse(
            None,
            None,
            False,
            "missing/non-list findings",
        )

    questions_raw = data.get("questions")
    questions = questions_raw if isinstance(questions_raw, list) else None

    return ReviewParse(findings_raw, questions, True, None)


def _parse_findings_json(raw: str) -> list[dict[str, Any]] | None:
    """Backwards-compatible shim returning just the findings list (or
    None on parse failure). Existing callers in this module use it."""
    parse = _parse_response_json(raw)
    return parse.findings if parse.parse_ok else None
