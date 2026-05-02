# plan-review-loop

Runs Codex over every plan before plan-mode is allowed to exit. Blocking
findings (P0/P1) deny the exit; the findings come back as context so the
next attempt has them in hand. The loop closes when the plan is clean.

## Read first: data egress

This plugin sends plan content to external CLIs. Don't install it on
machines where that's a problem.

- The full plan markdown is sent to whichever provider runs first in
  `CLAUDE_PLAN_REVIEW_CHAIN`. Default chain is `codex,gemini,claude`:
  `codex` (OpenAI Codex CLI, `gpt-5.4` at `xhigh` reasoning) is the
  primary; `gemini` (Google Gemini CLI) and `claude` (Anthropic Claude
  CLI) are fallbacks tried in order if the previous one fails.
- On re-review, prior findings are sent too.
- Hooks run with your local user permissions.
- Don't use this on plans containing secrets, customer data, or
  proprietary code.

On disk, per-cycle log files default to **full content**: prompt,
provider stdout, stderr, findings, and metadata. This is intentional so
the loop is auditable and reproducible. Set
`CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY=1` to drop content fields and
keep only timestamps, sizes, and findings counts. Files are 0600 in
0700 dirs; the log dir is pruned to the 50 most recent entries.

## Platform

macOS and Linux. Hook entry points are POSIX shell launchers.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher denies plan
  exit with a remediation message; set `CLAUDE_PLAN_REVIEW_FAIL_OPEN=1`
  to bypass.
- `codex` CLI on `PATH`. Required by the default chain. If missing, the
  hook tries the next provider; if all fail, denies (same bypass).
- `gemini` and `claude` CLIs are optional fallbacks. Only consulted
  when listed in `CLAUDE_PLAN_REVIEW_CHAIN` (they are by default).

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install plan-review-loop@claude-code-recipes
/reload-plugins
```

`/reload-plugins` is required; newly enabled plugins don't apply to the
current session.

## What runs when

PreToolUse hook on `ExitPlanMode`:

1. Resolve the plan file from the hook payload (falls back to newest
   `*.md` in `${CLAUDE_CONFIG_DIR}/plans/` within the last hour).
2. Acquire a per-plan-path lock. Concurrent reviews on the same plan
   serialize; the second one is denied with "review already in progress
   (started Xs ago, pid Y)".
3. Send the plan to the first provider in the chain. Try the next on
   failure.
4. P0/P1 findings cause a deny + findings as `additionalContext`.
   P2-only causes an allow + advisory context. Clean causes an allow.
5. On clean, all state files for the plan path are removed so the next
   `ExitPlanMode` starts at iteration 1.

SessionStart hook: a preflight that reports missing prereqs at the start
of each session. Cached so unchanged status doesn't re-emit.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_PLAN_REVIEW_CHAIN` | `codex,gemini,claude` | Comma-separated provider list. Tried in order; first to return a clean response wins. Valid: `codex`, `gemini`, `claude`. |
| `CLAUDE_PLAN_REVIEW_FAIL_OPEN` | unset | Set to `1` to allow plan exit when prereqs fail or all providers fail. Default denies. |
| `CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY` | unset | Set to `1` to drop full prompt/stdout/stderr from per-cycle logs and keep only metadata. Default writes everything. |
| `CLAUDE_PLAN_REVIEW_PLAN_MAX_AGE_SECONDS` | `3600` | Max age of plans considered when falling back to "newest plan in plans dir". |
| `CLAUDE_PLAN_REVIEW_DUMP_DIR` | unset | Dump each raw hook stdin to this directory. |
| `CLAUDE_PLAN_FILE` | unset | Explicit plan file path; overrides discovery. |

## State and logs

Under `${CLAUDE_PLUGIN_DATA}/`:

- `review-state/`: per-plan iteration state, `.lock`, `.in-progress`.
- `review-log/`: per-iteration JSON dumps (full prompt and provider
  stdout by default). Pruned to 50 most recent.
- `review-chain.log`: append-only chain-execution log with timestamps
  for each provider attempt.
- `hooks/`: per-event hook activity logs and a JSONL archive of every
  hook stdin (capped 20 MB, rotated).
- `health.json`: last hook outcome.
- `preflight.json`: last preflight report.

Ad-hoc CLI invocations outside the plugin runtime fall back to
`${XDG_DATA_HOME}/claude-plan-review/` then
`~/.local/share/claude-plan-review/`.

## Ad-hoc CLI

```text
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_runner.py /path/to/plan.md
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_runner.py /path/to/plan.md --json-output
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_runner.py /path/to/plan.md --reset
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_runner.py /path/to/plan.md --chain codex,claude
```

Same code path as the hook. Iteration state and locking are shared.

## Troubleshooting

**Hook didn't fire.** `/reload-plugins` (or restart the host CLI).
Confirm with `/plugin list`.

**Deny: "all providers failed".** Install `codex` (or another provider
in your chain), or set `CLAUDE_PLAN_REVIEW_FAIL_OPEN=1`.

**Review takes minutes.** Codex `xhigh` reasoning runs 5 to 15 minutes
on complex plans. The hook timeout is 1260s. If you want a faster gate,
set `CLAUDE_PLAN_REVIEW_CHAIN=claude` (Sonnet at default reasoning is
seconds).

**Two sessions can't review the same plan at once.** Correct: the
second is denied with the lock-busy message. Wait for the first to
finish.

**Stale `.in-progress` after a crash.** Detected automatically via
`os.kill(pid, 0)`. If it's wedged, delete the relevant
`${CLAUDE_PLUGIN_DATA}/review-state/*.in-progress`.

## Disable / uninstall

```text
/plugin disable plan-review-loop@claude-code-recipes
/plugin uninstall plan-review-loop@claude-code-recipes
```
