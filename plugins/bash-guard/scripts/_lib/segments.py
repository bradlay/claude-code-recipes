# Shell segmentation helpers for the guard's additional inspection pass.
#
# ``guard.split_chained_commands`` deliberately does NOT split on pipe
# (``|``) — a pipeline is evaluated as one unit so rules like "block
# ``base64 -d | bash``" can see the whole pipeline string. That design
# stays unchanged.
#
# This module adds a SEPARATE signal: it derives the individual stages of
# a pipeline, and the inner command text of any ``$(...)``/backtick/
# ``<(...)``/``>(...)`` substitution, so each derived piece can ALSO be
# checked against the rule list. Without this, a guarded command can hide
# behind a benign leading token in either shape:
#
#   echo ok | git push --force
#   echo $(git push --force)
#
# In both cases the rules never see ``git push --force`` directly because
# the outer command is ``echo``. This module only *extracts* candidate
# segments; the caller (``guard.evaluate_chain``) decides whether and how
# to evaluate them, gated by ``settings.inspect_segments``.

from __future__ import annotations


def split_pipeline_segments(command: str) -> list[str]:
    """Split *command* on pipe (``|``) only, honouring quotes and escapes.

    Does not touch ``&&``, ``||`` (logical OR, not a pipe), ``;``, ``&``,
    or newlines — those sequence separators are the caller's job
    (``guard.split_chained_commands``). This function is meant to be
    applied to ONE already-split sub-command, to see its pipeline stages
    individually without changing how the pipeline is evaluated as a
    whole.
    """
    segments: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(command)

    while i < n:
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

        # `||` is logical OR, not a pipe — leave it in the current
        # segment untouched (it is a sequence separator handled upstream).
        if ch == "|" and i + 1 < n and command[i + 1] == "|":
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue

        if ch == "|":
            part = "".join(current).strip()
            if part:
                segments.append(part)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    part = "".join(current).strip()
    if part:
        segments.append(part)

    return segments


def _balanced_paren_end(command: str, start: int, n: int) -> int:
    """Index of the ``)`` that closes the ``(`` whose inner text begins at
    ``start`` (i.e. ``start`` is the position just past the opening paren).

    Honours nested parens and quote spans the same way the shell does.
    Returns ``n`` if the group is unbalanced (so callers consume to
    end-of-string).
    """
    depth = 1
    j = start
    sub_single = False
    sub_double = False
    while j < n and depth > 0:
        c = command[j]
        if c == "\\" and not sub_single:
            j += 2
            continue
        if c == "'" and not sub_double:
            sub_single = not sub_single
        elif c == '"' and not sub_single:
            sub_double = not sub_double
        elif not sub_single and not sub_double:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    return j


def extract_command_substitutions(command: str) -> list[str]:
    """Return the inner command text of every ``$(...)``, backtick, and
    ``<(...)``/``>(...)`` process substitution.

    Shell command/process substitution *executes* its contents, so a
    guarded command hidden inside one (e.g. ``echo $(git push --force)``,
    ```` echo `git push --force` ````, or ``cat <(git push --force)``)
    runs for real. Without this, such a command evades every pattern rule
    anchored on the outer command — the guarded ``git push`` is never
    seen because the outer token is ``echo``.

    The shell does NOT expand substitutions inside single quotes, so
    single-quoted spans are skipped (no false positive on
    ``echo '$(x)'``). Substitutions inside double quotes ARE expanded, so
    they are extracted. Nested substitutions are extracted recursively.

    Note: this closes the "dangerous command *inside* a substitution"
    class. It does not attempt to model token-reuniting expansion such as
    ``git $(printf push) --force`` (where the substitution yields a bare
    word that reassembles a guarded command) — that requires evaluating
    shell expansion and is out of scope for a static guard.
    """
    results: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False
    while i < n:
        ch = command[i]

        # Backslash escapes the next char everywhere except inside single quotes.
        if ch == "\\" and not in_single:
            i += 2
            continue

        # A single quote is a delimiter only when NOT inside double
        # quotes — inside "..." an apostrophe is a literal character.
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        # A double quote is a delimiter only when NOT inside single quotes.
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        # Single-quoted spans are literal — the shell does not expand
        # them. Double-quoted spans DO expand $(...) / backticks, so
        # keep scanning.
        if in_single:
            i += 1
            continue

        # $( ... ) command substitution, and <( ... ) / >( ... ) process
        # substitution — all three execute their inner command, so a
        # guarded command can hide inside any of them. Same
        # balanced-paren, quote-aware nesting for each.
        is_dollar_paren = ch == "$" and i + 1 < n and command[i + 1] == "("
        is_process_sub = ch in ("<", ">") and i + 1 < n and command[i + 1] == "("
        if is_dollar_paren or is_process_sub:
            start = i + 2
            j = _balanced_paren_end(command, start, n)
            inner = command[start:j].strip()
            if inner:
                results.append(inner)
                results.extend(extract_command_substitutions(inner))
            i = j + 1
            continue

        # `...` backtick substitution
        if ch == "`":
            j = i + 1
            start = j
            while j < n and command[j] != "`":
                if command[j] == "\\":
                    j += 2
                    continue
                j += 1
            inner = command[start:j].strip()
            if inner:
                results.append(inner)
                results.extend(extract_command_substitutions(inner))
            i = j + 1
            continue

        i += 1

    return results


def derive_inspection_segments(sub_command: str) -> list[str]:
    """Every additional segment that should be checked against the rule
    list for one already sequence-split sub-command: its pipeline stages
    (beyond the first, which is the sub-command itself and already
    evaluated as a whole) plus the inner command of every command/process
    substitution, at any nesting depth.

    Order is stable but not otherwise meaningful — callers combine
    decisions by taking the most restrictive one, never by position.
    """
    segments: list[str] = []
    pipeline_stages = split_pipeline_segments(sub_command)
    if len(pipeline_stages) > 1:
        segments.extend(pipeline_stages)
    segments.extend(extract_command_substitutions(sub_command))
    return segments
