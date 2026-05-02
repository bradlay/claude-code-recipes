# Path helpers for plugin state, logs, and host-CLI config discovery.
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
    2. ``$XDG_DATA_HOME/claude-bash-guard/`` (ad-hoc CLI invocation).
    3. ``~/.local/share/claude-bash-guard/`` (final fallback).
    """
    pd = os.environ.get("CLAUDE_PLUGIN_DATA")
    if pd:
        return _mkdir_secure(Path(pd))
    return _mkdir_secure(_xdg("XDG_DATA_HOME", ".local/share") / "claude-bash-guard")


def hooks_log_dir() -> Path:
    return _mkdir_secure(data_dir() / "hooks")


def hooks_raw_jsonl() -> Path:
    return hooks_log_dir() / "hooks-raw.jsonl"


def health_file() -> Path:
    return data_dir() / "health.json"


def approvals_dir() -> Path:
    return _mkdir_secure(data_dir() / "approvals")


def blocked_log() -> Path:
    return data_dir() / "blocked.log"


def errors_log() -> Path:
    return data_dir() / "errors.log"


def user_rules_file() -> Path:
    """User override rules at ${XDG_CONFIG_HOME}/claude-bash-guard/rules.yaml."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "claude-bash-guard" / "rules.yaml"


def claude_config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".claude"
