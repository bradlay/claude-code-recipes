# posttooluse-bash-audit

A PostToolUse(Bash) hook that appends every Bash command Claude runs to
a local audit log. Always fail-open; never blocks; the log is a
convenience for "what did Claude actually run last week?", not a gate.

## Privacy and scope

- Logs **only Bash command text** (truncated to 200 chars) plus a
  timestamp and the Claude session id.
- Stays **local**. The log lives under `${CLAUDE_PLUGIN_DATA}/audit.log`,
  is mode `0600`, and is never shipped anywhere by this plugin.
- Logs only `Bash` tool invocations. Edit/Write/Read are not logged.
- Some people consider PostToolUse logging surveillance-y. That's a
  reasonable take. Don't install if you don't want a local record of
  what your assistant ran.

## Platform

macOS and Linux. Hook entry point is a POSIX shell launcher.

## Requirements

- `python3` (3.10+) on `PATH`. If missing, the launcher exits 0 and the
  command runs unaudited.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install posttooluse-bash-audit@claude-code-recipes
/reload-plugins
```

## Log format

One line per Bash command:

```
<iso-timestamp-utc> | <session_id> | <command-summary>
```

- `<command-summary>` is the first 200 chars of the command, with
  newlines collapsed to spaces. Long commands get an `...` suffix.
- Lines are append-only. Rotate manually if the log gets large
  (e.g. with `logrotate` or just delete it).

Example:

```
2026-05-02T10:34:11Z | abc-123-... | git status
2026-05-02T10:34:25Z | abc-123-... | npm test --silent
2026-05-02T10:35:02Z | abc-123-... | python3 scripts/build.py --release
```

## Reading the log

Just `cat` it. The format is grep-friendly:

```bash
# Everything from a specific session
grep abc-123 ${CLAUDE_PLUGIN_DATA}/audit.log

# Anything that ran git
grep '| git ' ${CLAUDE_PLUGIN_DATA}/audit.log
```

(`${CLAUDE_PLUGIN_DATA}` resolves to
`~/.claude/plugins/data/posttooluse-bash-audit-claude-code-recipes/`
when invoked through the host CLI's plugin runtime.)

## Failure mode

Any error inside the hook (audit log write fails, `OSError`, anything)
returns the empty pass-through `{}` and the bash command's result
flows through normally. This hook never delays or interferes with the
tool result.

## Disable / uninstall

```text
/plugin disable posttooluse-bash-audit@claude-code-recipes
/plugin uninstall posttooluse-bash-audit@claude-code-recipes
```
