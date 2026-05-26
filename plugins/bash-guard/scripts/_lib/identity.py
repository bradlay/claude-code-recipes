"""Identity-binding feature for the bash-guard.

The full bash-guard rules engine is regex-only; this module does the
part regex cannot — actually probing ``gh api /user`` with the active
``GH_TOKEN`` and resolving the target repo owner from URL args, the
configured remote, or ``--repo`` flags.

Activation is opt-in via ``settings.identity_bindings`` in the user
override (or default) rules file::

    settings:
      identity_bindings:
        sddcinfo: [sddcinfo]
        bradlay: [bradlay]

When the setting is absent the feature is off and ``check_identity``
returns ``None`` immediately. When the setting is present, write-
targeting ``git push`` / write-flavored ``gh`` commands are checked:
if the token authenticates as a login whose allowed owners do NOT
include the target, return a deny reason. The check runs BEFORE rule
evaluation in ``evaluate_chain`` so an ``ask`` rule that the user has
already approved cannot leak a cross-owner write past the binding.

Fail-open posture: any error while probing identity (``gh`` missing,
``gh api /user`` failure, network down) returns ``None`` so legit
offline work isn't blocked. Destructive-op rules in the regex engine
are unaffected.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import paths

_CACHE_TTL_SECONDS = 60.0
_GH_API_TIMEOUT = 5.0
_GIT_TIMEOUT = 3.0

# Which `gh` subcommand actions count as writes.
_WRITE_GH_SUBCOMMANDS: dict[str, set[str]] = {
    "repo": {"create", "edit", "delete", "rename", "archive", "deploy-key", "fork", "sync"},
    "pr": {"create", "edit", "merge", "close", "reopen", "comment", "review", "ready"},
    "issue": {
        "create",
        "edit",
        "close",
        "reopen",
        "comment",
        "transfer",
        "delete",
        "pin",
        "unpin",
    },
    "release": {"create", "edit", "delete", "upload"},
    "workflow": {"run", "enable", "disable"},
    "secret": {"set", "delete"},
    "variable": {"set", "delete"},
    "label": {"create", "edit", "delete", "clone"},
    "gist": {"create", "edit", "delete"},
    "ssh-key": {"add", "delete"},
    "gpg-key": {"add", "delete"},
}

_WRITE_GH_API_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _identity_cache_dir() -> Path:
    return paths.data_dir() / "identity"


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _split_inline_env(command: str) -> tuple[dict[str, str], str]:
    """Peel ``VAR=value`` prefixes off a command. Returns (env, rest)."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return {}, command
    env: dict[str, str] = {}
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        key, value = tokens[i].split("=", 1)
        env[key] = value
        i += 1
    return env, " ".join(shlex.quote(t) for t in tokens[i:])


def _resolve_remote_url(remote: str, cwd: Path | None) -> str | None:
    """Look up ``remote.<name>.url`` via ``git config`` inside *cwd*."""
    cwd_str = str(cwd) if cwd else str(Path.cwd())
    try:
        # ruff S607: ``git`` is resolved via PATH on purpose — operators
        # may use mise/asdf/conda-managed gits, and pinning an absolute
        # path here would break those.
        result = subprocess.run(
            ["git", "-C", cwd_str, "config", "--get", f"remote.{remote}.url"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _owner_from_url(url: str) -> str | None:
    """Extract ``<owner>`` from any common GitHub URL form."""
    match = re.search(
        r"(?:github\.com[:/]|^|\s)([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+(?:\.git)?$",
        url,
    )
    return match.group(1).lower() if match else None


def _detect_git_target(command: str, cwd: Path | None) -> tuple[str | None, bool]:
    """For a ``git push`` command, return ``(owner, is_write)``.

    Only ``push`` is currently treated as a write — ``fetch``/``pull``
    are read-only against the remote even if they modify the local
    working tree, and the regex engine already covers anything
    destructive like ``push --force``.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, False
    if not tokens or tokens[0] != "git":
        return None, False
    sub_idx = 1
    while sub_idx < len(tokens) and tokens[sub_idx].startswith("-"):
        if tokens[sub_idx] == "-C" and sub_idx + 1 < len(tokens):
            cwd = Path(tokens[sub_idx + 1])
            sub_idx += 2
            continue
        sub_idx += 1
    if sub_idx >= len(tokens) or tokens[sub_idx] != "push":
        return None, False
    args = [t for t in tokens[sub_idx + 1 :] if not t.startswith("-")]
    if args:
        first = args[0]
        if "://" in first or first.startswith("git@") or "@" in first[:60]:
            owner = _owner_from_url(first)
            if owner:
                return owner, True
        url = _resolve_remote_url(first, cwd)
        if url:
            return _owner_from_url(url), True
    url = _resolve_remote_url("origin", cwd)
    return (_owner_from_url(url) if url else None), True


def _detect_gh_target(command: str, cwd: Path | None) -> tuple[str | None, bool]:
    """For a ``gh`` command, return ``(owner, is_write)``."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, False
    if not tokens or tokens[0] != "gh" or len(tokens) < 2:
        return None, False
    sub = tokens[1]
    if sub == "api":
        method = "GET"
        path_arg: str | None = None
        i = 2
        while i < len(tokens):
            tok = tokens[i]
            if tok in {"-X", "--method"} and i + 1 < len(tokens):
                method = tokens[i + 1].upper()
                i += 2
                continue
            if tok.startswith("--method="):
                method = tok.split("=", 1)[1].upper()
                i += 1
                continue
            if not tok.startswith("-") and path_arg is None:
                path_arg = tok
            i += 1
        owner = None
        if path_arg:
            match = re.match(r"^/?(?:repos|users|orgs)/([A-Za-z0-9][A-Za-z0-9-]{0,38})", path_arg)
            owner = match.group(1).lower() if match else None
        return owner, method in _WRITE_GH_API_METHODS

    write_actions = _WRITE_GH_SUBCOMMANDS.get(sub, set())
    is_write = any(token in write_actions for token in tokens[2:])
    if not is_write:
        return None, False

    repo_owner: str | None = None
    i = 2
    while i < len(tokens):
        token = tokens[i]
        value: str | None = None
        if token in {"-R", "--repo"} and i + 1 < len(tokens):
            value = tokens[i + 1]
        elif token.startswith("--repo="):
            value = token.split("=", 1)[1]
        if value:
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})/", value)
            repo_owner = match.group(1).lower() if match else None
            break
        i += 1
    if repo_owner is None:
        url = _resolve_remote_url("origin", cwd)
        repo_owner = _owner_from_url(url) if url else None
    return repo_owner, True


