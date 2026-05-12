# Stop-session-check checklist builder.
#
# Pure functions: take a Path, return checklist data. The hook entry
# point translates the checklist into a hook decision (continue with
# message, or block with reason).

from __future__ import annotations

import contextlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# How long before a plan file is considered "stale enough to ignore."
_RECENT_PLAN_WINDOW_SECONDS = 1800  # 30 min


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command and return stdout, or None on any failure."""
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError):
        result = subprocess.run(
            [git_bin, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def detect_repo(cwd: Path) -> tuple[str | None, Path | None]:
    """Return (repo_name, repo_toplevel_path) or (None, None) if not in
    a git repo."""
    toplevel = _git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if not toplevel:
        return None, None
    p = Path(toplevel)
    return p.name, p


def uncommitted_count(cwd: Path) -> int | None:
    """Count of files with any uncommitted state (staged or unstaged).
    None if the git command failed."""
    status = _git(["status", "--porcelain"], cwd=cwd)
    if status is None:
        return None
    if not status:
        return 0
    return len(status.splitlines())


def push_status(cwd: Path) -> tuple[str | None, int | None, str | None]:
    """Return (branch, ahead_count, reason)."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if not branch:
        return None, None, None

    upstream = _git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], cwd=cwd)
    if not upstream:
        return branch, None, "no upstream"

    ahead = _git(["rev-list", "--count", f"{upstream}..{branch}"], cwd=cwd)
    if ahead is None:
        return branch, None, "could not determine"
    try:
        return branch, int(ahead), None
    except ValueError:
        return branch, None, "could not parse rev-list"


def detect_repo_type(repo_path: Path) -> str:
    """Best-effort repo type from file markers. Used only to render
    test/deploy hints; never affects the blocking decision."""
    if (repo_path / "wrangler.toml").exists():
        return "cloudflare-worker"
    apps_dir = repo_path / "apps"
    if apps_dir.exists():
        for child in apps_dir.iterdir():
            if child.is_dir() and (child / "wrangler.toml").exists():
                return "cloudflare-worker"
    if (repo_path / "pyproject.toml").exists():
        return "python"
    if (repo_path / "package.json").exists():
        return "node"
    return "unknown"


def detect_test_hint(repo_path: Path, repo_type: str) -> str | None:
    if (repo_path / "playwright.config.ts").exists() or (
        repo_path / "playwright.config.js"
    ).exists():
        return "pnpm playwright test"
    if repo_type == "python" and (repo_path / "tests").exists():
        return "pytest"
    if repo_type == "node" and (repo_path / "package.json").exists():
        return "pnpm test"
    return None


def detect_deploy_hint(repo_type: str) -> str | None:
    if repo_type == "cloudflare-worker":
        return "wrangler deploy"
    return None


def has_recent_local_plan(repo_path: Path) -> dict[str, str] | None:
    """Check for plan files in `<repo>/.claude/plans/` modified within
    the recent window. Project-local only (does not look in
    ~/.claude/plans/ which would create cross-repo false positives)."""
    plans_dir = repo_path / ".claude" / "plans"
    if not plans_dir.exists():
        return None

    cutoff = time.time() - _RECENT_PLAN_WINDOW_SECONDS

    for plan_file in plans_dir.glob("*.md"):
        with contextlib.suppress(OSError):
            if plan_file.stat().st_mtime > cutoff:
                return {
                    "status": "info",
                    "text": (
                        f"Recent plan file detected ({plan_file.name}); "
                        "if still in plan mode, exit it before stopping."
                    ),
                }
    return None


def build_checklist(repo_name: str, repo_path: Path) -> dict[str, Any]:
    """Build the checklist data dict. Items have a status of `done`,
    `nudge`, `info`, or `unknown`.

    The hook never blocks on these — repo state is shared across every
    Claude session in the working tree, so a `nudge` ("3 commits ahead
    of remote") may belong to *another* session. Items are advisory."""
    repo_type = detect_repo_type(repo_path)
    deploy_hint = detect_deploy_hint(repo_type)
    test_hint = detect_test_hint(repo_path, repo_type)

    items: list[dict[str, str]] = []
    actions: list[str] = []

    # 1. Committed?
    uncommitted = uncommitted_count(repo_path)
    if uncommitted is None:
        items.append({"status": "unknown", "text": "Could not check commit status"})
    elif uncommitted == 0:
        items.append({"status": "done", "text": "All changes committed"})
    else:
        items.append(
            {
                "status": "nudge",
                "text": (
                    f"{uncommitted} uncommitted file(s) in the working tree "
                    "(could be yours or another session's)"
                ),
            }
        )
        actions.append("If any of your work is uncommitted, commit it before stopping")

    # 2. Pushed?
    branch, ahead, reason = push_status(repo_path)
    if branch is None:
        items.append({"status": "unknown", "text": "Could not check push status"})
    elif reason:
        items.append({"status": "nudge", "text": f"Branch `{branch}` not pushed ({reason})"})
        actions.append(f"Push branch `{branch}` when you're ready")
    elif ahead and ahead > 0:
        items.append(
            {
                "status": "nudge",
                "text": (
                    f"Branch `{branch}` is {ahead} commit(s) ahead of remote "
                    "(may include other sessions' commits)"
                ),
            }
        )
        actions.append(f"Push branch `{branch}` when you're ready")
    else:
        items.append({"status": "done", "text": f"Branch `{branch}` is up to date with remote"})

    # 3. Deploy hint (info only)
    if deploy_hint:
        items.append({"status": "info", "text": f"Consider deploy: `{deploy_hint}`"})

    # 4. Test hint (info only)
    if test_hint:
        items.append({"status": "info", "text": f"Run tests? (`{test_hint}`)"})
        actions.append(f"Run tests: {test_hint}")

    # 5. Recent local plan files (info only)
    recent_plan = has_recent_local_plan(repo_path)
    if recent_plan:
        items.append(recent_plan)

    return {
        "repo_name": repo_name,
        "repo_type": repo_type,
        "branch": branch,
        "items": items,
        "actions": actions,
    }


def format_checklist(data: dict[str, Any]) -> str:
    """Render checklist data as a human-readable advisory message."""
    items = data["items"]
    actions = data["actions"]

    lines = []
    for item in items:
        marker = "[x]" if item["status"] == "done" else "[ ]"
        lines.append(f"{marker} {item['text']}")
    checklist = "\n".join(lines)

    header = f"[session-end] Completion checklist for `{data['repo_name']}` ({data['repo_type']}):"
    message = f"{header}\n{checklist}\n"
    if actions:
        message += "\nSuggested next steps:\n" + "\n".join(f"  - {a}" for a in actions)
    return message
