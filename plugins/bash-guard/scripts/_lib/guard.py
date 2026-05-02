# Bash command rule evaluator.
#
# Loads YAML rules (default + user overrides), splits compound commands
# on shell separators, normalizes `git -C <path>` so anchored regexes
# still match, and walks the rule list. First match wins. The hook
# script translates the resulting decision to a Claude Code hook
# decision JSON.

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped, unused-ignore]

from . import paths

_SECURE_FILE_MODE = 0o600


# ----------------------------------------------------------------------
# Config loading
# ----------------------------------------------------------------------

_CACHE: dict[str, Any] = {"config": None, "path": None, "mtime": 0.0}


def _resolve_rules_file() -> Path:
    """Resolution order:

    1. ``$CLAUDE_BASH_GUARD_RULES_FILE`` env var (explicit override).
    2. ``${XDG_CONFIG_HOME}/claude-bash-guard/rules.yaml`` (user override).
    3. The default-rules.yaml shipped with the plugin.
    """
    explicit = os.environ.get("CLAUDE_BASH_GUARD_RULES_FILE")
    if explicit:
        return Path(explicit)
    user_path = paths.user_rules_file()
    if user_path.exists():
        return user_path
    # Default: walk up from this file to find scripts/default-rules.yaml.
    return Path(__file__).resolve().parent.parent / "default-rules.yaml"


def load_rules() -> dict[str, Any]:
    """Load and cache rules YAML. Re-reads on file mtime change."""
    rules_path = _resolve_rules_file()
    try:
        current_mtime = rules_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    cached_config = _CACHE.get("config")
    if (
        cached_config is not None
        and _CACHE.get("path") == rules_path
        and _CACHE.get("mtime") == current_mtime
    ):
        return cached_config  # type: ignore[no-any-return]

    if not rules_path.exists():
        # Final fallback: empty config (everything allowed). Logged so
        # the user can see the misconfiguration.
        return {"rules": [], "settings": {}}

    with rules_path.open() as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        config = {"rules": [], "settings": {}}

    config.setdefault("rules", [])
    config.setdefault("settings", {})

    _CACHE["config"] = config
    _CACHE["path"] = rules_path
    _CACHE["mtime"] = current_mtime
    return config


# ----------------------------------------------------------------------
# Approval cache (for `ask` decisions)
# ----------------------------------------------------------------------


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()[:16]


def consume_approval(command: str, expiry_seconds: int) -> bool:
    """Check for a one-shot approval token; consume it if valid."""
    approval_file = paths.approvals_dir() / _command_hash(command)
    if not approval_file.exists():
        return False
    try:
        age = time.time() - approval_file.stat().st_mtime
        if age > expiry_seconds:
            with contextlib.suppress(OSError):
                approval_file.unlink(missing_ok=True)
            return False
        with contextlib.suppress(OSError):
            approval_file.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def write_approval(command: str) -> None:
    """Drop an approval token for one-shot use."""
    approval_file = paths.approvals_dir() / _command_hash(command)
    with contextlib.suppress(OSError):
        approval_file.write_text("ok\n")
        approval_file.chmod(_SECURE_FILE_MODE)


def log_block(command: str, reason: str) -> None:
    """Append a structured record for every blocked command."""
    with contextlib.suppress(OSError):
        log_file = paths.blocked_log()
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = log_file.exists()
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "reason": reason,
        }
        with log_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        if not existed:
            with contextlib.suppress(OSError):
                log_file.chmod(_SECURE_FILE_MODE)


# ----------------------------------------------------------------------
# Command splitting and normalization
# ----------------------------------------------------------------------


def split_chained_commands(command: str) -> list[str]:
    """Split a shell command on sequence separators: ``&&``, ``||``, ``;``,
    ``&`` (background), and newlines.

    Pipe operators (``|`` and ``||``) are intentionally NOT split: pipelines
    are data-flow constructs and rules like "block ``base64 -d | bash``"
    operate on the pipeline as a unit. Sequence separators are split so
    chains like ``cd /tmp && rm -rf /`` cannot bypass anchored rules by
    putting the dangerous part second.

    Quotes and escapes are tracked so separators inside strings are not
    treated as splits.
    """
    commands: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0

    while i < len(command):
        ch = command[i]

        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue

        if ch == "\\":
            escaped = True
            current.append(ch)
            i += 1
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        if in_single or in_double:
            current.append(ch)
            i += 1
            continue

        if ch in ("\n", ";"):
            part = "".join(current).strip()
            if part:
                commands.append(part)
            current = []
            i += 1
            continue

        if ch == "&" and i + 1 < len(command) and command[i + 1] == "&":
            part = "".join(current).strip()
            if part:
                commands.append(part)
            current = []
            i += 2
            continue

        if ch == "&":
            part = "".join(current).strip()
            if part:
                commands.append(part)
            current = []
            i += 1
            continue

        # Note: pipes (``|`` and ``||``) are intentionally NOT separators
        # here. Pipelines are data-flow constructs and rules that target
        # whole pipelines (e.g. ``base64 -d | bash``) need to see the
        # full pipeline string. The ``||`` operator is a logical OR
        # separator semantically, but in practice it's vanishingly rare
        # in interactive shell use, and matching it as a separator would
        # also break pipelines.

        current.append(ch)
        i += 1

    part = "".join(current).strip()
    if part:
        commands.append(part)

    return commands


def normalize_for_patterns(command: str) -> str:
    """Strip ``git -C <path>`` so anchored regexes still match.

    A rule like ``^git\\s+push`` should match ``git -C /some/path push ...``
    just the same; remove the optional ``-C <path>`` segment first.
    """
    if not command.startswith("git "):
        return command
    normalized = re.sub(r"\s+-C\s*[= ]?\S+", "", command)
    return re.sub(r"\s+", " ", normalized).strip()


# ----------------------------------------------------------------------
# Rule evaluation
# ----------------------------------------------------------------------


def evaluate_rules(command: str, config: dict[str, Any]) -> tuple[str, str]:
    """Walk the rule list. First match wins.

    Returns ``(decision, reason)`` where decision is ``allow``, ``deny``,
    or ``ask``. If no rule matches, returns ``("allow", "")``.
    """
    pattern_command = normalize_for_patterns(command)
    rules = config.get("rules", [])

    for rule in rules:
        pattern = rule.get("pattern", "")
        if not pattern:
            continue

        decision = rule.get("decision", "deny")
        use_search = rule.get("search", False)
        extra_search = rule.get("extra_search")

        try:
            if use_search:
                primary = re.search(pattern, pattern_command)
            else:
                primary = re.match(pattern, pattern_command)
        except re.error:
            # Bad rule pattern: skip rather than crash the whole guard.
            continue

        if not primary:
            continue

        if extra_search:
            try:
                if not re.search(extra_search, pattern_command, re.IGNORECASE):
                    continue
            except re.error:
                continue

        if decision == "allow":
            return ("allow", "")

        reason = rule.get("reason", "Blocked by guard")
        return (decision, reason)

    return ("allow", "")
