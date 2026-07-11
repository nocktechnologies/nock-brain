#!/usr/bin/env python3
"""Brain-think — cited synthesis + gap analysis (gbrain `think` steal)

Turns raw recall into a briefing packet: the top-ranked facts as CITATIONS
built from the structured fact fields (date, kind, confidence, id, content —
never a fabricated string), plus the GAP block: the query terms the brain has
no signal on, freshness/staleness, and the verdict. The agent reading the
packet synthesizes the prose; this tool never composes prose itself.

That split is deliberate. For an agent-integrated brain the LLM *is* the agent,
so a separate synthesis call would just add cost (the money gate) and a
hallucination surface. brain-think assembles cited, gap-annotated evidence; the
agent speaks "here is what I have, here is what I do not" from it.

Composes with brain-check (reused for the verdict + gap) and budget-recall's
BM25 search (reused for ranked evidence), so the packet reflects what the live
recall would actually surface.

Usage:
    python3 brain-think.py "reddit dev license"
    python3 brain-think.py --json "vps root access mar gateway"
    python3 brain-think.py --facts /path/to/facts.json --max-cite 8 "boardroom"
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

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
DEFAULT_MAX_CITE = 6

_BC = None


def _bc():
    """Lazily load brain-check.py by path (hyphenated -> not importable by
    name). Reused for the verdict + gap-note, and via it the shared BM25
    search, so brain-think never reimplements ranking."""
    global _BC
    if _BC is None:
        path = BIN_DIR / "brain-check.py"
        spec = importlib.util.spec_from_file_location("brain_check", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load brain-check.py from {path}")
        _BC = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BC)
    return _BC


def think(facts: list[dict], query: str, *, now=None,
          stale_days: int = DEFAULT_STALE_DAYS,
          max_cite: int = DEFAULT_MAX_CITE) -> dict:
    """Return a cited briefing packet for `query` over `facts`.

    `now` is injectable for deterministic freshness tests.
    """
    bc = _bc()
    verdict = bc.check(facts, query, now=now, stale_days=stale_days)
    ranked = bc._br().search(facts, query, now=now)

    citations = [{
        "n": i + 1,
        "kind": f.get("kind", "fact"),
        "date": f.get("source_date", "unknown"),
        "confidence": f.get("confidence"),
        "content": (bc._content(f) or "")[:300],
        "id": f.get("id"),
    } for i, f in enumerate(ranked[:max_cite])]

    return {
        "query": query,
        "verdict": verdict["verdict"],
        "citations": citations,
        "citation_count": len(citations),
        "gap": {
            "matched_terms": verdict["matched_terms"],
            "missing_terms": verdict["missing_terms"],
            "freshness": verdict["freshness"],
            "stale": verdict["stale"],
            "strong_hits": verdict["strong_hits"],
            "note": verdict["advice"],
        },
    }


def main():
    p = argparse.ArgumentParser(
        description="Cited recall briefing with a gap note (exists/probable/unknown).")
    p.add_argument("query", nargs="*")
    p.add_argument("--json", action="store_true")
    p.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    p.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    p.add_argument("--max-cite", type=int, default=DEFAULT_MAX_CITE)
    args = p.parse_args()

    query = " ".join(args.query).strip()
    if not query:
        print("usage: brain-think.py <query terms>", file=sys.stderr)
        sys.exit(2)

    facts = load_facts(args.facts)
    packet = think(facts, query, stale_days=args.stale_days, max_cite=args.max_cite)

    if args.json:
        print(json.dumps(packet, indent=2, default=str))
        return

    print(f"THINK: {query}   [{packet['verdict'].upper()}]\n")
    print("What the brain holds (cited):")
    if packet["citations"]:
        for c in packet["citations"]:
            conf = f", conf {c['confidence']}" if c["confidence"] is not None else ""
            print(f"  [{c['n']}] ({c['date']}, {c['kind']}{conf}) {c['content'][:160]}")
    else:
        print("  — nothing on this —")
    gap = packet["gap"]
    print("\nGap:")
    if gap["missing_terms"]:
        print("  - no signal on: " + ", ".join(gap["missing_terms"]))
    if gap["freshness"]:
        print(f"  - newest evidence: {gap['freshness']}"
              f"{' [STALE]' if gap['stale'] else ''}")
    print(f"  - {gap['note'] or ''}")


if __name__ == "__main__":
    main()
