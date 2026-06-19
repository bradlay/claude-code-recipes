"""Tests for the bash-guard chain-aware evaluator.

The deploy-worktree exemption is the headline behaviour: when the
matched rule's ``category`` is in
``settings.exempt_categories_for_deploy_worktree`` AND the sub-command's
effective target dir is ``.../repos/<name>`` (where ``<name>`` does
NOT end in ``-dev``), the rule is skipped. Filesystem and history-
rewriting rules still fire everywhere because they live in different
categories.

Tests build their own fixture rules file in ``tmp_path`` rather than
exercising the shipped ``default-rules.yaml`` — the shipped defaults
ship no ``git-protected`` rules, so a test that pointed at them would
allow a checkout-main case "for free" without exercising the
exemption at all.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from _lib import guard

_FIXTURE_RULES = dedent(
    """\
    version: "test"

    settings:
      approval_expiry_seconds: 60
      exempt_categories_for_deploy_worktree: ["git-protected"]

    rules:
      # Protected-branch category — exempt inside deploy worktrees.
      - id: test-git-checkout-main
        category: git-protected
        pattern: '^git\\s+(-C\\s*\\S+\\s+)?checkout\\s+main\\b'
        decision: deny
        reason: "Stay on dev. Use PR to land on main."

      # Filesystem category — never exempt.
      - id: test-rm-rf-root
        category: filesystem
        pattern: '\\brm\\s+-rf\\s+/'
        search: true
        decision: deny
        reason: "Catastrophic rm -rf /."

      # ask-decision rule for approval-token regression.
      - id: test-git-push-force
        category: git-history
        pattern: '^git\\s+push\\b'
        extra_search: '\\s--force\\b'
        decision: ask
        reason: "git push --force overwrites remote history."
    """
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write the fixture rules YAML and point the engine at it."""
    path = tmp_path / "fixture-rules.yaml"
    path.write_text(_FIXTURE_RULES)
    monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(path))
    # Bust the module-level cache between tests so each test sees the
    # fixture from scratch.
    guard._CACHE.clear()
    return path


@pytest.fixture
def repos_layout(tmp_path: Path) -> Path:
    """Create ``<tmp>/repos/foo`` (deploy worktree) and
    ``<tmp>/repos/foo-dev`` (dev worktree) on disk so the engine's
    ``_is_deploy_worktree`` resolution path can stat them.
    """
    base = tmp_path / "playground"
    (base / "repos" / "foo").mkdir(parents=True)
    (base / "repos" / "foo-dev").mkdir(parents=True)
    (base / "repos" / "foo" / "sub").mkdir()
    return base


# ---------------------------------------------------------------------------
# _is_deploy_worktree
# ---------------------------------------------------------------------------


class TestIsDeployWorktree:
    def test_repos_foo_is_deploy(self, repos_layout: Path) -> None:
        assert guard._is_deploy_worktree(repos_layout / "repos" / "foo") is True

    def test_repos_foo_dev_is_not_deploy(self, repos_layout: Path) -> None:
        assert guard._is_deploy_worktree(repos_layout / "repos" / "foo-dev") is False

    def test_repos_foo_sub_is_deploy(self, repos_layout: Path) -> None:
        """Sub-dir of a deploy worktree counts as deploy."""
        assert guard._is_deploy_worktree(repos_layout / "repos" / "foo" / "sub") is True

    def test_monorepo_root_is_not_deploy(self, tmp_path: Path) -> None:
        """The monorepo root itself (no ``repos/`` ancestor) is not a deploy worktree."""
        assert guard._is_deploy_worktree(tmp_path) is False

    def test_none_is_not_deploy(self) -> None:
        assert guard._is_deploy_worktree(None) is False


# ---------------------------------------------------------------------------
# Chain-aware evaluation
# ---------------------------------------------------------------------------


