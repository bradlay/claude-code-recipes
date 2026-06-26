# Backend registry: the single source of truth mapping a user-facing
# backend key to a concrete (CLI binary, argv, model). The chain, the
# probes, the preflight, and the interactive picker all read this table so
# a model id or CLI invocation shape is defined in exactly one place.
#
# Every model default is overridable by env; never hardcode a model id
# elsewhere. The defaults are treated as values to re-verify against the
# actual CLIs in non-interactive stdin mode, not load-bearing constants.
#
# Keys are the chain currency and the picker choices:
#   opus    -> claude --print --model claude-opus-4-8   (self-review)
#   sonnet  -> claude --print --model claude-sonnet-4-6 (self-review)
#   codex   -> codex exec (gpt-5.5, xhigh)
#   gemini  -> agy -p --model "Gemini 3.1 Pro (High)"   (gateway; replaces
#              the dead gemini CLI — note the binary is `agy`, so a legacy
#              CLAUDE_PLAN_REVIEW_CHAIN=gemini transparently routes here)
#   local   -> OpenAI-compat backend via local_provider.py (autoswe only)

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Absolute path so the subprocess (fresh sys.path) can find it.
_LOCAL_PROVIDER = str(Path(__file__).resolve().parent / "local_provider.py")

# Legacy chain aliases -> canonical backend keys. Keeps existing
# CLAUDE_PLAN_REVIEW_CHAIN values working after the gemini->agy migration
# and the claude->opus/sonnet split.
LEGACY_ALIASES: dict[str, str] = {
    "claude": "sonnet",  # old single claude leg defaulted to sonnet-4-6
    "agy": "gemini",  # the binary name, in case someone spells it that way
}


@dataclass(frozen=True)
class Backend:
    """One reviewable backend. `binary` is what must be on PATH (or
    sys.executable for the local HTTP provider). `online` marks backends
    offered in the interactive ExitPlanMode picker (local is autoswe-only)."""

    key: str
    label: str
    binary: str
    online: bool
    model_env: str
    model_default: str
    _run: Callable[[str], list[str]]
    _probe: Callable[[str], list[str]]

    def model(self) -> str:
        return os.environ.get(self.model_env, "").strip() or self.model_default

    def run_argv(self) -> list[str]:
        """Full argv for a review run. Prompt is fed on stdin by the caller."""
        return self._run(self.model())

    def probe_argv(self) -> list[str]:
        """Lighter argv for the auth/model-access probe (stdin-fed)."""
        return self._probe(self.model())


def _claude_run(model: str) -> list[str]:
    return ["claude", "--print", "--model", model]


def _codex_run(model: str) -> list[str]:
    return [
        "codex",
        "exec",
        "-c",
        f'model="{model}"',
        "-c",
        'model_reasoning_effort="xhigh"',
        "--skip-git-repo-check",
    ]


def _codex_probe(model: str) -> list[str]:
    # Cheap reasoning effort for the probe; the run uses xhigh.
    return [
        "codex",
        "exec",
        "-c",
        f'model="{model}"',
        "-c",
        'model_reasoning_effort="low"',
        "--skip-git-repo-check",
    ]


def _agy_run(model: str) -> list[str]:
    # agy reads the prompt from stdin when -p is empty (verified).
    return ["agy", "-p", "", "--model", model]


def _local_run(_model: str) -> list[str]:
    return [sys.executable, _LOCAL_PROVIDER]


REGISTRY: dict[str, Backend] = {
    "opus": Backend(
        key="opus",
        label="Opus 4.8 (self-review)",
        binary="claude",
        online=True,
        model_env="CLAUDE_PLAN_REVIEW_OPUS_MODEL",
        model_default="claude-opus-4-8",
        _run=_claude_run,
        _probe=_claude_run,
    ),
    "sonnet": Backend(
        key="sonnet",
        label="Sonnet 4.6 (self-review)",
        binary="claude",
        online=True,
        model_env="CLAUDE_PLAN_REVIEW_SONNET_MODEL",
        model_default="claude-sonnet-4-6",
        _run=_claude_run,
        _probe=_claude_run,
    ),
    "codex": Backend(
        key="codex",
        label="codex (gpt-5.5)",
        binary="codex",
        online=True,
        model_env="CLAUDE_PLAN_REVIEW_CODEX_MODEL",
        model_default="gpt-5.5",
        _run=_codex_run,
        _probe=_codex_probe,
    ),
    "gemini": Backend(
        key="gemini",
        label="Gemini 3.1 Pro (via agy)",
        binary="agy",
        online=True,
        model_env="CLAUDE_PLAN_REVIEW_AGY_MODEL",
        model_default="Gemini 3.1 Pro (High)",
        _run=_agy_run,
        _probe=_agy_run,
    ),
    "local": Backend(
        key="local",
        label="local qwen (autoswe)",
        binary=sys.executable,
        online=False,
        model_env="CLAUDE_PLAN_REVIEW_LOCAL_MODEL",
        model_default="",
        _run=_local_run,
        _probe=_local_run,
    ),
}

# Self-review backends get the adversarial focused preamble (see runner).
SELF_REVIEW_KEYS: frozenset[str] = frozenset({"opus", "sonnet"})

# Stable order for the picker.
ONLINE_KEYS: list[str] = [k for k, b in REGISTRY.items() if b.online]


def normalize_key(raw: str) -> str | None:
    """Map a raw chain token (possibly a legacy alias) to a canonical
    backend key, or None if it is not a known backend."""
    token = raw.strip()
    if token in REGISTRY:
        return token
    return LEGACY_ALIASES.get(token)


def is_known(raw: str) -> bool:
    return normalize_key(raw) is not None
