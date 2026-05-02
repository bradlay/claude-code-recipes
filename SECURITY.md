# Security

## Reporting a vulnerability

Email: `security@lay.jp`

Please include enough detail to reproduce the issue. We aim to acknowledge
reports within 72 hours.

## Threat model for plugins in this repo

These plugins run as hooks inside the host CLI's process tree, with the
local user's permissions. They read files, fork subprocesses, and write to
plugin-managed state directories. They do not run network listeners.

### `plan-review-loop` specific

**Data egress**: every plan exit sends the full plan markdown to whichever
provider runs first in `CLAUDE_PLAN_REVIEW_CHAIN` (default: `codex`,
falling through to `gemini` and `claude` if those are also installed). On
re-review, prior findings are sent too. Per-cycle log files default to
storing the full prompt and provider output on disk so the loop is
auditable. See the plugin README for the opt-out
(`CLAUDE_PLAN_REVIEW_LOGS_METADATA_ONLY=1`).

**Don't install** the plugin on machines where plan content would
include secrets, customer data, or proprietary code you wouldn't paste
into the corresponding chat product.

**Lock and state files** are written under `${CLAUDE_PLUGIN_DATA}` with
mode 0600 in directories created 0700. Inheritance from a permissive
umask is overridden via explicit `chmod`.

**Subprocess execution** is `subprocess.run([...], shell=False)` with
list-form args; no shell metacharacter expansion. The prompt content is
passed as a final positional argument to the provider CLI, never
interpolated into a shell command.

**Hook input** is JSON over stdin. We never `eval` or `exec` it. Plan
files are read with `Path.read_text()`. Plan paths are validated to
exist before use.

**Stale-lock handling** uses `os.kill(pid, 0)` to probe the holder.
If the holder is dead, the sentinel is removed and the lock is retried
once. PID-reuse risk is minimal on Linux (default `pid_max` is 4M+).

## Scope

We accept reports for any of:

- Hook code that reads or writes outside of `${CLAUDE_PLUGIN_DATA}`
  (other than the plan file the hook was invoked against and standard
  log paths).
- Subprocess invocations vulnerable to argument injection.
- Lock or state corruption that allows two reviews to run concurrently
  for the same plan path.
- File-permission regressions (anything writing 0644 instead of 0600,
  or 0755 dirs instead of 0700).
- Secret leakage paths beyond the documented egress in
  `CLAUDE_PLAN_REVIEW_CHAIN`.

## Out of scope

- The fact that plan content goes to external providers; this is the
  plugin's documented purpose.
- Issues in the upstream provider CLIs (`codex`, `gemini`, `claude`)
  themselves; report those to their respective vendors.
