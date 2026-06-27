"""Shared fact-store validation and loading helpers."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_FACT_FIELDS = {"id", "kind", "status", "confidence", "content", "source_date", "evidence"}
RECALL_ITEM_FIELDS = {"kind", "status", "confidence", "content", "source_date"}

# The owning agent/source of a fact (gbrain-style fleet scoping). DELIBERATELY
# NOT in the required-field sets above: it is optional, so a single-brain store
# and every pre-source fact stay valid and read as all-DEFAULT_SOURCE. Add it to
# a fact to scope it; leave it off and the fact belongs to the default brain.
DEFAULT_SOURCE = "mira"


def fact_source(fact: Any) -> str:
    """The source/owner of a fact. Missing, blank, or non-string `source`
    defaults to DEFAULT_SOURCE — so backward compatibility is automatic and a
    null source can never read as a distinct scope."""
    if isinstance(fact, dict):
        src = fact.get("source")
        if isinstance(src, str) and src.strip():
            return src.strip()
    return DEFAULT_SOURCE


def malformed_fact_reason(fact: Any, required_fields: set[str] | None = None) -> str:
    if not isinstance(fact, dict):
        return "not an object"
    required = required_fields or REQUIRED_FACT_FIELDS
    missing = sorted(field for field in required if field not in fact)
    if missing:
        return "missing " + ", ".join(missing)
    return ""


def valid_fact(fact: Any, required_fields: set[str] | None = None) -> bool:
    return not malformed_fact_reason(fact, required_fields)


def filter_valid_facts(
    facts: Any,
    *,
    source: str = "facts",
    required_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(facts, list):
        print(f"{source}: skipped malformed fact store (expected list)", file=sys.stderr)
        return []

    valid: list[dict[str, Any]] = []
    skipped = 0
    for fact in facts:
        if valid_fact(fact, required_fields):
            valid.append(fact)
        else:
            skipped += 1
    if skipped:
        print(f"{source}: skipped {skipped} malformed fact record(s)", file=sys.stderr)
    return valid


def load_facts(
    path: Path | None,
    *,
    source: str | None = None,
    required_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    label = source or str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{label}: skipped malformed fact store ({exc})", file=sys.stderr)
        return []
    return filter_valid_facts(data, source=label, required_fields=required_fields)


# ── Bi-temporal validity (N-borrow-2: supersede-over-delete with a window) ───
# Facts may carry OPTIONAL `valid_at` / `invalid_at` ISO-8601 bounds. A fact is
# "currently valid" iff valid_at <= now < invalid_at, treating a MISSING bound as
# open (-inf / +inf). Both fields absent ⇒ always valid — so every existing fact
# and every caller is unaffected. This lets recall stop surfacing a fact as
# *current* once it has been superseded/expired, while the fact stays in the
# store for historical queries (recoverable via include_superseded).
def _parse_ts(value: Any) -> "datetime | None":
    """Parse an ISO-8601 timestamp; return None on anything unparseable
    (lenient by design — a malformed bound must never break recall)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Normalize to aware UTC so comparisons against an aware `now` never raise.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fact_currently_valid(fact: Any, now: "datetime | None" = None) -> bool:
    """True if `fact`'s bi-temporal validity window contains `now`.

    Missing/blank/unparseable `valid_at` or `invalid_at` are treated as open
    bounds, so a fact without these fields is always valid (backward compatible).
    """
    if not isinstance(fact, dict):
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    valid_at = _parse_ts(fact.get("valid_at"))
    invalid_at = _parse_ts(fact.get("invalid_at"))
    if valid_at is not None and now < valid_at:
        return False  # not yet in effect
    if invalid_at is not None and now >= invalid_at:
        return False  # window has closed (superseded/expired)
    return True
