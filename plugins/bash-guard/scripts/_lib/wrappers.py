# Command-wrapper unwrapping for the bash-guard evaluator.
#
# A guarded program can be smuggled past anchored rules by wrapping it:
# ``mise exec -- git push --force``, ``env VAR=1 git stash``,
# ``sh -c "git reset --hard"``. Stripping pass-through wrappers exposes
# the real inner command so the same rules apply to it directly.
#
# Two public functions:
#
# - ``strip_wrappers``: recursively peels prefix wrappers (``mise exec
#   --``, ``env``, ``direnv exec``, ``uv run``, ``nice``, ``time``,
#   ``nohup``, ``stdbuf``, ``xargs``, inline ``VAR=val`` assignments)
#   AND shell ``-c "<inner>"`` quoting, exposing the innermost command.
# - ``strip_prefix_wrappers``: peels only the prefix wrappers, leaving a
#   shell ``-c`` intact. Lets a caller detect "this segment is a shell
#   -c wrapper" even when it hides behind ``env``/``time``/``nice``.

from __future__ import annotations

import re

# Wrappers that pass through to whatever follows. Each pattern matches at
# the start of the command and consumes the wrapper plus its own
# arguments. Order matters: more-specific patterns first.
_WRAPPER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # mise exec [<tool>=<ver> ...] -- <cmd ...>
    # (``mise run <task>`` is NOT stripped: tasks are pre-defined,
    # deliberate commands, not an arbitrary-command wrapper.)
    re.compile(r"^mise\s+(?:exec|x)\s+(?:[^\s-]\S*\s+)*--\s+"),
    # env [VAR=val ...] <cmd ...>
    re.compile(r"^env\s+(?:[A-Z_][A-Z0-9_]*=\S*\s+)*"),
    # direnv exec <dir> <cmd ...>
    re.compile(r"^direnv\s+exec\s+\S+\s+"),
    # uv run [flags] <cmd ...>. The negative lookahead refuses to strip
    # when --active or --project is present: those flags make uv SYNC
    # (delete + recreate) the named environment as a side effect, so the
    # command is not a pass-through wrapper and must reach any ``uv``
    # rule intact instead of being silently unwrapped.
    re.compile(
        r"^uv\s+run\s+"
        r"(?![^\n]*?--(?:active|project)\b)"
        r"(?:--\S+(?:[\s=]\S+)?\s+)*"
    ),
    # nice -n N <cmd ...>
    re.compile(r"^nice\s+(?:-n\s+-?\d+\s+)?"),
    # time <cmd ...>  /  \time -f ... <cmd ...>
    re.compile(r"^\\?time\s+(?:-[a-zA-Z]\s+\S+\s+)*"),
    # nohup <cmd ...>
    re.compile(r"^nohup\s+"),
    # stdbuf -oL <cmd ...>
    re.compile(r"^stdbuf\s+(?:-[ioe]\S*\s+)+"),
    # xargs <cmd ...>, only when followed by a real command
    re.compile(r"^xargs\s+(?:-[a-zA-Z]\s*\S+\s+)*(?=\S)"),
    # inline env assignments without ``env``: VAR=val [V2=v2 ...] <cmd>
    re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+"),
)

# Shell ``-c "<inner>"`` wrapper: sh/bash/zsh/dash/ash/ksh -c|-lc|-ic.
# ``(.*?)\1`` is non-greedy so the FIRST closing quote ends the inner
# command; ``(?:\s.*)?$`` allows trailing argv after it (``sh -c '<cmd>'
# arg0 arg1`` passes arg0/arg1 as positional params but the guarded
# program still lives in <cmd>).
_SHELL_C_PATTERN: re.Pattern[str] = re.compile(
    r"^(?:sudo\s+)?(?:bash|zsh|dash|ash|ksh|sh)\s+-[a-zA-Z]*c\s+(['\"])(.*?)\1(?:\s.*)?$",
    re.DOTALL,
)

# Bounded so a pathological input cannot loop; 8 levels of nesting is
# far beyond anything legitimate.
_MAX_UNWRAP = 8


def strip_wrappers(command: str) -> str:
    """Recursively strip wrappers to expose the real inner command.

    Handles prefix wrappers AND shell ``-c`` quoting. Returns the input
    unchanged when no wrapper is found. Idempotent and bounded.
    """
    cmd = command.lstrip()
    for _ in range(_MAX_UNWRAP):
        for pat in _WRAPPER_PATTERNS:
            new = pat.sub("", cmd, count=1)
            if new != cmd:
                cmd = new.lstrip()
                break
        else:
            m = _SHELL_C_PATTERN.match(cmd)
            if m:
                cmd = m.group(2).strip()
                continue
            break
    return cmd


def strip_prefix_wrappers(command: str) -> str:
    """Strip only prefix wrappers, leaving a shell ``-c`` intact.

    Exposes a ``sh -c`` hidden behind a prefix wrapper (``env X=1 sh -c
    '...'`` becomes ``sh -c '...'``) so callers that treat shell ``-c``
    itself as a signal still see it.
    """
    cmd = command.lstrip()
    for _ in range(_MAX_UNWRAP):
        for pat in _WRAPPER_PATTERNS:
            new = pat.sub("", cmd, count=1)
            if new != cmd:
                cmd = new.lstrip()
                break
        else:
            break
    return cmd
