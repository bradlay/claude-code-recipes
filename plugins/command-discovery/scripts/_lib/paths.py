# Path helpers for plugin state and inventory-config discovery.
# CLAUDE_PLUGIN_DATA takes precedence over XDG; falls back to ~/.local/share.
# All directories created with 0o700.

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
    return _mkdir_secure(_xdg("XDG_DATA_HOME", ".local/share") / "claude-command-discovery")


def inventory_file() -> Path:
    """Resolve the command-inventory config file. Resolution order:

    1. ``$COMMAND_DISCOVERY_INVENTORY`` (explicit path override).
    2. ``$XDG_CONFIG_HOME/claude-command-discovery/inventory.yaml``.
    3. ``~/.config/claude-command-discovery/inventory.yaml`` (final fallback).

    A missing file is a normal, silent no-op state for this hook - it is
    never created automatically.
    """
    override = os.environ.get("COMMAND_DISCOVERY_INVENTORY")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / "claude-command-discovery" / "inventory.yaml"
