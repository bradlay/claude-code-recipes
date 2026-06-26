"""The local provider's focused (autoswe) system-prompt switch."""

from __future__ import annotations

from _lib import local_provider


def test_focused_marker_selects_focused_prompt(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_PLAN_REVIEW_LOCAL_FOCUSED", "1")
    assert local_provider._system_prompt() == local_provider._FOCUSED_SYSTEM_PROMPT


def test_default_uses_full_prompt(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CLAUDE_PLAN_REVIEW_LOCAL_FOCUSED", raising=False)
    assert local_provider._system_prompt() == local_provider._SYSTEM_PROMPT


def test_focused_prompt_keeps_json_schema(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Parser depends on the findings/questions schema being present.
    assert '"findings"' in local_provider._FOCUSED_SYSTEM_PROMPT
    assert '"questions"' in local_provider._FOCUSED_SYSTEM_PROMPT
