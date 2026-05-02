# stop-session-check

A Stop hook that runs at session end and surfaces a completion
checklist: uncommitted changes, branch ahead of remote, recent plan
files, plus repo-type-specific test/deploy hints. **Blocks** the stop
when there are uncommitted changes or unpushed commits so you don't
walk away from half-staged work.

## What it does

When the session is about to end:

1. Detects the current git repo (skips silently if not in one).
2. Detects repo type from file markers: Python (pyproject.toml), Node
   (package.json), Cloudflare Worker (wrangler.toml).
3. Builds a checklist of items, each with a status:
   - **`done`** — already taken care of (e.g. all changes committed).
   - **`todo`** — blocking; the session won't stop until resolved.
   - **`info`** — informational hint (e.g. "consider running tests").
4. If any items are `todo`, emits a `decision: block` envelope with the
   full checklist as the reason. The host CLI surfaces this; the model
   can address it (commit, push) and re-attempt the stop.
5. If there are no `todo` items, emits a pass-through `{}` and the
   session stops normally.

## Recursive-stop bypass

When the host CLI re-invokes the stop hook (after the model addressed a
prior block), the payload sets `stop_hook_active: true`. The hook
detects this and lets the stop go unconditionally so you're never
trapped behind a recursive block.

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
| Uncommitted changes | `done` if 0; `todo` otherwise |
| Branch ahead of remote | `done` if 0 ahead; `todo` if N ahead or no upstream |
| Test hint (pytest, pnpm test, pnpm playwright test) | `info` if matching markers exist |
| Deploy hint (wrangler deploy) | `info` if Cloudflare Worker detected |
| Recent local plan file | `info` if `<repo>/.claude/plans/*.md` modified in last 30 min |
| Many uncommitted (>5) | `todo` — nudges toward smaller commits |

`done` and `info` are non-blocking. `todo` blocks the stop.

## Failure mode

- Any error inside the hook returns `{}` (allow stop). The user is
  never wedged.
- Recursive invocation (`stop_hook_active: true`) always allows the
  stop.
- Not in a git repo: `{}` (allow stop).

## Disable / uninstall

```text
/plugin disable stop-session-check@claude-code-recipes
/plugin uninstall stop-session-check@claude-code-recipes
```