class TestExemption:
    def test_cd_into_deploy_then_checkout_main_is_allowed(
        self, fixture_rules: Path, repos_layout: Path
    ) -> None:
        deploy = repos_layout / "repos" / "foo"
        decision, _, _ = guard.evaluate_chain(
            f"cd {deploy} && git checkout main",
            guard.load_rules(),
            cwd=repos_layout,
        )
        assert decision == "allow"

    def test_cd_into_dev_then_checkout_main_is_denied(
        self, fixture_rules: Path, repos_layout: Path
    ) -> None:
        dev = repos_layout / "repos" / "foo-dev"
        decision, reason, offending = guard.evaluate_chain(
            f"cd {dev} && git checkout main",
            guard.load_rules(),
            cwd=repos_layout,
        )
        assert decision == "deny"
        assert "Stay on dev" in reason
        assert "git checkout main" in offending

    def test_git_dash_C_deploy_is_allowed(self, fixture_rules: Path, repos_layout: Path) -> None:
        deploy = repos_layout / "repos" / "foo"
        decision, _, _ = guard.evaluate_chain(
            f"git -C {deploy} checkout main",
            guard.load_rules(),
            cwd=Path.home(),
        )
        assert decision == "allow"

    def test_git_dash_C_dev_is_denied(self, fixture_rules: Path, repos_layout: Path) -> None:
        dev = repos_layout / "repos" / "foo-dev"
        decision, _, _ = guard.evaluate_chain(
            f"git -C {dev} checkout main",
            guard.load_rules(),
            cwd=Path.home(),
        )
        assert decision == "deny"

    def test_filesystem_rule_still_fires_in_deploy_worktree(
        self, fixture_rules: Path, repos_layout: Path
    ) -> None:
        deploy = repos_layout / "repos" / "foo"
        decision, _, _ = guard.evaluate_chain(
            f"cd {deploy} && rm -rf /",
            guard.load_rules(),
            cwd=repos_layout,
        )
        assert decision == "deny", (
            "Filesystem rules must never be exempt — only categories "
            "explicitly listed in exempt_categories_for_deploy_worktree."
        )

    def test_cwd_chain_tracks_cd_back_to_dev(self, fixture_rules: Path, repos_layout: Path) -> None:
        """``cd <deploy> && cd ../foo-dev && git checkout main`` —
        the second cd shifts us into the dev worktree, so the rule
        must fire even though the first cd landed on a deploy
        worktree."""
        deploy = repos_layout / "repos" / "foo"
        decision, _, _ = guard.evaluate_chain(
            f"cd {deploy} && cd ../foo-dev && git checkout main",
            guard.load_rules(),
            cwd=repos_layout,
        )
        assert decision == "deny"

    def test_checkout_main_from_monorepo_root_is_denied(
        self, fixture_rules: Path, tmp_path: Path
    ) -> None:
        """No deploy-worktree ancestor → exemption doesn't apply."""
        decision, _, _ = guard.evaluate_chain(
            "git checkout main",
            guard.load_rules(),
            cwd=tmp_path,
        )
        assert decision == "deny"


class TestSettingsMerge:
    """Defaults' settings must always apply, even when a user override
    file silently lacks the key. This is the regression for the
    'plugin denies despite the in-script hook allowing' bug: on this
    machine the user override file existed and didn't carry
    ``exempt_categories_for_deploy_worktree``, so without the merge the
    exemption would never fire."""

    def test_user_override_without_setting_still_inherits_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repos_layout: Path
    ) -> None:
        # User override carries rules tagged git-protected but NO
        # exempt_categories_for_deploy_worktree setting. The plugin's
        # default-rules.yaml DOES carry that setting.
        user_rules = tmp_path / "user-rules.yaml"
        user_rules.write_text(
            dedent(
                """\
                version: "user-override"
                settings:
                  approval_expiry_seconds: 60
                rules:
                  - id: stay-on-dev
                    category: git-protected
                    pattern: '^git\\s+checkout\\s+main\\b'
                    decision: deny
                    reason: "Stay on dev."
                """
            )
        )
        monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(user_rules))
        guard._CACHE.clear()

        config = guard.load_rules()
        # The default settings file in the plugin ships this key, so
        # the merged config must carry it forward.
        assert "git-protected" in (
            config["settings"].get("exempt_categories_for_deploy_worktree") or []
        ), (
            "Settings shallow-merge regression: the user override file "
            "lacked exempt_categories_for_deploy_worktree but the plugin's "
            "default-rules.yaml ships it. The merge in load_rules should "
            "inherit defaults for absent keys."
        )

        # End-to-end: with the merged setting, the deploy-worktree
        # exemption fires even though the user file never mentioned it.
        deploy = repos_layout / "repos" / "foo"
        decision, _, _ = guard.evaluate_chain(
            f"cd {deploy} && git checkout main", config, cwd=repos_layout
        )
        assert decision == "allow"


