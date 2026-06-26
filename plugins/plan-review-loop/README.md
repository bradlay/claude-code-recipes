# plan-review-loop

Reviews every plan before plan-mode is allowed to exit. On the first
`ExitPlanMode` of a session it asks which backend to review with — Opus
4.8, Sonnet 4.6, codex (`gpt-5.5`), or Gemini 3.1 Pro (via the `agy`
gateway) — offering only backends that pass a live auth/model probe.
Blocking findings (P0/P1) deny the exit; the findings come back as
context so the next attempt has them in hand. The loop closes when the
plan is clean. Under `autoswe` runs the review happens locally against the
qwen vLLM with no prompt.

## Read first: data egress

This plugin sends plan content to external CLIs. Don't install it on
machines where that's a problem.

- The full plan markdown is sent to the selected backend's CLI: `codex`
  (OpenAI Codex CLI, `gpt-5.5` at `xhigh`), `agy` (multi-model gateway,
  serving Gemini 3.1 Pro), or `claude` (Anthropic Claude CLI, for the
  Opus/Sonnet self-review legs). Under `autoswe` the plan goes only to the
  local OpenAI-compatible vLLM at `CLAUDE_PLAN_REVIEW_LOCAL_URL`, not to
  any cloud CLI.
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
- At least one backend CLI on `PATH`: `claude` (for the Opus 4.8 /
  Sonnet 4.6 self-review legs), `codex` (`gpt-5.5`), or `agy` (the gateway
  serving Gemini 3.1 Pro). The picker offers only backends whose CLI is
  installed and whose auth/model probe passes; if none are usable the hook
  denies (bypass with `CLAUDE_PLAN_REVIEW_FAIL_OPEN=1`).
- For `autoswe`/local review, an OpenAI-compatible endpoint at
  `CLAUDE_PLAN_REVIEW_LOCAL_URL` (e.g. the autosre vLLM).

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
3. Resolve the review backend. If one is already chosen for the session
   (or set via `CLAUDE_PLAN_REVIEW_CHAIN` / `CLAUDE_PLAN_REVIEW_AUTOSELECT`,
   or forced to `local` under `autoswe`), use it. Otherwise deny once with
   a prompt listing the probe-verified backends; pick one (via
   `AskUserQuestion` + the printed `bin/plan-review-select` command, or
   `/plan-review-backend`) and re-run `ExitPlanMode`. The choice is sticky
   for the session.
4. Send the plan to the selected backend, re-probed immediately before use.
5. P0/P1 findings cause a deny + findings as `additionalContext`.
   P2-only causes an allow + advisory context. Clean causes an allow.
6. On clean, all state files for the plan path are removed so the next
   `ExitPlanMode` starts at iteration 1.

SessionStart hook: a preflight that probes every online backend (and
`local`) so the picker reads a warm cache and the session opens with each
backend's health.

## Choosing a backend

Backends offered in the picker (only those whose probe currently passes):

| Key | Backend | Model |
|---|---|---|
| `opus` | self-review via `claude` | `claude-opus-4-8` |
| `sonnet` | self-review via `claude` | `claude-sonnet-4-6` |
| `codex` | OpenAI Codex CLI | `gpt-5.5` (xhigh) |
| `gemini` | `agy` gateway | `Gemini 3.1 Pro (High)` |

The first `ExitPlanMode` of a session asks which to use; the choice is
sticky. Change it any time with `/plan-review-backend` (or
`/plan-review-backend clear` to be re-asked). Self-review (`opus`/`sonnet`)
runs Claude in a fresh, adversarial context — it is independent of the
planning session but shares model-family blind spots, so `codex`/`gemini`
add an outside view. To skip the prompt entirely, set
`CLAUDE_PLAN_REVIEW_CHAIN` or `CLAUDE_PLAN_REVIEW_AUTOSELECT`.

