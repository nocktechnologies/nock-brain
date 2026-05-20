#!/usr/bin/env python3
"""Mark a fact as superseded when a decision has been replaced.

Usage:
    python3 supersede-fact.py <fact_id> --reason "direction changed"
    python3 supersede-fact.py --search "pricing model" --mark-superseded --by <id>
    python3 supersede-fact.py --list-superseded
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

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

    facts = json.loads(args.facts.read_text())

    if args.list_superseded:
        superseded = [f for f in facts if f.get("status") == "superseded"]
        print(f"{len(superseded)} superseded facts:")
        for f in superseded:
            by = f.get("superseded_by", "?")
            print(f"  {f['id']} [{f['kind']}] superseded by {by}")
            print(f"    {f['content'][:120]}")
        return

    if args.search:
        terms = args.search.lower().split()
        results = [f for f in facts
                   if f.get("status") != "superseded"
                   and all(t in f["content"].lower() for t in terms)]
        if not results:
            print("No matching current facts found.")
            return
        print(f"{len(results)} matching current facts:")
        for f in results:
            print(f"  {f['id']} [{f['source_date']}] [{f['kind']}]")
            print(f"    {f['content'][:150]}\n")
        if args.mark_superseded:
            for f in results:
                f["status"] = "superseded"
                f["superseded_at"] = datetime.now(timezone.utc).isoformat()
                if args.by:
                    f["superseded_by"] = args.by
                if args.reason:
                    f["supersession_reason"] = args.reason
            args.facts.write_text(json.dumps(facts, indent=2, default=str))
            print(f"Marked {len(results)} facts as superseded.")
        return

    if not args.fact_id:
        parser.print_help()
        return

    fact = next((f for f in facts if f["id"] == args.fact_id), None)
    if not fact:
        print(f"Fact {args.fact_id} not found.", file=sys.stderr)
        sys.exit(1)

    fact["status"] = "superseded"
    fact["superseded_at"] = datetime.now(timezone.utc).isoformat()
    if args.by:
        fact["superseded_by"] = args.by
    if args.reason:
        fact["supersession_reason"] = args.reason

    args.facts.write_text(json.dumps(facts, indent=2, default=str))
    print(f"Fact {args.fact_id} marked as superseded.")
    print(f"  Was: [{fact['kind']}] {fact['content'][:120]}")


if __name__ == "__main__":
    main()
