# Secret redaction for guard log writes.
#
# Command strings written to the hook's log files can carry
# ``Authorization: Bearer ...``, ``--password ...``,
# ``SOME_CLIENT_SECRET=...``, or bare hex tokens. This module scrubs
# common secret shapes before they're persisted. All patterns are
# case-insensitive.
#
# Intentional over-redaction: ``-p`` / ``-t`` also redact benign
# ``ssh -p 22``, ``docker -p 8080:80``, ``tar -t`` — acceptable for a
# security debug log (we lose a port number, never a secret). Do not
# "fix" this by narrowing the flag set; over-redaction is the safe
# direction.

from __future__ import annotations

import re

# Quoted (possibly multi-word) value, or a bare whitespace-delimited token.
_REDACT_VALUE = r"""("[^"]*"|'[^']*'|\S+)"""

_REDACTORS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bBearer\s+\S+", re.IGNORECASE), "Bearer <redacted>"),
    (
        # (?<!\S): flag preceded by whitespace or start-of-string. A
        # plain \b fails here — no word boundary between a space and
        # the leading '-'.
        re.compile(
            r"(?<!\S)(--password|--token|--secret|--api-key|--client-secret|-p|-t)([=\s]+)"
            + _REDACT_VALUE,
            re.IGNORECASE,
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(
            r"\b(Authorization|X-Client-Secret|X-Access-Client-Secret|X-Auth-Token|X-Api-Key)"
            r"(\s*[:=]\s*)" + _REDACT_VALUE,
            re.IGNORECASE,
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(
            r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|PEPPER))(\s*=\s*)"
            + _REDACT_VALUE,
            re.IGNORECASE,
        ),
        r"\1\2<redacted>",
    ),
    # AWS access key id shape: AKIA/ASIA + 16 uppercase alnum chars.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # GitHub token shapes: ghp_/gho_/ghu_/ghs_/ghr_/github_pat_ + body.
    (
        re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{36,}\b"),
        "<redacted-github-token>",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        "<redacted-github-token>",
    ),
    # PEM-style private key blocks.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted-private-key>",
    ),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<redacted-hex>"),
)


def redact_secrets(text: str) -> str:
    """Scrub secret-bearing substrings from *text* before it hits a log file.

    Conservative by design — over-redaction in a local debug log is
    fine; under-redaction leaks.
    """
    for pat, repl in _REDACTORS:
        text = pat.sub(repl, text)
    return text