Under `autoswe` the backend is always the local qwen vLLM (proven reachable
before each review, no cloud fallback) and you are never prompted.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_PLAN_REVIEW_CHAIN` | unset | Comma-separated backend list; **bypasses the interactive picker**. Valid keys: `opus`, `sonnet`, `codex`, `gemini`, `local` (legacy `claude`→`sonnet`, `agy`→`gemini` accepted). Wins over `CLAUDE_PLAN_REVIEW_TIER`. |
| `CLAUDE_PLAN_REVIEW_AUTOSELECT` | unset | A single backend key reviewed non-interactively (skips the picker, never prompts). |
| `CLAUDE_PLAN_REVIEW_TIER` | `strict` | Tier preset used when neither the picker nor an explicit chain applies. `strict` → `codex,gemini,opus`. `fast` → `sonnet` only (cheap, seconds-per-review). Unknown values fall back to `strict`. |
| `CLAUDE_PLAN_REVIEW_OPUS_MODEL` / `_SONNET_MODEL` / `_CODEX_MODEL` / `_AGY_MODEL` | per backend | Override a backend's model id. Defaults: `claude-opus-4-8`, `claude-sonnet-4-6`, `gpt-5.5`, `Gemini 3.1 Pro (High)`. |
| `CLAUDE_PLAN_REVIEW_LOCAL_URL` | `http://localhost:8010` | OpenAI-compatible base URL for the `local` backend (autoswe points this at the qwen vLLM). |
| `CLAUDE_PLAN_REVIEW_LOCAL_FOCUSED` | unset | Set to `1` for a tighter, shorter local-review system prompt (autoswe sets this). |
| `CLAUDE_PLAN_REVIEW_FAIL_OPEN` | unset | Set to `1` to allow plan exit when prereqs fail or all providers fail. Default denies. |
| `CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY` | unset | Set to `1` to drop full prompt/stdout/stderr from per-cycle logs and keep only metadata. Default writes everything. |
| `CLAUDE_PLAN_REVIEW_PLAN_MAX_AGE_SECONDS` | `3600` | Max age of plans considered when falling back to "newest plan in plans dir". |
| `CLAUDE_PLAN_REVIEW_DUMP_DIR` | unset | Dump each raw hook stdin to this directory. |
| `CLAUDE_PLAN_FILE` | unset | Explicit plan file path; overrides discovery. |

## State and logs

Under `${CLAUDE_PLUGIN_DATA}/`:

- `review-state/`: per-plan iteration state, `.lock`, `.in-progress`.
- `review-log/`: per-iteration JSON dumps (full prompt and provider
  stdout by default). Records carry `result_status` (one of `ok`,
  `error`, `empty`, `unparseable`), `shadow_config_signature`, and
  `parse_error`. Older records without those fields get classified
  on read by the same rules. Non-shadow records pruned to 50 most
  recent; shadow records kept by time (8 days, with a 200-record
  fresh-install floor; override with
  `CLAUDE_PLAN_REVIEW_SHADOW_RETAIN_DAYS`).
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

## Inspecting shadow runs

Shadow runs (parallel reviews emitted post-decision when
`CLAUDE_PLAN_REVIEW_SHADOW` is set) land as `*_shadow.json`
under `review-log/`. The `plan-review-shadow` CLI surfaces them:

| Command | Purpose |
| --- | --- |
| `plan-review-shadow list` | Newest 20 shadow runs (filter `--since`, `--status ok\|fail\|all`). |
| `plan-review-shadow show latest` | Full record for the newest run. Pass a path to inspect a specific record. |
| `plan-review-shadow stats` | Both views: 7d history aggregates across all signatures, plus 24h health under the current signature. `--scope history` or `--scope current` to constrain. |

`--json` for machine output. `--help` for filters.

The `current` view is the same severity preflight emits at
SessionStart; pre-flight will also flag chronic shadow failure
(`degraded`, `critical`) until the operator resolves it.

## Troubleshooting

**Hook didn't fire.** `/reload-plugins` (or restart the host CLI).
Confirm with `/plugin list`.

**Deny: "all providers failed".** Install `codex` (or another provider
in your chain), or set `CLAUDE_PLAN_REVIEW_FAIL_OPEN=1`.

**Review takes minutes.** Codex `xhigh` reasoning runs 5 to 15 minutes
on complex plans. The hook timeout is 1260s. If you want a faster gate,
set `CLAUDE_PLAN_REVIEW_TIER=fast` (Sonnet at default reasoning is
seconds) — useful for routine plans where you don't need the paid chain.
`CLAUDE_PLAN_REVIEW_CHAIN=...` lets you spell out an arbitrary order
when neither tier preset fits.

**Two sessions can't review the same plan at once.** Correct: the
second is denied with the lock-busy message. Wait for the first to
finish.

**Stale `.in-progress` after a crash.** Detected automatically via
`os.kill(pid, 0)`. If it's wedged, delete the relevant
`${CLAUDE_PLUGIN_DATA}/review-state/*.in-progress`.

**Preflight reports `shadow degraded` / `shadow critical`.** Real
shadow reviews are failing under the current config. Run
`plan-review-shadow stats --scope current` for the in-scope rate
and consecutive-failure streak; `plan-review-shadow list --status fail`
to see recent failures. Common causes: the local backend is down
(check the URL/model env vars), or `CLAUDE_PLAN_REVIEW_LOCAL_MAX_TOKENS`
is too low for the model's reasoning budget (empty output classifies
as `empty`). Editing any of the shadow env vars rotates the
`shadow_config_signature` and clears in-scope failures naturally.

**Preflight reports `shadow warming`.** Zero in-scope shadow runs
under the current config — either freshly enabled or freshly
re-configured. Trigger one ExitPlanMode to validate.

## Disable / uninstall

```text
/plugin disable plan-review-loop@claude-code-recipes
/plugin uninstall plan-review-loop@claude-code-recipes
```
