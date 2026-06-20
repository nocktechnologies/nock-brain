#!/usr/bin/env python3
"""Backfill the `source` (owning agent) field onto facts that lack it.

Stage A of gbrain-style fleet scoping: a single-brain store predates the
`source` field, so every existing fact reads as the default brain. This stamps
an explicit `source` onto facts that have none, making scope filtering
(budget-recall `search(sources=...)`) meaningful without changing recall for
anyone who does not pass a scope.

Idempotent: a fact that already carries a non-blank `source` is never touched,
so re-running is a no-op. `--dry-run` reports without writing.

Usage:
    python3 backfill-source.py --dry-run
    python3 backfill-source.py --source mira
    python3 backfill-source.py --facts /path/to/facts.json --source mira
"""
import argparse
import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import DEFAULT_SOURCE
from _store import secure_write_json

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def backfill(facts: list, source: str) -> int:
    """Stamp `source` onto every fact dict that lacks a non-blank `source`.
    Mutates `facts` in place; returns the number stamped."""
    stamped = 0
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        existing = fact.get("source")
        if isinstance(existing, str) and existing.strip():
            continue
        fact["source"] = source
        stamped += 1
    return stamped


def main():
    p = argparse.ArgumentParser(description="Backfill the fact `source` field.")
    p.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    p.add_argument("--source", default=DEFAULT_SOURCE,
                   help=f"source to stamp (default: {DEFAULT_SOURCE})")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    # A blank/whitespace --source would write a value that fact_source() reads
    # as the default (unstamped), so the run would claim success yet leave the
    # facts re-stampable on the next run — breaking idempotency. Reject it.
    source = args.source.strip()
    if not source:
        print("--source must be a non-blank name", file=sys.stderr)
        sys.exit(2)

    if not args.facts.exists():
        print(f"no facts file at {args.facts}", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(args.facts.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.facts}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, list):
        print("fact store is not a list; refusing to touch it", file=sys.stderr)
        sys.exit(1)

    # Count without mutating, for the dry-run report.
    needing = sum(
        1 for f in data
        if isinstance(f, dict) and not (isinstance(f.get("source"), str) and f.get("source").strip())
    )
    total = len(data)

    if args.dry_run:
        print(f"dry-run: {needing}/{total} facts would be stamped source={source!r}")
        return

    stamped = backfill(data, source)
    secure_write_json(args.facts, data, ensure_ascii=False)
    print(f"stamped source={source!r} onto {stamped}/{total} facts "
          f"(file: {args.facts})")


if __name__ == "__main__":
    main()
