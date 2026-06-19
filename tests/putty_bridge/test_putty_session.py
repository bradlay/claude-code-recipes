"""Tests for putty-bridge: validation, .reg generation, tmux snippet, CLI."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import putty_session as ps  # noqa: E402

_GENERATOR = (
    Path(__file__).resolve().parent.parent.parent
    / "plugins"
    / "putty-bridge"
    / "scripts"
    / "putty_session.py"
)


# ---------- validation ----------


class TestValidate:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("session", "foo\rbar"),
            ("session", "foo\nbar"),
            ("session", "foo\0bar"),
            ("session", "../../etc/passwd"),
            ("session", "foo[bar]"),
            ("host", "evil\nhost"),
            ("host", "host\rname"),
            ("user", "user;rm -rf"),
            ("user", "u\0ser"),
            ("font", "Cascadia,Mono"),  # comma blocked (PuTTY parses comma-lists)
            ("font", "Font\rname"),
        ],
    )
    def test_rejects(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match=field):
            ps._validate(field, value)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("session", "ClaudeCode"),
            ("session", "Claude Code"),
            ("session", "prod.v1"),
            ("session", "host_42"),
            ("host", "example.com"),
            ("host", "10.0.0.5"),
            ("host", ""),  # template
            ("user", "alice"),
            ("user", ""),  # template
            ("font", "Cascadia Mono"),
            ("font", "Consolas"),
            ("font", "JetBrains Mono"),
        ],
    )
    def test_accepts(self, field: str, value: str) -> None:
        assert ps._validate(field, value) == value


# ---------- palette ----------


class TestPalette:
    def test_all_have_22_entries(self) -> None:
        for name, palette in ps.PALETTES.items():
            assert len(palette.rgb) == 22, name

    def test_all_in_range(self) -> None:
        for name, palette in ps.PALETTES.items():
            for i, (r, g, b) in enumerate(palette.rgb):
                assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255, f"{name}[{i}]"

    def test_deuteranopia_red_green_distinguishable(self) -> None:
        # Deuteranopes lose the M-cone (green); discrimination collapses to
        # the L/S axis, so red/green must differ on the blue channel for the
        # pair to remain distinguishable. Okabe Vermillion (red, B=0) vs
        # Okabe Bluish Green (green, B=115) — a 100+ delta on B.
        red = ps.DEUTERANOPIA.rgb[8]
        green = ps.DEUTERANOPIA.rgb[10]
        assert abs(red[2] - green[2]) > 60, (red, green)


# ---------- .reg writer ----------


class TestRenderReg:
    def _render(
        self,
        *,
        sessions: list[ps.SessionEntry] | None = None,
        **overrides: object,
    ) -> bytes:
        if sessions is None:
            sessions = [ps.SessionEntry(name="ClaudeCode", host="", user="")]
        kwargs: dict[str, object] = {
            "sessions": sessions,
            "port": 22,
            "font": "Cascadia Mono",
            "font_height": 10,
            "palette": ps.DEUTERANOPIA,
            "scrollback": 50000,
        }
        kwargs.update(overrides)
        return ps.render_reg(**kwargs)  # type: ignore[arg-type]

    def test_bom_and_header(self) -> None:
        out = self._render()
        assert out.startswith(b"\xff\xfe"), "missing UTF-16-LE BOM"
        text = out[2:].decode("utf-16-le")
        assert text.startswith("Windows Registry Editor Version 5.00\r\n"), text[:80]

    def test_section_header_under_HKCU_only(self) -> None:
        text = self._render()[2:].decode("utf-16-le")
        assert "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\ClaudeCode]" in text
        # Each session generates exactly two section lines: a delete
        # header `[-...]` then a create header `[...]`. Both must be
        # confined to the SimonTatham subtree.
        sections = [line for line in text.splitlines() if line.startswith("[")]
        assert len(sections) == 2, sections
        for sec in sections:
            stripped = sec[2:] if sec.startswith("[-") else sec[1:]
            assert stripped.startswith("HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\")

    def test_idempotent_delete_then_recreate(self) -> None:
        # Each session must be preceded by a `[-...]` delete header so
        # re-imports converge to a pristine state with no stale keys.
        text = self._render()[2:].decode("utf-16-le")
        section = "[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\ClaudeCode]"
        delete = "[-HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\ClaudeCode]"
        assert delete in text
        # The delete header appears immediately before the create header.
        assert text.index(delete) < text.index(section)

    def test_two_sections_when_update_defaults(self) -> None:
        text = self._render(
            sessions=[
                ps.SessionEntry(name="Default Settings", host="", user=""),
                ps.SessionEntry(name="GB10", host="10.0.0.5", user="alice"),
            ]
        )[2:].decode("utf-16-le")
        sections = [line for line in text.splitlines() if line.startswith("[")]
        # Two sessions, each with a [-...] delete + [...] create -> 4 headers.
        assert len(sections) == 4, sections
        assert any("Default%20Settings]" in s and not s.startswith("[-") for s in sections)
        assert any(s.endswith("Sessions\\GB10]") and not s.startswith("[-") for s in sections)
        # Defaults section must have empty host/user.
        # split returns: [pre, "...Default Settings]\n...", "...GB10]\n..."]
        chunks = text.split("[HKEY_CURRENT_USER")
        defaults_block = next(
            c
            for c in chunks
            if c.startswith("\\Software\\SimonTatham\\PuTTY\\Sessions\\Default%20Settings]")
        )
        assert '"HostName"=""' in defaults_block
        assert '"UserName"=""' in defaults_block

    def test_session_name_url_encoded(self) -> None:
        text = self._render(sessions=[ps.SessionEntry(name="Claude Code", host="", user="")])[
            2:
        ].decode("utf-16-le")
        assert "Sessions\\Claude%20Code]" in text

    def test_security_critical_values(self) -> None:
        text = self._render()[2:].decode("utf-16-le").replace("\r", "")
        # Each pair: assertion that exact value (not just key presence) lands.
        for line in [
            '"TrueColour"=dword:00000001',
            '"LineCodePage"="UTF-8"',
            '"UTF8linedraw"=dword:00000001',
            '"UTF8Override"=dword:00000001',
            '"TerminalType"="xterm-256color"',
            '"MouseIsXterm"=dword:00000002',  # Compromise: right-click pastes
            '"MouseOverride"=dword:00000001',
            '"MouseAutocopy"=dword:00000001',
            '"CtrlShiftCV"=dword:00000001',
            '"CtrlShiftIns"=dword:00000001',
            '"RawCNP"=dword:00000000',
            '"PasteRTF"=dword:00000000',
            '"RemoteQTitleAction"=dword:00000000',  # security: no title query reply
            '"AgentFwd"=dword:00000000',  # security: no silent agent fwd
            '"Font"="Cascadia Mono"',
            '"Xterm256Colour"=dword:00000001',
        ]:
            assert line in text, f"missing: {line}"

    def test_dword_format(self) -> None:
        text = self._render()[2:].decode("utf-16-le").replace("\r", "")
        dword_re = re.compile(r'^"[A-Za-z0-9_-]+"=dword:[0-9a-f]{8}$')
        for line in text.splitlines():
            if "dword:" in line:
                assert dword_re.match(line), f"malformed DWORD: {line!r}"

    def test_palette_lands_in_colour_slots(self) -> None:
        text = self._render()[2:].decode("utf-16-le").replace("\r", "")
        # Okabe Vermillion #D55E00 -> ANSI red slot Colour8.
        assert '"Colour8"="213,94,0"' in text
        # Okabe Bluish Green #009E73 -> ANSI green slot Colour10.
        assert '"Colour10"="0,158,115"' in text
        # All 22 slots populated.
        for i in range(22):
            assert f'"Colour{i}"=' in text, f"missing Colour{i}"

    def test_defense_in_depth_rejects_control_chars(self) -> None:
        with pytest.raises(ValueError, match="host"):
            self._render(sessions=[ps.SessionEntry(name="x", host="evil\nhost", user="")])
        with pytest.raises(ValueError, match="user"):
            self._render(sessions=[ps.SessionEntry(name="x", host="", user="bad\rusr")])
        with pytest.raises(ValueError, match="font"):
            self._render(font="Font\0name")

    def test_empty_sessions_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            self._render(sessions=[])

    def test_crlf_line_endings(self) -> None:
        text = self._render()[2:].decode("utf-16-le")
        assert "\r\n" in text
        # No bare LFs that aren't part of CRLF.
        for i, ch in enumerate(text):
            if ch == "\n":
                assert text[i - 1] == "\r", f"bare LF at offset {i}"


# ---------- tmux snippet ----------


class TestRenderTmux:
    def test_managed_block_markers(self) -> None:
        out = ps.render_tmux_conf(session="ClaudeCode")
        assert "# >>> putty-bridge:ClaudeCode >>>" in out
        assert "# <<< putty-bridge:ClaudeCode <<<" in out

    def test_set_clipboard_external_not_on(self) -> None:
        out = ps.render_tmux_conf(session="x")
        assert "set -g set-clipboard external" in out
        # Stronger: bare `set-clipboard on` (which would let arbitrary in-tmux
        # apps drive the system clipboard) must not appear anywhere.
        assert "set-clipboard on" not in out

    def test_terminfo_fallback(self) -> None:
        out = ps.render_tmux_conf(session="x")
        assert "if-shell" in out and "infocmp tmux-256color" in out
        assert 'set -g default-terminal "screen-256color"' in out

    def test_no_version_branches(self) -> None:
        # tmux 3.4 floor means no %if/%else version branching needed.
        out = ps.render_tmux_conf(session="x")
        assert "%if" not in out
        assert "%else" not in out

    def test_no_tabs(self) -> None:
        assert "\t" not in ps.render_tmux_conf(session="x")


# ---------- CLI end-to-end ----------


class TestCli:
    def test_no_args_emits_template(self, tmp_path: Path) -> None:
        rc = ps.main(["--out", str(tmp_path)])
        assert rc == 0
        assert (tmp_path / "ClaudeCode.reg").stat().st_size > 0
        assert (tmp_path / "ClaudeCode.tmux.conf").stat().st_size > 0

    def test_full_flags(self, tmp_path: Path) -> None:
        rc = ps.main(
            [
                "--session",
                "test",
                "--host",
                "10.0.0.5",
                "--user",
                "alice",
                "--font",
                "Consolas",
                "--font-height",
                "12",
                "--palette",
                "tango-dark",
                "--out",
                str(tmp_path),
            ]
        )
        assert rc == 0
        text = (tmp_path / "test.reg").read_bytes()[2:].decode("utf-16-le")
        assert '"HostName"="10.0.0.5"' in text
        assert '"UserName"="alice"' in text
        assert '"Font"="Consolas"' in text

    def test_rejects_injection(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(_GENERATOR),
                "--host",
                "evil\nhost",
                "--out",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "host" in result.stderr
        # And no files leaked into the output dir.
        assert list(tmp_path.iterdir()) == []

    def test_rerun_overwrites(self, tmp_path: Path) -> None:
        rc1 = ps.main(["--session", "rerun", "--out", str(tmp_path)])
        rc2 = ps.main(["--session", "rerun", "--out", str(tmp_path)])
        assert rc1 == 0 and rc2 == 0
        # Same two files, no duplication.
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["rerun.reg", "rerun.tmux.conf"]
