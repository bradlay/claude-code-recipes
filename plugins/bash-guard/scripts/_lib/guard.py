# Bash command rule evaluator.
#
# Loads YAML rules (default + user overrides), splits compound commands
# on shell separators, normalizes `git -C <path>` so anchored regexes
# still match, and walks the rule list. First match wins. The hook
# script translates the resulting decision to a Claude Code hook
# decision JSON.
#
# Chain-aware evaluation: a command like ``cd /some/path && git push``
# is split into sub-commands; the evaluator tracks the effective ``cwd``
# across leading ``cd`` statements so a per-sub-command ``git -C <path>``
# resolves the right target. When a matched rule's ``category`` is
# listed in ``settings.exempt_categories_for_deploy_worktree`` AND the
# target directory looks like a deploy worktree (``repos/<name>`` not
# ending in ``-dev``), the rule is skipped — the sddcinfo convention
# where ``repos/<name>`` is the branch-``main`` deploy copy of the
# service and the protected-branch guard ("stay on dev") must not apply.

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped, unused-ignore]

from . import identity, paths, segments, shell_ast, wrappers

_SECURE_FILE_MODE = 0o600


# ----------------------------------------------------------------------
# Config loading
# ----------------------------------------------------------------------

_CACHE: dict[str, Any] = {"config": None, "path": None, "mtime": 0.0}


def _default_rules_path() -> Path:
    """Path to the default-rules.yaml shipped with the plugin."""
    return Path(__file__).resolve().parent.parent / "default-rules.yaml"


