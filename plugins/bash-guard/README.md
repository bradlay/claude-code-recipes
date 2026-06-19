# bash-guard

A PreToolUse(Bash) hook that evaluates every shell command against a
configurable rule set before it runs. Default rules block the obvious
foot-guns (`rm -rf /`, `git reset --hard`, `--no-verify`, untrusted
`curl | sh`, identity-management commands) and stay out of the way for
everything else.

## Read first

This is a **fail-closed gate**. Misconfiguration (rules file missing,
Python missing, broken regex) returns deny by default — set
`CLAUDE_BASH_GUARD_FAIL_OPEN=1` to bypass during outages.

The default rule set ships ~20 rules and is intentionally conservative:
it blocks irreversible destruction and known bypass patterns, and it
asks (via deny + retry message) for ambiguous commands like
`git push --force` or `chmod 777`. It does NOT push opinions about
where you should run linters, package managers, or how you should
structure your repo.

## Platform

macOS and Linux. Hook entry point is a POSIX shell launcher.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher denies all
  Bash commands with a remediation message; bypass with
  `CLAUDE_BASH_GUARD_FAIL_OPEN=1`.
- `PyYAML` (`pip install pyyaml`). Used to parse the rules file.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install bash-guard@claude-code-recipes
/reload-plugins
```

`/reload-plugins` (or a fresh session) is required.

## Default rules

| Category | Sample rules | Decision |
|---|---|---|
| Filesystem catastrophes | `rm -rf /`, `dd of=/dev/sd*`, `mkfs`, `> /dev/sd*` | deny |
| Filesystem warnings | `chmod 777` | ask |
| Git destructive | `git reset --hard`, `git checkout --`, `git restore`, `git clean -fd` | deny |
| Git history-rewriting | `git push --force` (without `--force-with-lease`), `--no-verify` on commit/push | deny / ask |
| Git stash destructive | `git stash drop` / `clear` | ask |
| Network untrusted | `curl ... \| sh`, `eval $(curl ...)`, `bash <(curl ...)`, `base64 -d \| sh` | deny / ask |
| Identity / auth | `gh auth login/switch`, `git config --global/--system`, `wrangler/cloudflared login`, `npm login` | ask |

`ask` decisions emit a deny with a re-run-to-approve message — the user
re-runs the same command within the approval window and it goes
through. (PreToolUse hooks emit allow or deny only; `ask` is implemented
as a deny-with-marker.)

## Customizing rules

Three ways, in priority order:

1. **Set `CLAUDE_BASH_GUARD_RULES_FILE=/abs/path/to/rules.yaml`** —
   explicit override; useful for testing.
2. **Place rules at `${XDG_CONFIG_HOME}/claude-bash-guard/rules.yaml`**
   (default `~/.config/claude-bash-guard/rules.yaml`) — picked up
   automatically per machine.
3. **Edit the shipped default at**
   `${CLAUDE_PLUGIN_ROOT}/scripts/default-rules.yaml` — local edits
   are wiped on plugin update; use this for one-off experiments.

Rule schema:

```yaml
- id: my-rule           # unique
  category: my-cat      # cosmetic
  pattern: '^...regex'  # regex against the (normalized) command
  decision: deny        # deny | ask | allow
  reason: "..."         # message shown to Claude
  search: false         # use re.search instead of re.match
  extra_search: '--x'   # optional second regex; both must match (AND)
```

Rules are evaluated in order; first match wins.

## Configuration

| Variable | Effect |
|---|---|
| `CLAUDE_BASH_GUARD_RULES_FILE` | Explicit rules file path. |
| `CLAUDE_BASH_GUARD_FAIL_OPEN` | Set to `1` to allow commands through when prereqs fail. Default denies. |
| `CLAUDE_BASH_GUARD_DUMP_DIR` | Dump each raw hook stdin to this directory (for fixture collection). |

## State and logs

Under `${CLAUDE_PLUGIN_DATA}/`:

- `approvals/` — one-shot approval tokens for `ask` decisions.
- `blocked.log` — JSON-lines audit of every blocked command.
- `errors.log` — guard internal errors (fail-closed path).
- `hooks/` — per-event hook activity logs and a JSONL archive of every
  hook stdin (capped 20 MB, rotated).
- `health.json` — last hook outcome.

## How chains are handled

The guard splits compound commands on **sequence** separators
(`&&`, `||`, `;`, `&`, newlines) and evaluates each sub-command
independently — so `cd /tmp && rm -rf /` cannot bypass an anchored rule
by hiding the dangerous part second.

**Pipes are NOT split**: pipelines like `base64 -d | bash` need to be
seen as a unit so anti-pipe-to-shell rules can match. Rules that target
sub-commands inside a pipeline can still use `search: true` to scan
across the whole pipeline.

`git -C <path>` is normalized away before pattern matching so a rule
anchored as `^git\s+push` still fires when called as
`git -C /repo push`.

The evaluator tracks `cwd` across leading `cd <path>` statements in
the chain, matching bash semantics:

```
cd /home/me/repos/foo && git checkout main
```

Here the second sub-command's effective target dir is
`/home/me/repos/foo` even though the hook payload's `cwd` was
elsewhere. A `git -C <path>` prefix overrides per-sub-command — so
`git -C /home/me/repos/foo checkout main` from any starting cwd
resolves the target to `/home/me/repos/foo`.

## Deploy-worktree exemption

Some setups keep a deploy worktree of `main` next to the dev worktree:

```
~/repos/foo         # branch main, runs the live service
~/repos/foo-dev     # branch dev, where work happens
```

A "stay on dev; PR to main" rule should fire in `repos/foo-dev` but
NOT in `repos/foo` — the deploy copy legitimately runs
`git checkout main / pull / ff-merge`. Tag those rules with a category
listed in `settings.exempt_categories_for_deploy_worktree`:

```yaml
settings:
  exempt_categories_for_deploy_worktree: ["git-protected"]

rules:
  - id: stay-on-dev-checkout
    category: git-protected
    pattern: '^git\s+(-C\s*\S+\s+)?checkout\s+(main|master)\b'
    decision: deny
    reason: "Stay on dev. Use PR to land on main."
```

The exemption fires when the sub-command's effective target dir
(from `cd` tracking or a `git -C <path>` prefix) is inside a
`repos/<name>` directory where `<name>` does NOT end in `-dev`. The
monorepo root and any `*-dev` worktree stay fully protected.
Filesystem and history-rewriting rules live in different categories
and continue to fire everywhere — only categories you explicitly list
are exempt.

## Settings inheritance

`settings:` keys are merged across the plugin's defaults and any user
override at `~/.config/claude-bash-guard/rules.yaml`. Keys in the user
file always win; keys it omits inherit from the defaults. This means
the deploy-worktree exemption above stays active even on machines
where the user override file silently doesn't carry the
`exempt_categories_for_deploy_worktree` line.

`rules:` are NOT merged — when a user override exists, its rules
fully replace the defaults. If you want to add to defaults rather than
replace them, copy the default-rules.yaml as a starting point.

## Approving an `ask` decision

When the guard returns `ask`, you'll get a deny with a message saying
"re-run within 60s to acknowledge". Re-run the exact same command
within the approval window. The token is one-shot.

To increase the window, override `settings.approval_expiry_seconds` in
your rules.yaml.

## Disable / uninstall

```text
/plugin disable bash-guard@claude-code-recipes
/plugin uninstall bash-guard@claude-code-recipes
```
