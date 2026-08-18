"""Tests for the rule-schema extensions: ``replacement:`` on rules and
``scope:`` / ``settings.category_scopes`` directory gating.

Fail-closed property under test for scoping: an unknown scope name or
an unresolvable target keeps the rule ACTIVE. A typo in settings can
only over-enforce, never silently disable a rule.
"""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent

import pytest

from _lib import guard


def _config_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> dict:
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(path))
    guard._CACHE.clear()
    return guard.load_rules()


class TestReplacement:
    def test_replacement_is_appended_to_deny_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                """\
                version: "t"
                settings: {}
                rules:
                  - id: bare-commit
                    pattern: '^git\\s+commit\\b'
                    decision: deny
                    reason: "Use the wrapper."
                    replacement: "repo-tool commit <paths>"
                """
            ),
        )
        decision, reason, _ = guard.evaluate_chain("git commit -m x", config, None)
        assert decision == "deny"
        assert "Run instead: repo-tool commit <paths>" in reason

    def test_rule_without_replacement_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                """\
                version: "t"
                settings: {}
                rules:
                  - id: bare-commit
                    pattern: '^git\\s+commit\\b'
                    decision: deny
                    reason: "Use the wrapper."
                """
            ),
        )
        _, reason, _ = guard.evaluate_chain("git commit -m x", config, None)
        assert "Run instead" not in reason


_PRINT_POLICY_RULES = dedent(
    """\
    version: "t"
    settings:
      print_policies:
        - program: democli
          opt_in_env: DEMOCLI_PRINT_OK
          recursion_env: DEMOCLI_IN_PRINT
          fallback_hint: "Use the interactive mode."
    rules: []
    """
)


class TestPrintPolicies:
    def test_print_invocation_default_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEMOCLI_PRINT_OK", raising=False)
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, reason, _ = guard.evaluate_chain("democli -p hello", config, None)
        assert decision == "deny"
        assert "DEMOCLI_PRINT_OK" in reason

    def test_print_in_later_chain_segment_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEMOCLI_PRINT_OK", raising=False)
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, _, _ = guard.evaluate_chain("echo ok && democli -p x", config, None)
        assert decision == "deny"

    def test_wrapped_shell_c_print_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEMOCLI_PRINT_OK", raising=False)
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, _, _ = guard.evaluate_chain(
            "sh -c 'exec \"$@\"' _ democli -p x", config, None
        )
        assert decision == "deny"

    def test_opt_in_env_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEMOCLI_PRINT_OK", "1")
        monkeypatch.delenv("DEMOCLI_IN_PRINT", raising=False)
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, _, _ = guard.evaluate_chain("democli -p hello", config, None)
        assert decision == "allow"

    def test_recursion_sentinel_denies_even_with_opt_in(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEMOCLI_PRINT_OK", "1")
        monkeypatch.setenv("DEMOCLI_IN_PRINT", "1")
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, reason, _ = guard.evaluate_chain("democli -p hello", config, None)
        assert decision == "deny"
        assert "recursi" in reason.lower()

    def test_non_print_invocation_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DEMOCLI_PRINT_OK", raising=False)
        config = _config_from(tmp_path, monkeypatch, _PRINT_POLICY_RULES)
        decision, _, _ = guard.evaluate_chain("democli status", config, None)
        assert decision == "allow"


_CHAIN_POLICY_RULES = dedent(
    """\
    version: "t"
    settings:
      chain_policy:
        enabled: true
        standalone_leading_tokens: [mytool]
        deny_cd_prefix: true
    rules: []
    """
)


class TestChainPolicy:
    def test_chained_standalone_tool_denied_with_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _CHAIN_POLICY_RULES)
        decision, reason, _ = guard.evaluate_chain(
            "mytool status && echo done", config, None
        )
        assert decision == "deny"
        assert "Run instead: mytool status" in reason

    def test_standalone_tool_alone_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _CHAIN_POLICY_RULES)
        assert guard.evaluate_chain("mytool status", config, None)[0] == "allow"

    def test_cd_prefix_denied_with_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _CHAIN_POLICY_RULES)
        decision, reason, _ = guard.evaluate_chain(
            "cd /tmp && ls -la", config, None
        )
        assert decision == "deny"
        assert "Run instead: ls -la" in reason

    def test_quoted_mention_does_not_fire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _CHAIN_POLICY_RULES)
        decision, _, _ = guard.evaluate_chain(
            "echo 'mytool status && stuff'", config, None
        )
        assert decision == "allow"

    def test_disabled_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            'version: "t"\nsettings: {}\nrules: []\n',
        )
        decision, _, _ = guard.evaluate_chain(
            "cd /tmp && ls -la", config, None
        )
        assert decision == "allow"

    def test_specific_rule_wins_over_chain_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = _CHAIN_POLICY_RULES.replace(
            "rules: []",
            dedent(
                """\
                rules:
                  - id: stash
                    pattern: '^git\\s+stash\\b'
                    decision: deny
                    reason: "Specific: stash discards work."
                """
            ),
        )
        config = _config_from(tmp_path, monkeypatch, text)
        decision, reason, _ = guard.evaluate_chain(
            "git stash && mytool status", config, None
        )
        assert decision == "deny"
        assert "Specific" in reason


