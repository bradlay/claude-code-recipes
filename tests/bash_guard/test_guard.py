"""Unit tests for the bash-guard rule evaluator and chain splitter.

Tests the deterministic in-process bits without driving the full hook.
End-to-end shell-launcher tests live in the CI smoke job.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _lib import guard


class TestChainSplit:
    def test_no_separators(self) -> None:
        assert guard.split_chained_commands("ls -la") == ["ls -la"]

    def test_and(self) -> None:
        parts = guard.split_chained_commands("cd /tmp && ls")
        assert parts == ["cd /tmp", "ls"]

    def test_semicolon(self) -> None:
        parts = guard.split_chained_commands("ls; pwd")
        assert parts == ["ls", "pwd"]

    def test_newline(self) -> None:
        parts = guard.split_chained_commands("ls\npwd")
        assert parts == ["ls", "pwd"]

    def test_background(self) -> None:
        parts = guard.split_chained_commands("long-task &")
        assert parts == ["long-task"]

    def test_pipe_not_split(self) -> None:
        # Pipes preserve the pipeline so rules like
        # ``base64 -d | bash`` see the whole thing.
        parts = guard.split_chained_commands("cat x | grep y | wc")
        assert parts == ["cat x | grep y | wc"]

    def test_quoted_separator_not_split(self) -> None:
        parts = guard.split_chained_commands('echo "hello && world"')
        assert parts == ['echo "hello && world"']

    def test_escaped_separator(self) -> None:
        parts = guard.split_chained_commands(r"echo a \&\& b")
        assert parts == [r"echo a \&\& b"]


class TestNormalize:
    def test_strips_git_C(self) -> None:
        assert (
            guard.normalize_for_patterns("git -C /some/path push") == "git push"
        )

    def test_passthrough_non_git(self) -> None:
        assert guard.normalize_for_patterns("ls -la") == "ls -la"


class TestEvaluateRules:
    @pytest.fixture
    def config(self) -> dict:
        return {
            "settings": {},
            "rules": [
                {
                    "id": "rm-rf",
                    "pattern": r"^rm\s+-rf\s+/$",
                    "decision": "deny",
                    "reason": "no",
                },
                {
                    "id": "git-force",
                    "pattern": r"^git\s+push\b",
                    "extra_search": r"--force\b",
                    "decision": "ask",
                    "reason": "force",
                },
                {
                    "id": "echo-allow",
                    "pattern": r"^echo\s+pls",
                    "decision": "allow",
                    "reason": "",
                },
            ],
        }

    def test_no_match_allows(self, config: dict) -> None:
        decision, reason = guard.evaluate_rules("ls -la", config)
        assert decision == "allow"
        assert reason == ""

    def test_deny_match(self, config: dict) -> None:
        decision, reason = guard.evaluate_rules("rm -rf /", config)
        assert decision == "deny"
        assert reason == "no"

    def test_extra_search_required(self, config: dict) -> None:
        decision, _ = guard.evaluate_rules("git push origin main", config)
        assert decision == "allow"

    def test_extra_search_match(self, config: dict) -> None:
        decision, _ = guard.evaluate_rules("git push --force origin x", config)
        assert decision == "ask"

    def test_allow_rule_short_circuits(self, config: dict) -> None:
        decision, _ = guard.evaluate_rules("echo pls", config)
        assert decision == "allow"

    def test_first_match_wins(self) -> None:
        cfg = {
            "settings": {},
            "rules": [
                {"pattern": r"^echo", "decision": "allow", "reason": ""},
                {"pattern": r"^echo", "decision": "deny", "reason": "second"},
            ],
        }
        decision, _ = guard.evaluate_rules("echo hi", cfg)
        assert decision == "allow"

    def test_bad_regex_skipped(self) -> None:
        cfg = {
            "settings": {},
            "rules": [
                {"pattern": r"[invalid(regex", "decision": "deny", "reason": "x"},
                {"pattern": r"^echo", "decision": "deny", "reason": "echoed"},
            ],
        }
        decision, reason = guard.evaluate_rules("echo hi", cfg)
        assert decision == "deny"
        assert reason == "echoed"


class TestApprovalCache:
    def test_no_approval_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        assert guard.consume_approval("ls", expiry_seconds=60) is False

    def test_write_then_consume(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        guard.write_approval("rm -rf /tmp/x")
        assert guard.consume_approval("rm -rf /tmp/x", 60) is True
        # one-shot
        assert guard.consume_approval("rm -rf /tmp/x", 60) is False

    def test_expired_approval(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        guard.write_approval("git push --force")
        approval = guard.paths.approvals_dir() / guard._command_hash("git push --force")
        os.utime(approval, (0, 0))
        assert guard.consume_approval("git push --force", 60) is False


class TestRulesLoading:
    def test_default_rules_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        guard._CACHE["config"] = None
        config = guard.load_rules()
        assert "rules" in config
        assert len(config["rules"]) > 0
        ids = [r.get("id", "") for r in config["rules"]]
        assert "rm-rf-root" in ids

    def test_user_override_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
        custom = tmp_path / "custom-rules.yaml"
        custom.write_text(
            "version: '1'\nsettings: {}\nrules:\n  - id: only-rule\n"
            "    pattern: '^echo$'\n    decision: deny\n    reason: nope\n"
        )
        monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(custom))
        guard._CACHE["config"] = None
        config = guard.load_rules()
        ids = [r.get("id", "") for r in config["rules"]]
        assert ids == ["only-rule"]
