# Loads the user-supplied command inventory that tells the discovery
# engine which CLI vendor to watch and which subcommand paths are valid.
#
# Config shape (YAML or JSON, ``inventory.yaml`` by default):
#
#   vendor: mycli
#   commands:
#     - status
#     - deploy prod
#     - deploy staging
#     - config set
#
# ``vendor`` is the program name as typed on the command line (the first
# word of the invocation). ``commands`` is a flat list of known subcommand
# paths, each a space-separated sequence of words exactly as a user would
# type them after the vendor name.
#
# Bounded by design: a hostile or corrupted config can only ever produce a
# capped in-memory list, never unbounded work downstream.

from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path

from . import paths

MAX_COMMANDS = 5000
MAX_COMMAND_LEN = 200


@dataclasses.dataclass(frozen=True)
class Inventory:
    vendor: str
    commands: tuple[str, ...]


def _parse_raw(text: str, suffix: str) -> object | None:
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: PLC0415 (optional dependency)
        except ImportError:
            return None
        with contextlib.suppress(Exception):
            loaded: object = yaml.safe_load(text)
            return loaded
        return None
    # Default / unknown extension: try JSON first, then YAML if available.
    with contextlib.suppress(json.JSONDecodeError):
        decoded: object = json.loads(text)
        return decoded
    with contextlib.suppress(ImportError):
        import yaml  # noqa: PLC0415 (optional dependency)

        with contextlib.suppress(Exception):
            fallback: object = yaml.safe_load(text)
            return fallback
    return None


def load_inventory(path: Path | None = None) -> Inventory | None:
    """Load and validate the inventory config. Returns ``None`` on any
    missing file, unreadable file, or malformed content - the caller
    treats that as "stay silent", never as an error to surface."""
    target = path if path is not None else paths.inventory_file()

    try:
        if not target.is_file():
            return None
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    raw = _parse_raw(text, target.suffix.lower())
    if not isinstance(raw, dict):
        return None

    vendor = raw.get("vendor")
    if not isinstance(vendor, str) or not vendor.strip():
        return None

    raw_commands = raw.get("commands")
    if not isinstance(raw_commands, list):
        return None

    commands: list[str] = []
    for item in raw_commands[:MAX_COMMANDS]:
        if not isinstance(item, str):
            continue
        cleaned = " ".join(item.split())[:MAX_COMMAND_LEN]
        if cleaned:
            commands.append(cleaned)

    if not commands:
        return None

    return Inventory(vendor=vendor.strip(), commands=tuple(commands))
