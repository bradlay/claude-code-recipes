# precompact-context-keeper

When the host CLI compacts the conversation, the post-compaction model
loses earlier turns. If the project framing was established only in
those earlier turns, the model wakes up on the other side without it.

This plugin runs on `PreCompact` and emits a `systemMessage` containing
CLAUDE.md (first 1500 chars) plus a tiny git-state summary (current
branch + uncommitted-changes summary). The post-compaction model sees
that as a system note and re-anchors on the project before continuing.

## Why

Compaction is silent. You don't always notice when it happens, and the
new model doesn't know what it forgot. A small re-injection from
PreCompact is a cheap safety net.

## Platform

macOS and Linux. Hook entry point is a POSIX shell launcher.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher exits 0 and
  compaction proceeds without the injection (fail-open: this hook is
  informational, not a gate).
- `git` is optional — if present the work-state section is included; if
  missing it's skipped silently.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install precompact-context-keeper@claude-code-recipes
/reload-plugins
```

## What it injects

```
PRESERVE ACROSS COMPACTION

---

## Project Config (CLAUDE.md)
<first 1500 chars of CLAUDE.md, truncation marker if longer>

---

## Current Work State
Branch: <current-branch>
Uncommitted:
<git status --short, capped at 15 lines>
```

If neither CLAUDE.md nor git state is available, the hook emits nothing
and compaction proceeds normally.

## State and logs

Under `${CLAUDE_PLUGIN_DATA}/`:

- `hooks/` — per-event hook activity logs and a JSONL archive of every
  hook stdin (capped 20 MB, rotated).

This hook keeps no decision state.

## Failure mode

Any error returns an empty pass-through `{}` and compaction proceeds.
Never blocks.

## Disable / uninstall

```text
/plugin disable precompact-context-keeper@claude-code-recipes
/plugin uninstall precompact-context-keeper@claude-code-recipes
```
