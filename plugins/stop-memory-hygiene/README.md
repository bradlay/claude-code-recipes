# stop-memory-hygiene

A Stop hook that keeps an eye on Claude Code's auto-memory index and
suggests a prune before it gets too large to load usefully. It never
blocks a stop.

## What it does

When a session stops, the hook finds the project's memory directory from
the `transcript_path` in the Stop payload
(`.claude/projects/<slug>/memory/`) and measures `MEMORY.md`:

- size over 110 KB,
- more than 60 index lines longer than 200 characters,
- more than 25 topic files in the memory directory that the index never
  links (by `[text](file.md)` or `[[name]]`).

If any threshold is crossed it emits one `systemMessage` naming the
problems and suggesting a prune pass. Otherwise it returns
`{"continue": true}`. Notices are rate-limited per project.

## Platform

macOS and Linux. Windows-style transcript paths are parsed, but the
launcher is a POSIX shell script.

## Requirements

- `python3` 3.10 or newer on `PATH`. If `python3` is missing, the launcher
  exits 0 and the stop proceeds with no notice.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install stop-memory-hygiene@claude-code-recipes
/reload-plugins
```

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_MEMORY_HYGIENE_SIZE_KB` | `110` | Index size budget in KB. |
| `CLAUDE_MEMORY_HYGIENE_LONG_LINES` | `60` | Allowed number of index lines over 200 characters. |
| `CLAUDE_MEMORY_HYGIENE_ORPHANS` | `25` | Allowed number of topic files not linked from the index. |
| `CLAUDE_MEMORY_HYGIENE_INTERVAL_SECONDS` | `86400` | Minimum time between notices for one project. |

## State

`${CLAUDE_PLUGIN_DATA}/<project-slug>.last-notice`: its `mtime` is the last
notice time for that project. Delete it to allow the next stop to report
again. If `CLAUDE_PLUGIN_DATA` is unset, the stamp is written as
`.hygiene-last-notice` inside the memory directory instead.

## Failure mode

Any error returns `{"continue": true}`. This hook never blocks a stop.

## Disable / uninstall

```text
/plugin disable stop-memory-hygiene@claude-code-recipes
/plugin uninstall stop-memory-hygiene@claude-code-recipes
```