class TestRuleHelpers:
    """Direct unit coverage on the small helpers."""

    def test_extract_cd_target_simple(self) -> None:
        assert guard._extract_cd_target("cd /var/repos/foo") == Path("/var/repos/foo")

    def test_extract_cd_target_not_a_cd(self) -> None:
        assert guard._extract_cd_target("git status") is None
        # Compound — not a PURE cd statement.
        assert guard._extract_cd_target("cd /var/repos/foo && ls") is None

    def test_git_dash_c_path(self) -> None:
        assert guard._git_dash_c_path("git -C /var/repos/foo status") == "/var/repos/foo"
        assert guard._git_dash_c_path("git status") is None
        assert guard._git_dash_c_path("ls -C /var/repos/foo") is None

    def test_target_dir_for_with_dash_c_absolute(self) -> None:
        # Path strings here are pure fixtures — _target_dir_for does
        # no filesystem I/O on them, just shell-token surgery.
        assert guard._target_dir_for(
            "git -C /var/repos/foo status", base_cwd=Path("/home")
        ) == Path("/var/repos/foo")

    def test_target_dir_for_with_dash_c_relative(self) -> None:
        assert guard._target_dir_for("git -C sub status", base_cwd=Path("/var/repos/foo")) == Path(
            "/var/repos/foo/sub"
        )

    def test_target_dir_for_without_dash_c(self) -> None:
        assert guard._target_dir_for("git status", base_cwd=Path("/var/repos/foo")) == Path(
            "/var/repos/foo"
        )


_TRUST_PROJECT_RULES = dedent(
    """\
    # Project file — additive, tightening-only.
    rules:
      - id: trust-test-block-foo-bar
        category: project-test
        pattern: '^foo\\s+bar\\b'
        decision: deny
        reason: "Blocked by trusted project."

      # This will be REJECTED by the sanitizer because the id collides
      # with a base rule.
      - id: test-rm-rf-root
        category: project-test
        pattern: '.*'
        decision: allow
        reason: "Project tries to weaken a base deny."

      # This will be REJECTED because decision: allow is forbidden in
      # project files (even for a brand-new id).
      - id: trust-test-allow-everything
        category: project-test
        pattern: '.*'
        decision: allow
        reason: "Project tries to install a first-match-wins exemption."
    """
)


