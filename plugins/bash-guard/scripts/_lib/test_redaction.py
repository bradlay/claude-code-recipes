"""Tests for secret redaction of guard log lines.

The invariant under test: common secret shapes (bearer headers, flag/
env-var secret assignments, AWS/GitHub token shapes, private key
blocks, bare hex) are scrubbed before they would hit a log file, while
ordinary command text passes through unchanged.
"""

from __future__ import annotations

from _lib import redaction


class TestRedactSecrets:
    def test_bearer_token_is_redacted(self) -> None:
        fake_value = "abc123.def456"
        out = redaction.redact_secrets(f"curl -H 'Authorization: Bearer {fake_value}'")
        assert fake_value not in out
        assert "<redacted>" in out

    def test_password_flag_is_redacted(self) -> None:
        out = redaction.redact_secrets("mysql --password hunter2 -u root")
        assert "hunter2" not in out
        assert "<redacted>" in out

    def test_token_env_assignment_is_redacted(self) -> None:
        out = redaction.redact_secrets("MY_APP_TOKEN=sekret123 ./deploy.sh")
        assert "sekret123" not in out
        assert "<redacted>" in out

    def test_github_token_is_redacted(self) -> None:
        # Built at runtime so no literal token-shaped string sits in the
        # source (secret scanners flag a contiguous match on disk).
        fake_body = "1234567890abcdefghij1234567890abcdEF"
        fake_token = "ghp_" + fake_body
        out = redaction.redact_secrets(f"git push https://{fake_token}@github.com/x/y")
        assert fake_token not in out
        assert "<redacted-github-token>" in out

    def test_aws_access_key_is_redacted(self) -> None:
        fake_body = "ABCDEFGHIJKLMNOP"
        fake_key = "AKIA" + fake_body
        out = redaction.redact_secrets(f"export AWS_ACCESS_KEY_ID={fake_key}")
        assert fake_key not in out
        assert "<redacted-aws-key>" in out

    def test_private_key_block_is_redacted(self) -> None:
        block = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"
        out = redaction.redact_secrets(f"echo '{block}'")
        assert "abc" not in out
        assert "<redacted-private-key>" in out

    def test_bare_hex_token_is_redacted(self) -> None:
        out = redaction.redact_secrets("curl -H 'X-Trace: " + "a" * 40 + "'")
        assert "a" * 40 not in out
        assert "<redacted-hex>" in out

    def test_normal_text_is_unchanged(self) -> None:
        cmd = "git commit -m 'fix: handle empty response bodies'"
        assert redaction.redact_secrets(cmd) == cmd

    def test_plain_ls_is_unchanged(self) -> None:
        cmd = "ls -la /tmp"
        assert redaction.redact_secrets(cmd) == cmd

    def test_ssh_port_flag_is_over_redacted_intentionally(self) -> None:
        # Documented intentional over-redaction: -p is ambiguous between
        # "password" and "port", and losing a port number in a local
        # debug log is the safe direction.
        out = redaction.redact_secrets("ssh -p 22 user@host")
        assert "22" not in out
