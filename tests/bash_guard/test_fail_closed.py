"""The guard has no environment-variable bypass.

The former CLAUDE_BASH_GUARD_FAIL_OPEN=1 disabled the entire
enforcement layer from the parent shell. These tests drive the REAL
launcher (bin/bash-guard) as a subprocess with the variable set and
assert the guard still denies: the variable must be inert on the shim
path, not merely ignored by the Python evaluator. A second test
asserts that trying to SET the variable is itself denied by the
evaluator's blocked_env_vars check.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "bash-guard"
_LAUNCHER = _PLUGIN_DIR / "bin" / "bash-guard"

_RULES = dedent(
    """\
    version: "test-fail-closed"
    settings: {}
    rules:
      - id: test-git-stash
        category: git-history
        pattern: '^git\\s+stash\\b'
        decision: deny
        reason: "git stash discards work."
    """
)


def _run_launcher(tmp_path: Path, command: str, extra_env: dict[str, str]) -> dict:
    rules = tmp_path / "rules.yaml"
    rules.write_text(_RULES)
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(tmp_path),
        }
    )
    env = dict(os.environ)
    env.update(extra_env)
    env["CLAUDE_BASH_GUARD_RULES_FILE"] = str(rules)
    # Point XDG dirs at the sandbox so the launcher's logging cannot
    # touch the real home.
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    proc = subprocess.run(
        [str(_LAUNCHER)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestNoEnvBypass:
    def test_denied_command_stays_denied_with_fail_open_set(self, tmp_path: Path) -> None:
        out = _run_launcher(
            tmp_path, "git stash", {"CLAUDE_BASH_GUARD_FAIL_OPEN": "1"}
        )
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny"

    def test_denied_command_denied_without_the_var_too(self, tmp_path: Path) -> None:
        out = _run_launcher(tmp_path, "git stash", {})
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny"

    def test_allowed_command_still_allowed_with_var_set(self, tmp_path: Path) -> None:
        out = _run_launcher(
            tmp_path, "git status", {"CLAUDE_BASH_GUARD_FAIL_OPEN": "1"}
        )
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "allow"


class TestSettingTheVarIsDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "export CLAUDE_BASH_GUARD_FAIL_OPEN=1",
            "CLAUDE_BASH_GUARD_FAIL_OPEN=1 git stash",
            "env CLAUDE_BASH_GUARD_FAIL_OPEN=1 git status",
            "echo ok && export CLAUDE_BASH_GUARD_FAIL_OPEN=1",
        ],
    )
    def test_setting_blocked_var_is_denied(self, tmp_path: Path, command: str) -> None:
        out = _run_launcher(tmp_path, command, {})
        decision = out["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny"
