# Per-provider auth + model-access probes with TTL cache.
#
# Each probe verifies (a) the CLI is callable and (b) the chain's required
# model returns a sane response — not a silently downgraded model on a
# free tier. Results are cached at $DATA_DIR/probe-cache.json with a 24h
# TTL and invalidated when relevant credential files are touched.
#
# Why probe at all: the chain hardcodes high-tier models (gpt-5.4,
# auto-gemini-3, claude-sonnet-4-6). A free or downgraded account silently
# substitutes a weaker model and tanks review quality. The probe asserts
# the actual model returns a recognizable response; an account that
# can't access the model returns an error or a wrong-model identifier.

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import paths
from .chain import (
    CLAUDE_SONNET_MODEL,
    CODEX_MODEL,
    GEMINI_MODEL,
)

PROBE_TIMEOUT_SECONDS = 60
PROBE_CACHE_TTL_SECONDS = 24 * 3600
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_EXPECTED_TOKEN = "ok"
_SECURE_FILE_MODE = 0o600


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str = ""
    model: str = ""
    last_probed: float = 0.0
    cred_signature: str = ""
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cache_file() -> Path:
    return paths.data_dir() / "probe-cache.json"


def _load_cache() -> dict[str, dict[str, Any]]:
    path = _cache_file()
    if not path.exists():
        return {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(path.read_text())
    return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(_SECURE_FILE_MODE)


def _cred_signature(name: str) -> str:
    """Hash the inputs that, if changed, should invalidate a cached probe.
    Includes credential-file mtimes and the resolved model name (env var
    overrides count). The hash is deterministic per-machine, never logged."""
    parts: list[str] = [name]
    if name == "codex":
        parts.append(CODEX_MODEL)
        for env_key in ("OPENAI_API_KEY",):
            parts.append(env_key + "=" + os.environ.get(env_key, ""))
        for path_str in (Path.home() / ".codex" / "auth.json",):
            with contextlib.suppress(OSError):
                parts.append(f"{path_str}:{path_str.stat().st_mtime}")
    elif name == "gemini":
        parts.append(GEMINI_MODEL)
        for env_key in ("GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
            parts.append(env_key + "=" + os.environ.get(env_key, ""))
        for path_str in (Path.home() / ".gemini" / "oauth_creds.json",):
            with contextlib.suppress(OSError):
                parts.append(f"{path_str}:{path_str.stat().st_mtime}")
    elif name == "claude":
        parts.append(CLAUDE_SONNET_MODEL)
    elif name == "local":
        parts.append(os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_URL", ""))
        parts.append(os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_MODEL", ""))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _run_cli_probe(argv: list[str]) -> tuple[bool, str]:
    """Pipe PROBE_PROMPT through stdin; assert response contains the token."""
    try:
        result = subprocess.run(
            argv,
            input=PROBE_PROMPT,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {PROBE_TIMEOUT_SECONDS}s"
    except OSError as e:
        return False, f"OSError: {e}"
    if result.returncode != 0:
        stderr_preview = (result.stderr or "").strip()[:300]
        return False, f"rc={result.returncode}; stderr: {stderr_preview!r}"
    body = (result.stdout or "").strip().lower()
    if PROBE_EXPECTED_TOKEN not in body:
        return False, f"unexpected response: {body[:200]!r}"
    return True, body[:200]


def _probe_codex() -> tuple[bool, str]:
    if shutil.which("codex") is None:
        return False, "codex not on PATH"
    return _run_cli_probe([
        "codex", "exec",
        "-c", f'model="{CODEX_MODEL}"',
        "-c", 'model_reasoning_effort="low"',
        "--skip-git-repo-check",
    ])


def _probe_gemini() -> tuple[bool, str]:
    if shutil.which("gemini") is None:
        return False, "gemini not on PATH"
    return _run_cli_probe([
        "gemini", "--model", GEMINI_MODEL, "-p", "",
    ])


def _probe_claude() -> tuple[bool, str]:
    if shutil.which("claude") is None:
        return False, "claude not on PATH"
    return _run_cli_probe([
        "claude", "--print", "--model", CLAUDE_SONNET_MODEL,
    ])


def _probe_local() -> tuple[bool, str]:
    """Probe the local OpenAI-compat backend's /v1/models endpoint."""
    base = os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_URL", "").rstrip("/")
    if not base:
        return False, "CLAUDE_PLAN_REVIEW_LOCAL_URL not set"
    url = f"{base}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
        return False, f"GET {url} failed: {e}"
    models = [m.get("id", "") for m in payload.get("data", [])]
    if not models:
        return False, f"empty model list at {url}"
    expected = os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_MODEL", "")
    if expected and expected not in models:
        return False, f"expected model {expected!r} not served (got {models})"
    return True, f"served: {models[0]}"


_PROBES: dict[str, Any] = {
    "codex": _probe_codex,
    "gemini": _probe_gemini,
    "claude": _probe_claude,
    "local": _probe_local,
}


def probe_provider(name: str, *, force: bool = False) -> ProbeResult:
    """Run the probe for `name`, honoring TTL cache unless force=True."""
    if name not in _PROBES:
        return ProbeResult(name=name, ok=False, detail="unknown provider")

    cache = _load_cache()
    sig = _cred_signature(name)
    now = time.time()

    if not force:
        entry = cache.get(name)
        if (
            entry
            and entry.get("cred_signature") == sig
            and now - float(entry.get("last_probed", 0)) < PROBE_CACHE_TTL_SECONDS
        ):
            return ProbeResult(
                name=name,
                ok=bool(entry.get("ok")),
                detail=str(entry.get("detail", "")),
                model=str(entry.get("model", "")),
                last_probed=float(entry.get("last_probed", 0)),
                cred_signature=sig,
                cached=True,
            )

    ok, detail = _PROBES[name]()
    model = {
        "codex": CODEX_MODEL,
        "gemini": GEMINI_MODEL,
        "claude": CLAUDE_SONNET_MODEL,
        "local": os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_MODEL", "auto"),
    }.get(name, "")

    result = ProbeResult(
        name=name, ok=ok, detail=detail, model=model,
        last_probed=now, cred_signature=sig, cached=False,
    )
    cache[name] = result.to_dict()
    with contextlib.suppress(OSError):
        _save_cache(cache)
    return result


def probe_chain(chain: list[str], *, force: bool = False) -> list[ProbeResult]:
    return [probe_provider(name, force=force) for name in chain]
