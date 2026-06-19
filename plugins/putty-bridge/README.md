# putty-bridge

Generate a Windows PuTTY session (`.reg`) plus a tmux snippet so a
remote tmux + Claude Code session has true colour, full UTF-8, mouse-aware
tmux, native copy/paste, and a deuteranopia-friendly dark palette. No
PuTTY GUI clicks.

## What it does

`/putty-session` writes two files to `--out` (default `$HOME`):

- `<session>.reg` — UTF-16-LE-BOM Windows registry import file. Double-click
  on Windows imports the session under
  `HKCU\Software\SimonTatham\PuTTY\Sessions\<session>`.
- `<session>.tmux.conf` — drop-in tmux snippet wrapped in managed
  `# >>> putty-bridge:<session> >>>` markers so reruns can be merged in
  place.

## Install

```text
/plugin marketplace add bradlay/claude-code-recipes
/plugin install putty-bridge@claude-code-recipes
/reload-plugins
```

## Usage

```sh
/putty-session
/putty-session --host 10.0.0.5 --user alice
/putty-session --session prod --host prod.example.com --palette tango-dark
```

| Flag | Default | Effect |
|---|---|---|
| `--session NAME` | `ClaudeCode` | PuTTY session name. `^[A-Za-z0-9 _.-]{1,64}$`. |
| `--host HOST` | (empty) | SSH hostname. Empty leaves a template the user fills in. |
| `--user USER` | (empty) | SSH username. |
| `--port N` | `22` | SSH port. |
| `--out DIR` | `$HOME` | Where to write the two files. |
| `--font NAME` | `Cascadia Mono` | Font face. Comma rejected (PuTTY parses comma-lists). |
| `--font-height N` | `11` | Point size. |
| `--palette NAME` | `deuteranopia` | One of `deuteranopia`, `solarized-dark`, `tango-dark`. |
| `--scrollback N` | `50000` | Scrollback line count. |

## Requirements

- **PuTTY ≥ 0.81** on Windows. Earlier versions lack `CtrlShiftCV` and
  `MouseAutocopy`.
- **tmux ≥ 3.4** on the remote. Earlier versions lack
  `set-clipboard external`.
- **Cascadia Mono** on Windows 11 by default; on Windows 10 either
  install Cascadia Code or pass `--font Consolas` for the universal fallback.

## What gets baked in (.reg)

- **Encoding:** `LineCodePage="UTF-8"`, `UTF8Override=1`,
  `UTF8linedraw=1` (so legacy VT100/ACS box-drawing maps to Unicode —
  fixes tmux pane borders and any ncurses TUI).
- **Colour:** `TrueColour=1`, `ANSIColour=1`, `Xterm256Colour=1`,
  `BoldAsColour=1`, all 22 `Colour0..Colour21` slots filled from the
  selected palette. Default palette is Okabe-Ito-derived for
  deuteranopia on a soft `#1A1A1A` background.
- **Terminal:** `TerminalType="xterm-256color"`,
  `BackspaceIsDelete=1`, `BellStyle=0`, `ScrollbackLines=50000`.
- **Mouse:** `MouseIsXterm=2` ("Compromise" — right-click pastes,
  middle-click extends selection). `MouseOverride=1` so Shift+drag
  inside a tmux mouse-mode pane bypasses tmux and uses PuTTY's local
  selection. `MouseAutocopy=1` auto-copies the selection to the
  Windows clipboard instantly.
- **Keyboard copy/paste:** `CtrlShiftCV=1`, `CtrlShiftIns=1`.
  `RawCNP=0`, `PasteRTF=0` strip formatting on paste.
- **Security:** `RemoteQTitleAction=0` (declines xterm title queries —
  historic CSI injection vector), `AgentFwd=0` (never silently forward
  the SSH agent).

## What gets baked in (tmux snippet)

```tmux
set -g default-terminal "tmux-256color"
if-shell '! infocmp tmux-256color >/dev/null 2>&1' \
    'set -g default-terminal "screen-256color"'
set -ga terminal-overrides ",xterm-256color:RGB"
set -ga terminal-overrides ",xterm-256color:Tc"
set -g mouse on
set -g set-clipboard external
set -g focus-events on
setw -g xterm-keys on
```

`set-clipboard external` (not `on`): only tmux itself, driven by the
user's keypresses in copy-mode, can write the system clipboard via
OSC 52 — arbitrary processes inside tmux cannot. With vanilla PuTTY
this line is effectively inert (PuTTY doesn't speak OSC 52); kept for
forward compatibility if you later switch terminals.

## Copy/paste model

- **Windows clipboard → session (paste):** right-click in PuTTY,
  Shift+Insert, or Ctrl+Shift+V.
- **Session → Windows clipboard (copy):** Shift+drag in PuTTY (Shift
  bypasses tmux's mouse capture so PuTTY sees the drag); the selection
  is auto-copied. Ctrl+Shift+C also copies the active selection.

The copy path is **PuTTY's local selection**, not OSC 52. That's by
design: vanilla PuTTY 0.81+ does not implement OSC 52 writes, so no
remote process can drive your Windows clipboard via terminal escapes.

## Install on Windows + remote

1. Inspect the `.reg` first — open it in Notepad to see what it sets.
2. Double-click the `.reg` → Yes to import.
3. Open PuTTY, select the session, click Open.
4. On the remote: `cat <out>/<session>.tmux.conf >> ~/.tmux.conf` then
   `tmux source-file ~/.tmux.conf`.
5. Re-running `/putty-session` with the same `--session`: edit the
   existing `# >>> putty-bridge:<session> >>>` block in
   `~/.tmux.conf` in place. Markers make the block easy to find.

## Verify

- tmux pane borders render as Unicode boxes (`┌─┐`, not `?`).
- `git diff` shows distinct red/green even with deuteranopia.
- Right-click in PuTTY pastes the Windows clipboard.
- Shift+drag inside a tmux pane copies to the Windows clipboard
  (paste into Notepad to confirm).

## Uninstall

```text
/plugin disable putty-bridge@claude-code-recipes
/plugin uninstall putty-bridge@claude-code-recipes
```

To remove from PuTTY, delete the session under
`HKCU\Software\SimonTatham\PuTTY\Sessions\<session>` (PuTTY GUI:
Sessions list → Delete). To remove from tmux, delete the
`# >>> putty-bridge:<session> >>>` block from `~/.tmux.conf`.
