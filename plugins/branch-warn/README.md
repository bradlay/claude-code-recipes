# branch-warn

A small UserPromptSubmit hook that emits "on branch X" once per hour as
a `systemMessage`. Catches the "wait, I'm on `main` and I should be on a
feature branch" footgun without nagging.

## What it does

Every time you submit a prompt, the hook checks the current git branch:

- **Protected branch** (default `main` or `master`): emit a louder
  `WARNING: on '<X>' branch. Consider a feature branch...` message.
- **Any other branch**: emit a quieter `On branch: <X>` hint.
- **Throttled to once per hour** (configurable). The marker resets when
  the throttle window expires.
- **Not in a git repo / git missing**: no-op.

## Platform

macOS and Linux.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher exits 0 and the
  prompt proceeds without the hint.
- `git` is optional; missing git just means no hint emitted.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install branch-warn@claude-code-recipes
/reload-plugins
```

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_BRANCH_WARN_THROTTLE_SECONDS` | `3600` | Throttle window. Set to e.g. `60` to test, or `86400` for daily. |
| `CLAUDE_BRANCH_WARN_PROTECTED` | `main,master` | Comma-separated. Branches in this list trigger the louder warning. |

## State

Under `${CLAUDE_PLUGIN_DATA}/`:

- `warned` — empty marker file. Its `mtime` is the last time the hook
  emitted a hint. Delete it to force the next prompt to emit again.

## Failure mode

Any error returns an empty pass-through `{}` and the prompt proceeds
normally. This hook never blocks user input.

## Disable / uninstall

```text
/plugin disable branch-warn@claude-code-recipes
/plugin uninstall branch-warn@claude-code-recipes
```
