#!/usr/bin/env python3
"""Regenerate a stratified gold-fact ID sample for the recall eval.

DEV / MAINTAINER TOOL. It samples clean, current, prose facts from a source
store (read-only) stratified across kinds and dates, and emits a gold scaffold
that a human then completes with hand-authored paraphrase queries.

IMPORTANT — queries are NOT machine-generated. This generator only picks the
IDS (the reproducible, mechanical half). Each paraphrase query must be authored
by a human, overlap-guarded against the fact's verbatim wording, so recall is
not trivially lexical. That is why the current committed set
(docs/evals/recall-gold-v1.json) is a reconstruction of a lost n=90 set and can
be *extended* deliberately but never fully auto-regenerated.

Usage:
    python3 bin/gen-recall-gold.py                         # print scaffold
    python3 bin/gen-recall-gold.py --out docs/evals/recall-gold-v2.json
    python3 bin/gen-recall-gold.py --source path/to/facts.json --per-kind ...
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = Path.home() / ".nock-brain" / "facts.json"

# Stratification target, mirroring the pilot's _gold.py.
DEFAULT_TARGETS = {
    "directive": 9, "decision": 7, "bug": 7, "architecture": 7, "correction": 6,
}


def is_prose(content: str) -> bool:
    """Clean prose: readable sentence-shaped content, not JSON/verdict/log noise."""
    c = str(content)
    if len(c) < 180 or len(c) > 1200:
        return False
    if any(t in c for t in ('VERDICT:', 'Answer on the first line', 'EARLIER:',
                            '{"', '":"', 'json')):
        return False
    if c.count('/') > 6 or c.count(':') > 8:
        return False
    return True


def sample_gold(facts: list[dict], targets: dict[str, int]) -> list[dict]:
    current = [
        f for f in facts
        if f.get("status", "current") == "current"
        and f.get("confidence", 0) >= 0.7
        and is_prose(f.get("content"))
    ]
    by_kind: dict[str, list[dict]] = collections.defaultdict(list)
    for f in current:
        by_kind[f.get("kind")].append(f)
    gold: list[dict] = []
    for kind, n in targets.items():
        lst = sorted(by_kind.get(kind, []), key=lambda f: str(f.get("source_date")))
        if not lst:
            continue
        step = max(1, len(lst) // n)  # spread across dates, don't cluster on 05-19
        gold.extend(lst[::step][:n])
    seen: set[str] = set()
    out: list[dict] = []
    for f in gold:
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out", type=Path, help="write scaffold JSON (default: stdout)")
    ap.add_argument("--per-kind", type=json.loads, default=None,
                    help='override targets, e.g. \'{"directive":12,"bug":8}\'')
    args = ap.parse_args()

    if not args.source.exists():
        print(f"source store not found: {args.source}", file=sys.stderr)
        return 2

    facts = json.loads(args.source.read_text())
    targets = args.per_kind or DEFAULT_TARGETS
    gold = sample_gold(facts, targets)

    scaffold = {
        "_meta": {
            "name": args.out.stem if args.out else "recall-gold-scaffold",
            "n": len(gold),
            "scoring": "identity — gold id must appear in select_recall()['included']",
            "gold_definition": "current prose facts conf>=0.7, stratified by kind/date",
            "queries": "HAND-AUTHOR each query below (replace the TODO); "
                       "overlap-guard against the fact's verbatim wording",
            "generated_by": "bin/gen-recall-gold.py (ids only; queries are human)",
        },
        # id -> query. Emitted as a TODO the author fills in.
        "queries": {f["id"]: "TODO: paraphrase query" for f in gold},
    }
    # A commented preview of each fact, to help the author write the query.
    preview = {
        f["id"]: {
            "kind": f.get("kind"), "date": f.get("source_date"),
            "content": re.sub(r"\s+", " ", str(f.get("content", "")))[:240],
        }
        for f in gold
    }
    scaffold["_fact_preview"] = preview

    text = json.dumps(scaffold, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
        dc = collections.Counter(str(f.get("source_date"))[:7] for f in gold)
        print(f"wrote {args.out} — {len(gold)} gold ids "
              f"(date-month spread: {dict(dc)}); now hand-author the queries.",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
