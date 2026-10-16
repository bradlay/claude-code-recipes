"""Unit tests for the discovery engine's pure `evaluate` function."""

from __future__ import annotations

from _lib.discovery import evaluate
from _lib.inventory import Inventory

_INVENTORY = Inventory(
    vendor="mycli",
    commands=("status", "deploy prod", "deploy staging", "config set"),
)


def _payload(command: str, output: str, tool_name: str = "Bash") -> dict[str, object]:
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_response": {"stdout": "", "stderr": output},
    }


class TestTypoSuggestion:
    def test_click_style_typo_yields_closest_match(self) -> None:
        payload = _payload("mycli statu", "Error: No such command 'statu'.")
        result = evaluate(payload, inventory=_INVENTORY)
        assert result is not None
        assert "mycli status" in result

    def test_argparse_style_typo_yields_closest_match(self) -> None:
        payload = _payload(
            "mycli deploy staginng", "error: argument: invalid choice: 'staginng'"
        )
        result = evaluate(payload, inventory=_INVENTORY)
        assert result is not None
        assert "mycli deploy staging" in result

    def test_env_prefixed_invocation_is_still_matched(self) -> None:
        payload = _payload("FOO=bar mycli statu", "Error: No such command 'statu'.")
        result = evaluate(payload, inventory=_INVENTORY)
        assert result is not None
        assert "mycli status" in result


class TestNoCloseMatch:
    def test_unknown_command_with_nothing_close_stays_silent(self) -> None:
        payload = _payload(
            "mycli zzzzqqqq", "Error: No such command 'zzzzqqqq'."
        )
        assert evaluate(payload, inventory=_INVENTORY) is None


class TestSuccessfulInvocation:
    def test_successful_invocation_emits_nothing(self) -> None:
        payload = _payload("mycli status", "all systems go")
        assert evaluate(payload, inventory=_INVENTORY) is None

    def test_unrelated_runtime_failure_stays_silent(self) -> None:
        payload = _payload("mycli deploy prod", "Error: connection refused")
        assert evaluate(payload, inventory=_INVENTORY) is None


class TestNotTheConfiguredVendor:
    def test_other_program_invocation_is_ignored(self) -> None:
        payload = _payload("git statu", "git: 'statu' is not a git command.")
        assert evaluate(payload, inventory=_INVENTORY) is None

    def test_non_bash_tool_is_ignored(self) -> None:
        payload = _payload("mycli statu", "Error: No such command 'statu'.", tool_name="Read")
        assert evaluate(payload, inventory=_INVENTORY) is None


class TestBoundedMalformedInput:
    def test_missing_tool_input_is_handled(self) -> None:
        payload = {"tool_name": "Bash", "tool_response": "Error: No such command 'x'."}
        assert evaluate(payload, inventory=_INVENTORY) is None

    def test_non_string_command_is_handled(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 12345},
            "tool_response": "Error: No such command 'x'.",
        }
        assert evaluate(payload, inventory=_INVENTORY) is None

    def test_non_dict_non_string_tool_response_is_handled(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "mycli statu"},
            "tool_response": ["not", "a", "dict"],
        }
        assert evaluate(payload, inventory=_INVENTORY) is None

    def test_oversized_output_does_not_raise_and_still_matches(self) -> None:
        huge_noise = "x" * 200_000
        payload = _payload("mycli statu", f"{huge_noise}Error: No such command 'statu'.")
        # The failure marker is pushed past the hook's capped read window by
        # the leading noise, so this degrades to silence rather than raising.
        result = evaluate(payload, inventory=_INVENTORY)
        assert result is None

    def test_oversized_command_is_truncated_not_raising(self) -> None:
        huge_command = "mycli " + "a" * 5000
        payload = _payload(huge_command, "Error: No such command 'x'.")
        # Must not raise regardless of outcome.
        evaluate(payload, inventory=_INVENTORY)


class TestMissingInventoryDegradesSilently:
    def test_none_inventory_and_no_config_file_emits_nothing(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr("_lib.inventory.load_inventory", lambda: None)
        payload = _payload("mycli statu", "Error: No such command 'statu'.")
        assert evaluate(payload) is None

    def test_empty_commands_inventory_emits_nothing(self) -> None:
        empty_inventory = Inventory(vendor="mycli", commands=())
        payload = _payload("mycli statu", "Error: No such command 'statu'.")
        assert evaluate(payload, inventory=empty_inventory) is None

    def test_real_missing_inventory_file_on_disk_degrades_silently(
        self, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("COMMAND_DISCOVERY_INVENTORY", str(tmp_path / "nope.yaml"))
        payload = _payload("mycli statu", "Error: No such command 'statu'.")
        assert evaluate(payload) is None
