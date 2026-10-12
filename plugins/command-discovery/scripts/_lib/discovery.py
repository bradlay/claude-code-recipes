# PostToolUse(Bash) discovery engine: catch a guessed/typo'd subcommand of
# a configured CLI vendor and suggest the closest known command.
#
# Design notes - why this is low-risk:
#
# * **Advisory only.** PostToolUse runs *after* the tool already executed.
#   This never blocks, never re-runs anything, never mutates state. The
#   worst case if it misfires is an ignorable line of suggestion text.
# * **Deterministic trigger.** It only looks at output for a small set of
#   well-known "no such command" idioms used by common CLI frameworks
#   (click, argparse-style, Cobra/urfave-style). A runtime failure (network,
#   auth, a permissions error) does not contain those idioms, so the hook
#   stays silent - no false positives on legitimate failures.
# * **Fail-silent.** Any exception, missing inventory, or no close match ->
#   emit nothing. An advisory hook must never be able to break a session.
# * **Bounded work.** Tool output is capped before it is ever handed to a
#   regex; the inventory is capped at load time (see ``inventory.py``); and
#   candidate ranking is capped to a small, fixed number of results.

from __future__ import annotations

import difflib
import re
from typing import Any

from . import inventory as inventory_mod

_TAG = "[command-discovery]"

# Caps that bound the work this hook can ever do, regardless of input size.
_MAX_OUTPUT_CHARS = 20_000
_MAX_COMMAND_CHARS = 2_000
_MAX_ATTEMPTED_WORDS = 10
_MAX_CANDIDATES = 5
_CLOSE_MATCH_CUTOFF = 0.5

# Common "no such command" idioms across CLI frameworks. Each pattern must
# capture exactly the offending token in group 1.
_FAILURE_PATTERNS = (
    re.compile(r"No such command '([^']+)'"),  # click
    re.compile(r"[Uu]nknown command:?\s+\"?'?([^\"'\s]+)\"?'?"),  # argparse/cobra-style
    re.compile(r"invalid choice:\s*'([^']+)'"),  # argparse
    re.compile(r"is not a\s+\S*\s*command\b.*?'([^']+)'", re.DOTALL),  # cobra/git-style
)


def _build_invocation_regex(vendor: str) -> re.Pattern[str]:
    """A ``<vendor> ...`` invocation, tolerating leading `FOO=bar` env
    assignments. ``vendor`` comes from the loaded inventory config, so it
    is escaped before being embedded in the pattern."""
    escaped = re.escape(vendor)
    return re.compile(rf"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*{escaped}\s+(\S.*)$")


def _collect_output(tool_response: object) -> str:
    """Flatten whatever Bash put in tool_response into one searchable
    string, bounded so a huge tool_response can never drive unbounded
    regex work."""
    if isinstance(tool_response, str):
        text = tool_response
    elif isinstance(tool_response, dict):
        text = "\n".join(str(v) for v in tool_response.values() if isinstance(v, str))
    else:
        text = ""
    return text[:_MAX_OUTPUT_CHARS]


def _attempted_path(vendor_args: str) -> str:
    """Extract the command-path words (everything before the first
    option/flag), bounded to a small number of words."""
    words: list[str] = []
    for tok in vendor_args.split():
        if tok.startswith("-"):
            break
        words.append(tok)
        if len(words) >= _MAX_ATTEMPTED_WORDS:
            break
    return " ".join(words)


def _detect_failure(output: str) -> str | None:
    """Return the offending token if `output` looks like a "no such
    command" failure from a common CLI framework, else None."""
    for pattern in _FAILURE_PATTERNS:
        m = pattern.search(output)
        if m:
            return m.group(1)
    return None


def _candidates(attempted_path: str, known_commands: tuple[str, ...]) -> list[str]:
    if not attempted_path:
        return []
    matches = difflib.get_close_matches(
        attempted_path, known_commands, n=_MAX_CANDIDATES, cutoff=_CLOSE_MATCH_CUTOFF
    )
    if matches:
        return matches
    # Fall back to matching on the first word only (handles the common
    # "typo'd top-level subcommand" case get_close_matches can miss when
    # the rest of the path differs a lot).
    first_word = attempted_path.split(" ", 1)[0]
    close_first_words = difflib.get_close_matches(
        first_word,
        sorted({c.split(" ", 1)[0] for c in known_commands}),
        n=_MAX_CANDIDATES,
        cutoff=_CLOSE_MATCH_CUTOFF,
    )
    if close_first_words:
        return [c for c in known_commands if c.split(" ", 1)[0] in close_first_words][
            :_MAX_CANDIDATES
        ]
    return []


def _suggestion_text(vendor: str, bad: str, attempted_path: str, candidates: list[str]) -> str:
    listed = "\n".join(f"  - {vendor} {c}" for c in candidates)
    return (
        f"{_TAG} `{vendor} {attempted_path}` is not a recognized command "
        f"(the CLI rejected '{bad}'). Closest known commands:\n"
        f"{listed}\n"
        f"Confirm exact syntax before retrying - do not guess again."
    )


def evaluate(
    payload: dict[str, Any], inventory: inventory_mod.Inventory | None = None
) -> str | None:
    """Return correction text to inject, or None to stay silent.

    Pure function - unit-testable without stdin/stdout plumbing. When
    `inventory` is None it is loaded from the configured inventory file;
    a missing or invalid inventory degrades to silence, never an error.
    """
    if payload.get("tool_name") not in {"Bash", "exec_command"}:
        return None

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if not isinstance(command, str) or not command:
        return None
    command = command[:_MAX_COMMAND_CHARS]

    if inventory is None:
        inventory = inventory_mod.load_inventory()
    if inventory is None or not inventory.commands:
        return None

    m = _build_invocation_regex(inventory.vendor).match(command)
    if not m:
        return None  # not an invocation of the configured vendor
    vendor_args = m.group(1)

    output = _collect_output(payload.get("tool_response"))
    bad = _detect_failure(output)
    if bad is None:
        return None  # success, or a failure that isn't a command-name miss

    attempted_path = _attempted_path(vendor_args)
    if not attempted_path:
        return None

    matches = _candidates(attempted_path, inventory.commands)
    if not matches:
        return None

    return _suggestion_text(inventory.vendor, bad, attempted_path, matches)
