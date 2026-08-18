# Single source of truth for a "print mode" default-deny gate on a headless
# CLI invocation (e.g. `claude -p` / `--print`, or any other CLI with an
# unattended, non-interactive mode).
#
# Why this exists
# ----------------
# A headless/print invocation can be spawned from inside another headless
# invocation — a print call whose own output triggers a second print call,
# recursively. Left unchecked that is a fork bomb: unbounded child
# processes, exhausted memory/swap, and (for a paid CLI) potentially
# unbounded API usage. This module is default-deny with a two-part guard:
# an explicit opt-in AND a recursion sentinel, so a print invocation can
# never spawn another print invocation by accident.
#
# Policy (default-deny, recursion-safe)
# ---------------------------------------
# - A non-print invocation always passes through.
# - A print-mode invocation is ALLOWED only if ALL hold:
#     * the opt-in env var is set (explicit opt-in — an automation runner
#       sets it deliberately, or an operator sets it for one session);
#     * the recursion-sentinel env var is NOT already set (a print call
#       spawned by a print call is denied outright);
#     * if a hard-block date is configured, today is before it.
#   Otherwise it is BLOCKED.
#
# Pure stdlib only — no third-party or plugin-internal imports beyond this
# repo's own bash-guard `_lib` modules — so a rendered PATH shim can run
# the decider even when the rest of the plugin/venv is degraded. If it
# cannot run at all, the shim fails closed (see `render_shim_text`).

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from _lib.guard import split_chained_commands
from _lib.wrappers import strip_prefix_wrappers

# Shim exit-code contract. The shim acts ONLY on these; anything else
# (e.g. a missing/broken decider returning 127) is treated as block —
# fail-closed.
EXIT_ALLOW_NO_SENTINEL = 0  # allow, do not set the recursion sentinel
EXIT_ALLOW_SET_SENTINEL = 3  # allow a print call, set the sentinel env var
EXIT_BLOCK = 97


@dataclass(frozen=True)
class PrintPolicy:
    """Configuration for one gated CLI. Each field is an env var name
    except `program` (the bare executable name to match, e.g. ``"claude"``)
    and `hard_block_date` (an actual date, or None to disable the cliff)."""

    program: str
    opt_in_env: str
    recursion_env: str
    hard_block_date: date | None = None
    fallback_hint: str = ""


@dataclass(frozen=True)
class Decision:
    """Outcome of the gate for one invocation."""

    allow: bool
    is_print: bool
    reason: str

    def exit_code(self) -> int:
        if not self.allow:
            return EXIT_BLOCK
        return EXIT_ALLOW_SET_SENTINEL if self.is_print else EXIT_ALLOW_NO_SENTINEL


def is_print_mode(args: Sequence[str]) -> bool:
    """True if argv (the tokens after the program name) requests print
    mode.

    Detects ``-p`` / ``--print`` anywhere before a ``--`` terminator,
    including when not the first flag, and inside a combined short cluster
    (``-np``). Conservative on clusters — under default-deny, over-detecting
    print is the safe direction.
    """
    for tok in args:
        if tok == "--":
            break
        if tok in ("-p", "--print"):
            return True
        # Combined short-flag cluster, e.g. "-np". Long flags ("--foo") and
        # a lone "-" are excluded.
        if len(tok) >= 2 and tok[0] == "-" and tok[1] != "-" and "p" in tok[1:]:
            return True
    return False


