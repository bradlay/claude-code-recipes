"""Tests for the identity-binding feature.

Coverage focuses on:

* Feature gate — ``settings.identity_bindings`` absent disables it.
* Cross-owner deny — token authenticates as login X but the command
  targets owner Y not in X's allowed list.
* Same-owner allow (no deny).
* Fail-open paths — missing token, missing ``gh``, failed probe, or
  unresolvable target owner all return None (the rules engine still
  fires for any matching destructive pattern).
* P0-4 regression — identity check runs BEFORE rule evaluation so an
  ``ask`` rule that the operator has already approved cannot leak a
  cross-owner write past the binding.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest

from _lib import guard, identity

# Setter returned by the `fake_gh` fixture: pass a login (or None) to make
# every identity probe in the test resolve to it.
_FakeGh = Callable[[str | None], None]


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force a fresh identity cache dir per test + bust the engine caches.

    The identity cache writes JSON under paths.data_dir(); pointing
    CLAUDE_PLUGIN_DATA at tmp keeps tests hermetic.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    guard._CACHE.clear()
    guard._TRUST_CACHE.clear()
    guard._PROJECT_CONFIG_LRU.clear()


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> _FakeGh:
    """Replace ``_identity_for_token`` with a controllable stub so we
    don't actually shell out to ``gh api /user``. The fixture returns
    a setter; tests call ``fake_gh("sddcinfo")`` to make every probe
    in the test return that login (or None to simulate failure)."""

    state: dict[str, str | None] = {"login": None}

    def stub(_token: str) -> str | None:
        return state["login"]

    monkeypatch.setattr(identity, "_identity_for_token", stub)

    def setter(login: str | None) -> None:
        state["login"] = login

    return setter


_BINDINGS = {"sddcinfo": ["sddcinfo"], "bradlay": ["bradlay"]}


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


class TestFeatureGate:
    def test_empty_bindings_disables_feature(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")
        fake_gh("sddcinfo")
        # Cross-owner push, but bindings={} → no enforcement.
        result = identity.check_identity(
            "git push https://github.com/bradlay/foo.git main",
            cwd=Path.cwd(),
            bindings={},
        )
        assert result is None

    def test_none_bindings_disables_feature(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_fake")
        fake_gh("sddcinfo")
        result = identity.check_identity(
            "git push https://github.com/bradlay/foo.git main",
            cwd=Path.cwd(),
            bindings=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Cross-owner deny + same-owner allow
# ---------------------------------------------------------------------------


class TestBinding:
    def test_cross_owner_push_denies(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_sddcinfo_pat")
        fake_gh("sddcinfo")
        reason = identity.check_identity(
            "git push https://github.com/bradlay/foo.git main",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert reason is not None
        assert "FORBIDDEN" in reason
        assert "'sddcinfo'" in reason
        assert "bradlay" in reason

    def test_same_owner_push_allows(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_sddcinfo_pat")
        fake_gh("sddcinfo")
        reason = identity.check_identity(
            "git push https://github.com/sddcinfo/foo.git main",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert reason is None

    def test_gh_secret_set_cross_owner_denies(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        """Owner resolution path covered today: ``--repo`` flag.
        Positional ``<owner>/<repo>`` after ``gh repo create`` is not
        currently extracted — that's a known gap shared with the
        legacy standalone identity-guard and would need explicit
        per-subcommand parsing to close."""
        monkeypatch.setenv("GH_TOKEN", "ghp_bradlay_pat")
        fake_gh("bradlay")
        reason = identity.check_identity(
            "gh secret set MYVAR --repo sddcinfo/foo --body shh",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert reason is not None
        assert "'bradlay'" in reason


# ---------------------------------------------------------------------------
# Fail-open inability paths
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_no_token_returns_none(self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        fake_gh("sddcinfo")
        result = identity.check_identity(
            "git push https://github.com/bradlay/foo.git main",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert result is None

    def test_failed_probe_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_unknown")
        fake_gh(None)  # simulate gh api /user failure
        result = identity.check_identity(
            "git push https://github.com/bradlay/foo.git main",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert result is None

    def test_non_write_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, fake_gh: _FakeGh
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_sddcinfo_pat")
        fake_gh("sddcinfo")
        # `gh pr list` is read-only — no enforcement.
        result = identity.check_identity(
            "gh pr list --repo bradlay/foo",
            cwd=Path.cwd(),
            bindings=_BINDINGS,
        )
        assert result is None


# ---------------------------------------------------------------------------
# P0-4 regression: identity pre-empts approved-ask
# ---------------------------------------------------------------------------


class TestIdentityBeforeRuleEval:
    """The standalone ``identity-guard.py`` ran BEFORE the regex guard
    so a wrong-token write was denied even if some rule would only
    ``ask``. Codex review iteration 2 flagged that running identity
    AFTER the rules would let ``git push --force ...`` hit the ``ask``
    path, get user-approved, and proceed without ever enforcing the
    owner/token binding. This test exercises ``evaluate_chain`` with
    a real approval token already on disk for the command — identity
    deny MUST still fire."""

    def test_approved_ask_is_overridden_by_identity_deny(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_gh: _FakeGh,
    ) -> None:
        # Rules file: an `ask` rule for `git push --force` + bindings.
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            dedent(
                """\
                settings:
                  approval_expiry_seconds: 60
                  identity_bindings:
                    sddcinfo: [sddcinfo]
                    bradlay: [bradlay]
                rules:
                  - id: ask-push-force
                    category: git-history
                    pattern: '^git\\s+push\\b'
                    extra_search: '\\s--force\\b'
                    decision: ask
                    reason: "git push --force overwrites remote history."
                """
            )
        )
        monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(rules_path))
        monkeypatch.setenv("GH_TOKEN", "ghp_sddcinfo_pat")
        fake_gh("sddcinfo")
        guard._CACHE.clear()

        cmd = "git push --force https://github.com/bradlay/test-repo main"

        # Pre-write an approval token for *cmd* — the would-be bypass.
        # We use the engine's own helper to ensure the hash matches.
        guard.write_approval(cmd)

        decision, reason, _ = guard.evaluate_chain(cmd, guard.load_rules(), cwd=Path.cwd())
        assert decision == "deny"
        assert "FORBIDDEN" in reason, (
            "Identity binding must deny BEFORE the rules engine consults "
            "the approval cache. A stale or operator-pasted approval for "
            "this command must not let a cross-owner write through."
        )

    def test_in_repo_push_still_allowed_when_owner_matches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_gh: _FakeGh
    ) -> None:
        """Sanity: when login matches the target, the chain proceeds
        to rule evaluation as usual."""
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            dedent(
                """\
                settings:
                  identity_bindings:
                    sddcinfo: [sddcinfo]
                rules: []
                """
            )
        )
        monkeypatch.setenv("CLAUDE_BASH_GUARD_RULES_FILE", str(rules_path))
        monkeypatch.setenv("GH_TOKEN", "ghp_sddcinfo_pat")
        fake_gh("sddcinfo")
        guard._CACHE.clear()

        decision, _, _ = guard.evaluate_chain(
            "git push https://github.com/sddcinfo/test-repo main",
            guard.load_rules(),
            cwd=Path.cwd(),
        )
        assert decision == "allow"


# ---------------------------------------------------------------------------
# Token cache hit (no subprocess on the second call)
# ---------------------------------------------------------------------------


class TestTokenCache:
    def test_cache_hit_skips_subprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Pre-populate the cache file the way _identity_for_token does.
        cache_dir = tmp_path / "plugin-data" / "identity"
        cache_dir.mkdir(parents=True)
        token = "ghp_cached"  # noqa: S105 - test fixture, not a real credential
        fp = identity._token_fingerprint(token)
        cache_file = cache_dir / f"{fp}.json"
        cache_file.write_text(json.dumps({"login": "sddcinfo", "ts": 0}))

        # Cause any subprocess call to fail loudly so we can prove
        # the cache short-circuit fires before the call site.
        def boom(*_a: object, **_k: object) -> object:  # pragma: no cover - failure-path guard
            raise AssertionError(
                "subprocess.run should not be called when the identity cache file is fresh."
            )

        # identity does `import subprocess`, so patching the module object
        # patches the same `run` the module calls — and avoids reaching
        # through identity for a non-reexported attribute.
        monkeypatch.setattr(subprocess, "run", boom)

        result = identity._identity_for_token(token)
        assert result == "sddcinfo"
