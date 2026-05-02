"""Unit tests for stop-session-check checklist logic.

Drives the pure functions against synthetic project trees. The git
probes are exercised by initializing real (small) git repos in tmp_path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _lib import checklist


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git_run(repo: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    subprocess.run([git, *args], cwd=repo, check=True, env=_git_env(), capture_output=True)


def _git_init(repo: Path) -> None:
    """Best-effort init a temp repo with a single commit. Skip the test
    if git isn't available or the init fails."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _git_run(repo, "init", "-q", "-b", "main")
    _git_run(repo, "config", "user.email", "t@t")
    _git_run(repo, "config", "user.name", "t")
    (repo / "seed").write_text("x")
    _git_run(repo, "add", "seed")
    _git_run(repo, "commit", "-q", "-m", "seed")


class TestDetectRepoType:
    def test_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert checklist.detect_repo_type(tmp_path) == "python"

    def test_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert checklist.detect_repo_type(tmp_path) == "node"

    def test_cloudflare_worker_root(self, tmp_path: Path) -> None:
        (tmp_path / "wrangler.toml").write_text("name='x'")
        assert checklist.detect_repo_type(tmp_path) == "cloudflare-worker"

    def test_unknown(self, tmp_path: Path) -> None:
        assert checklist.detect_repo_type(tmp_path) == "unknown"


class TestHints:
    def test_pytest_hint(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        assert checklist.detect_test_hint(tmp_path, "python") == "pytest"

    def test_pnpm_test_hint(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{}")
        assert checklist.detect_test_hint(tmp_path, "node") == "pnpm test"

    def test_playwright_hint(self, tmp_path: Path) -> None:
        (tmp_path / "playwright.config.ts").write_text("")
        # Playwright detection short-circuits regardless of repo type.
        assert checklist.detect_test_hint(tmp_path, "node") == "pnpm playwright test"

    def test_no_hint_for_unknown(self, tmp_path: Path) -> None:
        assert checklist.detect_test_hint(tmp_path, "unknown") is None

    def test_deploy_hint_cloudflare(self) -> None:
        assert checklist.detect_deploy_hint("cloudflare-worker") == "wrangler deploy"

    def test_no_deploy_for_python(self) -> None:
        assert checklist.detect_deploy_hint("python") is None


class TestRecentLocalPlan:
    def test_no_plans_dir(self, tmp_path: Path) -> None:
        assert checklist.has_recent_local_plan(tmp_path) is None

    def test_old_plan_ignored(self, tmp_path: Path) -> None:
        plans = tmp_path / ".claude" / "plans"
        plans.mkdir(parents=True)
        (plans / "old.md").write_text("x")
        # Backdate to 2 hours ago.
        os.utime(plans / "old.md", (0, 0))
        assert checklist.has_recent_local_plan(tmp_path) is None

    def test_recent_plan_flagged(self, tmp_path: Path) -> None:
        plans = tmp_path / ".claude" / "plans"
        plans.mkdir(parents=True)
        (plans / "recent.md").write_text("x")
        result = checklist.has_recent_local_plan(tmp_path)
        assert result is not None
        assert result["status"] == "info"
        assert "recent.md" in result["text"]


class TestGitProbes:
    def test_not_a_repo_returns_none(self, tmp_path: Path) -> None:
        name, path = checklist.detect_repo(tmp_path)
        assert name is None and path is None

    def test_clean_repo(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        name, path = checklist.detect_repo(tmp_path)
        assert name == tmp_path.name
        assert path == tmp_path
        assert checklist.uncommitted_count(tmp_path) == 0
        branch, ahead, reason = checklist.push_status(tmp_path)
        assert branch == "main"
        # No upstream configured in test repo.
        assert reason == "no upstream"
        assert ahead is None

    def test_uncommitted_change(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "new.txt").write_text("x")
        assert checklist.uncommitted_count(tmp_path) == 1


class TestBuildChecklist:
    def test_dirty_repo_has_blocking(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "uncommitted.txt").write_text("x")
        data = checklist.build_checklist(tmp_path.name, tmp_path)
        statuses = [i["status"] for i in data["items"]]
        assert "todo" in statuses

    def test_clean_repo_no_blocking_for_committed(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        # Add and commit the pyproject after init so the working tree
        # is fully clean for the build_checklist call.
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        _git_run(tmp_path, "add", "pyproject.toml")
        _git_run(tmp_path, "commit", "-q", "-m", "add pyproject")

        data = checklist.build_checklist(tmp_path.name, tmp_path)
        commit_item = next(i for i in data["items"] if "committed" in i["text"])
        assert commit_item["status"] == "done"


class TestFormatChecklist:
    def test_blocking_header(self) -> None:
        data = {
            "repo_name": "r",
            "repo_type": "python",
            "branch": "main",
            "items": [{"status": "todo", "text": "fix this"}],
            "actions": ["do that"],
        }
        msg = checklist.format_checklist(data)
        assert "STOP" in msg
        assert "REQUIRED before stopping" in msg
        assert "do that" in msg

    def test_non_blocking_header(self) -> None:
        data = {
            "repo_name": "r",
            "repo_type": "python",
            "branch": "main",
            "items": [{"status": "info", "text": "just so you know"}],
            "actions": [],
        }
        msg = checklist.format_checklist(data)
        assert "session-end" in msg
        assert "STOP" not in msg
