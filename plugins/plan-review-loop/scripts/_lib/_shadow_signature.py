"""Shadow runtime config signature.

Single source of truth for the env-var hash used by both the writer
(persisted on every shadow cycle log) and the reader (preflight scope
filter). When any of the listed env vars change, the signature rotates;
records under prior signatures naturally drop out of current-scope
health calculations.

Operator escape hatch when stale failures need to be cleared after a
config fix: tweak any one of these vars (typically MAX_TOKENS), restart
Claude Code, scope rotates.
"""

from __future__ import annotations

import hashlib
import os

# Ordered list — the signature only changes when one of these changes,
# regardless of dict iteration order across Python versions.
_SHADOW_CONFIG_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_PLAN_REVIEW_SHADOW",
    "CLAUDE_PLAN_REVIEW_LOCAL_URL",
    "CLAUDE_PLAN_REVIEW_LOCAL_MODEL",
    "CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS",
    "CLAUDE_PLAN_REVIEW_LOCAL_TEMPERATURE",
    "CLAUDE_PLAN_REVIEW_LOCAL_TIMEOUT",
)


def current_shadow_config_signature() -> str:
    """16-char hex sha256 of the joined env values."""
    parts = [f"{var}={os.environ.get(var, '')}" for var in _SHADOW_CONFIG_ENV_VARS]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]
