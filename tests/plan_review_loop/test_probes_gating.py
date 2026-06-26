"""Unit tests for the probe-gated picker availability + cache freshness."""

from __future__ import annotations

import pytest
from _lib import probes


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))


def test_available_backends_filters_failures(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    outcomes = {
        "opus": (True, "ok"),
        "sonnet": (False, "no auth"),
        "codex": (True, "ok"),
        "gemini": (False, "not on PATH"),
    }
    monkeypatch.setattr(probes, "_run_probe", lambda name: outcomes[name])
    avail = probes.available_backends(force=True)
    assert {r.name for r in avail} == {"opus", "codex"}
    assert all(r.ok for r in avail)


def test_available_backends_empty_when_all_fail(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(probes, "_run_probe", lambda name: (False, "down"))
    assert probes.available_backends(force=True) == []


def test_probe_cache_then_reprobe_on_stale(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fake(name: str) -> tuple[bool, str]:
        calls.append(name)
        return True, "ok"

    monkeypatch.setattr(probes, "_run_probe", fake)

    first = probes.probe_provider("codex")
    assert first.cached is False
    second = probes.probe_provider("codex")  # within TTL -> cached
    assert second.cached is True
    # A short max_age forces a fresh probe (the picker's positive-TTL behavior).
    third = probes.probe_provider("codex", max_age=0)
    assert third.cached is False
    assert calls.count("codex") == 2