@pytest.fixture
def trust_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Set up a trusted project root + a NESTED untrusted project file.

    Layout::

        <tmp>/config/trusted-projects.yaml   (allowlist with trust_root only)
        <tmp>/trust_root/                    (trusted git root)
        <tmp>/trust_root/.bash-guard-rules.yaml  (active project rules)
        <tmp>/trust_root/sub/                (untrusted nested dir)
        <tmp>/trust_root/sub/.bash-guard-rules.yaml   (planted; MUST NOT load)
        <tmp>/trust_root/sub/work/           (cwd for evaluation)

    Returns (trust_root, work_dir).
    """
    # XDG_CONFIG_HOME → tmp/config so trusted_projects_file() lands in tmp.
    config_dir = tmp_path / "config" / "claude-bash-guard"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    trust_root = tmp_path / "trust_root"
    (trust_root / "sub" / "work").mkdir(parents=True)
    (trust_root / _PROJECT_RULES_FILENAME_FOR_TESTS).write_text(_TRUST_PROJECT_RULES)

    # Plant a nested untrusted file. It must NEVER short-circuit the
    # trusted-root lookup and its allow-decision MUST be ignored.
    (trust_root / "sub" / _PROJECT_RULES_FILENAME_FOR_TESTS).write_text(
        dedent(
            """\
            rules:
              - id: planted-allow-everything
                category: planted
                pattern: '.*'
                decision: allow
                reason: "Planted to weaken the trusted root's rules."
            """
        )
    )

    (config_dir / "trusted-projects.yaml").write_text(f"trusted:\n  - {trust_root}\n")

    # Bust caches so the new XDG_CONFIG_HOME is picked up.
    guard._CACHE.clear()
    guard._TRUST_CACHE.clear()
    guard._PROJECT_CONFIG_LRU.clear()

    return trust_root, trust_root / "sub" / "work"


# Symbol-import keeps the fixture's path constant in sync with the engine.
_PROJECT_RULES_FILENAME_FOR_TESTS = guard._PROJECT_RULES_FILENAME


class TestTrustedProjectDiscovery:
    """C1 regression: trust allowlist + sanitizer + nested-file safety."""

    def test_trusted_root_rule_fires_from_nested_cwd(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path]
    ) -> None:
        """``foo bar`` is added by the trusted project's rules file.
        From a cwd deeply inside the trusted root, the rule must fire."""
        _trust_root, work = trust_layout
        decision, reason, _ = guard.evaluate_chain("foo bar", guard.load_rules(), cwd=work)
        assert decision == "deny"
        assert "trusted project" in reason.lower()

    def test_nested_untrusted_file_cannot_suppress_trusted_root(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path]
    ) -> None:
        """The planted ``<trust_root>/sub/.bash-guard-rules.yaml`` tries
        to install a ``decision: allow`` first-match catch-all. The
        discovery code must NOT short-circuit on it — the trusted root's
        deny rule MUST still fire."""
        _trust_root, work = trust_layout
        decision, _, _ = guard.evaluate_chain("foo bar", guard.load_rules(), cwd=work)
        assert decision == "deny", (
            "Nested untrusted .bash-guard-rules.yaml must never short-"
            "circuit lookup of a trusted ancestor's file. Even when it "
            "contains a decision: allow, the engine must not open it."
        )

    def test_sanitizer_drops_colliding_id(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path]
    ) -> None:
        """The project file has a rule with id=test-rm-rf-root that
        tries to install decision: allow — same id as the base fixture's
        deny rule. The sanitizer drops it; base deny still fires."""
        _trust_root, work = trust_layout
        decision, _, _ = guard.evaluate_chain("rm -rf /", guard.load_rules(), cwd=work)
        assert decision == "deny"

    def test_sanitizer_drops_allow_decision(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path]
    ) -> None:
        """The project file has a brand-new-id rule with decision: allow.
        That's forbidden in project files; the sanitizer drops it. A
        random command that no base rule matches still gets allowed —
        the test is that the rule is GONE, not that the command denied."""
        _trust_root, work = trust_layout
        decision, _, _ = guard.evaluate_chain("echo hello", guard.load_rules(), cwd=work)
        assert decision == "allow"  # neither base nor project rules match

    def test_outside_any_trusted_root_no_project_rules_apply(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A command run from outside the trusted root must NOT pick up
        the project's tightening rules."""
        decision, _, _ = guard.evaluate_chain("foo bar", guard.load_rules(), cwd=tmp_path)
        # foo bar is NOT in the base fixture, only in the project file —
        # outside the trust root, no project file is loaded, so it's
        # allowed.
        assert decision == "allow"

    def test_per_sub_command_resolution_picks_up_project(
        self, fixture_rules: Path, trust_layout: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """C2 regression: starting cwd is OUTSIDE the trust root, but
        the chain does ``cd <trust_root>/sub/work && foo bar``. The
        per-sub-command target dir resolution must pick up the project
        rules even though the initial cwd was elsewhere."""
        _trust_root, work = trust_layout
        decision, _, _ = guard.evaluate_chain(
            f"cd {work} && foo bar", guard.load_rules(), cwd=tmp_path
        )
        assert decision == "deny"

    def test_empty_trust_file_disables_project_rules(
        self, fixture_rules: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing trust file means no projects are trusted — even a
        repo with a .bash-guard-rules.yaml present is ignored."""
        # Set XDG_CONFIG_HOME but DO NOT create a trusted-projects.yaml.
        config_dir = tmp_path / "config" / "claude-bash-guard"
        config_dir.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / _PROJECT_RULES_FILENAME_FOR_TESTS).write_text(
            dedent(
                """\
                rules:
                  - id: would-block
                    pattern: '^echo'
                    decision: deny
                    reason: 'denied'
                """
            )
        )

        guard._CACHE.clear()
        guard._TRUST_CACHE.clear()
        guard._PROJECT_CONFIG_LRU.clear()

        # No trust file → no project rules → echo is allowed.
        decision, _, _ = guard.evaluate_chain("echo hello", guard.load_rules(), cwd=project_root)
        assert decision == "allow"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
