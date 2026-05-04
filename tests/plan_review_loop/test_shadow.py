"""Tests for shadow-mode dispatch and the shadow_runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from _lib import chain, shadow_runner


class TestShadowFromEnv:
    def test_unset_returns_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert chain._shadow_from_env() == []

    def test_empty_returns_empty(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_PLAN_REVIEW_SHADOW": ""}, clear=True):
            assert chain._shadow_from_env() == []

    def test_single_provider(self) -> None:
        with mock.patch.dict(
            os.environ, {"CLAUDE_PLAN_REVIEW_SHADOW": "local"}, clear=True
        ):
            assert chain._shadow_from_env() == ["local"]

    def test_multiple_with_whitespace(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLAN_REVIEW_SHADOW": "local, codex ,gemini"},
            clear=True,
        ):
            assert chain._shadow_from_env() == ["local", "codex", "gemini"]

    def test_unknown_provider_dropped(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLAN_REVIEW_SHADOW": "local,bogus"},
            clear=True,
        ):
            assert chain._shadow_from_env() == ["local"]


class TestDispatchShadowRuns:
    def test_no_shadow_env_returns_empty_no_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain.paths, "data_dir", lambda: tmp_path)
        monkeypatch.delenv("CLAUDE_PLAN_REVIEW_SHADOW", raising=False)
        with mock.patch.object(subprocess, "Popen") as popen:
            result = chain._dispatch_shadow_runs("prompt", "codex", {})
        assert result == []
        popen.assert_not_called()

    def test_skips_primary_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If shadow=local and primary=local, skip — would just duplicate the
        # log we already wrote.
        monkeypatch.setattr(chain.paths, "data_dir", lambda: tmp_path)
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_SHADOW", "local")
        with mock.patch.object(subprocess, "Popen") as popen:
            result = chain._dispatch_shadow_runs("prompt", "local", {})
        assert result == []
        popen.assert_not_called()

    def test_dispatches_when_shadow_differs_from_primary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chain.paths, "data_dir", lambda: tmp_path)
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_SHADOW", "local")
        with mock.patch.object(subprocess, "Popen") as popen:
            result = chain._dispatch_shadow_runs(
                "the prompt",
                "codex",
                {"plan_path": "/plans/x.md", "iteration": 3},
            )
        assert result == ["local"]
        popen.assert_called_once()
        args = popen.call_args
        cmd = args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("shadow_runner.py")
        # Job descriptor file path is last arg
        job_path = Path(cmd[2])
        assert job_path.exists()
        job = json.loads(job_path.read_text())
        assert job["prompt"] == "the prompt"
        assert job["primary_provider"] == "codex"
        assert job["providers"] == ["local"]
        assert job["metadata"]["iteration"] == 3
        # Detached + silenced
        assert args[1]["start_new_session"] is True
        assert args[1]["stdin"] == subprocess.DEVNULL
        assert args[1]["stdout"] == subprocess.DEVNULL
        assert args[1]["stderr"] == subprocess.DEVNULL
        # CLAUDE_PLAN_REVIEW_SHADOW must be stripped from child env to
        # prevent the shadow from spawning its own shadow.
        assert "CLAUDE_PLAN_REVIEW_SHADOW" not in args[1]["env"]


class TestShadowRunner:
    def test_missing_job_returns_1(self, tmp_path: Path) -> None:
        rc = shadow_runner._run_job(tmp_path / "nonexistent.json")
        assert rc == 1

    def test_empty_job_returns_0(self, tmp_path: Path) -> None:
        job = tmp_path / "job.json"
        job.write_text(json.dumps({"prompt": "", "providers": []}))
        rc = shadow_runner._run_job(job)
        assert rc == 0

    def test_invokes_try_provider_with_shadow_flag(self, tmp_path: Path) -> None:
        job = tmp_path / "job.json"
        job.write_text(
            json.dumps(
                {
                    "prompt": "the prompt",
                    "primary_provider": "codex",
                    "metadata": {"plan_path": "/plans/x.md", "iteration": 1},
                    "providers": ["local"],
                }
            )
        )
        with mock.patch.object(shadow_runner, "_try_provider") as m:
            rc = shadow_runner._run_job(job)
        assert rc == 0
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        assert kwargs["shadow"] is True
        assert kwargs["primary_provider"] == "codex"
        assert kwargs["metadata"]["plan_path"] == "/plans/x.md"

    def test_main_deletes_job_file(self, tmp_path: Path) -> None:
        job = tmp_path / "job.json"
        job.write_text(json.dumps({"prompt": "p", "providers": ["local"]}))
        with mock.patch.object(shadow_runner, "_try_provider"):
            shadow_runner.main([str(job)])
        assert not job.exists()

    def test_unknown_provider_skipped(self, tmp_path: Path) -> None:
        job = tmp_path / "job.json"
        job.write_text(
            json.dumps({"prompt": "p", "providers": ["bogus"], "metadata": {}})
        )
        with mock.patch.object(shadow_runner, "_try_provider") as m:
            rc = shadow_runner._run_job(job)
        assert rc == 0
        m.assert_not_called()
