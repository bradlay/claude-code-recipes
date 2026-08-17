"""Tests for the rule-schema extensions: ``replacement:`` on rules and
``scope:`` / ``settings.category_scopes`` directory gating.

Fail-closed property under test for scoping: an unknown scope name or
an unresolvable target keeps the rule ACTIVE. A typo in settings can
only over-enforce, never silently disable a rule.
"""

from __future__ import annotations

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
