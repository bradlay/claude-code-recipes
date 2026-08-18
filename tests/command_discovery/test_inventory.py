"""Unit tests for the command-inventory config loader."""

from __future__ import annotations

import json
from pathlib import Path

from _lib import inventory as inv


class TestLoadInventory:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("vendor: mycli\ncommands:\n  - status\n  - deploy prod\n")
        loaded = inv.load_inventory(cfg)
        assert loaded is not None
        assert loaded.vendor == "mycli"
        assert loaded.commands == ("status", "deploy prod")

    def test_loads_valid_json(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.json"
        cfg.write_text(json.dumps({"vendor": "mycli", "commands": ["status", "deploy prod"]}))
        loaded = inv.load_inventory(cfg)
        assert loaded is not None
        assert loaded.vendor == "mycli"
        assert loaded.commands == ("status", "deploy prod")

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert inv.load_inventory(tmp_path / "does-not-exist.yaml") is None

    def test_malformed_yaml_returns_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("vendor: mycli\ncommands: [unterminated flow sequence\n")
        assert inv.load_inventory(cfg) is None

    def test_missing_vendor_returns_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("commands:\n  - status\n")
        assert inv.load_inventory(cfg) is None

    def test_missing_commands_returns_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("vendor: mycli\n")
        assert inv.load_inventory(cfg) is None

    def test_empty_commands_list_returns_none(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("vendor: mycli\ncommands: []\n")
        assert inv.load_inventory(cfg) is None

    def test_non_string_commands_are_skipped(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        cfg.write_text("vendor: mycli\ncommands:\n  - status\n  - 42\n  - true\n")
        loaded = inv.load_inventory(cfg)
        assert loaded is not None
        assert loaded.commands == ("status",)

    def test_oversized_command_list_is_capped(self, tmp_path: Path) -> None:
        cfg = tmp_path / "inventory.yaml"
        many = "\n".join(f"  - cmd{i}" for i in range(inv.MAX_COMMANDS + 500))
        cfg.write_text(f"vendor: mycli\ncommands:\n{many}\n")
        loaded = inv.load_inventory(cfg)
        assert loaded is not None
        assert len(loaded.commands) <= inv.MAX_COMMANDS
