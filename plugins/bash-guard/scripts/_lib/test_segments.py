"""Tests for the additional inspection pass: pipeline stages and
command/process substitutions.

The invariant under test: a guarded command hidden behind a benign
leading token, via a pipe (``echo ok | git stash``) or a substitution
(``echo $(git stash)``), is still caught, while a derived ``allow`` never
masks a deny/ask found elsewhere, and quoted literals mentioning
operators never cause a false split.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from _lib import guard, segments

# ---------------------------------------------------------------------------
# split_pipeline_segments unit behaviour
# ---------------------------------------------------------------------------


class TestSplitPipelineSegments:
    def test_simple_pipe_splits(self) -> None:
        assert segments.split_pipeline_segments("echo ok | git stash") == [
            "echo ok",
            "git stash",
        ]

    def test_no_pipe_is_single_segment(self) -> None:
        assert segments.split_pipeline_segments("git status") == ["git status"]

    def test_double_pipe_is_not_split(self) -> None:
        # `||` is logical OR, a sequence separator handled elsewhere, not
        # a pipe.
        assert segments.split_pipeline_segments("false || echo ok") == ["false || echo ok"]

    def test_quoted_pipe_is_not_split(self) -> None:
        assert segments.split_pipeline_segments("git commit -m 'a | b'") == [
            "git commit -m 'a | b'",
        ]

    def test_multi_stage_pipeline(self) -> None:
        assert segments.split_pipeline_segments("cat f | grep x | git stash") == [
            "cat f",
            "grep x",
            "git stash",
        ]


# ---------------------------------------------------------------------------
# extract_command_substitutions unit behaviour
# ---------------------------------------------------------------------------


class TestExtractCommandSubstitutions:
    def test_dollar_paren_is_extracted(self) -> None:
        assert segments.extract_command_substitutions("echo $(git stash)") == ["git stash"]

    def test_backtick_is_extracted(self) -> None:
        assert segments.extract_command_substitutions("echo `git stash`") == ["git stash"]

    def test_process_substitution_is_extracted(self) -> None:
        assert segments.extract_command_substitutions("cat <(git stash)") == ["git stash"]

    def test_single_quoted_is_not_expanded(self) -> None:
        assert segments.extract_command_substitutions("echo '$(git stash)'") == []

    def test_double_quoted_is_expanded(self) -> None:
        assert segments.extract_command_substitutions('echo "$(git stash)"') == ["git stash"]

    def test_apostrophe_inside_double_quotes_does_not_flip_mode(self) -> None:
        assert segments.extract_command_substitutions('echo "it\'s $(git stash)"') == ["git stash"]

    def test_nested_substitution_is_extracted_recursively(self) -> None:
        result = segments.extract_command_substitutions("echo $(echo $(git stash))")
        assert "echo $(git stash)" in result
        assert "git stash" in result

    def test_no_substitution_returns_empty(self) -> None:
        assert segments.extract_command_substitutions("git status") == []


# ---------------------------------------------------------------------------
# Integration through evaluate_chain
# ---------------------------------------------------------------------------

_FIXTURE_RULES = dedent(
    """\
    version: "test-segments"

    settings: {}

    rules:
      - id: test-git-stash
        category: git-history
        pattern: '^git\\s+stash\\b'
        decision: deny
        reason: "git stash discards work."

      - id: test-echo-allowed
        category: benign
        pattern: '^echo\\b'
        decision: allow
    """
)

_FIXTURE_RULES_NO_INSPECT = _FIXTURE_RULES.replace(
    "settings: {}", "settings:\n  inspect_segments: false"
)


def _config_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> dict[str, Any]:
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(path))
    guard._CACHE.clear()
    return guard.load_rules()


class TestEvaluateChainWithDerivedSegments:
    def test_deny_in_pipeline_second_stage_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, reason, offending = guard.evaluate_chain("echo x | git stash", config, None)
        assert decision == "deny"
        assert "stash" in reason
        assert offending == "echo x | git stash"

    def test_deny_inside_command_substitution_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, reason, _ = guard.evaluate_chain("echo $(git stash)", config, None)
        assert decision == "deny"
        assert "stash" in reason

    def test_quoted_literal_mentioning_operator_does_not_split(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A commit message quoting a pipe/substitution-shaped string must
        # not be misread as an actual pipeline/substitution.
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain(
            "git commit -m 'fix: pipe stdin | git stash for real usage'", config, None
        )
        assert decision == "allow"

    def test_benign_pipeline_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("echo ok | echo done", config, None)
        assert decision == "allow"

    def test_inspect_segments_false_disables_the_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES_NO_INSPECT)
        decision, _, _ = guard.evaluate_chain("echo x | git stash", config, None)
        assert decision == "allow"

    def test_derived_allow_cannot_mask_a_deny_on_the_sub_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole sub-command as written (``git stash``) hits the deny
        # rule; a pipeline stage or substitution match on ``allow`` must
        # never override that.
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("git stash", config, None)
        assert decision == "deny"

    def test_deny_in_later_chain_segment_via_pipeline_fires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `true` matches no rule (neither allow nor deny) so the chain
        # walk continues to the second sub-command, unlike an explicit
        # allow-rule match, which is a deliberate allowlist short-circuit
        # for the whole chain, not something this pass changes.
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, offending = guard.evaluate_chain("true && echo x | git stash", config, None)
        assert decision == "deny"
        assert "git stash" in offending
