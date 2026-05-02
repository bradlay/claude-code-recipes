# Changelog

All notable changes to plugins in this marketplace are recorded here. Each
plugin's entries are namespaced under its name.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the marketplace itself follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
loosely (per-plugin versions in their `plugin.json`).

## posttooluse-bash-audit

### 0.1.0 — 2026-05-02

Initial release.

- PostToolUse(Bash) hook that appends every Bash command (timestamp +
  session id + 200-char summary, newlines collapsed) to
  `${CLAUDE_PLUGIN_DATA}/audit.log`.
- Always fail-open; never blocks the tool result.
- File permissions 0600 in 0700 dirs.
- 7 unit tests covering append behaviour, truncation, newline collapse,
  non-Bash skip, empty-command skip, and file mode.

## precompact-context-keeper

### 0.1.0 — 2026-05-02

Initial release.

- PreCompact hook that emits a `systemMessage` containing CLAUDE.md
  (first 1500 chars) plus current branch + uncommitted-changes
  summary, just before the host CLI compacts the conversation.
- Fail-open: any error returns a pass-through and compaction proceeds.
- 6 unit tests against synthetic project trees.

## subagent-context-injector

### 0.1.0 — 2026-05-02

Initial release.

- SubagentStart hook on `Plan` and `Explore` matchers.
- Injects project context as `additionalContext`: CLAUDE.md (first 8K),
  `.claude/rules/*.md` first-line summaries, current branch +
  working-tree status + last 5 commits, top-level directory listing.
- 12K total budget.
- Fail-open: any error returns a pass-through and the subagent starts
  without the injection.
- Resolves `git` via `shutil.which()` so the partial-path security
  warning doesn't fire.
- 11 unit tests against synthetic project trees.

## bash-guard

### 0.1.0 — 2026-05-02

Initial release.

- PreToolUse(Bash) hook with rules-based command evaluation.
- ~20 default rules covering filesystem catastrophes, destructive git
  ops, history-rewriting, untrusted network execution, and identity
  changes.
- Three decision types: `deny` (block), `ask` (one-shot approval),
  `allow` (explicit allowlist).
- Compound-command splitting on sequence separators (`&&`, `||`, `;`,
  `&`, newlines); pipelines preserved as units so anti-pipe-to-shell
  rules see the full pipeline.
- `git -C <path>` normalization so anchored rules still fire.
- User overrides via `${XDG_CONFIG_HOME}/claude-bash-guard/rules.yaml`
  or `CLAUDE_BASH_GUARD_RULES_FILE`.
- Fail-closed default; `CLAUDE_BASH_GUARD_FAIL_OPEN=1` bypass.
- POSIX shell launcher fails closed when `python3` is missing.
- 0700 dirs, 0600 files for state and logs.
- 23 unit tests + CI smoke matrix exercising representative commands.

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
