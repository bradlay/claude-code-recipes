# agent-fleet

Skills for running several Claude Code or Codex sessions as one coordinated
fleet, plus a few standalone engineering workflows that fit well inside one.

## The fleet model

Four roles, each in its own session:

| Role | Job |
|---|---|
| operator | The only session the human talks to. Relays directives and decisions. Never executes. |
| leader | Decides, gates plans and results, handles succession. Never implements. |
| dispatcher | Spawns and briefs workers, relays status, runs commit, push and deploy commands on gated work. Never authors. |
| workers | Do the actual work inside assigned paths, then report and hold. |

The skills encode the rules that keep this working over many hours: plan and
result gates, explicit briefs, idle-with-output detection, context-based
succession from an authoritative figure, a push window for a shared checkout,
and a single channel for operator decisions so a question never freezes the
fleet.

## Skills

| Skill | What it does | Requires | Optional |
|---|---|---|---|
| `fleet-setup` | Structure contract, operator invariants, stand-up and verify | a way to run several sessions | Herdr, Codex, role-boundary hooks |
| `fleet-lead` | Leader playbook: briefing, gates, supervision, succession, push window | fleet-setup | |
| `fleet-sweep` | Periodic stock-take, classification, recovery, C.1 to C.7 compliance check | fleet-setup | a scheduler |
| `codebase-scan` | Batched subagent scan of a repository with a synthesis pass | git (or Glob) | python3 for JSON checks |
| `dependency-scan` | Vulnerability and outdated-package scan with native tools | the ecosystem's tools | pip-audit, cargo-audit, govulncheck |
| `pr-ci-watch` | Report-only CI status of open PRs | `gh` | |
| `deploy-health-watch` | Poll health endpoints after a deploy | `curl` | |
| `release-readiness` | Read-only go/no-go for an upgrade or promotion | git | `gh` |

Runtime adapters live in `skills/fleet-setup/references/`: `herdr.md` (Herdr
terminal multiplexer), `claude-code.md` (Monitor, scheduling, optional
role-boundary hooks), `codex.md` (Codex native task tree).

## Adapting to your setup

- Placeholders such as `<coordinator-model>`, `<fast-model>`, `<lower>` and
  `<upper>` are yours to fill in. Pick models by role: the strongest for the
  leader, a fast one for the operator and dispatcher.
- Commands for a specific multiplexer appear only in its adapter reference.
  Check them against `--help` for your installed version.
- Nothing here installs a scheduler or a hook by itself. Supervision claims
  hold only once you have observed the schedule or monitor firing.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install agent-fleet@claude-code-recipes
/reload-plugins
```

## Disable / uninstall

```text
/plugin disable agent-fleet@claude-code-recipes
/plugin uninstall agent-fleet@claude-code-recipes
```
