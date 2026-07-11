#!/usr/bin/env python3
"""Mark a fact as superseded when a decision has been replaced.

Usage:
    python3 supersede-fact.py <fact_id> --reason "direction changed"
    python3 supersede-fact.py --search "pricing model" --mark-superseded --by <id>
    python3 supersede-fact.py --list-superseded
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts
from _store import secure_write_json

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fact_id", nargs="?", default="")
    parser.add_argument("--by", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--search", default="")
    parser.add_argument("--mark-superseded", action="store_true")
    parser.add_argument("--list-superseded", action="store_true")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    args = parser.parse_args()

    if not args.facts.exists():
        print("No facts.json found.", file=sys.stderr)
        sys.exit(1)

    facts = load_facts(args.facts)

    if args.list_superseded:
        superseded = [f for f in facts if f.get("status") == "superseded"]
        print(f"{len(superseded)} superseded facts:")
        for f in superseded:
            by = f.get("superseded_by", "?")
            print(f"  {f.get('id', '')} [{f.get('kind', 'fact')}] superseded by {by}")
            print(f"    {str(f.get('content', ''))[:120]}")
        return

    if args.search:
        terms = args.search.lower().split()
        results = [f for f in facts
                   if f.get("status") != "superseded"
                   and all(t in str(f.get("content", "")).lower() for t in terms)]
        if not results:
            print("No matching current facts found.")
            return
        print(f"{len(results)} matching current facts:")
        for f in results:
            print(f"  {f.get('id', '')} [{f.get('source_date', 'unknown')}] [{f.get('kind', 'fact')}]")
            print(f"    {str(f.get('content', ''))[:150]}\n")
        if args.mark_superseded:
            stamp = datetime.now(timezone.utc).isoformat()
            for f in results:
                f["status"] = "superseded"
                f["superseded_at"] = stamp
                # Bi-temporal: close the validity window at the supersession time
                # so recall stops treating it as current, while it stays queryable.
                # Overwrite a missing OR future-dated invalid_at so supersession
                # takes effect immediately (setdefault would leave a future bound
                # in place, delaying the close); never push an already-past close later.
                if not f.get("invalid_at") or f["invalid_at"] > stamp:
                    f["invalid_at"] = stamp
                if args.by:
                    f["superseded_by"] = args.by
                if args.reason:
                    f["supersession_reason"] = args.reason
            secure_write_json(args.facts, facts, indent=2, default=str)
            print(f"Marked {len(results)} facts as superseded.")
        return

    if not args.fact_id:
        parser.print_help()
        return

    fact = next((f for f in facts if f.get("id") == args.fact_id), None)
    if not fact:
        print(f"Fact {args.fact_id} not found.", file=sys.stderr)
        sys.exit(1)

    stamp = datetime.now(timezone.utc).isoformat()
    fact["status"] = "superseded"
    fact["superseded_at"] = stamp
    # Bi-temporal: close the validity window so recall stops surfacing it as
    # current (it stays in the store for historical queries). Overwrite a missing
    # OR future-dated invalid_at so supersession takes effect immediately; never
    # push an already-past close later.
    if not fact.get("invalid_at") or fact["invalid_at"] > stamp:
        fact["invalid_at"] = stamp
    if args.by:
        fact["superseded_by"] = args.by
    if args.reason:
        fact["supersession_reason"] = args.reason

    secure_write_json(args.facts, facts, indent=2, default=str)
    print(f"Fact {args.fact_id} marked as superseded.")
    print(f"  Was: [{fact.get('kind', 'fact')}] {str(fact.get('content', ''))[:120]}")


if __name__ == "__main__":
    main()
