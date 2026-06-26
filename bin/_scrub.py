"""Secret scrubbing shared by all NockBrain extraction paths."""
import re


# N8392-A: prefixes that mark a fact's content as a raw, un-synthesized
# artifact rather than a durable fact. These are tool_use.input JSON blobs,
# tool_result payloads, bus inbox dumps (=== AGENT MESSAGE / TELEGRAM /
# SYSTEM), fenced code, command tags, and cat -n line-numbered file dumps.
# Genuine facts start with a [TAG] (e.g. [DECISION]) or with prose, never with
# these prefixes.
_STRUCTURAL_NOISE_PREFIXES = (
    '{"',
    '[{"',
    "=== AGENT MESSAGE",
    "=== TELEGRAM",
    "=== SYSTEM",
    "=== ",  # three-equals + space, generic bus/dump header
    "```",
    "<command-",
)
_LINE_NUMBERED_DUMP_RE = re.compile(r"^\d+\t")


def is_structural_noise(content: str) -> bool:
    """Return True if *content* is a raw, un-synthesized artifact, not a fact.

    Guards the JSONL->facts path (N8392-A): refine-sessions.py was minting
    facts out of raw tool_use.input JSON and bus-dump message text, because the
    inferred 'merge' pattern fires on "PR #6 merged" text buried inside a
    command or inbox dump. This prefix-based discriminator drops those before
    classification. purge-fact.py can also import it to sweep existing noise.

    Content is structural noise IFF content.lstrip() either:
      - startswith one of _STRUCTURAL_NOISE_PREFIXES, OR
      - matches ^\\d+\\t (cat -n line-numbered file dumps).

    Prefix-based ONLY (never substring): a genuine fact like
    "[CORRECTION] ...CRM_AGENT_NAME=mira was passing author_surface=..." must be
    spared, so we never match 'CRM_AGENT_NAME=' anywhere in the text. Empty or
    whitespace-only input returns False.
    """
    if not content:
        return False
    stripped = content.lstrip()
    if not stripped:
        return False
    if stripped.startswith(_STRUCTURAL_NOISE_PREFIXES):
        return True
    if _LINE_NUMBERED_DUMP_RE.match(stripped):
        return True
    return False


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
