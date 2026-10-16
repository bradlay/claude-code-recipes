"""Unit tests for the default-deny print-mode gate."""

from __future__ import annotations

from datetime import date

from _lib import print_policy as pp

POLICY = pp.PrintPolicy(
    program="claude",
    opt_in_env="CLAUDE_PRINT_OK",
    recursion_env="CLAUDE_PRINT_ACTIVE",
    fallback_hint="Use another CLI instead.",
)


class TestIsPrintMode:
    def test_non_print_passes(self) -> None:
        assert pp.is_print_mode(["--model", "x"]) is False

    def test_short_flag(self) -> None:
        assert pp.is_print_mode(["-p", "hello"]) is True

    def test_long_flag(self) -> None:
        assert pp.is_print_mode(["--print"]) is True

    def test_flag_not_first(self) -> None:
        assert pp.is_print_mode(["--model", "x", "-p"]) is True

    def test_combined_short_cluster(self) -> None:
        assert pp.is_print_mode(["-np"]) is True

    def test_terminator_stops_detection(self) -> None:
        assert pp.is_print_mode(["--", "-p"]) is False


class TestDecideDefaultDeny:
    def test_non_print_always_allowed(self) -> None:
        d = pp.decide(POLICY, ["--model", "x"], env={})
        assert d.allow is True
        assert d.is_print is False

    def test_print_without_opt_in_is_denied(self) -> None:
        d = pp.decide(POLICY, ["-p", "hi"], env={})
        assert d.allow is False
        assert "opt-in" in d.reason

    def test_print_with_opt_in_is_allowed(self) -> None:
        d = pp.decide(POLICY, ["-p", "hi"], env={"CLAUDE_PRINT_OK": "1"})
        assert d.allow is True
        assert d.is_print is True

    def test_recursion_sentinel_denies_even_with_opt_in(self) -> None:
        # This is the fork-bomb guard: a print call spawned inside another
        # print call must be denied even when the opt-in is set.
        d = pp.decide(
            POLICY,
            ["-p", "hi"],
            env={"CLAUDE_PRINT_OK": "1", "CLAUDE_PRINT_ACTIVE": "1"},
        )
        assert d.allow is False
        assert "recursive" in d.reason

    def test_hard_block_date_denies_on_or_after(self) -> None:
        cliff_policy = pp.PrintPolicy(
            program="claude",
            opt_in_env="CLAUDE_PRINT_OK",
            recursion_env="CLAUDE_PRINT_ACTIVE",
            hard_block_date=date(2030, 1, 1),
        )
        d = pp.decide(
            cliff_policy,
            ["-p"],
            env={"CLAUDE_PRINT_OK": "1"},
            today=date(2030, 1, 1),
        )
        assert d.allow is False
        assert "hard-blocked" in d.reason

    def test_hard_block_date_allows_before_cliff(self) -> None:
        cliff_policy = pp.PrintPolicy(
            program="claude",
            opt_in_env="CLAUDE_PRINT_OK",
            recursion_env="CLAUDE_PRINT_ACTIVE",
            hard_block_date=date(2030, 1, 1),
        )
        d = pp.decide(
            cliff_policy,
            ["-p"],
            env={"CLAUDE_PRINT_OK": "1"},
            today=date(2029, 12, 31),
        )
        assert d.allow is True

    def test_exit_code_contract(self) -> None:
        assert pp.decide(POLICY, ["--model", "x"], env={}).exit_code() == pp.EXIT_ALLOW_NO_SENTINEL
        assert (
            pp.decide(POLICY, ["-p"], env={"CLAUDE_PRINT_OK": "1"}).exit_code()
            == pp.EXIT_ALLOW_SET_SENTINEL
        )
        assert pp.decide(POLICY, ["-p"], env={}).exit_code() == pp.EXIT_BLOCK


class TestWrappedPrintBackstop:
    def test_direct_shell_c_with_print_is_denied(self) -> None:
        assert pp.looks_like_wrapped_print("sh -c 'claude -p hi'", "claude") is True

    def test_shell_c_hidden_behind_env_prefix(self) -> None:
        assert pp.looks_like_wrapped_print("env X=1 sh -c 'claude -p hi'", "claude") is True

    def test_trailing_argv_form_cannot_hide_the_program(self) -> None:
        cmd = 'sh -c \'exec "$@"\' _ claude -p hi'
        assert pp.looks_like_wrapped_print(cmd, "claude") is True

    def test_non_shell_c_command_not_flagged(self) -> None:
        assert pp.looks_like_wrapped_print("claude -p hi", "claude") is False

    def test_quoted_mention_in_a_non_shell_c_segment_not_flagged(self) -> None:
        # A segment that merely MENTIONS "sh -c ... claude -p" inside quotes
        # (e.g. a commit message argument) must not false-positive: the
        # segment itself doesn't START with a shell -c wrapper.
        cmd = 'git commit -m "reproduces: sh -c \'claude -p\'"'
        assert pp.looks_like_wrapped_print(cmd, "claude") is False


class TestGuardOutcome:
    def test_non_claude_command_returns_none(self) -> None:
        assert pp.guard_outcome(POLICY, "ls -la", env={}) is None

    def test_bare_claude_print_denied(self) -> None:
        result = pp.guard_outcome(POLICY, "claude -p hi", env={})
        assert result is not None
        reason, decision = result
        assert decision == "deny"

    def test_bare_claude_print_allowed_with_opt_in(self) -> None:
        result = pp.guard_outcome(POLICY, "claude -p hi", env={"CLAUDE_PRINT_OK": "1"})
        assert result is None

    def test_absolute_path_matched_by_basename(self) -> None:
        result = pp.guard_outcome(POLICY, "/usr/local/bin/claude -p hi", env={})
        assert result is not None
        assert result[1] == "deny"

    def test_leading_env_assignment_does_not_hide_invocation(self) -> None:
        result = pp.guard_outcome(POLICY, "FOO=bar claude -p hi", env={})
        assert result is not None
        assert result[1] == "deny"

    def test_lookalike_program_not_matched(self) -> None:
        assert pp.guard_outcome(POLICY, "myclaude-helper -p", env={}) is None

    def test_wrapped_shell_c_denied_even_though_outer_program_is_sh(self) -> None:
        result = pp.guard_outcome(POLICY, "sh -c 'claude -p hi'", env={})
        assert result is not None
        assert result[1] == "deny"


class TestShimRendering:
    def test_shim_contains_fail_closed_backstop(self) -> None:
        text = pp.render_shim_text(POLICY, guard_dir="/tmp/guard-bin")
        assert "exit 97" in text
        assert "PROGRAM=" in text

    def test_shim_bakes_in_paths(self) -> None:
        text = pp.render_shim_text(POLICY, guard_dir="/tmp/guard-bin", python_exe="/usr/bin/python3")
        assert "/usr/bin/python3" in text


class TestMainEntrypoint:
    def test_block_writes_reason_to_stderr(self, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = pp.main(["claude", "-p", "hi"])
        assert rc == pp.EXIT_BLOCK
        captured = capsys.readouterr()
        assert "opt-in" in captured.err

    def test_missing_program_arg_blocks(self, capsys) -> None:  # type: ignore[no-untyped-def]
        rc = pp.main([])
        assert rc == pp.EXIT_BLOCK
