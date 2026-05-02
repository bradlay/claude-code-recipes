# Changelog

All notable changes to plugins in this marketplace are recorded here. Each
plugin's entries are namespaced under its name.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the marketplace itself follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
loosely (per-plugin versions in their `plugin.json`).

## plan-review-loop

### 0.1.0 — 2026-05-02

Initial release.

- PreToolUse hook on `ExitPlanMode`. Runs Codex (default
  `gpt-5.4` at `xhigh` reasoning) over every plan; falls through to
  `gemini` and `claude` CLIs if installed and configured.
- Per-plan-path locking with deny-if-busy semantics. Concurrent reviews
  on the same plan path serialize.
- Atomic state writes; state files keyed by both plan path and content
  hash so plan edits start a fresh iteration counter.
- Stale-lock detection via `os.kill(pid, 0)`.
- POSIX shell launchers in `bin/` so the gate fails closed when
  `python3` is missing (set `CLAUDE_PLAN_REVIEW_FAIL_OPEN=1` to bypass).
- SessionStart preflight that surfaces missing prereqs once per session.
- 0700 dirs, 0600 files, log dir capped to 50 most recent entries.
- Full per-iteration audit logs by default (prompt + provider stdout +
  stderr); `CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY=1` opts out.
- CI: ruff, mypy --strict, shellcheck, pytest (55 unit tests),
  manifest validation, namespace-leak gate, executable-bit gate, end-to-end
  smoke test.
- CodeQL + gitleaks security workflows.
- Dependabot for GitHub Actions.