def _resolve_rules_file() -> Path:
    """Resolution order for the active *rules* document:

    1. ``$CLAUDE_BASH_GUARD_RULES_FILE`` env var (explicit override).
    2. ``${XDG_CONFIG_HOME}/claude-bash-guard/rules.yaml`` (user override).
    3. The default-rules.yaml shipped with the plugin.

    Note that ``settings`` are merged separately by ``load_rules`` so
    defaults still apply even when the user override exists and silently
    lacks a key (see settings-merge handling there).
    """
    explicit = os.environ.get("CLAUDE_BASH_GUARD_RULES_FILE")
    if explicit:
        return Path(explicit)
    user_path = paths.user_rules_file()
    if user_path.exists():
        return user_path
    return _default_rules_path()


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file into a dict (empty/invalid → empty dict)."""
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def load_rules() -> dict[str, Any]:
    """Load and cache the effective rules document.

    ``rules:`` come from the active rules file (user override if
    present, else default-rules.yaml — same as before).

    ``settings:`` always shallow-merge the plugin's default settings
    UNDER the active file's settings. Without this, a user override
    file that omitted ``exempt_categories_for_deploy_worktree`` would
    silently disable the deploy-worktree exemption, even though the
    plugin defaults set it. Keys in the active file always win;
    defaults only fill in absences.
    """
    rules_path = _resolve_rules_file()
    try:
        current_mtime = rules_path.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    # If we're using the default rules path, treat it as the only
    # source — no double-merge of defaults onto themselves. Otherwise
    # the merge of defaults.settings has to invalidate the cache when
    # the defaults file itself changes.
    default_path = _default_rules_path()
    using_default = rules_path.resolve() == default_path.resolve()
    try:
        default_mtime = 0.0 if using_default else default_path.stat().st_mtime
    except OSError:
        default_mtime = 0.0
    cache_key = (rules_path, current_mtime, default_mtime)

    cached_config = _CACHE.get("config")
    if cached_config is not None and _CACHE.get("key") == cache_key:
        return cached_config  # type: ignore[no-any-return]

    active = _read_yaml(rules_path)
    config: dict[str, Any] = {
        "rules": active.get("rules") or [],
        "settings": dict(active.get("settings") or {}),
    }

    if not using_default:
        default_settings = _read_yaml(default_path).get("settings") or {}
        for key, value in default_settings.items():
            config["settings"].setdefault(key, value)

    _expand_pattern_placeholders(config)

    _CACHE["config"] = config
    _CACHE["key"] = cache_key
    return config


_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _expand_pattern_placeholders(config: dict[str, Any]) -> None:
    """Expand ``{setting_name}`` placeholders in rule patterns.

    A placeholder whose name matches a LIST-valued setting expands to a
    non-capturing alternation of the escaped values, so
    ``checkout\\s+({protected_branches})`` with
    ``protected_branches: [main, master]`` becomes
    ``checkout\\s+((?:main|master))``. This makes the settings comment
    that always promised this behaviour true. An unknown or non-list
    placeholder is left as written: the literal ``{name}`` will then
    simply never match a real command, which fails safe for allow rules
    and is caught by rule tests for deny rules.
    """

    def _expand(text: str, settings: dict[str, Any]) -> str:
        def _sub(m: re.Match[str]) -> str:
            values = settings.get(m.group(1))
            if isinstance(values, list) and values:
                return "(?:" + "|".join(re.escape(str(v)) for v in values) + ")"
            return m.group(0)

        return _PLACEHOLDER_RE.sub(_sub, text)

    settings = config.get("settings", {}) or {}
    for rule in config.get("rules", []) or []:
        for key in ("pattern", "extra_search"):
            value = rule.get(key)
            if isinstance(value, str) and "{" in value:
                rule[key] = _expand(value, settings)


# ----------------------------------------------------------------------
# Trusted-projects discovery (project-level rules.yaml extension)
# ----------------------------------------------------------------------

# Project-rule files live at ``<git-root>/.bash-guard-rules.yaml`` and
# are loaded ONLY when the git root is explicitly listed in
# ``~/.config/claude-bash-guard/trusted-projects.yaml``. The trust
# allowlist is the only thing that selects which files get loaded:
# untrusted files (nested, planted by an agent, anywhere) are NEVER
# opened, so they cannot short-circuit lookup of a trusted ancestor's
# rules or suppress its tightening rules.
#
# Project rules are tightening-only: project files cannot introduce a
# ``decision: allow``, cannot redefine an existing rule's ``id``, and
# their ``settings:`` block is ignored entirely. The combination of
# (out-of-repo trust allowlist + in-engine sanitizer) means a hostile
# repo edit cannot weaken the active policy.

_PROJECT_RULES_FILENAME = ".bash-guard-rules.yaml"

_TRUST_CACHE: dict[str, Any] = {"set": None, "mtime": 0.0, "path": None}

_PROJECT_CONFIG_LRU: dict[Path, dict[str, Any]] = {}


def _load_trusted_projects() -> frozenset[Path]:
    """Read the trust allowlist into a frozenset of resolved absolute
    paths. Cached on the file's mtime so a flip in the file invalidates
    in-process state. A missing or unparseable file means "no projects
    are trusted" — the safest default."""
    path = paths.trusted_projects_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    cached: frozenset[Path] | None = _TRUST_CACHE.get("set")
    if (
        cached is not None
        and _TRUST_CACHE.get("path") == path
        and _TRUST_CACHE.get("mtime") == mtime
    ):
        return cached

    trusted: set[Path] = set()
    doc = _read_yaml(path)
    for entry in doc.get("trusted") or []:
        if not isinstance(entry, str):
            continue
        try:
            trusted.add(Path(entry).expanduser().resolve())
        except OSError:
            continue

    result = frozenset(trusted)
    _TRUST_CACHE["set"] = result
    _TRUST_CACHE["mtime"] = mtime
    _TRUST_CACHE["path"] = path
    # Bust the per-target LRU because the trust set changing can flip
    # any project's load decision.
    _PROJECT_CONFIG_LRU.clear()
    return result


def _discover_trusted_project_files(target_dir: Path) -> list[Path]:
    """Walk from *target_dir* up to filesystem root. For each ancestor
    that is listed in the trust set AND contains a
    ``.bash-guard-rules.yaml`` file, return its path. Untrusted
    ancestors are skipped entirely — their content is never read.

    Returns project files in order from deepest trusted ancestor to
    shallowest (so deeper-project rules append after shallower ones,
    matching a "nearer project wins on ordering ties" intuition; but
    since project rules cannot override existing ids, ordering only
    affects the relative position among additive rules).
    """
    try:
        cur = target_dir.resolve()
    except OSError:
        return []
    trusted = _load_trusted_projects()
    if not trusted:
        return []
    out: list[Path] = []
    seen: set[Path] = set()
    while True:
        if cur in trusted and cur not in seen:
            seen.add(cur)
            candidate = cur / _PROJECT_RULES_FILENAME
            if candidate.exists():
                out.append(candidate)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return out


