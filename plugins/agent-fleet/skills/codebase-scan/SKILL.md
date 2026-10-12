---
name: codebase-scan
description: Deep architectural analysis of a repository with a swarm of background subagents, one batch of files each, followed by a single cross-file synthesis pass. Invoke when the user asks for a full-repo audit, a deep architectural scan, cross-file pattern analysis, or the status of a scan already running. Results stay on disk so the orchestrator's context stays small.
user-invocable: true
---

# Codebase Scan

You orchestrate a scan; subagents do the reading. The orchestrator never reads
source files or result files itself, so its own context stays small (aim for
under about 10K tokens for the whole run).

Invocation: `/codebase-scan [path] [--dry-run] [--batch-tokens N] [--parallel N]`

- `path`: repository root (default: current directory).
- `--dry-run`: build the batch plan and stop.
- `--batch-tokens`: target tokens per batch (default 40000; estimate tokens as
  file size in bytes divided by 4).
- `--parallel`: workers per round (default 4).

## Session layout

Every run gets an explicit session ID, for example `scan-20260101-1200`, and its
own directory. Use the explicit ID in every step; never rely on "the latest".

```text
.codebase-scan/<session>/
  plan.json              # session id, repo root, batch list, totals
  batches/batch-NNN.json # {"batch_id", "repo_path", "files": [...], "output_file"}
  results/batch-NNN.json # written by workers
  results/synthesis.json # written by the synthesis pass
```

Suggest adding `.codebase-scan/` to `.gitignore` before the first run.

## Step 1: plan batches

1. List tracked source files with `git ls-files` (fall back to Glob outside a
   git repository). Exclude vendored code, lockfiles, generated output, and
   binaries.
2. Group files by top-level directory so related files land together, then cut
   each group into batches of about `--batch-tokens`.
3. Write `plan.json` and one `batches/batch-NNN.json` per batch, each naming its
   `output_file` under `results/`.

With `--dry-run`, report the batch count, file count and estimated tokens, then
stop.

## Step 2: pilot

Run two workers on the first two batches and wait for both. Check that each
result file exists, parses as JSON, and carries the required keys from
[WORKER-PROMPT.md](WORKER-PROMPT.md) (for example with
`python3 -m json.tool <file> > /dev/null`). If either fails, stop and report;
fix the prompt or the batching before spending a full run.

## Step 3: full run

Launch up to `--parallel` background subagents at a time, each with a fast model:

```text
Agent(
  subagent_type="general-purpose",
  model="<fast-model>",
  run_in_background=true,
  prompt="You are a codebase scan worker.
1. Read your instructions from: ${CLAUDE_PLUGIN_ROOT}/skills/codebase-scan/WORKER-PROMPT.md
2. Your batch definition file: <absolute path to batches/batch-NNN.json>
3. Follow the instructions and write your result file.
Return ONLY: Wrote N file analyses to <output_file>"
)
```

`${CLAUDE_PLUGIN_ROOT}` is the installed plugin's directory; if your setup does
not substitute it, give the absolute path of the `WORKER-PROMPT.md` that sits
next to this file.

Wait for every worker in the round to finish before starting the next. Report
`Round N: done/total, remaining`. Re-queue any batch whose result is missing or
invalid, once; report persistent failures instead of looping.

## Step 4: synthesis

Run one subagent with your strongest model:

```text
Agent(
  subagent_type="general-purpose",
  model="<coordinator-model>",
  prompt="Cross-file synthesis for codebase scan <session>.
Read every results/batch-*.json under <absolute session dir>.
Extract per file: file_path, purpose, exports, imports_from, env_vars_used,
architectural_role. In chunks of about 100 files identify dependency edges,
shared environment variables, circular import risks, implicit contracts
(shared data shapes without explicit types), and dead code (exports nobody
imports).
Write results/synthesis.json as
{\"cross_file_findings\": [{\"title\", \"severity\": \"critical|high|medium|low|info\",
\"category\": \"security|performance|architecture|code-quality|configuration\",
\"description\", \"recommendation\", \"confidence\"}]}.
Return ONLY the path and counts by severity."
)
```

## Step 5: report

Summarize counts by severity and category, list the high-confidence critical and
high findings with file and line, and give the session directory. The batch
results double as an index of what each file does: paste the relevant entries
into later Explore or Plan prompts so those agents start with known context
instead of rediscovering it.

## Status (pair with /loop)

`/codebase-scan --status <session>` reports, without starting work: batches
total, results present and valid, failed batches, and whether synthesis exists.
For example `/loop 2m /codebase-scan --status <session>` during a long run.

## Rules

- Never read batch or result files into the orchestrator's context beyond the
  existence and validity checks.
- Never use a session ID you did not create or read from `plan.json`.
- Workers write to disk and return one line. A worker that returns JSON in its
  reply has not followed the prompt; re-run it.
