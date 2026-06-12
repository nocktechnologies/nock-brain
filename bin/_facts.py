"""Shared fact-store validation and loading helpers."""
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_FACT_FIELDS = {"id", "kind", "status", "confidence", "content", "source_date", "evidence"}
RECALL_ITEM_FIELDS = {"kind", "status", "confidence", "content", "source_date"}


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
