# Per-backend auth + model-access probes with TTL cache.
#
# Each probe verifies (a) the CLI is callable and (b) the backend's required
# model returns a sane response — not a silently downgraded model on a free
# tier. Results are cached at $DATA_DIR/probe-cache.json with a TTL and
# invalidated when relevant credential files / model selections change.
#
# Why probe at all: the picker promises that a backend is verified working
# before it is offered, and the chain hardcodes high-tier models. A free or
# downgraded account silently substitutes a weaker model and tanks review
# quality. The probe asserts the actual model returns a recognizable
# response; an account that can't access it returns an error.
#
# Backend keys (opus, sonnet, codex, gemini [agy], local) come from
# _lib/backends.py so model ids and argv live in exactly one place.

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

from . import backends, paths

PROBE_TIMEOUT_SECONDS = 60
PROBE_CACHE_TTL_SECONDS = 24 * 3600
# When building the interactive picker we want positives that can't be more
# than a few minutes stale, so an expired-in-window auth doesn't get offered.
PICKER_PROBE_TTL_SECONDS = 300
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_EXPECTED_REPLY = "ok"
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

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.last_probed)


def _cache_file() -> Path:
    return paths.data_dir() / "probe-cache.json"


def _load_cache() -> dict[str, dict[str, Any]]:
    path = _cache_file()
    if not path.exists():
        return {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        result: dict[str, dict[str, Any]] = json.loads(path.read_text())
        return result
    return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    with contextlib.suppress(OSError):
        path.chmod(_SECURE_FILE_MODE)


def _model_for(name: str) -> str:
    backend = backends.REGISTRY.get(name)
    if backend is None:
        return ""
    if name == "local":
        return os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_MODEL", "") or "auto"
    return backend.model()


def _cred_signature(name: str) -> str:
    """Hash the inputs that, if changed, should invalidate a cached probe:
    the resolved model name plus any credential files / env for that backend.
    Deterministic per-machine, never logged."""
    parts: list[str] = [name, _model_for(name)]
    if name == "codex":
        for env_key in ("OPENAI_API_KEY",):
            parts.append(env_key + "=" + os.environ.get(env_key, ""))
        for path_str in (Path.home() / ".codex" / "auth.json",):
            with contextlib.suppress(OSError):
                parts.append(f"{path_str}:{path_str.stat().st_mtime}")
    elif name == "gemini":
        # agy gateway auth; invalidate on its config changing if present.
        for path_str in (Path.home() / ".config" / "agy" / "config.json",):
            with contextlib.suppress(OSError):
                parts.append(f"{path_str}:{path_str.stat().st_mtime}")
    elif name == "local":
        parts.append(os.environ.get("CLAUDE_PLAN_REVIEW_LOCAL_URL", ""))
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def _run_cli_probe(argv: list[str]) -> tuple[bool, str]:
    """Pipe PROBE_PROMPT through stdin; assert the response contains the token."""
    # Mark the child so a probed `claude --print` session's SessionStart
    # preflight no-ops instead of probing again (infinite recursion).
    probe_env = {**os.environ, "CLAUDE_PLAN_REVIEW_NESTED": "1"}
    try:
        result = subprocess.run(
            argv,
            input=PROBE_PROMPT,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            env=probe_env,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {PROBE_TIMEOUT_SECONDS}s"
    except OSError as e:
        return False, f"OSError: {e}"
    if result.returncode != 0:
        stderr_preview = (result.stderr or "").strip()[:300]
        return False, f"rc={result.returncode}; stderr: {stderr_preview!r}"
    body = (result.stdout or "").strip().lower()
    if PROBE_EXPECTED_REPLY not in body:
        return False, f"unexpected response: {body[:200]!r}"
    return True, body[:200]


def _probe_cli_backend(name: str) -> tuple[bool, str]:
    backend = backends.REGISTRY[name]
    if shutil.which(backend.binary) is None:
        return False, f"{backend.binary} not on PATH"
    return _run_cli_probe(backend.probe_argv())


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


def _run_probe(name: str) -> tuple[bool, str]:
    if name == "local":
        return _probe_local()
    return _probe_cli_backend(name)


def probe_provider(
    name: str,
    *,
    force: bool = False,
    max_age: float = PROBE_CACHE_TTL_SECONDS,
) -> ProbeResult:
    """Run the probe for backend `name`, honoring the TTL cache unless
    force=True. `max_age` caps how old a cached entry may be before it is
    re-probed (the picker passes a short value so positives stay fresh)."""
    if name not in backends.REGISTRY:
        return ProbeResult(name=name, ok=False, detail="unknown backend")

    cache = _load_cache()
    sig = _cred_signature(name)
    now = time.time()

    if not force:
        entry = cache.get(name)
        if (
            entry
            and entry.get("cred_signature") == sig
            and now - float(entry.get("last_probed", 0)) < max_age
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

    ok, detail = _run_probe(name)
    result = ProbeResult(
        name=name,
        ok=ok,
        detail=detail,
        model=_model_for(name),
        last_probed=now,
        cred_signature=sig,
        cached=False,
    )
    cache[name] = result.to_dict()
    with contextlib.suppress(OSError):
        _save_cache(cache)
    return result


def probe_chain(chain: list[str], *, force: bool = False) -> list[ProbeResult]:
    return [probe_provider(name, force=force) for name in chain]


def available_backends(
    *,
    max_age: float = PICKER_PROBE_TTL_SECONDS,
    force: bool = False,
) -> list[ProbeResult]:
    """The online backends whose probe currently passes — the 'tested before
    surfacing' gate for the interactive picker. Positives older than
    `max_age` are re-probed so an expired-in-window auth is not offered."""
    results: list[ProbeResult] = []
    for key in backends.ONLINE_KEYS:
        result = probe_provider(key, force=force, max_age=max_age)
        if result.ok:
            results.append(result)
    return results
