"""Secret scrubbing shared by all NockBrain extraction paths."""
import re


SENSITIVE_ENV_ASSIGNMENT = re.compile(
    r"(?im)(\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\b\s*=\s*)([^\s\r\n]+)"
)

SECRET_PATTERNS = [
    # Telegram bot tokens: 123456789:AA... or URL segments like bot123456789:AA...
    re.compile(r"(?<![A-Za-z0-9_])(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    # Common bare token prefixes seen in shell output and tool results.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk_[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b"),
    re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{20,}\b"),
    # Bearer tokens and common key assignments.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=:-]{16,}"
    ),
    # PEM private-key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def scrub_secrets(text: str) -> tuple[str, int]:
    redactions = 0
    scrubbed, count = SENSITIVE_ENV_ASSIGNMENT.subn(r"\1[REDACTED_SECRET]", text)
    redactions += count
    for pattern in SECRET_PATTERNS:
        scrubbed, count = pattern.subn("[REDACTED_SECRET]", scrubbed)
        redactions += count
    return scrubbed, redactions
