"""
secret_scanner.py
Regex-based detection of hardcoded secrets/API keys.

Why this exists: in Step 1 we found bandit's hardcoded-secret detector only
looks for variable names like `password`/`secret` — it completely missed a
hardcoded Stripe key (`STRIPE_API_KEY = "sk_live_..."`) because the *value*
looked like a real secret even though the variable name didn't trip bandit's
pattern. This module closes that gap with known secret-format patterns.
"""
import re
from dataclasses import dataclass


@dataclass
class SecretFinding:
    line_number: int
    line_content: str
    secret_type: str
    matched_text: str


# (name, compiled regex) — patterns for common real-world secret formats.
# Kept deliberately generic/well-known formats only, to avoid false positives
# on normal-looking variable assignments.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Stripe API Key", re.compile(r"sk_(live|test)_[A-Za-z0-9]{16,}")),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub Personal Access Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("Slack Token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Generic Bearer Token", re.compile(r"['\"]Bearer [A-Za-z0-9\-_.]{20,}['\"]")),
    (
        "Hardcoded Secret Assignment",
        re.compile(
            r"(?i)(api_key|apikey|secret_key|access_key|auth_token)\s*=\s*['\"][A-Za-z0-9\-_/+=]{12,}['\"]"
        ),
    ),
    ("Private Key Block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]


def scan_lines(lines: list[tuple[int, str]]) -> list[SecretFinding]:
    """
    Scan a list of (line_number, line_content) tuples for hardcoded secrets.
    Designed to take the `added_lines` from a ChangedFile directly.
    """
    findings = []
    for line_number, content in lines:
        for secret_type, pattern in SECRET_PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append(
                    SecretFinding(
                        line_number=line_number,
                        line_content=content.strip(),
                        secret_type=secret_type,
                        matched_text=match.group(0),
                    )
                )
    return findings


def scan_file_content(file_content: str) -> list[SecretFinding]:
    """Scan a full file's text (all lines), not just a diff."""
    lines = [(i + 1, line) for i, line in enumerate(file_content.splitlines())]
    return scan_lines(lines)