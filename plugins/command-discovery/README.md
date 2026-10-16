# command-discovery

PostToolUse(Bash) hook that watches for a failed invocation of a configured
CLI vendor (an unknown/typo'd subcommand) and returns a did-you-mean
suggestion via `additionalContext`, so the model self-corrects on the next
turn instead of guessing again.

- **Never blocks.** PostToolUse runs after the command already executed;
  this hook only ever adds an advisory line, never re-runs anything or
  mutates state.
- **Deterministic trigger.** It only fires on a small set of well-known "no
  such command" idioms used by common CLI frameworks (click, argparse-style,
  Cobra/urfave-style). A runtime failure (network, auth, a permissions
  error) does not match those idioms, so the hook stays silent on
  legitimate failures.
- **Fail-silent.** Any error, missing config, or no close match emits
  nothing. An advisory hook must never be able to break a session.
- **Bounded work.** Tool output and the inventory file are both capped
  before they're processed, and candidate ranking is capped to a small,
  fixed number of results, so a huge or hostile input can never turn this
  into unbounded work.

## Configuration

This plugin has no built-in knowledge of any specific CLI. You point it at
a small inventory file describing the vendor's program name and its known
subcommand paths:

```yaml
# ~/.config/claude-command-discovery/inventory.yaml
vendor: mycli
commands:
  - status
  - deploy prod
  - deploy staging
  - config set
```

`vendor` is the program name as typed on the command line. `commands` is a
flat list of known subcommand paths, each a space-separated sequence of
words exactly as a user would type them after the vendor name.

The inventory file location resolves in this order:

1. `$COMMAND_DISCOVERY_INVENTORY` (explicit path override).
2. `$XDG_CONFIG_HOME/claude-command-discovery/inventory.yaml`.
3. `~/.config/claude-command-discovery/inventory.yaml` (final fallback).

A missing or malformed inventory file is a normal, silent no-op state: the
hook simply never fires, and it is never created automatically.

## Requirements

`pyyaml` if your inventory file is YAML (the recommended format above). A
plain JSON inventory file works with no extra dependency.