def _sanitize_project_rules(
    project_rules: list[Any], existing_ids: set[str]
) -> list[dict[str, Any]]:
    """Apply the tightening-only contract:

    * Drop entries lacking ``id``, ``pattern``, ``decision``, or ``reason``.
    * Drop entries whose ``id`` collides with an existing rule (base
      defaults / user override).
    * Drop entries with ``decision: allow`` — project files cannot
      install first-match-wins exemptions for base policy.
    """
    out: list[dict[str, Any]] = []
    for entry in project_rules:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("id")
        if not rule_id:
            continue
        if rule_id in existing_ids:
            continue
        if not entry.get("pattern") or not entry.get("decision") or not entry.get("reason"):
            continue
        if entry.get("decision") == "allow":
            continue
        out.append(entry)
        existing_ids.add(rule_id)
    return out


def resolve_config_for(
    target_dir: Path | None,
    base_config: dict[str, Any],
) -> dict[str, Any]:
    """Return the effective config for a sub-command targeting
    *target_dir*. The base config (defaults + user override + merged
    settings, from ``load_rules``) is used unchanged when no trusted
    project file applies.

    When one or more trusted ancestors carry a project file, their
    sanitized rules are appended after the base rules so first-match-
    wins semantics keep base rules firing first; project rules add
    new triggers without ever overriding existing ids or installing
    allow-decisions. Settings are NEVER taken from project files.
    """
    if target_dir is None:
        return base_config
    try:
        key = target_dir.resolve()
    except OSError:
        return base_config

    cached = _PROJECT_CONFIG_LRU.get(key)
    if cached is not None:
        return cached

    project_files = _discover_trusted_project_files(target_dir)
    if not project_files:
        _PROJECT_CONFIG_LRU[key] = base_config
        return base_config

    base_rules = list(base_config.get("rules") or [])
    existing_ids: set[str] = {
        rid
        for r in base_rules
        if isinstance(r, dict) and isinstance(rid := r.get("id"), str) and rid
    }
    appended: list[dict[str, Any]] = []
    for pf in project_files:
        doc = _read_yaml(pf)
        sanitized = _sanitize_project_rules(doc.get("rules") or [], existing_ids)
        appended.extend(sanitized)

    if not appended:
        _PROJECT_CONFIG_LRU[key] = base_config
        return base_config

    effective: dict[str, Any] = {
        "rules": base_rules + appended,
        "settings": base_config.get("settings", {}),
    }
    _PROJECT_CONFIG_LRU[key] = effective
    return effective


# ----------------------------------------------------------------------
# Approval cache (for `ask` decisions)
# ----------------------------------------------------------------------


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()[:16]


