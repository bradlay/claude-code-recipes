"""Tests for the `plan-review-shadow` CLI.

Exercises the public argparse entrypoint via in-process `main()`
calls. Records are written to a tmp `${CLAUDE_PLUGIN_DATA}/review-log/`
and the CLI reads them through the same _load_logs path the real bin
uses, so the filter contracts (newest-first sort, status filter, latest
resolution) are verified end-to-end.
"""

from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import shadow_cli


def _write_record(
    log_dir: Path,
    *,
    name: str,
    result_status: str = "ok",
    signature: str = "abc",
    age_seconds: float = 60.0,
    plan_title: str = "demo plan",
    iteration: int = 1,
    elapsed: float = 1.5,
    findings_count: int = 0,
    error: str | None = None,
    parse_error: str | None = None,
    findings: list[dict[str, Any]] | None = None,
    shadow: bool = True,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = time.time() - age_seconds
    record: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
        "provider": "local",
        "primary_provider": "codex",
        "elapsed_seconds": elapsed,
        "returncode": 0 if error is None else 1,
        "error": error,
        "parse_error": parse_error,
        "shadow": shadow,
        "result_status": result_status,
        "shadow_config_signature": signature,
        "iteration": iteration,
        "plan_path": "/tmp/x.md",
        "plan_title": plan_title,
        "findings_count": findings_count,
        "findings": findings or [],
    }
    p = log_dir / name
    p.write_text(json.dumps(record))
    os.utime(p, (ts, ts))
    return p


class TestListCommand:
    def test_empty_dir_human_message(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list"])
        assert rc == 0
        assert "no shadow records" in buf.getvalue()

    def test_empty_dir_json_is_empty_list(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list", "--json"])
        assert rc == 0
        assert json.loads(buf.getvalue()) == []

    def test_newest_first(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="old_shadow.json", age_seconds=300)
        _write_record(log_dir, name="newest_shadow.json", age_seconds=10)
        _write_record(log_dir, name="middle_shadow.json", age_seconds=60)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert [Path(d["path"]).name for d in data] == [
            "newest_shadow.json",
            "middle_shadow.json",
            "old_shadow.json",
        ]

    def test_status_ok_filter(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="ok_shadow.json", result_status="ok")
        _write_record(log_dir, name="err_shadow.json", result_status="error")
        _write_record(log_dir, name="emp_shadow.json", result_status="empty")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list", "--status", "ok", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert {Path(d["path"]).name for d in data} == {"ok_shadow.json"}

    def test_status_fail_filter_excludes_unknown(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="ok_shadow.json", result_status="ok")
        _write_record(log_dir, name="err_shadow.json", result_status="error")
        _write_record(log_dir, name="unk_shadow.json", result_status="unknown")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list", "--status", "fail", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert {Path(d["path"]).name for d in data} == {"err_shadow.json"}

    def test_limit(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        for i in range(15):
            _write_record(
                log_dir, name=f"r{i:02}_shadow.json", age_seconds=10 * i + 5
            )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list", "--limit", "5", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert len(data) == 5

    def test_human_table_strips_ansi(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        # Plan title with an ANSI red escape that would otherwise reflow
        # the output table.
        _write_record(
            log_dir,
            name="ansi_shadow.json",
            plan_title="\x1b[31mboom\x1b[0m plan",
        )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["list"])
        assert rc == 0
        out = buf.getvalue()
        assert "\x1b[" not in out
        assert "boom plan" in out


class TestShowCommand:
    def test_latest_empty_dir_returns_1(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stderr(err):
            rc = shadow_cli.main(["show", "latest"])
        assert rc == 1
        assert "no shadow records" in err.getvalue()

    def test_show_latest_resolves_newest(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="old_shadow.json", age_seconds=300)
        _write_record(
            log_dir, name="newest_shadow.json", age_seconds=10, plan_title="newest"
        )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["show", "latest"])
        assert rc == 0
        assert "newest" in buf.getvalue()

    def test_show_path_returns_full_record_json(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        path = _write_record(
            log_dir,
            name="r_shadow.json",
            findings=[{"severity": "P1", "title": "boom"}],
            findings_count=1,
        )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["show", str(path), "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        # `--json` returns the raw record dict, not the rendered subset.
        assert data["plan_title"] == "demo plan"
        assert data["findings"][0]["title"] == "boom"

    def test_show_missing_path_returns_1(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stderr(err):
            rc = shadow_cli.main(["show", "/no/such/path.json"])
        assert rc == 1
        assert "not found" in err.getvalue()

    def test_show_renders_findings(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(
            log_dir,
            name="f_shadow.json",
            findings=[
                {"severity": "P0", "title": "leak"},
                {"severity": "P2", "title": "nit"},
            ],
            findings_count=2,
        )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["show", "latest"])
        assert rc == 0
        out = buf.getvalue()
        assert "[P0] leak" in out
        assert "[P2] nit" in out


class TestStatsCommand:
    def test_default_scope_both_emits_both_views(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="ok_shadow.json", result_status="ok")
        _write_record(log_dir, name="err_shadow.json", result_status="error")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["stats", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert "history" in data
        assert "current" in data
        assert data["history"]["total"] == 2
        assert data["history"]["ok"] == 1
        assert data["history"]["fail"] == 1

    def test_scope_history_only(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        _write_record(log_dir, name="ok_shadow.json", result_status="ok")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["stats", "--scope", "history", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        # Direct dict, not wrapped under "history" key.
        assert data["total"] == 1
        assert "severity" not in data

    def test_scope_current_returns_health_dict(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["stats", "--scope", "current", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["severity"] == "warming"
        assert "config_signature" in data

    def test_history_collects_error_types(self, tmp_path: Path) -> None:
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path)}
        log_dir = tmp_path / "review-log"
        for i in range(3):
            _write_record(
                log_dir,
                name=f"timeout{i}_shadow.json",
                result_status="error",
                error="connection timeout after 30s",
            )
        _write_record(
            log_dir,
            name="parse_shadow.json",
            result_status="unparseable",
            parse_error="json decode: unexpected EOF",
        )
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(buf):
            rc = shadow_cli.main(["stats", "--scope", "history", "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data["total"] == 4
        assert data["fail"] == 4
        # Error types index drops empty strings, keeps both signatures.
        assert "connection timeout after 30s" in data["error_types"]
        assert "json decode: unexpected EOF" in data["error_types"]
        assert data["error_types"]["connection timeout after 30s"] == 3


class TestParserExit:
    def test_no_subcommand_exits_2(self) -> None:
        # argparse default for missing required subcommand.
        try:
            shadow_cli.main([])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("expected SystemExit")
