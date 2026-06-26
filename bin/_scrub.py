"""Secret scrubbing shared by all NockBrain extraction paths."""
import re


# N8392-A: prefixes / patterns that mark a fact's content as a raw,
# un-synthesized artifact rather than a durable fact — tool_use.input JSON
# blobs, tool_result payloads, bus inbox dumps (=== AGENT MESSAGE / TELEGRAM /
# SYSTEM and other ===... headers), fenced code, harness notification blobs,
# command tags, and cat -n line-numbered file dumps. Genuine facts start with a
# [TAG] (e.g. [DECISION], [INSIGHT]) or with prose, never with these.
_STRUCTURAL_NOISE_PREFIXES = (
    '{"',
    '[{"',
    "```",
    "<command-",
    "<task-notification",
    "<system-reminder",
)
# A leading [UPPER-CASE TAG] marks a synthesized/tagged fact. Checked FIRST as
# an escape hatch so no broad matcher below can ever drop a genuine tagged fact
# (e.g. an [INSIGHT] note that happened to open with '==='). Note '[{"' is NOT
# matched here ('{' is not A-Z), so JSON-array noise is still caught below.
_GENUINE_TAG_RE = re.compile(r"^\[[A-Z][A-Z0-9 _/-]*\]")
# 3+ '=' at line start = a bus/shell/verification dump header. Covers
# '=== AGENT MESSAGE', '=== TELEGRAM', '=== SYSTEM', the generic '=== ...', and
# longer banners like '===== #8129 FULL ====='.
_EQUALS_DUMP_RE = re.compile(r"^={3,}")
_LINE_NUMBERED_DUMP_RE = re.compile(r"^\d+\t")


def is_structural_noise(content: str) -> bool:
    """Return True if *content* is a raw, un-synthesized artifact, not a fact.

    Guards the JSONL->facts path (N8392-A): refine-sessions.py was minting
    facts out of raw tool_use.input JSON and bus-dump message text, because the
    inferred 'merge' pattern fires on "PR #6 merged" text buried inside a
    command or inbox dump. This prefix/pattern discriminator drops those before
    classification. It is importable by purge-fact.py to sweep pre-existing
    noise with the identical rule.

    Content is structural noise IFF content.lstrip(), and it is NOT a [TAG]ged
    fact, either:
      - startswith one of _STRUCTURAL_NOISE_PREFIXES, OR
      - matches ^={3,}  (=== ... bus/shell/dump headers), OR
      - matches ^\\d+\\t (cat -n line-numbered file dumps).

    Prefix/pattern-based ONLY (never substring): a genuine fact like
    "[CORRECTION] ...CRM_AGENT_NAME=mira was passing author_surface=..." must be
    spared, so we never match 'CRM_AGENT_NAME=' anywhere in the text, and a
    leading [TAG] is an explicit escape hatch. Empty/whitespace returns False.
    """
    if not content:
        return False
    stripped = content.lstrip()
    if not stripped:
        return False
    if _GENUINE_TAG_RE.match(stripped):
        return False
    if stripped.startswith(_STRUCTURAL_NOISE_PREFIXES):
        return True
    if _EQUALS_DUMP_RE.match(stripped) or _LINE_NUMBERED_DUMP_RE.match(stripped):
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