def consume_approval(command: str, expiry_seconds: int) -> bool:
    """Check for a one-shot approval token; consume it if valid.

    Consumption is an ATOMIC RENAME claim: exactly one concurrent caller
    can win the rename, so two racing invocations can never both spend
    the same one-shot token (the previous stat-then-unlink sequence let
    both return True). The claimed file is removed afterwards either way.
    """
    approval_file = paths.approvals_dir() / _command_hash(command)
    claim = approval_file.with_name(
        approval_file.name + f".claimed-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        approval_file.rename(claim)
    except OSError:
        return False
    try:
        age = time.time() - claim.stat().st_mtime
        return age <= expiry_seconds
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            claim.unlink(missing_ok=True)


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


def _pattern_view(command: str, mask_inert_literals: bool) -> str:
    """The text rules are actually matched against for one sub-command
    form: optionally AST-masked (``shell_ast.mask_command``), then
    ``normalize_for_patterns``.

    Masking only ever REMOVES text a rule could match (proven-inert
    literal arguments of a read-only inspection command are replaced by
    a placeholder); it never adds or hides an executing form. When
    bashlex is unavailable, or the command doesn't parse, or masking is
    disabled via ``settings.mask_inert_literals``, this is a no-op and
    behaves exactly as before — the safe degradation direction.
    """
    if mask_inert_literals:
        command = shell_ast.mask_command(command)
    return normalize_for_patterns(command)


# ----------------------------------------------------------------------
# Rule evaluation
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Cwd tracking + deploy-worktree exemption
# ----------------------------------------------------------------------


_CD_STATEMENT_RE = re.compile(r"^\s*cd\s+(?:--\s+)?(\S+)\s*$")


def _extract_cd_target(sub_command: str) -> Path | None:
    """If *sub_command* is a pure ``cd <path>`` statement, return its
    target as a ``Path`` (with ``~`` and ``$ENV`` expansion). Otherwise
    return None. The path is NOT resolved relative to a base cwd here —
    callers do that with the running ``current_cwd``.
    """
    match = _CD_STATEMENT_RE.match(sub_command)
    if not match:
        return None
    raw = os.path.expandvars(match.group(1).strip("'\""))
    return Path(raw).expanduser()


def _git_dash_c_path(sub_command: str) -> str | None:
    """Return the value of a leading ``git -C <path>``, if present."""
    try:
        tokens = shlex.split(sub_command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-C" and i + 1 < len(tokens):
            return tokens[i + 1]
        i += 1
    return None


def _target_dir_for(sub_command: str, base_cwd: Path | None) -> Path | None:
    """Directory the sub-command effectively acts on.

    For ``git -C <path> <args>`` this is the explicit ``-C`` path
    (resolved relative to *base_cwd* if relative). For everything else
    it's the running cwd context.
    """
    dash_c = _git_dash_c_path(sub_command)
    if dash_c is None:
        return base_cwd
    path = Path(dash_c).expanduser()
    if not path.is_absolute() and base_cwd is not None:
        path = base_cwd / path
    return path


def _is_deploy_worktree(path: Path | None) -> bool:
    """True iff *path* is inside a deploy worktree.

    The sddcinfo convention is ``repos/<name>`` = the branch-``main``
    deploy worktree that runs the live service, and ``repos/<name>-dev``
    = the branch-``dev`` working copy. The protected-branch guard
    ("stay on dev; PR to main") must NOT apply to a deploy worktree —
    it legitimately tracks ``main`` (checkout / pull / ff-merge / push).
    The monorepo root and any ``*-dev`` worktree are NOT deploy
    worktrees, so they stay fully protected. History-rewriting and
    destructive ops belong to other categories and remain blocked
    everywhere.
    """
    if path is None:
        return False
    try:
        parts = path.resolve().parts
    except OSError:
        parts = path.parts
    for i, part in enumerate(parts):
        if part == "repos" and i + 1 < len(parts):
            return not parts[i + 1].endswith("-dev")
    return False


# ----------------------------------------------------------------------
# Rule evaluation
# ----------------------------------------------------------------------


def _rule_matches(rule: dict[str, Any], pattern_command: str) -> bool:
    """True iff *rule*'s ``pattern`` (and optional ``extra_search``)
    match *pattern_command*. Bad regexes are silently skipped so one
    broken rule doesn't crash the whole guard."""
    pattern = rule.get("pattern", "")
    if not pattern:
        return False
    use_search = rule.get("search", False)
    extra_search = rule.get("extra_search")
    try:
        if use_search:
            primary = re.search(pattern, pattern_command)
        else:
            primary = re.match(pattern, pattern_command)
    except re.error:
        return False
    if not primary:
        return False
    if extra_search:
        try:
            if not re.search(extra_search, pattern_command, re.IGNORECASE):
                return False
        except re.error:
            return False
    return True


def evaluate_chain(
    command: str,
    config: dict[str, Any],
    cwd: Path | None,
) -> tuple[str, str, str]:
    """Walk the command's sub-commands in order, tracking the effective cwd.

    Each pure ``cd <path>`` statement updates the running cwd context
    for everything that follows in the same compound command (matching
    bash semantics) and is NOT itself evaluated against the rule list.
    Every other sub-command is evaluated with its effective target dir:
    a leading ``git -C <path>`` pins that one call to ``<path>``; else
    the running cwd is used.

    Per-sub-command config: the effective rules are re-resolved against
    the sub-command's target dir via ``resolve_config_for``, so a
    trusted project's ``.bash-guard-rules.yaml`` applies even when the
    initial ``cwd`` was outside that project (``cd /path/to/project &&
    <cmd>`` or ``git -C /path/to/project <cmd>``). Settings always come
    from the base config — project files cannot change settings.

    Returns ``(decision, reason, offending_sub)`` where ``decision`` is
    ``"allow"``, ``"deny"``, or ``"ask"``. If no rule matches anywhere
    in the chain, returns ``("allow", "", "")``.
    """
    base_settings = config.get("settings", {}) or {}
    exempt_cats = set(base_settings.get("exempt_categories_for_deploy_worktree") or [])
    identity_bindings = base_settings.get("identity_bindings") or {}
    category_scopes = base_settings.get("category_scopes") or {}
    mask_inert_literals = base_settings.get("mask_inert_literals", True)

    # Setting a blocked environment variable is itself a denied action.
    # This replaces the removed CLAUDE_BASH_GUARD_FAIL_OPEN bypass:
    # instead of one exported variable silently disabling enforcement,
    # the engine denies the export. Checked on the RAW command (before
    # splitting or stripping) so `export V=1`, `V=1 cmd`, `env V=1 cmd`
    # and chained forms are all caught. A false positive on a quoted
    # mention is acceptable: deny is the safe direction.
    blocked_vars = base_settings.get("blocked_env_vars")
    if blocked_vars is None:
        blocked_vars = ["CLAUDE_BASH_GUARD_FAIL_OPEN"]
    for var in blocked_vars:
        if re.search(
            r"(?:^|[\s;&|(`])(?:export\s+)?" + re.escape(str(var)) + r"\s*=",
            command,
        ):
            return (
                "deny",
                f"Setting {var} is blocked: it would disable or weaken the guard. "
                f"Disable the plugin visibly via /plugin disable instead.",
                command,
            )

    # Print-policy gates: settings.print_policies lists CLIs whose
    # headless print mode is default-denied (opt-in via env var, with a
    # recursion sentinel). Evaluated on the RAW command first so the
    # fail-closed wrapped-`sh -c` backstop sees quote tricks the chain
    # splitter cannot, then per sub-command below. Local import: the
    # policy module imports helpers from this one.
    print_policies: list[Any] = []
    if base_settings.get("print_policies"):
        from . import print_policy as _pp  # noqa: PLC0415 (import cycle)

        for entry in base_settings["print_policies"]:
            if not isinstance(entry, dict) or not entry.get("program"):
                continue
            print_policies.append(
                _pp.PrintPolicy(
                    program=str(entry["program"]),
                    opt_in_env=str(entry.get("opt_in_env", "")),
                    recursion_env=str(entry.get("recursion_env", "")),
                    fallback_hint=str(entry.get("fallback_hint", "")),
                )
            )
        for pol in print_policies:
            hit = _pp.guard_outcome(pol, command)
            if hit is not None:
                reason, decision = hit
                return (decision, reason, command)

    current_cwd = cwd

    for sub_command in split_chained_commands(command):
        if not sub_command:
            continue

        cd_target = _extract_cd_target(sub_command)
        if cd_target is not None:
            new_path = cd_target
            if not new_path.is_absolute() and current_cwd is not None:
                new_path = current_cwd / cd_target
            with contextlib.suppress(OSError):
                if new_path.is_dir():
                    current_cwd = new_path
            continue

        target_dir = _target_dir_for(sub_command, current_cwd)

        # Identity binding runs BEFORE rule evaluation. An ``ask`` rule
        # that the operator has already approved must NOT leak a
        # cross-owner write past the binding, so identity-deny wins
        # over any rule-level decision.
        if identity_bindings:
            identity_reason = identity.check_identity(sub_command, target_dir, identity_bindings)
            if identity_reason:
                return ("deny", identity_reason, sub_command)

        # Per-sub-command print-policy pass: catches an invocation that
        # is not the first program in the compound command
        # (``echo ok && <program> -p ...``), including with wrappers
        # stripped. The raw-command pass above already handled the
        # wrapped-`sh -c` backstop.
        if print_policies:
            from . import print_policy as _pp  # noqa: PLC0415 (import cycle)

            for pol in print_policies:
                for candidate in (sub_command, wrappers.strip_wrappers(sub_command)):
                    hit = _pp.guard_outcome(pol, candidate)
                    if hit is not None:
                        reason, decision = hit
                        return (decision, reason, sub_command)

        effective = resolve_config_for(target_dir, config)
        rules = effective.get("rules", []) or []

        # Evaluate the sub-command as written AND (when enabled and
        # different) with pass-through wrappers stripped, so ``mise exec
        # -- git push`` or ``sh -c "git stash"`` is judged by the same
        # rules as the bare inner command. The most restrictive outcome
        # across the forms wins: a wrapper must never weaken a decision,
        # and an allow-rule match on the wrapped form cannot mask a deny
        # on the real program inside it.
        forms = [sub_command]
        if base_settings.get("strip_wrappers", True):
            stripped = wrappers.strip_wrappers(sub_command)
            if stripped and stripped != sub_command:
                forms.append(stripped)

        outcome: tuple[str, str] | None = None
        for form in forms:
            hit = _first_rule_decision(
                rules,
                _pattern_view(form, mask_inert_literals),
                exempt_cats,
                target_dir,
                category_scopes,
            )
            if hit is None:
                continue
            if outcome is None or _MORE_RESTRICTIVE[hit[0]] > _MORE_RESTRICTIVE[outcome[0]]:
                outcome = hit

        # Additional inspection pass: pipeline stages beyond the first,
        # and the inner command of any command/process substitution, are
        # each evaluated against the same rules. A guarded command can
        # hide behind a benign leading token either way (``echo ok |
        # git push --force`` or ``echo $(git push --force)``); the
        # top-level chain split above never sees ``git push --force``
        # directly because the outer token is ``echo``. This is
        # ADDITIVE ONLY: a derived segment's ``allow`` is discarded (it
        # must never mask a deny/ask already found on the sub-command
        # as written), and only a derived deny/ask can raise the
        # outcome, never lower it.
        if base_settings.get("inspect_segments", True):
            for derived in segments.derive_inspection_segments(sub_command):
                hit = _first_rule_decision(
                    rules,
                    _pattern_view(derived, mask_inert_literals),
                    exempt_cats,
                    target_dir,
                    category_scopes,
                )
                if hit is None or hit[0] == "allow":
                    continue
                if outcome is None or _MORE_RESTRICTIVE[hit[0]] > _MORE_RESTRICTIVE[outcome[0]]:
                    outcome = hit

        if outcome is not None:
            decision, reason = outcome
            if decision == "allow":
                return ("allow", "", "")
            return (decision, reason, sub_command)

    # Chain-level policy runs LAST, after every segment was individually
    # allowed, so a specific rule's better replacement always wins over
    # this generic advice.
    chain_hit = _chain_policy_decision(command, base_settings.get("chain_policy") or {})
    if chain_hit is not None:
        return chain_hit

    return ("allow", "", "")


def _chain_policy_decision(command: str, policy: dict[str, Any]) -> tuple[str, str, str] | None:
    """Deny two compound-command shapes that defeat leading-token
    permission allow-lists, each with a ``Run instead:`` split the caller
    can re-run immediately.

    * ``standalone_leading_tokens``: a chain where any segment's leading
      token is a standalone-only command. Permission allow-lists match a
      command's leading token, so chaining or piping such a command
      always falls outside its allow-list entry and lands on the
      operator as an approval prompt.
    * ``deny_cd_prefix``: a chain whose first segment is ``cd``. The
      shell cwd persists between tool calls and most tools take a path
      argument, so the prefix is never needed and only defeats the
      allow-list.

    Leading-token matching only: a token inside a quoted argument never
    fires this. Disabled by default (``enabled: false``).
    """
    if not policy or not policy.get("enabled"):
        return None
    segs = [seg for seg in split_chained_commands(command) if seg]
    if len(segs) < 2:
        return None

    tokens = policy.get("standalone_leading_tokens")
    standalone_tokens = {str(t) for t in tokens} if isinstance(tokens, list) else set()
    if standalone_tokens:
        hits = [seg for seg in segs if seg.split(None, 1)[0] in standalone_tokens]
        if hits:
            return (
                "deny",
                "Chained invocation of a standalone-only command. Run it as "
                "its own call: chains and pipes fall outside leading-token "
                "permission allow-lists and land on the operator as an "
                "approval prompt.\nRun instead: " + "\n".join(hits),
                command,
            )

    if policy.get("deny_cd_prefix") and re.match(r"^cd(\s|$)", segs[0]):
        rest = [seg for seg in segs[1:] if not re.match(r"^cd(\s|$)", seg)]
        suggestion = ("\nRun instead: " + "\n".join(rest)) if rest else ""
        return (
            "deny",
            "cd-prefixed command chain. Use absolute paths or the tool's "
            "path argument; the shell cwd persists between calls if you "
            "must change directory." + suggestion,
            command,
        )

    return None


_MORE_RESTRICTIVE = {"allow": 0, "ask": 1, "deny": 2}


def _rule_in_scope(
    rule: dict[str, Any],
    target_dir: Path | None,
    category_scopes: dict[str, Any],
) -> bool:
    """Scope gating: a rule (or its whole category) can be limited to
    named directory trees via ``settings.category_scopes``.

    ``category_scopes`` maps a scope name to a list of directory
    prefixes. A rule opts in with ``scope: <name>`` (rule-level wins) or
    inherits a scope whose name equals its ``category``. A scoped rule
    fires only when the effective target dir is inside one of the
    scope's directories. Fail-closed direction: an UNKNOWN scope name or
    an unresolvable target dir keeps the rule ACTIVE, so a typo in
    settings can only over-enforce, never silently disable a rule.
    """
    scope_name = rule.get("scope") or rule.get("category")
    if not scope_name or scope_name not in category_scopes:
        return True
    dirs = category_scopes.get(scope_name) or []
    if not dirs:
        return True
    if target_dir is None:
        return True
    try:
        resolved = target_dir.expanduser().resolve()
    except OSError:
        return True
    for entry in dirs:
        try:
            prefix = Path(str(entry)).expanduser().resolve()
        except OSError:
            continue
        if resolved == prefix or prefix in resolved.parents:
            return True
    return False


def _first_rule_decision(
    rules: list[dict[str, Any]],
    pattern_command: str,
    exempt_cats: set[str],
    target_dir: Path | None,
    category_scopes: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """First-match-wins rule walk for one command form.

    Returns ``(decision, reason)`` for the first matching rule, or None
    when nothing matches. Category-gated deploy-worktree exemption: a
    matching rule whose category the operator exempted is skipped when
    the effective target is inside a ``repos/<name>`` deploy worktree,
    so other categories (filesystem catastrophes, history rewrites)
    still fire there. A rule carrying ``replacement:`` has the suggested
    command appended to its reason, so a denial teaches the sanctioned
    alternative instead of dead-ending.
    """
    for rule in rules:
        if not _rule_matches(rule, pattern_command):
            continue
        if exempt_cats and rule.get("category") in exempt_cats and _is_deploy_worktree(target_dir):
            continue
        if category_scopes and not _rule_in_scope(rule, target_dir, category_scopes):
            continue
        decision = rule.get("decision", "deny")
        if decision == "allow":
            return ("allow", "")
        reason = rule.get("reason", "Blocked by guard")
        replacement = rule.get("replacement")
        if replacement:
            reason = f"{reason}\nRun instead: {replacement}"
        return (decision, reason)
    return None


def evaluate_rules(command: str, config: dict[str, Any]) -> tuple[str, str]:
    """Legacy single-command evaluator. Preserved as a thin wrapper
    around ``evaluate_chain`` for external callers; the hook entry now
    uses ``evaluate_chain`` directly so it can track cwd across the
    whole compound command.
    """
    decision, reason, _ = evaluate_chain(command, config, None)
    return (decision, reason)