def decide(
    policy: PrintPolicy,
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    today: date | None = None,
) -> Decision:
    """The whole policy for one gated CLI. ``args`` are the tokens after
    the program name."""
    env = os.environ if env is None else env
    today = date.today() if today is None else today

    if not is_print_mode(args):
        return Decision(allow=True, is_print=False, reason="non-print invocation")

    if policy.hard_block_date is not None and today >= policy.hard_block_date:
        return Decision(
            allow=False,
            is_print=True,
            reason=(
                f"FORBIDDEN: `{policy.program} -p` / `--print` is hard-blocked "
                f"from {policy.hard_block_date.isoformat()}. {policy.fallback_hint}"
            ),
        )

    if env.get(policy.recursion_env) == "1":
        return Decision(
            allow=False,
            is_print=True,
            reason=(
                f"FORBIDDEN: recursive `{policy.program} -p` (a print call spawned "
                f"inside another) — unbounded recursion risk. {policy.fallback_hint}"
            ),
        )

    if env.get(policy.opt_in_env) != "1":
        return Decision(
            allow=False,
            is_print=True,
            reason=(
                f"FORBIDDEN: `{policy.program} -p` / `--print` requires explicit "
                f"{policy.opt_in_env}=1 opt-in (default-deny). {policy.fallback_hint}"
            ),
        )

    cliff = (
        f", before {policy.hard_block_date.isoformat()}"
        if policy.hard_block_date is not None
        else ""
    )
    return Decision(
        allow=True,
        is_print=True,
        reason=f"{policy.program} -p allowed: opted in ({policy.opt_in_env}=1), non-recursive{cliff}",
    )


# ── Fail-closed backstop for a `sh -c '<inner>'` wrapper ───────────────────
#
# Regex/segment parsing of a `sh -c '<inner>'` string is fundamentally
# leaky — escaped quotes, or a trailing-argv form like
# `sh -c 'exec "$@"' _ claude -p` (the inner is a benign `exec "$@"`; the
# guarded program rides in on the positional params). Rather than chase a
# perfect shell lexer, when a shell `-c` wrapper is present AND a print
# invocation of the gated program appears ANYWHERE in the raw command,
# deny. False positives (e.g. a literal "claude -p" inside a sh -c string)
# are acceptable — that invocation would be denied anyway, so the caller
# just rephrases.
_SHELL_C_START = re.compile(r"^(?:sudo\s+)?(?:bash|zsh|dash|ash|ksh|sh)\s+-[a-zA-Z]*c\b")


def _print_invocation_anywhere(command: str, program: str) -> bool:
    pattern = re.compile(
        r"(?:^|[\s;&|`(\"'])" + re.escape(program) + r"\b[^\n;|&]*?(?:\s-[A-Za-z]*p\b|\s--print\b)"
    )
    return bool(pattern.search(command))


def looks_like_wrapped_print(command: str, program: str) -> bool:
    """Fail-closed signal for the print-mode gate.

    Fires when (a) a top-level segment *starts* with a shell ``-c``
    wrapper AND (b) a print invocation of ``program`` appears anywhere in
    the command. The shell-c-start test is quote-aware (segment split via
    `guard.split_chained_commands`) so a quoted mention of `sh -c ...
    claude -p` — e.g. in a commit message — doesn't false-positive; the
    print-invocation test runs on the WHOLE command so a quote trick that
    fools the splitter into separating `claude -p` (escaped quotes, or
    `sh -c 'exec "$@"' _ claude -p` trailing argv) still can't hide it.
    """
    # Test each segment both as-is and with prefix wrappers stripped, so a
    # `sh -c` hidden behind `env VAR=1`/`time`/`nice`/inline `VAR=val`
    # still counts as a shell-c start.
    starts_with_shell_c = any(
        _SHELL_C_START.match(seg) or _SHELL_C_START.match(strip_prefix_wrappers(seg))
        for seg in split_chained_commands(command)
    )
    return starts_with_shell_c and _print_invocation_anywhere(command, program)


# ── PreToolUse guard adapter ────────────────────────────────────────────────

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def guard_outcome(
    policy: PrintPolicy,
    command: str,
    env: Mapping[str, str] | None = None,
    today: date | None = None,
) -> tuple[str, str] | None:
    """PreToolUse-guard adapter: if ``command`` is an invocation of
    ``policy.program``, return ``(reason, decision)`` per the SAME
    ``decide()`` a rendered PATH shim would use; else ``None`` (let other
    guard rules handle it).

    Recognizes the invocation regardless of how the executable is spelled
    on the command line: a bare name, an absolute/relative path (matched
    by basename), and any number of leading shell-style ``VAR=value``
    environment assignments. Deliberately conservative: matches on the
    tokenized first program name only, never a bare substring.

    Also applies the fail-closed `looks_like_wrapped_print` backstop: a
    `sh -c` wrapper hiding a print invocation is denied even though the
    outer program name is `sh`, not ``policy.program``.
    """
    if looks_like_wrapped_print(command, policy.program):
        return (
            f"FORBIDDEN: `{policy.program} -p` / `--print` wrapped in a shell -c "
            f"invocation is denied (fail-closed backstop). {policy.fallback_hint}",
            "deny",
        )

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens) or Path(tokens[idx]).name != policy.program:
        return None

    argv = tokens[idx + 1 :]
    d = decide(policy, argv, env, today)
    if not d.allow:
        return d.reason, "deny"
    return None


