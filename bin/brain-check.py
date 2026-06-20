#!/usr/bin/env python3
"""Brain-check — does my brain already KNOW this? (gbrain `create_safety` steal)

Given a topic or claim, return a calibrated verdict — `exists` / `probable` /
`unknown` — plus the query terms the brain has NO signal on (the gap note). This
is the queryable, agent-facing companion to claim-guard: before asserting that
something is absent / unknown / unavailable, ask the brain. If the verdict is
`exists` or `probable`, do NOT claim absence — verify against live substrate.

The verdict reuses the SAME tokenizer + BM25 ranking the live recall uses, so
"the brain knows this" means "this would actually be recalled", not a parallel
heuristic that drifts from real recall.

    exists    broad, corroborated signal — safe to say "I have this".
    probable  some signal — the brain knows the topic but maybe not the
              specifics; verify, do not assert absence.
    unknown   no meaningful signal — absence is plausible, but verify anyway.

Usage:
    python3 brain-check.py "reddit dev license app"
    python3 brain-check.py --json "vps root access mar gateway"
    python3 brain-check.py --facts /path/to/facts.json "boardroom voting rules"

Exit code is always 0 (this is a query tool, not a gate). Read `verdict`.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_STALE_DAYS = 60
MAX_EVIDENCE = 3
# Coverage/gap/verdict are computed over the top-ranked pool, NOT the whole
# match set. On a large corpus a common query term ("app", "depth") matches
# hundreds of incidental facts; counting those would inflate coverage and mark
# a term "known" because of an unrelated fact. BM25 already floats the facts
# that match the rare, on-topic terms to the top, so the pool is where the real
# signal is. POOL_K bounds it.
POOL_K = 12

# Minimal stopword set so "what do I know about the boardroom" reduces to the
# meaningful term {boardroom}. Deliberately small — better to keep a borderline
# term than to silently drop signal and under-report what the brain holds.
STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "was", "were",
    "do", "does", "did", "i", "what", "about", "on", "in", "for", "it",
    "that", "this", "with", "my", "me", "you", "we", "have", "has", "had",
    "know", "any", "there", "if", "be", "been", "our", "us", "from", "at",
    "as", "by", "can", "will", "would",
}

_BR = None


def _br():
    """Lazily load budget-recall.py by path (hyphenated -> not importable by
    name) and reuse its tokenizer + BM25 search, so the verdict reflects what
    the live recall would actually surface."""
    global _BR
    if _BR is None:
        path = BIN_DIR / "budget-recall.py"
        spec = importlib.util.spec_from_file_location("budget_recall", path)
        _BR = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BR)
    return _BR


def meaningful_terms(query: str) -> list[str]:
    """Tokenize with the recall tokenizer, drop stopwords + 1-char tokens,
    preserve first-seen order, dedupe."""
    seen: dict[str, None] = {}
    for tok in _br()._tokenize(query):
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        seen.setdefault(tok, None)
    return list(seen.keys())


def _advice(verdict: str, result: dict) -> str:
    if verdict == "exists":
        msg = ("Brain HAS signal on this — do NOT claim absence or ignorance; "
               "cite the evidence.")
        if result["stale"]:
            msg += (f" Heads up: newest evidence is {result['freshness']} (stale) "
                    "— verify specifics against live substrate.")
        return msg
    if verdict == "probable":
        miss = ", ".join(result["missing_terms"]) or "(none)"
        return ("Partial signal — the brain knows the topic but not all of it "
                f"(no signal on: {miss}). Likely exists; verify against live "
                "substrate before asserting absence.")
    return ("No brain signal on this. Absence is plausible — but verify-before-"
            "claim still applies: check live substrate before telling Kevin it "
            "does not exist.")


def check(facts: list[dict], query: str, *, now=None,
          stale_days: int = DEFAULT_STALE_DAYS) -> dict:
    """Return the exists/probable/unknown verdict for `query` over `facts`.

    `now` is injectable for deterministic freshness tests.
    """
    br = _br()
    terms = meaningful_terms(query)
    result = {
        "query": query,
        "query_terms": terms,
        "verdict": "unknown",
        "term_coverage": 0.0,
        "matched_terms": [],
        "missing_terms": list(terms),
        "hits": 0,
        "strong_hits": 0,
        "freshness": None,
        "stale": False,
        "evidence": [],
        "advice": "",
    }
    if not terms:
        result["advice"] = "No meaningful query terms after stopword removal."
        return result

    ranked = br.search(facts, query, now=now)
    result["hits"] = len(ranked)

    # Restrict signal to the top-ranked pool — see POOL_K. `strong_hits` counts
    # pool facts that cover 2+ query terms (a genuine topical match, not one
    # incidental common-term hit).
    pool = ranked[:POOL_K]
    term_set = set(terms)
    matched: set[str] = set()
    top_overlap = 0.0
    strong_hits = 0
    for f in pool:
        covered = term_set & set(br._tokenize(str(f.get("content", ""))))
        if covered:
            matched |= covered
            top_overlap = max(top_overlap, len(covered) / len(term_set))
            if len(covered) >= 2:
                strong_hits += 1
    result["matched_terms"] = [t for t in terms if t in matched]
    result["missing_terms"] = [t for t in terms if t not in matched]
    result["strong_hits"] = strong_hits
    coverage = len(matched) / len(term_set)
    result["term_coverage"] = round(coverage, 3)

    if not ranked:
        verdict = "unknown"
    elif top_overlap >= 0.6 or strong_hits >= 2:
        verdict = "exists"
    elif matched:
        # Some real top-ranked signal but not corroborated/broad — the brain
        # knows the topic, maybe not the specifics. Bias here is deliberate:
        # for a guard against false absence claims, "go verify" is the safe
        # call; the missing_terms gap-note says exactly what is unconfirmed.
        verdict = "probable"
    else:
        verdict = "unknown"
    result["verdict"] = verdict

    # Evidence (top-ranked) and freshness (newest source_date across all matches).
    result["evidence"] = [{
        "kind": f.get("kind", "fact"),
        "source_date": f.get("source_date", "unknown"),
        "confidence": f.get("confidence"),
        "content": str(f.get("content", ""))[:200],
    } for f in ranked[:MAX_EVIDENCE]]

    newest = None
    for f in ranked:
        d = br._parse_date(f.get("source_date"))
        if d is not None and (newest is None or d > newest):
            newest = d
    if newest is not None:
        result["freshness"] = newest.date().isoformat()
        ref = br._resolve_now(now)
        nd = newest
        if nd.tzinfo is None and ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        elif nd.tzinfo is not None and ref.tzinfo is None:
            nd = nd.replace(tzinfo=None)
        result["stale"] = (ref - nd).days > stale_days

    result["advice"] = _advice(verdict, result)
    return result


def main():
    p = argparse.ArgumentParser(
        description="Does my brain already know this? (exists/probable/unknown)")
    p.add_argument("query", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    args = p.parse_args()

    query = " ".join(args.query).strip()
    if not query:
        print("usage: brain-check.py <query terms>", file=sys.stderr)
        sys.exit(2)

    facts = load_facts(args.facts)
    result = check(facts, query, stale_days=args.stale_days)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return

    print(f"{result['verdict'].upper()}  "
          f"(coverage {result['term_coverage']}, "
          f"{result['strong_hits']} strong / {result['hits']} total hits)")
    if result["missing_terms"]:
        print("  no brain signal on: " + ", ".join(result["missing_terms"]))
    if result["freshness"]:
        print(f"  newest evidence: {result['freshness']}"
              f"{' [STALE]' if result['stale'] else ''}")
    print("  " + result["advice"])
    for e in result["evidence"]:
        print(f"    - [{e['source_date']}] [{e['kind']}] {e['content'][:120]}")


if __name__ == "__main__":
    main()
