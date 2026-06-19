---
description: "Generate a Windows PuTTY .reg + tmux snippet so a remote tmux + Claude Code session has true color, full UTF-8, mouse-aware tmux, native copy/paste, and a deuteranopia-friendly dark palette."
argument-hint: "[--session NAME] [--host HOST] [--user USER] [--port 22] [--out DIR] [--font NAME] [--font-height N] [--palette deuteranopia|solarized-dark|tango-dark]"
allowed-tools: [Bash]
---

# /putty-session

Programmatically build a complete PuTTY session for connecting from
Windows to a Linux host where the user runs `tmux` + Claude Code. No
PuTTY GUI clicks required: import the generated `.reg`, append the tmux
snippet.

## Arguments

The user invoked this command with: `$ARGUMENTS`

All flags are optional. With no flags the script writes a template
session named `ClaudeCode` with empty `HostName` so the user fills it
in per connection.

## Instructions

1. Run the generator with the user's args verbatim:

   ```sh
   "${CLAUDE_PLUGIN_ROOT}/bin/putty-session" $ARGUMENTS
   ```

2. Print the script's stdout summary **verbatim** to the user. Do not
   reformat or paraphrase — paths matter.

## What the generator produces

- `<out>/<session>.reg` — UTF-16-LE-BOM Windows registry file. Double-click
  on Windows imports the session into PuTTY
  (`HKCU\Software\SimonTatham\PuTTY\Sessions\<session>`).
- `<out>/<session>.tmux.conf` — drop-in tmux snippet wrapped in managed
  begin/end markers.
