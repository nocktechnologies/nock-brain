#!/usr/bin/env python3
"""Query extracted facts from session transcripts.

Usage:
    python3 query-facts.py "what was decided about content strategy"
    python3 query-facts.py --kind directive --since 2026-05-18
    python3 query-facts.py --kind decision --limit 10
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def search(facts: list[dict], query: str = "", kind: str = "",
           since: str = "", include_superseded: bool = False,
           limit: int = 20) -> list[dict]:
    results = facts
    if not include_superseded:
        results = [f for f in results if f.get("status", "current") != "superseded"]
    if kind:
        results = [f for f in results if f.get("kind") == kind]
    if since:
        results = [f for f in results if f.get("source_date", "") >= since]
    if query:
        terms = query.lower().split()
        scored = []
        for f in results:
            content_lower = str(f.get("content", "")).lower()
            score = sum(1 for t in terms if t in content_lower)
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: -x[0])
        results = [f for _, f in scored]
    return results[:limit]


def format_fact(f: dict) -> str:
    parts = [f"[{f.get('source_date', 'unknown')}] [{f.get('kind', 'fact').upper()}]"]
    header = " ".join(parts)
    return f"{header}\n  {str(f.get('content', ''))[:200]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="*", default=[])
    parser.add_argument("--kind", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-superseded", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    args = parser.parse_args()

    if not args.facts.exists():
        print("No facts.json found. Run extract-facts.py first.", file=sys.stderr)
        sys.exit(1)

    facts = load_facts(args.facts)
    query_str = " ".join(args.query)
    results = search(facts, query=query_str, kind=args.kind,
                     since=args.since,
                     include_superseded=args.include_superseded,
                     limit=args.limit)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(f"{len(results)} results (of {len(facts)} total facts)\n")
        for f in results:
            print(format_fact(f))
            print()


if __name__ == "__main__":
    main()