class TestAtomicApproval:
    def test_token_single_use_under_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        from _lib import paths as paths_mod

        approvals = paths_mod.approvals_dir()
        approvals.mkdir(parents=True, exist_ok=True)
        guard.write_approval("git push --force origin x")

        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            got = guard.consume_approval("git push --force origin x", 60)
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1

    def test_expired_token_not_consumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os as _os

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        from _lib import paths as paths_mod

        approvals = paths_mod.approvals_dir()
        approvals.mkdir(parents=True, exist_ok=True)
        guard.write_approval("git push --force origin y")
        token = approvals / guard._command_hash("git push --force origin y")
        old = time.time() - 3600
        _os.utime(token, (old, old))
        assert guard.consume_approval("git push --force origin y", 60) is False


_SCOPED_RULES = dedent(
    """\
    version: "t"
    settings:
      category_scopes:
        managed: ["{managed_dir}"]
    rules:
      - id: scoped-stash
        category: managed
        pattern: '^git\\s+stash\\b'
        decision: deny
        reason: "No stash inside managed trees."
      - id: everywhere-rm
        category: filesystem
        pattern: '\\brm\\s+-rf\\s+/'
        search: true
        decision: deny
        reason: "Catastrophic."
    """
)


class TestCategoryScopes:
    def test_scoped_rule_fires_inside_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        managed.mkdir()
        config = _config_from(
            tmp_path, monkeypatch, _SCOPED_RULES.format(managed_dir=managed)
        )
        decision, _, _ = guard.evaluate_chain("git stash", config, managed)
        assert decision == "deny"

    def test_scoped_rule_silent_outside_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        elsewhere = tmp_path / "elsewhere"
        managed.mkdir()
        elsewhere.mkdir()
        config = _config_from(
            tmp_path, monkeypatch, _SCOPED_RULES.format(managed_dir=managed)
        )
        decision, _, _ = guard.evaluate_chain("git stash", config, elsewhere)
        assert decision == "allow"

    def test_unscoped_category_fires_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        elsewhere = tmp_path / "elsewhere"
        managed.mkdir()
        elsewhere.mkdir()
        config = _config_from(
            tmp_path, monkeypatch, _SCOPED_RULES.format(managed_dir=managed)
        )
        decision, _, _ = guard.evaluate_chain("rm -rf /tmp/x", config, elsewhere)
        assert decision == "deny"

    def test_unknown_scope_name_keeps_rule_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                """\
                version: "t"
                settings:
                  category_scopes: {}
                rules:
                  - id: scoped-stash
                    category: managed
                    scope: no-such-scope
                    pattern: '^git\\s+stash\\b'
                    decision: deny
                    reason: "No stash."
                """
            ),
        )
        decision, _, _ = guard.evaluate_chain("git stash", config, tmp_path)
        assert decision == "deny"

    def test_none_target_dir_keeps_scoped_rule_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        managed.mkdir()
        config = _config_from(
            tmp_path, monkeypatch, _SCOPED_RULES.format(managed_dir=managed)
        )
        decision, _, _ = guard.evaluate_chain("git stash", config, None)
        assert decision == "deny"

    def test_placeholder_expands_from_list_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                """\
                version: "t"
                settings:
                  protected_branches: [main, master]
                rules:
                  - id: checkout-protected
                    pattern: '^git\\s+checkout\\s+({protected_branches})\\b'
                    decision: deny
                    reason: "Stay off protected branches."
                """
            ),
        )
        assert guard.evaluate_chain("git checkout main", config, None)[0] == "deny"
        assert guard.evaluate_chain("git checkout master", config, None)[0] == "deny"
        assert guard.evaluate_chain("git checkout feature", config, None)[0] == "allow"

    def test_unknown_placeholder_left_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                """\
                version: "t"
                settings: {}
                rules:
                  - id: bad-placeholder
                    pattern: '^git\\s+checkout\\s+({no_such_setting})\\b'
                    decision: deny
                    reason: "x"
                """
            ),
        )
        assert guard.evaluate_chain("git checkout main", config, None)[0] == "allow"

    def test_rule_level_scope_overrides_category(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        managed = tmp_path / "managed"
        other = tmp_path / "other"
        managed.mkdir()
        other.mkdir()
        config = _config_from(
            tmp_path,
            monkeypatch,
            dedent(
                f"""\
                version: "t"
                settings:
                  category_scopes:
                    managed: ["{managed}"]
                    narrow: ["{other}"]
                rules:
                  - id: scoped-stash
                    category: managed
                    scope: narrow
                    pattern: '^git\\s+stash\\b'
                    decision: deny
                    reason: "No stash."
                """
            ),
        )
        assert guard.evaluate_chain("git stash", config, other)[0] == "deny"
        assert guard.evaluate_chain("git stash", config, managed)[0] == "allow"
