"""Generate a PuTTY session .reg + tmux snippet for tmux + Claude Code.

Targets PuTTY >= 0.81 and tmux >= 3.4. See README for what gets baked in.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import quote


@dataclass(frozen=True)
class Palette:
    """22 RGB tuples in PuTTY Colour0..Colour21 slot order.

    Slots: fg, fg-bold, bg, bg-bold, cursor-text, cursor, then 8 ANSI
    colours followed by their 8 bold/bright variants.
    """

    rgb: tuple[tuple[int, int, int], ...]

    def __post_init__(self) -> None:
        if len(self.rgb) != 22:
            raise ValueError(f"palette must have 22 entries, got {len(self.rgb)}")
        for i, (r, g, b) in enumerate(self.rgb):
            if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
                raise ValueError(f"palette entry {i} out of [0,255]: ({r},{g},{b})")


# Okabe-Ito-derived palette tuned for deuteranopia. Background is a soft
# #1A1A1A (not pure black) for less halo; foreground is a warm-ish off-
# white. The red slot uses Okabe's Vermillion (#D55E00) and green uses
# Bluish Green (#009E73), giving the ANSI red/green pair clear luminance
# + hue separation for red-green colour-blindness.
DEUTERANOPIA: Final = Palette(
    (
        (0xE0, 0xE0, 0xE0),  # 0 fg
        (0xFF, 0xFF, 0xFF),  # 1 fg-bold
        (0x1A, 0x1A, 0x1A),  # 2 bg
        (0x2A, 0x2A, 0x2A),  # 3 bg-bold
        (0x1A, 0x1A, 0x1A),  # 4 cursor-text
        (0xFF, 0xFF, 0xFF),  # 5 cursor
        (0x1A, 0x1A, 0x1A),  # 6 ANSI black
        (0x55, 0x55, 0x55),  # 7 bright black (grey)
        (0xD5, 0x5E, 0x00),  # 8 ANSI red (Okabe Vermillion)
        (0xFF, 0x77, 0x33),  # 9 bright red
        (0x00, 0x9E, 0x73),  # 10 ANSI green (Okabe Bluish Green)
        (0x33, 0xD9, 0xA6),  # 11 bright green
        (0xF0, 0xE4, 0x42),  # 12 ANSI yellow (Okabe Yellow)
        (0xFF, 0xF0, 0x66),  # 13 bright yellow
        (0x56, 0xB4, 0xE9),  # 14 ANSI blue (Okabe Sky Blue)
        (0x88, 0xD6, 0xF2),  # 15 bright blue
        (0xCC, 0x79, 0xA7),  # 16 ANSI magenta (Okabe Reddish Purple)
        (0xE8, 0x9B, 0xC0),  # 17 bright magenta
        (0x00, 0x72, 0xB2),  # 18 ANSI cyan (Okabe Blue)
        (0x2A, 0x9D, 0xD9),  # 19 bright cyan
        (0xC0, 0xC0, 0xC0),  # 20 ANSI white
        (0xFF, 0xFF, 0xFF),  # 21 bright white
    )
)

# Alt palettes — same slot order. Solarized Dark is a well-known fallback;
# tango-dark is the classic high-contrast 16-colour scheme.
SOLARIZED_DARK: Final = Palette(
    (
        (0x83, 0x94, 0x96),  # 0 fg (base0)
        (0x93, 0xA1, 0xA1),  # 1 fg-bold (base1)
        (0x00, 0x2B, 0x36),  # 2 bg (base03)
        (0x07, 0x36, 0x42),  # 3 bg-bold (base02)
        (0x00, 0x2B, 0x36),  # 4 cursor-text
        (0x83, 0x94, 0x96),  # 5 cursor
        (0x07, 0x36, 0x42),  # 6 black (base02)
        (0x00, 0x2B, 0x36),  # 7 bright black (base03)
        (0xDC, 0x32, 0x2F),  # 8 red
        (0xCB, 0x4B, 0x16),  # 9 bright red (orange)
        (0x85, 0x99, 0x00),  # 10 green
        (0x58, 0x6E, 0x75),  # 11 bright green (base01)
        (0xB5, 0x89, 0x00),  # 12 yellow
        (0x65, 0x7B, 0x83),  # 13 bright yellow (base00)
        (0x26, 0x8B, 0xD2),  # 14 blue
        (0x83, 0x94, 0x96),  # 15 bright blue (base0)
        (0xD3, 0x36, 0x82),  # 16 magenta
        (0x6C, 0x71, 0xC4),  # 17 bright magenta (violet)
        (0x2A, 0xA1, 0x98),  # 18 cyan
        (0x93, 0xA1, 0xA1),  # 19 bright cyan (base1)
        (0xEE, 0xE8, 0xD5),  # 20 white (base2)
        (0xFD, 0xF6, 0xE3),  # 21 bright white (base3)
    )
)

TANGO_DARK: Final = Palette(
    (
        (0xD3, 0xD7, 0xCF),  # 0 fg
        (0xEE, 0xEE, 0xEC),  # 1 fg-bold
        (0x2E, 0x34, 0x36),  # 2 bg
        (0x55, 0x57, 0x53),  # 3 bg-bold
        (0x2E, 0x34, 0x36),  # 4 cursor-text
        (0xD3, 0xD7, 0xCF),  # 5 cursor
        (0x2E, 0x34, 0x36),  # 6 black
        (0x55, 0x57, 0x53),  # 7 bright black
        (0xCC, 0x00, 0x00),  # 8 red
        (0xEF, 0x29, 0x29),  # 9 bright red
        (0x4E, 0x9A, 0x06),  # 10 green
        (0x8A, 0xE2, 0x34),  # 11 bright green
        (0xC4, 0xA0, 0x00),  # 12 yellow
        (0xFC, 0xE9, 0x4F),  # 13 bright yellow
        (0x34, 0x65, 0xA4),  # 14 blue
        (0x72, 0x9F, 0xCF),  # 15 bright blue
        (0x75, 0x50, 0x7B),  # 16 magenta
        (0xAD, 0x7F, 0xA8),  # 17 bright magenta
        (0x06, 0x98, 0x9A),  # 18 cyan
        (0x34, 0xE2, 0xE2),  # 19 bright cyan
        (0xD3, 0xD7, 0xCF),  # 20 white
        (0xEE, 0xEE, 0xEC),  # 21 bright white
    )
)

PALETTES: Final[dict[str, Palette]] = {
    "deuteranopia": DEUTERANOPIA,
    "solarized-dark": SOLARIZED_DARK,
    "tango-dark": TANGO_DARK,
}


# Per-field input allowlists. Universally rejects CR/LF/NUL anywhere.
_VALIDATORS: Final[dict[str, re.Pattern[str]]] = {
    "session": re.compile(r"^[A-Za-z0-9 _.-]{1,64}$"),
    "host": re.compile(r"^[A-Za-z0-9._:%-]{0,253}$"),  # may be empty (template)
    "user": re.compile(r"^[A-Za-z0-9._-]{0,32}$"),  # may be empty
    "font": re.compile(r"^[A-Za-z0-9 ._-]{1,64}$"),
}


def _validate(field: str, value: str) -> str:
    if "\r" in value or "\n" in value or "\0" in value:
        raise ValueError(f"{field}: control chars (CR/LF/NUL) not allowed")
    pat = _VALIDATORS[field]
    if not pat.fullmatch(value):
        raise ValueError(f"{field}: value {value!r} does not match {pat.pattern}")
    return value


@dataclass(frozen=True)
class SessionEntry:
    """A single saved PuTTY session: registry name + bound host/user."""

    name: str
    host: str = ""
    user: str = ""


def render_reg(
    *,
    sessions: list[SessionEntry],
    port: int,
    font: str,
    font_height: int,
    palette: Palette,
    scrollback: int,
) -> bytes:
    """Render one or more PuTTY sessions as a UTF-16-LE-BOM .reg byte stream.

    The first session is typically "Default Settings" (PuTTY's template for
    any new session) followed by named, host-bound entries.
    """
    if not sessions:
        raise ValueError("render_reg: at least one session required")

    # Defense in depth: re-check string fields even if caller already
    # validated. A leaked CR/LF here would inject extra registry entries.
    for entry in sessions:
        for field, value in (
            ("session", entry.name),
            ("host", entry.host),
            ("user", entry.user),
        ):
            if "\r" in value or "\n" in value or "\0" in value:
                raise ValueError(f"render_reg: {field} contains control char")
    if "\r" in font or "\n" in font or "\0" in font:
        raise ValueError("render_reg: font contains control char")

    def s(v: str) -> str:
        return '"' + v.replace("\\", r"\\").replace('"', r"\"") + '"'

    def d(v: int) -> str:
        return f"dword:{v & 0xFFFFFFFF:08x}"

    # Settings shared across every section. Connection-specific bits
    # (HostName, UserName) are appended per-entry.
    common: list[tuple[str, str]] = [
        ("Protocol", s("ssh")),
        ("PortNumber", d(port)),
        ("PingInterval", d(0)),
        ("PingIntervalSecs", d(30)),
        ("TerminalType", s("xterm-256color")),
        ("BackspaceIsDelete", d(1)),
        ("RemoteQTitleAction", d(0)),
        ("BellStyle", d(0)),
        ("ScrollbackLines", d(scrollback)),
        ("EraseToScrollback", d(1)),
        ("LineCodePage", s("UTF-8")),
        ("UTF8Override", d(1)),
        ("CJKAmbigWide", d(0)),
        ("UTF8linedraw", d(1)),
        ("TrueColour", d(1)),
        ("ANSIColour", d(1)),
        ("Xterm256Colour", d(1)),
        ("BoldAsColour", d(1)),
        ("Font", s(font)),
        ("FontHeight", d(font_height)),
        ("FontIsBold", d(0)),
        ("FontCharSet", d(0)),
        ("MouseIsXterm", d(2)),
        ("MouseOverride", d(1)),
        ("MouseAutocopy", d(1)),
        ("CtrlShiftCV", d(1)),
        ("CtrlShiftIns", d(1)),
        ("RawCNP", d(0)),
        ("PasteRTF", d(0)),
        ("RectSelect", d(0)),
        ("AgentFwd", d(0)),
        ("Compression", d(0)),
    ]

    # Word boundary: include path-like chars so double-click selects file
    # paths cleanly. Each Wordness<n> covers a 32-char ASCII range; value
    # 2 = "part of word".
    word_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-")
    for base in range(0, 256, 32):
        bits = [2 if chr(base + offset) in word_chars else 0 for offset in range(32)]
        common.append((f"Wordness{base}", s(",".join(str(b) for b in bits))))

    # Palette: PuTTY stores each colour as "R,G,B" in slots Colour0..Colour21.
    for idx, (r, g, b) in enumerate(palette.rgb):
        common.append((f"Colour{idx}", s(f"{r},{g},{b}")))

    lines: list[str] = ["Windows Registry Editor Version 5.00", ""]
    for entry in sessions:
        # PuTTY URL-encodes session names in the registry path; "Default
        # Settings" -> "Default%20Settings".
        encoded = quote(entry.name, safe="")
        section = rf"HKEY_CURRENT_USER\Software\SimonTatham\PuTTY\Sessions\{encoded}"
        # Delete-then-recreate so re-imports converge to a pristine state
        # — stale keys from a previous import or from PuTTY GUI tweaks
        # are removed, then exactly the keys below are written. Without
        # the leading `[-...]` header, Windows' .reg import only sets
        # the keys we name and silently leaves anything else.
        lines.append(f"[-{section}]")
        lines.append(f"[{section}]")
        lines.append(f'"HostName"={s(entry.host)}')
        lines.append(f'"UserName"={s(entry.user)}')
        lines.extend(f'"{k}"={v}' for k, v in common)
        lines.append("")
    lines.append("")  # trailing CRLF

    text = "\r\n".join(lines)
    return b"\xff\xfe" + text.encode("utf-16-le")


def render_tmux_conf(*, session: str) -> str:
    """Render the tmux snippet (idempotent install via managed markers)."""
    begin = f"# >>> putty-bridge:{session} >>> DO NOT EDIT INSIDE THIS BLOCK"
    end = f"# <<< putty-bridge:{session} <<<"
    return "\n".join(
        [
            begin,
            'set -g default-terminal "tmux-256color"',
            "if-shell '! infocmp tmux-256color >/dev/null 2>&1' \\",
            "    'set -g default-terminal \"screen-256color\"'",
            # True color: Tc (modern) + RGB (older terminfo). Glob matches
            # xterm-256color, screen-256color, putty-256color, tmux-256color.
            'set -ga terminal-overrides ",*256col*:Tc"',
            'set -ga terminal-overrides ",*256col*:RGB"',
            "set -g mouse on",
            "set -g focus-events on",
            # `external` not `on`: only tmux itself (driven by user keypresses
            # in copy-mode) can write the system clipboard via OSC 52, not
            # arbitrary processes inside tmux. Inert under PuTTY (which
            # doesn't speak OSC 52); kept for forward-compat with OSC-52-
            # aware terminals the user might switch to later.
            "set -g set-clipboard external",
            # Claude Code integration (see code.claude.com/docs/en/terminal-config):
            # extended-keys = Shift+Enter sends a newline instead of submit;
            # allow-passthrough = desktop notifications + progress bar reach
            # the outer terminal.
            "set -s extended-keys on",
            "set -as terminal-features 'xterm*:extkeys'",
            "set -g allow-passthrough on",
            end,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="putty-session",
        description=("Generate a Windows PuTTY .reg + tmux snippet for remote tmux + Claude Code."),
    )
    p.add_argument("--session", default="ClaudeCode")
    p.add_argument("--host", default="")
    p.add_argument("--user", default="")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--out", default=str(Path.home()))
    p.add_argument("--font", default="Cascadia Mono")
    p.add_argument("--font-height", type=int, default=10, dest="font_height")
    p.add_argument("--palette", choices=sorted(PALETTES), default="deuteranopia")
    p.add_argument("--scrollback", type=int, default=50000)
    p.add_argument(
        "--update-defaults",
        action="store_true",
        help=(
            "Also write a 'Default Settings' section so any NEW PuTTY"
            " session you create later inherits these defaults."
            " Off by default — opting in clobbers PuTTY's existing"
            " Default Settings."
        ),
    )
    args = p.parse_args(argv)

    try:
        session = _validate("session", args.session)
        host = _validate("host", args.host)
        user = _validate("user", args.user)
        font = _validate("font", args.font)
    except ValueError as exc:
        print(f"putty-session: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser()
    if not out_dir.is_dir():
        print(f"putty-session: --out {out_dir} is not a directory", file=sys.stderr)
        return 2

    sessions: list[SessionEntry] = []
    if args.update_defaults:
        sessions.append(SessionEntry(name="Default Settings", host="", user=""))
    sessions.append(SessionEntry(name=session, host=host, user=user))

    palette = PALETTES[args.palette]
    reg_bytes = render_reg(
        sessions=sessions,
        port=args.port,
        font=font,
        font_height=args.font_height,
        palette=palette,
        scrollback=args.scrollback,
    )
    tmux_text = render_tmux_conf(session=session)

    reg_path = out_dir / f"{session}.reg"
    tmux_path = out_dir / f"{session}.tmux.conf"
    reg_path.write_bytes(reg_bytes)
    tmux_path.write_text(tmux_text)
    reg_path.chmod(0o600)
    tmux_path.chmod(0o600)

    print(
        _summary(
            session=session,
            reg=reg_path,
            tmux=tmux_path,
            palette=args.palette,
            update_defaults=args.update_defaults,
        )
    )
    return 0


def _summary(*, session: str, reg: Path, tmux: Path, palette: str, update_defaults: bool) -> str:
    sections = f"'Default Settings' + '{session}'" if update_defaults else f"'{session}'"
    return f"""\