def _identity_for_token(token: str) -> str | None:
    """Return the GitHub login the token authenticates as. 60s file cache."""
    cache_dir = _identity_cache_dir()
    fingerprint = _token_fingerprint(token)
    cache_file = cache_dir / f"{fingerprint}.json"

    with contextlib.suppress(OSError, json.JSONDecodeError):
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < _CACHE_TTL_SECONDS:
            data = json.loads(cache_file.read_text())
            login = data.get("login")
            return login.lower() if isinstance(login, str) else None

    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    try:
        # ruff S607: ``gh`` is resolved via PATH for the same reason
        # as the git lookup above — operator-managed installs.
        result = subprocess.run(
            ["gh", "api", "/user", "--jq", ".login"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_GH_API_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    login = result.stdout.strip().lower()
    if not login:
        return None
    with contextlib.suppress(OSError):
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_file.write_text(json.dumps({"login": login, "ts": time.time()}))
        cache_file.chmod(0o600)
    return login


def check_identity(
    sub_command: str,
    cwd: Path | None,
    bindings: dict[str, list[str]] | None,
) -> str | None:
    """Return a deny reason if *sub_command* is a cross-owner write
    under the active ``GH_TOKEN``, else None.

    *bindings* maps a GitHub login → list of owners it's allowed to
    write to. An empty / missing bindings dict disables the feature
    (returns None).

    Fail-open for inability-to-enforce: missing token, missing ``gh``,
    failed ``gh api /user``, or unresolvable target owner all return
    None — the destructive-op rules already in the regex engine handle
    everything we can decide statically.
    """
    if not bindings:
        return None

    inline_env, rest = _split_inline_env(sub_command)
    target_owner: str | None = None
    is_write = False

    if re.match(r"^\s*git\b", rest):
        target_owner, is_write = _detect_git_target(rest, cwd)
    elif re.match(r"^\s*gh\b", rest):
        target_owner, is_write = _detect_gh_target(rest, cwd)

    if not is_write or target_owner is None:
        return None

    token = (
        inline_env.get("GH_TOKEN")
        or inline_env.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not token:
        return None

    login = _identity_for_token(token)
    if not login:
        return None

    allowed_owners = set(bindings.get(login) or [login])
    if target_owner in allowed_owners:
        return None

    target_hint = target_owner.upper()
    return (
        f"FORBIDDEN: GH_TOKEN authenticates as '{login}', but the command targets "
        f"'{target_owner}/...'. Tokens for '{login}' may only write to "
        f"{sorted(allowed_owners)}. Use the matching token, for example: "
        f"GH_TOKEN=$(./scripts/decrypt-creds.sh | "
        f"awk -F= '/^{target_hint}_GH_TOKEN=/{{print $2}}') <command>"
    )
