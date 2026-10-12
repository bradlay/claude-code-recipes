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
| [`posttooluse-bash-audit`](./plugins/posttooluse-bash-audit/) | Appends every executed Bash command (timestamp + session id + summary) to a local audit log. Always fail-open. |
| [`branch-warn`](./plugins/branch-warn/) | Emits "on branch X" once per hour on UserPromptSubmit. Quiet on feature branches, louder on `main`/`master`. |
| [`stop-session-check`](./plugins/stop-session-check/) | Stop hook that blocks session-end when there are uncommitted changes or unpushed commits. Surfaces a repo-type-aware completion checklist. |
| [`putty-bridge`](./plugins/putty-bridge/) | Generates a Windows PuTTY `.reg` plus a tmux snippet for running Claude Code in remote tmux with UTF-8, true colour, native copy/paste and secure defaults. |
| [`command-discovery`](./plugins/command-discovery/) | After a failed CLI call with an unknown or mistyped subcommand, suggests the closest known one so the model corrects itself. The command inventory is configurable for any CLI. |
| [`quota-advisory`](./plugins/quota-advisory/) | Stop hook that posts a one-line advisory when the active usage block crosses a configurable high-water mark, nudging `/compact`. Cached and budget-capped. |
| [`stop-memory-hygiene`](./plugins/stop-memory-hygiene/) | Stop hook that watches the auto-memory `MEMORY.md` index (size, long lines, unlinked topic files) and suggests a prune, at most once a day per project. Never blocks. |
| [`agent-fleet`](./plugins/agent-fleet/) | Skills for running several agent sessions as one fleet (operator, leader, dispatcher, workers) with gates, succession and liveness checks, plus scan, dependency, CI, deploy-health and release-readiness workflows. |

## Development

After cloning, activate the tracked git hooks so every push runs the
same gates CI does:

```sh
./scripts/install-hooks.sh
```

This sets `core.hooksPath` to `scripts/hooks/`. The pre-push hook
invokes `scripts/check.sh`, which mirrors `.github/workflows/ci.yml`
exactly: ruff, mypy, pytest, manifest validation, namespace gate,
hook-quoting gate, executable-bit gate, shellcheck, and `claude
plugin validate`. Bypass for emergency hotfixes only:

```sh
SKIP_PREPUSH=1 git push ...
```

## License

MIT.