# ── PATH shim rendering ─────────────────────────────────────────────────────
# The shim is GENERATED from this module (its single source). It parses NO
# argv: it runs this module as a standalone decider and branches only on
# the exit-code contract above.

_SHIM_TEMPLATE = r"""#!/bin/sh
# GENERATED shim for the print-mode gate. DO NOT EDIT.
#
# Single source of truth: print_policy.py (decide()). This shim parses NO
# argv. It asks the decider and acts ONLY on the exit code:
#   0  = allow, do not set sentinel
#   3  = allow print-mode, export the recursion sentinel first
#   97 = block
#   anything else (decider missing / python broken) = block (FAIL-CLOSED)
PYTHON=__PYTHON__
POLICY=__POLICY__
PROGRAM=__PROGRAM__
GUARD_DIR=__GUARD_DIR__
SENTINEL_ENV=__SENTINEL_ENV__

"$PYTHON" "$POLICY" "$PROGRAM" "$@"
rc=$?

# Resolve the REAL program: first executable on PATH NOT in this guard
# dir. Never re-enters this shim.
real=""
saved_ifs="$IFS"
IFS=:
for d in $PATH; do
  [ "$d" = "$GUARD_DIR" ] && continue
  if [ -x "$d/$PROGRAM" ]; then real="$d/$PROGRAM"; break; fi
done
IFS="$saved_ifs"
[ -z "$real" ] && real="$HOME/.local/bin/$PROGRAM"

if [ "$rc" = "0" ]; then
  exec "$real" "$@"
elif [ "$rc" = "3" ]; then
  eval "$SENTINEL_ENV=1"
  export "$SENTINEL_ENV"
  exec "$real" "$@"
fi

if [ "$rc" != "97" ]; then
  echo "BLOCKED: $PROGRAM -p gate could not evaluate (rc=$rc) - failing closed." >&2
  echo "  Policy: $POLICY  Python: $PYTHON" >&2
fi
exit 97
"""


def render_shim_text(
    policy: PrintPolicy, guard_dir: str, python_exe: str | None = None
) -> str:
    """Materialize the shim with absolute python + this-module paths baked
    in. Pure stdlib; safe to call without importing the heavier guard
    machinery."""
    policy_path = Path(__file__).resolve()
    python_path = Path(python_exe or sys.executable).resolve()
    return (
        _SHIM_TEMPLATE.replace("__PYTHON__", shlex.quote(str(python_path)))
        .replace("__POLICY__", shlex.quote(str(policy_path)))
        .replace("__PROGRAM__", shlex.quote(policy.program))
        .replace("__GUARD_DIR__", shlex.quote(guard_dir))
        .replace("__SENTINEL_ENV__", policy.recursion_env)
    )


def main(args: Sequence[str]) -> int:
    """Standalone decider entrypoint used by the shim.

    ``args`` is ``[program_name, *program_argv]`` (``sys.argv[1:]`` when
    run as a script via the shim, which passes the program name as
    ``$PROGRAM`` first).
    """
    if not args:
        print("print_policy: missing program name argument", file=sys.stderr)
        return EXIT_BLOCK
    program, argv = args[0], list(args[1:])
    policy = PrintPolicy(
        program=program,
        opt_in_env=f"{program.upper()}_PRINT_OK",
        recursion_env=f"{program.upper()}_PRINT_ACTIVE",
    )
    d = decide(policy, argv)
    if not d.allow or d.is_print:
        print(d.reason, file=sys.stderr)
    return d.exit_code()


if __name__ == "__main__":  # pragma: no cover - exercised via the shim subprocess
    raise SystemExit(main(sys.argv[1:]))
