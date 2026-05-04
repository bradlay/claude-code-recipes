# Local OpenAI-compatible plan-review provider.
#
# Invoked as a subprocess by `chain.py` when the chain (or shadow list)
# contains "local". Generic OpenAI Chat Completions client — works against
# any backend exposing `/v1/models` and `/v1/chat/completions` (vLLM,
# Ollama, llama.cpp server, LM Studio, ...).
#
# Configuration (all env vars):
#   CLAUDE_PLAN_REVIEW_LOCAL_URL         OpenAI-compat base URL.
#                                        Default: http://localhost:8010
#   CLAUDE_PLAN_REVIEW_LOCAL_MODEL       Model id. If unset, the runner
#                                        GETs /v1/models and uses data[0].id.
#   CLAUDE_PLAN_REVIEW_LOCAL_TIMEOUT     Total HTTP read timeout, seconds
#                                        (default 600).
#   CLAUDE_PLAN_REVIEW_LOCAL_TEMPERATURE Sampling temperature (default 0.1).
#   CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS  Generation cap (default 4096).
#   CLAUDE_PLAN_REVIEW_LOCAL_PRIORITY    vLLM scheduling priority (lower =
#                                        higher). Default 20 — below live
#                                        translation (-10) and the coding
#                                        agent (10), so plan reviews don't
#                                        steal slots. Silently ignored by
#                                        backends that don't support it.
#
# Output contract: prints the model's response to stdout (with any
# <think>…</think> blocks stripped — qwen3 reasoning parser wraps thinking
# output there). Exits 0 on success, non-zero on any error so `chain.py`'s
# executor falls through to the next provider.
#
# stdlib-only by design — the plugin has no external dependencies.

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://localhost:8010"
DEFAULT_TIMEOUT = 600
DEFAULT_TEMPERATURE = 0.1
DEFAULT_MAX_TOKENS = 4096
DEFAULT_PRIORITY = 20
MODELS_PROBE_TIMEOUT = 5

_SYSTEM_PROMPT = """\
You are a plan reviewer. The user will show you an implementation plan.

Return your analysis as a single JSON object with this exact schema:
{"findings": [{"severity": "P0|P1|P2", "title": "...", "description": "...", "recommendation": "..."}], "questions": ["..."]}

Severity levels:
- P0 (Critical): data loss, security vulnerability, system outage
- P1 (High): incorrect behavior, significant technical debt
- P2 (Medium): design improvements, missing edge cases

Only flag genuine issues with concrete recommendations. If the plan looks good,
return {"findings": [], "questions": []}.

Output ONLY the JSON object. No markdown fences, no narrative, no prose.
"""

_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>")


class LocalProviderError(RuntimeError):
    """Raised when the local provider can't produce a response."""


def _strip_think_blocks(text: str) -> str:
    """Remove <think>…</think> blocks emitted by qwen3 reasoning models."""
    return _THINK_BLOCK_RE.sub("", text)


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name, "").strip()
    return val or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def base_url() -> str:
    """OpenAI-compat base URL, no trailing slash."""
    return _env_str("CLAUDE_PLAN_REVIEW_LOCAL_URL", DEFAULT_URL).rstrip("/")


def _http_get_json(url: str, *, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - http(s) only via env
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
    result: dict[str, Any] = json.loads(body)
    return result


def _http_post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - http(s) only via env
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
    result: dict[str, Any] = json.loads(body)
    return result


def resolve_model(url: str) -> str:
    """Return the model id to use. Honors CLAUDE_PLAN_REVIEW_LOCAL_MODEL,
    else queries /v1/models and uses the first entry."""
    explicit = _env_str("CLAUDE_PLAN_REVIEW_LOCAL_MODEL", "")
    if explicit:
        return explicit

    try:
        body = _http_get_json(f"{url}/v1/models", timeout=MODELS_PROBE_TIMEOUT)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise LocalProviderError(
            f"GET {url}/v1/models failed: {exc}. "
            f"Set CLAUDE_PLAN_REVIEW_LOCAL_MODEL to skip auto-discovery."
        ) from exc

    data = body.get("data") or []
    if not data:
        raise LocalProviderError(
            f"GET {url}/v1/models returned no models. "
            f"Set CLAUDE_PLAN_REVIEW_LOCAL_MODEL explicitly."
        )

    model_id = data[0].get("id")
    if not isinstance(model_id, str) or not model_id:
        raise LocalProviderError(f"GET {url}/v1/models returned malformed data[0]: {data[0]!r}")
    return model_id


def call_model(url: str, model_id: str, prompt: str) -> str:
    """POST to /v1/chat/completions and return the assistant content."""
    timeout = _env_float("CLAUDE_PLAN_REVIEW_LOCAL_TIMEOUT", DEFAULT_TIMEOUT)
    temperature = _env_float("CLAUDE_PLAN_REVIEW_LOCAL_TEMPERATURE", DEFAULT_TEMPERATURE)
    max_tokens = _env_int("CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    priority = _env_int("CLAUDE_PLAN_REVIEW_LOCAL_PRIORITY", DEFAULT_PRIORITY)

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # vLLM honors this when --scheduling-policy=priority is set.
        # Backends that don't recognize it ignore it.
        "priority": priority,
    }

    try:
        body = _http_post_json(
            f"{url}/v1/chat/completions",
            payload,
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        raise LocalProviderError(
            f"POST {url}/v1/chat/completions returned {exc.code}: {err_body}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise LocalProviderError(f"POST {url}/v1/chat/completions failed: {exc}") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LocalProviderError(f"unexpected response shape: {body!r}") from exc

    if not isinstance(content, str):
        raise LocalProviderError(f"non-string content in response: {content!r}")

    return content


def main(argv: list[str] | None = None) -> int:
    """Entry point. Prompt is the last argv element (matches the CLI-provider
    contract used by chain.py: `<cmd> <prompt>`). Falls back to stdin if no
    argv prompt was passed."""
    if argv is None:
        argv = sys.argv[1:]

    prompt = argv[-1] if argv else sys.stdin.read()
    if not prompt or not prompt.strip():
        print("local provider error: empty prompt", file=sys.stderr)
        return 2

    url = base_url()
    try:
        model_id = resolve_model(url)
        content = call_model(url, model_id, prompt)
    except LocalProviderError as exc:
        print(f"local provider error: {exc}", file=sys.stderr)
        return 1

    cleaned = _strip_think_blocks(content)
    sys.stdout.write(cleaned)
    if not cleaned.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
