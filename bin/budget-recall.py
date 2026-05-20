#!/usr/bin/env python3
"""Budget-aware memory recall: retrieve facts within a token budget.

Returns a curated summary of relevant past-session facts that fits
within a configurable token cap.

Usage:
    python3 budget-recall.py "what did we decide about content strategy"
    python3 budget-recall.py --budget 800 "status of the audit"
    python3 budget-recall.py --budget 1500 --include-superseded "pricing history"
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = 1000
MAX_BUDGET = 1500
MIN_CONFIDENCE = 0.7


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def search(facts: list[dict], query: str, include_superseded: bool = False) -> list[dict]:
    results = []
    if not include_superseded:
        facts = [f for f in facts if f.get("status", "current") != "superseded"]
    facts = [f for f in facts if f.get("confidence", 0) >= MIN_CONFIDENCE]

    terms = query.lower().split()
    for f in facts:
        content_lower = f["content"].lower()
        score = sum(1 for t in terms if t in content_lower)
        if score > 0:
            results.append((score, f))

    results.sort(key=lambda x: (-x[0], -x[1].get("confidence", 0)))
    return [f for _, f in results]


def format_fact(f: dict) -> str:
    parts = [f"[{f['source_date']}]", f"[{f['kind'].upper()}]"]
    header = " ".join(parts)
    content = f["content"][:200]
    if f.get("status") == "superseded":
        content = f"[SUPERSEDED] {content}"
    return f"{header}\n{content}"


def budget_recall(query: str, facts_file: Path, budget: int = DEFAULT_BUDGET,
                  include_superseded: bool = False) -> str:
    if not facts_file.exists():
        return ""

    facts = json.loads(facts_file.read_text())
    if not facts:
        return ""

    results = search(facts, query, include_superseded)
    if not results:
        return ""

    output_lines = [f"Memory recall ({len(results)} matches, budget {budget} tokens):"]
    tokens_used = estimate_tokens(output_lines[0])
    included = 0

    for f in results:
        formatted = format_fact(f)
        fact_tokens = estimate_tokens(formatted)
        if tokens_used + fact_tokens > budget:
            remaining = len(results) - included
            if remaining > 0:
                output_lines.append(f"[...{remaining} more results truncated by budget]")
            break
        output_lines.append(formatted)
        tokens_used += fact_tokens
        included += 1

    output_lines.append(f"[{included} facts, ~{tokens_used} tokens]")
    return "\n\n".join(output_lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--include-superseded", action="store_true")
    args = parser.parse_args()

    budget = min(args.budget, MAX_BUDGET)
    query_str = " ".join(args.query)
    result = budget_recall(query_str, args.facts, budget, args.include_superseded)

    if result:
        print(result)
    else:
        print("No matching facts found.")


if __name__ == "__main__":
    main()