Wrote PuTTY session(s): {sections} (palette: {palette})

Artifacts:
  PuTTY .reg : {reg}
  tmux snippet: {tmux}

Install on Windows (PuTTY >= 0.81):
  1. Inspect first: open {reg.name} in Notepad to read what it sets.
  2. Double-click {reg.name} -> Yes to import into the registry.
  3. Open PuTTY, select the '{session}' session, click Open.

Install on remote (tmux >= 3.4):
  cat {tmux} >> ~/.tmux.conf
  tmux source-file ~/.tmux.conf
  (re-running /putty-session with the same --session: edit the existing
   marker block in ~/.tmux.conf in place; the snippet uses
   '# >>> putty-bridge:{session} >>>' / '# <<< putty-bridge:{session} <<<'
   markers so you can find it.)

Verify visually:
  - tmux pane borders render as Unicode boxes (not ?). Proves
    UTF8linedraw=1 + UTF-8 line code page worked.
  - 'git diff' / 'eza' show distinct red & green even with deuteranopia.
  - Right-click in PuTTY pastes the Windows clipboard.
  - Shift+drag inside a tmux mouse-mode pane selects + auto-copies to
    the Windows clipboard via PuTTY's local selection (no OSC 52).
"""


if __name__ == "__main__":
    sys.exit(main())
