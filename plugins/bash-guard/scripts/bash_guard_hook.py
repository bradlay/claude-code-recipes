#!/usr/bin/env python3
# PreToolUse(Bash) hook entry point for bash-guard.
#
# Reads the hook payload from stdin, splits the command on shell
# separators, evaluates each sub-command against the rule list, and
# emits a Claude Code hook decision:
#
#   allow  : no rule matched (or the matching rule was decision: allow)
#   deny   : a rule with decision: deny matched, OR an `ask` rule
#            matched and there is no live approval token
#   (Note: PreToolUse hooks emit allow or deny only. `ask` produces a
#   deny with a special ApprovalRequired marker so the user can approve
#   and re-run.)
#
# Failure posture: fail-CLOSED. Any error in the guard returns deny
# with a clear remediation message. Bypass via
# CLAUDE_BASH_GUARD_FAIL_OPEN=1 in the parent shell.

from __future__ import annotations

import contextlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lib import _io, paths  # noqa: E402
from _lib.guard import (  # noqa: E402
    consume_approval,
    evaluate_rules,
    load_rules,
    log_block,
    split_chained_commands,
    write_approval,  # noqa: F401  # exposed for future test seeding
)


def _write_health(status: str, *, reason: str = "", command: str = "") -> None:
    with contextlib.suppress(OSError):
        record = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "reason": reason,
            "command_preview": command[:200],
        }
        path = paths.health_file()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(record, indent=2) + "\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _on_error(inv: _io.HookInvocation, exc: Exception) -> int:
    """Fail-closed on any unhandled error in the evaluator."""
    with contextlib.suppress(OSError):
        log_path = paths.errors_log()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_path.open("a") as f:
            f.write(f"\n--- {datetime.now(timezone.utc).isoformat()} ---\n")
            f.write(f"Exception: {exc}\n")
            traceback.print_exc(file=f)
        with contextlib.suppress(OSError):
            log_path.chmod(0o600)
    _io.log(inv, f"guard error (fail-closed): {exc}", level="error")
    _write_health("error", reason=str(exc))
    return _io.fail_open_or_closed_pretooluse(
        inv,
        f"bash-guard internal error: {exc}",
    )


def main() -> int:
    inv = _io.parse_stdin(__file__)

    if inv.tool_name != "Bash":
        # Hook is registered with matcher: Bash so this should not
        # happen, but be defensive: allow non-Bash invocations through.
        return _io.emit_pretooluse_allow()

    command = inv.tool_input.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return _io.emit_pretooluse_allow()

    try:
        config = load_rules()
        sub_commands = split_chained_commands(command)

        decision = "allow"
        reason = ""
        offending_sub = ""

        for sub_cmd in sub_commands:
            if not sub_cmd:
                continue
            sub_decision, sub_reason = evaluate_rules(sub_cmd, config)
            if sub_decision != "allow":
                decision = sub_decision
                reason = sub_reason
                offending_sub = sub_cmd
                break

        if decision == "allow":
            return _io.emit_pretooluse_allow()

        if decision == "ask":
            expiry = int(config.get("settings", {}).get("approval_expiry_seconds", 60))
            if consume_approval(command, expiry):
                _io.log(inv, f"approval consumed for: {offending_sub[:80]}")
                return _io.emit_pretooluse_allow()
            # No approval: deny with the marker so the user can approve
            # and re-run.
            _write_health("ask", reason=reason, command=command)
            log_block(command, f"ask: {reason}")
            full = (
                f"{reason}\n\n"
                f"To proceed: re-run the same command after acknowledging the warning. "
                f"(One-shot approval valid for {expiry}s.)"
            )
            return _io.emit_pretooluse_deny(
                full,
                additional_context=(
                    f"Sub-command that triggered: {offending_sub[:200]}"
                ),
            )

        # decision == "deny"
        _write_health("deny", reason=reason, command=command)
        log_block(command, reason)
        return _io.emit_pretooluse_deny(
            reason,
            additional_context=(
                f"Sub-command that triggered: {offending_sub[:200]}\n\n"
                f"Edit ${{XDG_CONFIG_HOME}}/claude-bash-guard/rules.yaml to override "
                f"this rule, or set CLAUDE_BASH_GUARD_FAIL_OPEN=1 to bypass the entire guard."
            ),
        )

    except Exception as exc:
        return _on_error(inv, exc)


if __name__ == "__main__":
    sys.exit(main())
