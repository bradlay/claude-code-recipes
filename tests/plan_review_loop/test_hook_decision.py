"""Unit tests for plan_review_hook._decide_chain — the backend routing /
interactive-picker decision the ExitPlanMode hook makes before reviewing."""

from __future__ import annotations

from pathlib import Path

import plan_review_hook as hook
import pytest
from _lib import picker, probes
from _lib._io import HookInvocation
from _lib.probes import ProbeResult

_ENV_KEYS = (
    "AUTOSWE_RUN_ID",
    "CLAUDE_PLAN_REVIEW_NESTED",
    "CLAUDE_PLAN_REVIEW_CHAIN",
    "CLAUDE_PLAN_REVIEW_TIER",
    "CLAUDE_PLAN_REVIEW_AUTOSELECT",
    "CLAUDE_PLAN_REVIEW_LOCAL_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _inv(session: str = "sess") -> HookInvocation:
    return HookInvocation(
        event="PreToolUse",
        tool_name="ExitPlanMode",
        session_id=session,
        cwd=Path("/"),
        tool_input={},
        tool_response=None,
        transcript_path=None,
        raw={},
        hook_script="plan_review_hook.py",
    )


def _ok(name: str) -> ProbeResult:
    return ProbeResult(name=name, ok=True, model="m", last_probed=0.0)


def _bad(name: str) -> ProbeResult:
    return ProbeResult(name=name, ok=False, detail="boom", model="m", last_probed=0.0)


class TestNonInteractiveBranches:
    def test_nested_proceeds_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_NESTED", "1")
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain is None

    def test_explicit_chain_proceeds_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_CHAIN", "codex")
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain is None

    def test_tier_proceeds_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_TIER", "fast")
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain is None

    def test_autoselect_valid(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_AUTOSELECT", "claude")  # -> sonnet
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain == ["sonnet"]


class TestAutoswe:
    def test_autoswe_local_reachable(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("AUTOSWE_RUN_ID", "run-1")
        monkeypatch.setattr(probes, "probe_provider", lambda name, **kw: _ok(name))
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain == ["local"]

    def test_autoswe_local_unreachable_fails_closed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("AUTOSWE_RUN_ID", "run-1")
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_LOCAL_URL", "http://127.0.0.1:8010")
        monkeypatch.setattr(probes, "probe_provider", lambda name, **kw: _bad(name))
        d = hook._decide_chain(_inv())
        assert d.kind == "fail"
        assert "unreachable" in d.reason
        assert "127.0.0.1:8010" in d.reason


class TestInteractivePicker:
    def test_sticky_selection_reprobed_ok(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        picker.write_selection("sess", "opus")
        monkeypatch.setattr(probes, "probe_provider", lambda name, **kw: _ok(name))
        d = hook._decide_chain(_inv("sess"))
        assert d.kind == "proceed" and d.chain == ["opus"]

    def test_sticky_selection_stale_auth_reasks(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        picker.write_selection("sess", "opus")
        monkeypatch.setattr(probes, "probe_provider", lambda name, **kw: _bad(name))
        monkeypatch.setattr(probes, "available_backends", lambda **kw: [_ok("codex")])
        d = hook._decide_chain(_inv("sess"))
        assert d.kind == "deny" and d.health == "picker_prompt"
        # the stale selection was cleared
        assert picker.read_selection("sess") is None

    def test_no_selection_prompts(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(probes, "available_backends", lambda **kw: [_ok("opus"), _ok("codex")])
        d = hook._decide_chain(_inv("fresh"))
        assert d.kind == "deny" and d.health == "picker_prompt"
        assert "opus" in d.context

    def test_no_backend_available_fails(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(probes, "available_backends", lambda **kw: [])
        d = hook._decide_chain(_inv("fresh"))
        assert d.kind == "fail" and d.health == "no_backend_available"

    def test_loop_guard_fails_closed(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(probes, "available_backends", lambda **kw: [_ok("opus")])
        # exhaust the attempt budget without ever selecting
        for _ in range(picker.MAX_PICKER_ATTEMPTS):
            assert hook._decide_chain(_inv("loop")).health == "picker_prompt"
        d = hook._decide_chain(_inv("loop"))
        assert d.kind == "deny" and d.health == "picker_unselected"


class TestAutosweOverride:
    def test_explicit_chain_overrides_local(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # qwen is the autoswe default, not a hard lock: an explicit chain wins.
        monkeypatch.setenv("AUTOSWE_RUN_ID", "run-1")
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_CHAIN", "opus")
        monkeypatch.setattr(probes, "probe_provider", lambda name, **kw: _ok(name))
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain is None  # resolve_chain handles "opus"

    def test_autoselect_overrides_local(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("AUTOSWE_RUN_ID", "run-1")
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_AUTOSELECT", "opus")
        d = hook._decide_chain(_inv())
        assert d.kind == "proceed" and d.chain == ["opus"]

    def test_local_offered_in_picker_when_url_set(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # autosre claude: LOCAL_URL set, no AUTOSWE_RUN_ID -> picker incl. local.
        monkeypatch.setenv("CLAUDE_PLAN_REVIEW_LOCAL_URL", "http://x:8010")
        monkeypatch.setattr(probes, "available_backends", lambda **kw: [_ok("local"), _ok("opus")])
        d = hook._decide_chain(_inv("fresh-local"))
        assert d.kind == "deny" and d.health == "picker_prompt"
        assert "local" in d.context
