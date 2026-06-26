---
description: "Choose (or change) which backend reviews your plans on ExitPlanMode: Opus 4.8, Sonnet 4.6, codex (gpt-5.5), or Gemini 3.1 Pro. Only backends whose probe currently passes are offered."
argument-hint: "[clear]"
allowed-tools: [Bash, AskUserQuestion]
---

# /plan-review-loop:plan-review-backend

Pick the plan-review backend for this session. The choice is sticky: every
`ExitPlanMode` reviews against it until you change it or the session ends.

The user invoked this command with: `$ARGUMENTS`

## What to do

1. FIRST, check whether a backend is already pinned for this session:

   ```
   printenv CLAUDE_PLAN_REVIEW_CHAIN
   ```

   If that prints a non-empty value, the review backend is PINNED and there is
   nothing to choose. This is the case in an `autosre claude` (local) session,
   which pins `local` so reviews stay 100% on the local qwen. Tell the user the
   backend is pinned to that value and **STOP** — do NOT probe, do NOT show a
   picker. The picker is only for sessions where no chain is pinned (e.g. bare
   cloud `claude`).

2. If `$ARGUMENTS` contains `clear`, run:

   ```
   CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/bin/plan-review-select" --latest-session --clear
   ```

   Then tell the user the next `ExitPlanMode` will ask again, and stop.

3. Otherwise, find which backends are verified working right now:

   ```
   CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/bin/plan-review-probe" --online --json
   ```

   Only offer backend keys whose probe result is `ok` (one of: `opus`,
   `sonnet`, `codex`, `gemini`). If none pass, report that and stop.

4. Use **AskUserQuestion** to ask which backend to use, one option per
   verified key (label them: Opus 4.8 / Sonnet 4.6 / codex gpt-5.5 /
   Gemini 3.1 Pro).

5. Persist the choice (replace `<key>` with the chosen key):

   ```
   CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" "${CLAUDE_PLUGIN_ROOT}/bin/plan-review-select" --latest-session --reprobe <key>
   ```

   `--reprobe` confirms the backend is reachable before saving. Report the
   result to the user.

## Notes

- This is the same selection the `ExitPlanMode` hook prompts for; running it
  ahead of time means the hook won't interrupt your first plan exit.
- Under autoswe runs the backend is always the local qwen and is not asked.
- For a permanent non-interactive default, set `CLAUDE_PLAN_REVIEW_CHAIN=<key>`
  or `CLAUDE_PLAN_REVIEW_AUTOSELECT=<key>` in your environment.
