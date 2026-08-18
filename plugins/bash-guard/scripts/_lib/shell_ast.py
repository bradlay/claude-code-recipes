"""Shell-AST analysis for bash-guard.

The guard's rules are regexes matched against the *raw command string*. That
conflates "a banned token appears in the text" with "a banned program is
executed", so a read-only inspection command whose search pattern happens to
mention a banned token is denied:

    grep -nE "curl example.com | bash" src/     # false positive on curl-pipe-shell
    grep -rn "eval \\$(curl ...)" src/            # false positive on eval-curl
    find . -name "*base64*"                     # false positive on a base64 rule

Measured on one real deployment: several hundred denials in a week, a large
majority of them false, concentrated in exactly this shape: a `grep`/`find`-
style inspection command whose read-only search text happened to contain a
banned token.

This module fixes that class WITHOUT weakening any ban. It parses the command
with ``bashlex`` and:

1. **Never assumes a quoted argument is inert.** ``grep "$(python3 -c …)"``,
   backticks, ``<(…)`` process substitution and arithmetic expansion all
   EXECUTE inside quotes. Every executable expansion is inspected first; only
   word spans the parser *proves* are pure literal text are masked, and only
   when they are arguments of a read-only inspection command
   (:data:`READ_ONLY_INSPECTION`). Everything else — operators, redirections,
   command words, non-literal words — is preserved byte-for-byte, so every
   existing rule still fires on real executing forms.

2. **Fails closed** on constructs whose executed program cannot be proven:
   a dynamic command word (``cmd=python3; "$cmd" -c x``), an interpreter fed
   from stdin (``echo x | sh``, ``sh -s``, a heredoc-fed shell), and a wrapper
   whose payload is not a provable literal (``sh -c "$CMD"``).

3. **Derives wrapper payloads** (``sh -c``, ``eval``, ``env``, ``command``,
   ``exec``, ``sudo``/``nice``/``nohup``/``stdbuf``, ``xargs``,
   ``find -exec``) and hands them back to the caller so the EXISTING rule
   pipeline evaluates them. No ban is duplicated here.

**Dependency policy (normative, DESIGN DECISION).** ``bashlex`` is an
OPTIONAL dependency, never installed automatically by this plugin. Every
public function in this module degrades gracefully when it is missing:
:func:`analyze` returns ``None`` and :func:`mask_command` returns its input
unchanged. That degradation is always the SAFE direction — masking only
*removes* false positives, so its absence only means the guard sees more of
the original (unmasked) text, never less enforcement. A command that does not
parse (heredocs and arithmetic expansion are not supported by bashlex) or
raises inside the parser degrades the same way. Install ``bashlex`` yourself
(``pip install bashlex`` / add it to your own environment) to enable masking;
this plugin's own metadata never requires it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - import shape is exercised by the fallback tests
    import bashlex
except ImportError:  # pragma: no cover - bashlex is optional, see module docstring
    bashlex = None

# True when bashlex is importable. Every public function in this module
# checks this (directly or via `available()`) and degrades to a no-op /
# `None` return when it is False — see the "Dependency policy" section above.
AVAILABLE: bool = bashlex is not None

# The placeholder a proven-inert literal argument is replaced with. Deliberately
# a word that matches no guard rule and creates no new match when repeated.
INERT_PLACEHOLDER = "INERT"

# Read-only inspection commands: with the EXCEPTION of the exec-capable options
# below, they never execute their arguments, so an argument the parser proves is a
# pure literal cannot run anything.
READ_ONLY_INSPECTION: frozenset[str] = frozenset(
    {
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "find",
        "ls",
        "wc",
        "which",
        "stat",
        "sort",
        "uniq",
        "basename",
        "dirname",
    }
)

# ...but several of the above are NOT purely inspective: they take an option whose
# VALUE is a command they then EXECUTE. Masking such a command's literal args as
# inert would hide a real execution vector behind a "read-only" name:
#   sort --compress-program='python3 -c "..."'
#   rg   --pre=/tmp/evil
#   ack  --pager='sh -c "id"'
# The presence of any of these on a masking-candidate command is FAIL-CLOSED: mask
# nothing and deny, rather than pretend the arguments are inert data.
#
# `find -exec/-execdir/-ok/-okdir` is deliberately NOT here: its payload is already
# DERIVED back through the normal rule pipeline (`_derive_*`), which is strictly
# better than a blanket deny — `find . -exec python3 -c x` still denies via the
# python rule, while a benign `find . -exec ls {} ;` is correctly allowed. Pinned
# by `test_find_exec_payload_is_derived_not_masked`.
EXEC_CAPABLE_OPTIONS: frozenset[str] = frozenset(
    {
        "--compress-program",  # sort — runs the value as a compressor
        "--pre",               # rg — preprocessor command
        "--pre-glob",          # rg — selects which files hit --pre
        "--hostname-bin",      # rg — executed to resolve the hostname
        "--pager",             # ack / rg — the value is spawned
        "--filter",            # ack
    }
)

# The subset of READ_ONLY_INSPECTION that OWNS one of the options above. Only these
# need to fail closed on an argument we cannot read: the option NAME itself can be
# built by expansion (`o=--pre; rg "$o" 'python3 -c x'`), and simply declining to
# mask is NOT enough — the payload sits in argument position, where the anchored
# rules (`^python3 -c`) never fire, so it would sail through while ripgrep executes
# it as the preprocessor.
#
# Scoped deliberately: `grep`/`ls`/`wc`/… own no exec-capable option, so an
# unreadable argument to them is harmless and must NOT be denied — `grep
# "$pattern" file` is an everyday command.
#
# `find` IS here despite its `-exec` payload being derived through the rules,
# because that derivation only fires on a LITERAL `-exec`. An expansion-built
# option — `o=-exec; find . "$o" python3 -c 'payload' {} \;` — is unreadable, so
# the derivation never sees it and the interpreter lands in argument position
# where anchored rules do not match: arbitrary execution through the guard.
# So an unreadable argument to `find` fails closed. Accepted tradeoff: a benign
# `find "$dir" -name '*.py'` (variable path) also fails closed — write the path
# literally instead. Closing an arbitrary-execution hole outweighs that friction.
EXEC_OPTION_COMMANDS: frozenset[str] = frozenset({"sort", "rg", "ack", "find"})

# Shells: `-s` / stdin / heredoc make these read their script from stdin.
SHELL_WORDS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})

# Interpreters that execute whatever arrives on stdin.
INTERPRETERS: frozenset[str] = SHELL_WORDS | frozenset(
    {"fish", "python", "python2", "python3", "node", "ruby", "perl", "php", "osascript"}
)

# Wrappers that exec a program named in their own argv.
PASSTHROUGH_WRAPPERS: frozenset[str] = frozenset(
    {"env", "command", "exec", "sudo", "doas", "nice", "nohup", "stdbuf", "time", "xargs"}
)

# Flags of a passthrough wrapper that consume a following value word.
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset"}),
    "sudo": frozenset({"-u", "-g", "--user", "--group"}),
    "doas": frozenset({"-u"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "xargs": frozenset(
        {"-n", "-P", "-I", "-i", "-L", "-s", "-a", "-d", "-E", "--max-args", "--replace"}
    ),
}

_FIND_EXEC_FLAGS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_EXEC_TERMINATORS: frozenset[str] = frozenset({";", "+"})

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c$")
_SHELL_STDIN_FLAG_RE = re.compile(r"^-[a-zA-Z]*s$")

# bashlex cannot parse heredocs at all, so this is checked on the raw string
# BEFORE parsing: a heredoc-fed interpreter reads its script from stdin and its
# payload is unprovable, so it fails closed.
_HEREDOC_INTERPRETER_RE = re.compile(
    r"(?:^|[;&|\n])\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:(?:sudo|env|command|exec|nohup|nice)\s+)*"
    r"(?:sh|bash|zsh|dash|ash|ksh|python3?|node|ruby|perl)\b"
    r"[^\n<]*<<"
)

# Recursion bound for derived payloads (`sh -c "sh -c '…'"`).
MAX_DERIVE_DEPTH = 3


@dataclass
class AstResult:
    """Result of an AST analysis of one Bash command.

    Attributes:
        masked_command: the original command with proven-inert literal
            arguments of read-only inspection commands replaced by
            :data:`INERT_PLACEHOLDER`. Every other byte is preserved.
        derived_commands: wrapper payloads (``sh -c``, ``eval``, ``xargs``,
            ``find -exec``, …) that a caller MAY re-feed through its own rule
            pipeline. bash-guard's own wrapper-stripping and segment-
            derivation (``wrappers.py`` / ``segments.py``) already cover this
            ground for the guard's rule walk, so :func:`mask_command` (the
            function actually wired into the guard) ignores this field; it is
            exposed for callers that want the fuller analysis.
        deny_reason: set when the command must fail closed; a caller MAY deny
            immediately without consulting any rule. Also currently unused by
            the guard integration — see ``derived_commands`` above.
    """

    masked_command: str
    derived_commands: list[str] = field(default_factory=list)
    deny_reason: str | None = None


@dataclass
class _Ctx:
    """Mutable accumulator threaded through the AST walk."""

    mask_spans: list[tuple[int, int]] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    deny_reason: str | None = None

    def deny(self, reason: str) -> None:
        if self.deny_reason is None:
            self.deny_reason = reason


def available() -> bool:
    """True when bashlex is importable (AST mode); False forces fallback."""
    return AVAILABLE


def mask_command(command: str) -> str:
    """Return ``command`` with proven-inert literal arguments of read-only
    inspection commands replaced by :data:`INERT_PLACEHOLDER`.

    This is the function wired into bash-guard's rule evaluation (gated by
    ``settings.mask_inert_literals``). It NEVER changes what a shell would
    actually execute — masking only changes the *view* the pattern matcher
    sees, never the real command. When bashlex is unavailable, the command
    does not parse, or the command must fail closed for a reason unrelated to
    masking, this returns ``command`` UNCHANGED: the safe degradation
    direction, since an unmasked command can only be MORE likely to match a
    rule, never less.
    """
    result = analyze(command)
    if result is None:
        return command
    return result.masked_command


def analyze(command: str, _depth: int = 0) -> AstResult | None:
    """Analyze ``command``; return ``None`` to signal fallback (raw matching).

    ``None`` means: bashlex is unavailable, or the command does not parse.
    Callers MUST then use the raw command string unchanged.
    """
    if bashlex is None or not command.strip():
        return None

    if _HEREDOC_INTERPRETER_RE.search(command):
        return AstResult(
            masked_command=command,
            derived_commands=[],
            deny_reason=(
                "FORBIDDEN (fail-closed): an interpreter fed from a heredoc runs a "
                "script the guard cannot inspect. Write the script to a file and run "
                "it by path."
            ),
        )

    try:
        trees = bashlex.parse(command)
    except Exception:
        # Unparseable (arithmetic expansion, malformed quoting, …) — fall back
        # to raw-string matching, which is strictly more denying.
        return None

    ctx = _Ctx()
    for tree in trees:
        _visit(tree, command, ctx, stdin_fed=False)

    masked = _apply_masks(command, ctx.mask_spans)
    deny_reason = ctx.deny_reason
    derived_out: list[str] = []

    for payload in ctx.derived:
        if not payload.strip():
            continue
        if _depth >= MAX_DERIVE_DEPTH:
            derived_out.append(payload)
            continue
        nested = analyze(payload, _depth + 1)
        if nested is None:
            derived_out.append(payload)
            continue
        if nested.deny_reason and deny_reason is None:
            deny_reason = nested.deny_reason
        derived_out.append(nested.masked_command)
        derived_out.extend(nested.derived_commands)

    return AstResult(masked_command=masked, derived_commands=derived_out, deny_reason=deny_reason)


# ---------------------------------------------------------------------------
# AST walk
# ---------------------------------------------------------------------------


def _children(node: Any) -> list[Any]:
    out: list[Any] = []
    for attr in ("parts", "list", "command"):
        value = getattr(node, attr, None)
        if isinstance(value, list):
            out.extend(child for child in value if hasattr(child, "kind"))
        elif hasattr(value, "kind"):
            out.append(value)
    return out


def _visit(node: Any, source: str, ctx: _Ctx, stdin_fed: bool = False) -> None:
    kind = getattr(node, "kind", "")

    if kind == "pipeline":
        position = 0
        for part in getattr(node, "parts", []):
            if getattr(part, "kind", "") == "pipe":
                continue
            # Every command after the first in a pipeline reads its stdin from
            # the previous one — an interpreter there is stdin-fed.
            _visit(part, source, ctx, stdin_fed=position > 0)
            position += 1
        return

    if kind == "command":
        _visit_command(node, source, ctx, stdin_fed)
        return

    for child in _children(node):
        _visit(child, source, ctx)


def _is_literal(word: Any) -> bool:
    """True only when bashlex proves the word is pure literal text.

    A word carrying ANY expansion part (command substitution, process
    substitution, parameter, tilde) is never treated as literal.
    """
    return not getattr(word, "parts", None)


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _visit_command(node: Any, source: str, ctx: _Ctx, stdin_fed: bool) -> None:
    parts = list(getattr(node, "parts", []))
    words = [p for p in parts if getattr(p, "kind", "") == "word"]
    redirects = [p for p in parts if getattr(p, "kind", "") == "redirect"]

    # Descend into every executable expansion FIRST — a quoted argument is not
    # inherently inert, and nested commands must be seen before anything is masked.
    for part in parts:
        for child in _children(part):
            _visit(child, source, ctx)

    if not words:
        return

    cmd_word = words[0]
    args = words[1:]

    if not _is_literal(cmd_word):
        ctx.deny(
            "FORBIDDEN (fail-closed): the command word is built by expansion "
            f"({source[cmd_word.pos[0] : cmd_word.pos[1]]!r}), so the guard cannot prove "
            "which program runs. Write the program name literally."
        )
        return

    name = _basename(str(cmd_word.word))

    if name in INTERPRETERS:
        _check_stdin_fed(name, args, redirects, ctx, stdin_fed)

    if name in SHELL_WORDS:
        _derive_shell_c(source, node, args, ctx)
    elif name == "eval":
        _derive_eval(args, ctx)
    elif name in PASSTHROUGH_WRAPPERS:
        _derive_passthrough(source, node, name, args, ctx)

    if name in READ_ONLY_INSPECTION and not _has_exec_capable_option(name, args, ctx):
        _mask_inert_args(source, name, args, ctx)


def _has_exec_capable_option(name: str, args: list[Any], ctx: _Ctx) -> bool:
    """True when a 'read-only' command's args must NOT be masked as inert.

    `sort --compress-program=CMD`, `rg --pre=CMD` and `ack --pager=CMD` all run
    CMD. Masking such a command's literal arguments would hide that execution from
    every rule — the exact bypass this guard exists to prevent.

    Two outcomes, both returning True (= do not mask):
      • a LITERAL exec-capable option (incl. GNU abbreviations) → also DENY, since
        the command is provably executing its option value;
      • an UNREADABLE (expansion-built) argument → skip masking, do NOT deny. The
        option name itself may be built by expansion (`o=--pre; rg "$o" …`), so we
        cannot prove the command is inspective. Declining to mask is always safe:
        the command text is left exactly as written, so the raw rules still see any
        banned payload. Denying instead would false-positive on everyday commands
        like `grep "$pattern" file`.
    """
    for arg in args:
        if not _is_literal(arg):
            # An option NAME can be built by expansion: `o=--pre; rg "$o" 'python3
            # -c x' pattern`. We cannot read `$o`, so we cannot know whether this
            # command is about to execute one of its arguments.
            #
            # For a command that OWNS an exec-capable option, declining to mask is
            # NOT enough: the payload sits in argument position, where the anchored
            # rules (`^python3 -c`) never fire — it would be allowed while ripgrep
            # ran it as the preprocessor. So those FAIL CLOSED.
            #
            # Everything NOT in EXEC_OPTION_COMMANDS (grep/ls/wc/…) owns no such
            # option, so an unreadable argument is harmless: don't mask, don't deny.
            # That keeps `grep "$pattern" file` — an everyday command — working.
            # NOTE: `find` IS in EXEC_OPTION_COMMANDS (it has -exec/-execdir/-ok/-okdir)
            # and therefore reaches the fail-closed deny below — do not read this line
            # as excluding it.
            if name in EXEC_OPTION_COMMANDS:
                ctx.deny(
                    f"FORBIDDEN (fail-closed): `{name}` has an argument built by shell "
                    f"expansion, and `{name}` accepts options whose VALUE it executes "
                    "(--pre / --compress-program / --pager). The guard cannot prove "
                    "this invocation is read-only. Write the arguments literally."
                )
            return True
        word = str(arg.word)
        opt = word.split("=", 1)[0]  # --compress-program=CMD -> --compress-program
        if not opt.startswith("--") or len(opt) <= 2:
            continue
        # GNU long options may be ABBREVIATED to any unambiguous prefix, so
        # `sort --compress-prog=CMD` runs CMD exactly like `--compress-program`.
        # An exact-match check missed every abbreviation and was itself a bypass.
        # Treat any option that is a PREFIX of an exec-capable option as that
        # option (deliberately conservative: an ambiguous prefix that git/sort
        # would reject is still denied here — fail closed).
        if any(full.startswith(opt) for full in EXEC_CAPABLE_OPTIONS):
            ctx.deny(
                f"FORBIDDEN (fail-closed): `{name} {opt}` takes a COMMAND as its value "
                f"and executes it, so `{name}` is not read-only here and its arguments "
                "cannot be treated as inert data. Run the command directly (so the "
                "guard can see it)."
            )
            return True
    return False


def _check_stdin_fed(
    name: str, args: list[Any], redirects: list[Any], ctx: _Ctx, stdin_fed: bool
) -> None:
    """Fail closed when an interpreter takes its script from stdin."""
    if stdin_fed:
        ctx.deny(
            f"FORBIDDEN (fail-closed): `{name}` is fed from a pipe, so the script it "
            "runs cannot be inspected. Write the script to a file and run it by path."
        )
        return

    for redirect in redirects:
        rtype = str(getattr(redirect, "type", ""))
        if rtype.startswith("<"):
            ctx.deny(
                f"FORBIDDEN (fail-closed): `{name}` reads its script from stdin "
                f"(`{rtype}`), which the guard cannot inspect. Run the script by path."
            )
            return

    if name in SHELL_WORDS:
        for arg in args:
            if _is_literal(arg) and _SHELL_STDIN_FLAG_RE.match(str(arg.word)):
                ctx.deny(
                    f"FORBIDDEN (fail-closed): `{name} {arg.word}` reads the script from "
                    "stdin, which the guard cannot inspect. Run the script by path."
                )
                return


# ---------------------------------------------------------------------------
# Derivation of wrapper payloads
# ---------------------------------------------------------------------------


def _derive_shell_c(source: str, node: Any, args: list[Any], ctx: _Ctx) -> None:
    """`sh|bash|zsh -c <payload>` → derive <payload> (fail closed if unprovable)."""
    for index, arg in enumerate(args):
        if not _is_literal(arg) or not _SHELL_C_FLAG_RE.match(str(arg.word)):
            continue
        if index + 1 >= len(args):
            return
        payload = args[index + 1]
        if not _is_literal(payload):
            ctx.deny(
                "FORBIDDEN (fail-closed): a shell `-c` payload built by expansion "
                f"({source[payload.pos[0] : payload.pos[1]]!r}) cannot be inspected. "
                "Write the command out."
            )
            return
        ctx.derived.append(str(payload.word))
        return


def _derive_eval(args: list[Any], ctx: _Ctx) -> None:
    """`eval <words…>` → derive the concatenated words (fail closed if unprovable)."""
    if not args:
        return
    for arg in args:
        if not _is_literal(arg):
            ctx.deny(
                "FORBIDDEN (fail-closed): `eval` of an expansion cannot be inspected — "
                "the guard cannot prove what it runs. Write the command out."
            )
            return
    ctx.derived.append(" ".join(str(arg.word) for arg in args))


def _skip_wrapper_flags(name: str, args: list[Any]) -> int:
    """Index of the first argv word that is the wrapped program (len(args) if none)."""
    value_flags = _VALUE_FLAGS.get(name, frozenset())
    index = 0
    while index < len(args):
        arg = args[index]
        if not _is_literal(arg):
            break
        text = str(arg.word)
        if name == "env" and _ASSIGNMENT_RE.match(text):
            index += 1
            continue
        if not text.startswith("-"):
            break
        if text == "--":
            index += 1
            break
        index += 1
        if text in value_flags and index < len(args):
            index += 1
    return index


def _derive_passthrough(source: str, node: Any, name: str, args: list[Any], ctx: _Ctx) -> None:
    """`env|command|exec|sudo|nice|nohup|stdbuf|xargs <cmd> …` → derive `<cmd> …`."""
    start = _skip_wrapper_flags(name, args)
    if start >= len(args):
        return
    target = args[start]
    if not _is_literal(target):
        ctx.deny(
            f"FORBIDDEN (fail-closed): `{name}` runs a program named by an expansion "
            f"({source[target.pos[0] : target.pos[1]]!r}), so the guard cannot prove "
            "which program runs. Write the program name literally."
        )
        return
    ctx.derived.append(source[target.pos[0] : node.pos[1]].strip())


def _find_exec_regions(args: list[Any]) -> list[tuple[int, int]]:
    """Index ranges [start, end) of `find -exec <cmd …>` payload words."""
    regions: list[tuple[int, int]] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if _is_literal(arg) and str(arg.word) in _FIND_EXEC_FLAGS:
            start = index + 1
            end = start
            while end < len(args):
                candidate = args[end]
                if _is_literal(candidate) and str(candidate.word) in _FIND_EXEC_TERMINATORS:
                    break
                end += 1
            if end > start:
                regions.append((start, end))
            index = end + 1
            continue
        index += 1
    return regions


def _mask_inert_args(source: str, name: str, args: list[Any], ctx: _Ctx) -> None:
    """Mask literal arguments of a read-only inspection command.

    `find -exec <cmd>` payloads are NEVER masked — they execute. They are
    derived instead, so the existing rules see them.
    """
    exec_regions = _find_exec_regions(args) if name == "find" else []
    for start, end in exec_regions:
        first, last = args[start], args[end - 1]
        ctx.derived.append(source[first.pos[0] : last.pos[1]].strip())

    for index, arg in enumerate(args):
        if any(start <= index < end for start, end in exec_regions):
            continue
        if not _is_literal(arg):
            continue
        ctx.mask_spans.append((arg.pos[0], arg.pos[1]))


def _apply_masks(source: str, spans: list[tuple[int, int]]) -> str:
    """Replace each span with :data:`INERT_PLACEHOLDER`; all other bytes are preserved."""
    if not spans:
        return source
    out: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:  # defensive: never let spans overlap
            continue
        out.append(source[cursor:start])
        out.append(INERT_PLACEHOLDER)
        cursor = end
    out.append(source[cursor:])
    return "".join(out)
