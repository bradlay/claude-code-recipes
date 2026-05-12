# stop-session-check

A Stop hook that runs at session end and surfaces an **advisory**
completion checklist: uncommitted changes, branch ahead of remote,
recent plan files, plus repo-type-specific test/deploy hints. It
nudges you to commit/push your in-flight work but never blocks the
session from ending.

## Why advisory, not blocking

`git status` and ahead-of-remote counts are properties of the working
tree, not of any one Claude session. When you run two Claude sessions
against the same repo, session B sees session A's dirty files and
unpushed commits — so blocking on those would trap B behind work it
did not author. This hook prints the checklist as a nudge for *your*
work and lets the session stop.

## What it does

When the session is about to end:

1. Detects the current git repo (skips silently if not in one).
2. Detects repo type from file markers: Python (pyproject.toml), Node
   (package.json), Cloudflare Worker (wrangler.toml).
3. Builds a checklist of items, each with a status:
   - **`done`** — already taken care of (e.g. all changes committed).
   - **`nudge`** — advisory; could be your work or another session's.
     Surfaced in the checklist, never blocks.
   - **`info`** — informational hint (e.g. "consider running tests").
4. Always emits a pass-through that allows the stop, attaching the
   checklist as a message if there's anything non-`done` to surface.

## Recursive-stop bypass

When the host CLI re-invokes the stop hook, the payload sets
`stop_hook_active: true`. The hook detects this and lets the stop go
through with no message, so re-entries are quiet.

## Platform

macOS and Linux. Hook entry point is a POSIX shell launcher.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher exits 0 and the
  session stops without the checklist.
- `git` is optional; missing git just means no checklist.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install stop-session-check@claude-code-recipes
/reload-plugins
```

## Checklist items

| Item | Status if... |
|---|---|
| Uncommitted changes | `done` if 0; `nudge` otherwise |
| Branch ahead of remote | `done` if 0 ahead; `nudge` if N ahead or no upstream |
| Test hint (pytest, pnpm test, pnpm playwright test) | `info` if matching markers exist |
| Deploy hint (wrangler deploy) | `info` if Cloudflare Worker detected |
| Recent local plan file | `info` if `<repo>/.claude/plans/*.md` modified in last 30 min |

All statuses are non-blocking; the hook always allows the stop.

## Failure mode

- Any error inside the hook returns `{}` (allow stop). The user is
  never wedged.
- Recursive invocation (`stop_hook_active: true`) always allows the
  stop quietly.
- Not in a git repo: `{}` (allow stop).

## Disable / uninstall

```text
/plugin disable stop-session-check@claude-code-recipes
/plugin uninstall stop-session-check@claude-code-recipes
```
