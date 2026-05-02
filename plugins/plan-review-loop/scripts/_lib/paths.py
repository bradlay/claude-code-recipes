# Path helpers for plugin state, logs, and Claude Code config discovery.
# CLAUDE_PLUGIN_DATA takes precedence over XDG; falls back to ~/.local/share.
# All directories created with 0o700; callers chmod files to 0o600.

from __future__ import annotations

import contextlib
import os
from pathlib import Path

_SECURE_DIR_MODE = 0o700


def _xdg(env_var: str, default_relative: str) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        return Path(raw)
    return Path.home() / default_relative


def _mkdir_secure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=_SECURE_DIR_MODE)
    with contextlib.suppress(OSError):
        directory.chmod(_SECURE_DIR_MODE)
    return directory


def data_dir() -> Path:
    """Plugin data directory. Resolution order:

    1. ``$CLAUDE_PLUGIN_DATA`` (set by Claude Code when invoking plugin hooks).
    2. ``$XDG_DATA_HOME/claude-plan-review/`` (ad-hoc CLI invocation).
    3. ``~/.local/share/claude-plan-review/`` (final fallback).
    """
    pd = os.environ.get("CLAUDE_PLUGIN_DATA")
    if pd:
        return _mkdir_secure(Path(pd))
    return _mkdir_secure(_xdg("XDG_DATA_HOME", ".local/share") / "claude-plan-review")


def state_dir() -> Path:
    sd = os.environ.get("CLAUDE_PLUGIN_DATA")
    if sd:
        return _mkdir_secure(Path(sd) / "state")
    return _mkdir_secure(_xdg("XDG_STATE_HOME", ".local/state") / "claude-plan-review")


def review_state_dir() -> Path:
    return _mkdir_secure(data_dir() / "review-state")


def review_log_dir() -> Path:
    return _mkdir_secure(data_dir() / "review-log")


def hooks_log_dir() -> Path:
    return _mkdir_secure(data_dir() / "hooks")


def hooks_raw_jsonl() -> Path:
    return hooks_log_dir() / "hooks-raw.jsonl"


def health_file() -> Path:
    return data_dir() / "health.json"


def preflight_cache_file() -> Path:
    return data_dir() / "preflight.json"


def claude_config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".claude"


def claude_plans_dir() -> Path:
    return claude_config_dir() / "plans"
