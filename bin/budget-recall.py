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
import math
import re
import sys
from collections import Counter
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import RECALL_ITEM_FIELDS, load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_INSIGHTS = Path.home() / ".nock-brain" / "insights.json"
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = 1000
MAX_BUDGET = 1500
MIN_CONFIDENCE = 0.7

# BM25 parameters (Okapi defaults). k1 controls term-frequency saturation; b
# controls how strongly document length is normalized.
BM25_K1 = 1.5
BM25_B = 0.75


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def search(facts: list[dict], query: str, include_superseded: bool = False) -> list[dict]:
    """Rank facts against the query with Okapi BM25 — proper token matching with
    IDF (rarer query terms count for more) and document-length normalization.
    This replaces a naive substring-overlap count, which both over-matched
    (e.g. "cat" inside "category") and treated every term as equally important."""
    if not include_superseded:
        facts = [f for f in facts if f.get("status", "current") != "superseded"]
    facts = [f for f in facts if f.get("confidence", 0) >= MIN_CONFIDENCE]
    if not facts:
        return []

    query_terms = set(_tokenize(query))
    if not query_terms:
        return []

    # Corpus statistics for BM25, computed over the candidate set.
    docs = [_tokenize(f.get("content", "")) for f in facts]
    n_docs = len(docs)
    avgdl = sum(len(d) for d in docs) / n_docs if n_docs else 0.0
    doc_freq: Counter = Counter()
    for d in docs:
        for term in set(d):
            doc_freq[term] += 1

    results = []
    for f, doc in zip(facts, docs):
        tf = Counter(doc)
        dl = len(doc)
        score = 0.0
        for term in query_terms:
            df = doc_freq.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf[term]
            denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * (dl / avgdl if avgdl else 0))
            if denom > 0:
                score += idf * (freq * (BM25_K1 + 1)) / denom
        if score > 0:
            results.append((score, f))

    results.sort(key=lambda x: (-x[0], -x[1].get("confidence", 0)))
    return [f for _, f in results]


def format_fact(f: dict) -> str:
    parts = [f"[{f.get('source_date', 'unknown')}]", f"[{f.get('kind', 'fact').upper()}]"]
    header = " ".join(parts)
    content = str(f.get("content", ""))[:200]
    if f.get("status") == "superseded":
        content = f"[SUPERSEDED] {content}"
    return f"{header}\n{content}"


def _load(path: Path) -> list[dict]:
    return load_facts(path, required_fields=RECALL_ITEM_FIELDS)


def budget_recall(query: str, facts_file: Path, budget: int = DEFAULT_BUDGET,
                  include_superseded: bool = False, insights_file: Path | None = None) -> str:
    fact_results = search(_load(facts_file), query, include_superseded) if facts_file else []
    insight_results = search(_load(insights_file), query, include_superseded) if insights_file else []

    # Consolidated insights lead; drop the raw facts an insight already covers so
    # recall shows the synthesis, not the synthesis plus its own sources.
    covered = {sid for ins in insight_results for sid in ins.get("source_ids", [])}
    fact_results = [f for f in fact_results if f.get("id") not in covered]

    results = insight_results + fact_results
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

    output_lines.append(f"[{included} item(s), ~{tokens_used} tokens]")
    return "\n\n".join(output_lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS,
                        help="Synthesized-insight store (surfaced first); optional")
    parser.add_argument("--include-superseded", action="store_true")
    args = parser.parse_args()

    budget = min(args.budget, MAX_BUDGET)
    query_str = " ".join(args.query)
    result = budget_recall(query_str, args.facts, budget, args.include_superseded,
                           insights_file=args.insights)

    if result:
        print(result)
    else:
        print("No matching facts found.")


if __name__ == "__main__":
    main()
