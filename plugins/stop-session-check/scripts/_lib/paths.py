# Path helpers for plugin state and logs. CLAUDE_PLUGIN_DATA takes
# precedence over XDG; falls back to ~/.local/share. All directories
# created with 0o700; files chmod'd to 0o600.

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
    pd = os.environ.get("CLAUDE_PLUGIN_DATA")
    if pd:
        return _mkdir_secure(Path(pd))
    return _mkdir_secure(_xdg("XDG_DATA_HOME", ".local/share") / "claude-precompact")


def hooks_log_dir() -> Path:
    return _mkdir_secure(data_dir() / "hooks")


def hooks_raw_jsonl() -> Path:
    return hooks_log_dir() / "hooks-raw.jsonl"
