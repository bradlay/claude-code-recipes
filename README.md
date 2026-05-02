# claude-code-recipes

Plugins I use day-to-day. Public so others can pull what's useful.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install <plugin-name>@claude-code-recipes
/reload-plugins
```

## Plugins

| Plugin | What it does |
|---|---|
| [`plan-review-loop`](./plugins/plan-review-loop/) | Runs Codex against every plan before the planner is allowed to exit plan mode. Blocking findings deny the exit and feed back as context for the next iteration. |
| [`bash-guard`](./plugins/bash-guard/) | Evaluates every Bash command against a configurable rule set. Default rules block destructive git ops, filesystem-nuking commands, untrusted `curl \| sh` patterns, and identity-management changes. |
| [`subagent-context-injector`](./plugins/subagent-context-injector/) | Injects CLAUDE.md, `.claude/rules/*.md`, git state, and top-level structure into Plan/Explore subagents so they don't boot cold. |
| [`precompact-context-keeper`](./plugins/precompact-context-keeper/) | Threads CLAUDE.md + git state across the compaction boundary as a `systemMessage` so the post-compaction model still has the project framing. |

## License

MIT.
