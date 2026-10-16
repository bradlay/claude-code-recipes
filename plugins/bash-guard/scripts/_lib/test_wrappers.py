"""Tests for wrapper unwrapping: the module itself and its integration
into the chain evaluator.

The invariant under test: a pass-through wrapper must never WEAKEN a
decision. ``mise exec -- git push --force`` is judged like the bare
``git push --force``, an allow-rule match on the wrapped form cannot
mask a deny on the inner program, and disabling
``settings.strip_wrappers`` restores the pre-feature behaviour.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from _lib import guard, wrappers

# ---------------------------------------------------------------------------
# strip_wrappers unit behaviour
# ---------------------------------------------------------------------------


class TestStripWrappers:
    def test_mise_exec_is_stripped(self) -> None:
        assert wrappers.strip_wrappers("mise exec -- git push --force") == "git push --force"

    def test_mise_exec_with_tool_pin_is_stripped(self) -> None:
        assert wrappers.strip_wrappers("mise exec python@3.12 -- git stash") == "git stash"

    def test_env_with_assignments_is_stripped(self) -> None:
        assert wrappers.strip_wrappers("env FOO=1 BAR=2 git stash") == "git stash"

    def test_inline_assignment_is_stripped(self) -> None:
        assert wrappers.strip_wrappers("FOO=1 git stash") == "git stash"

    def test_shell_c_inner_is_extracted(self) -> None:
        assert wrappers.strip_wrappers("sh -c 'git reset --hard'") == "git reset --hard"

    def test_bash_lc_inner_is_extracted(self) -> None:
        assert wrappers.strip_wrappers('bash -lc "git checkout main"') == "git checkout main"

    def test_nested_wrappers_unwrap_recursively(self) -> None:
        assert wrappers.strip_wrappers("env FOO=1 sh -c 'nice -n 5 git stash'") == "git stash"

    def test_uv_run_plain_is_stripped(self) -> None:
        assert wrappers.strip_wrappers("uv run pytest") == "pytest"

    def test_uv_run_active_is_not_stripped(self) -> None:
        cmd = "uv run --active somecmd"
        assert wrappers.strip_wrappers(cmd) == cmd

    def test_uv_run_project_is_not_stripped(self) -> None:
        cmd = "uv run --project x somecmd"
        assert wrappers.strip_wrappers(cmd) == cmd

    def test_mise_run_task_is_not_stripped(self) -> None:
        cmd = "mise run deploy"
        assert wrappers.strip_wrappers(cmd) == cmd

    def test_unwrapped_command_is_unchanged(self) -> None:
        assert wrappers.strip_wrappers("git status") == "git status"

    def test_prefix_only_leaves_shell_c_intact(self) -> None:
        assert (
            wrappers.strip_prefix_wrappers("env X=1 sh -c 'exec \"$@\"' _ foo")
            == "sh -c 'exec \"$@\"' _ foo"
        )


# ---------------------------------------------------------------------------
# Integration through evaluate_chain
# ---------------------------------------------------------------------------

_FIXTURE_RULES = dedent(
    """\
    version: "test-wrappers"

    settings: {}

    rules:
      - id: test-git-stash
        category: git-history
        pattern: '^git\\s+stash\\b'
        decision: deny
        reason: "git stash discards work."

      - id: test-env-allowed
        category: benign
        pattern: '^env\\b'
        decision: allow
    """
)

_FIXTURE_RULES_NO_STRIP = _FIXTURE_RULES.replace(
    "settings: {}", "settings:\n  strip_wrappers: false"
)


def _config_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> dict[str, Any]:
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(path))
    guard._CACHE.clear()
    return guard.load_rules()


class TestEvaluateChainWithWrappers:
    def test_wrapped_deny_is_denied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, reason, _ = guard.evaluate_chain("mise exec -- git stash", config, None)
        assert decision == "deny"
        assert "stash" in reason

    def test_shell_c_wrapped_deny_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("sh -c 'git stash'", config, None)
        assert decision == "deny"

    def test_allow_on_wrapper_cannot_mask_inner_deny(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``env ...`` matches the allow rule as written, but the inner
        # program is the denied ``git stash``; deny must win.
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("env FOO=1 git stash", config, None)
        assert decision == "deny"

    def test_benign_wrapped_command_still_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("mise exec -- git status", config, None)
        assert decision == "allow"

    def test_wrapped_deny_in_later_chain_segment_is_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, offending = guard.evaluate_chain(
            "echo ok && env FOO=1 git stash", config, None
        )
        assert decision == "deny"
        assert "git stash" in offending

    def test_strip_wrappers_false_disables_unwrapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES_NO_STRIP)
        decision, _, _ = guard.evaluate_chain("mise exec -- git stash", config, None)
        assert decision == "allow"

    def test_bare_deny_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config_from(tmp_path, monkeypatch, _FIXTURE_RULES)
        decision, _, _ = guard.evaluate_chain("git stash", config, None)
        assert decision == "deny"
