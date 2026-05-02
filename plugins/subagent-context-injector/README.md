# subagent-context-injector

Subagents (`Plan`, `Explore`, etc.) start with their own context window
and don't inherit the parent session's project awareness. This plugin
injects a small project briefing — CLAUDE.md, `.claude/rules/*.md`
headings, current branch + git status, and the top-level directory
listing — as `additionalContext` on every Plan and Explore subagent
start.

Total budget: 12,000 characters. Truncates long inputs at section
budgets (CLAUDE.md gets 8K, the rest 800 each).

## Why

Without this hook a fresh Plan subagent has to either ask "what's this
project?" or guess. Either is wasted context. The injected briefing
mirrors what a human would tell a colleague before asking them to plan
something.

## Platform

macOS and Linux. Hook entry point is a POSIX shell launcher.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher exits 0 and the
  subagent boots without the injection (fail-open: this hook is
  informational, not a gate).
- `git` is optional — if present, the briefing includes branch + recent
  commits + working-tree status; if missing, that section is skipped.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install subagent-context-injector@claude-code-recipes
/reload-plugins
```

`/reload-plugins` (or a fresh session) is required.

## Sections injected

| Section | What | Budget |
|---|---|---|
| CLAUDE.md | First N chars of the project root's CLAUDE.md | 8000 |
| Rules | First-line summary per `.claude/rules/*.md` | 800 |
| Git state | Current branch, working-tree status, last 5 commits | 800 |
| Structure | Top-level dirs and files (hidden ones skipped) | 800 |

## Tunables

There are no env vars yet. If you want different budgets, fork
`scripts/subagent_context_hook.py` and edit the constants:

- `TOTAL_BUDGET` (default `12000`)
- `_BUDGET_CLAUDE_MD`, `_BUDGET_RULES`, `_BUDGET_GIT`,
  `_BUDGET_STRUCTURE`

## State and logs

Under `${CLAUDE_PLUGIN_DATA}/`:

- `hooks/` — per-event hook activity logs and a JSONL archive of every
  hook stdin (capped 20 MB, rotated).

That's it. This hook doesn't write any decision state because it
doesn't make decisions.

## Failure mode

Any error inside the hook (CLAUDE.md unreadable, git command timed
out, surprise `OSError` from `iterdir`) returns an empty pass-through
`{}` and the subagent starts without the injection. The hook never
blocks subagent startup.

## Disable / uninstall

```text
/plugin disable subagent-context-injector@claude-code-recipes
/plugin uninstall subagent-context-injector@claude-code-recipes
```
