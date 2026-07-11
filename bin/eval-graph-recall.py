#!/usr/bin/env python3
"""Benchmark recall quality against a live store: flat BM25 vs a variant
(semantic hybrid by default, or graph expansion).

Since Phase 2 this drives the REAL production selection path
(budget-recall's select_recall) rather than a replica, so what it measures
is exactly what the injection hook would emit. Each query carries a ground
truth the query text deliberately does not hand to BM25:

    "token:stripe"      any selected fact containing the token is a hit
    "id:462382c5026f"   only that specific fact is a hit (curated suites)

The default suite is the Phase 0 set (kept for continuity — note M1/M3 were
shown to have no genuinely on-topic fact in the store; the curated suite in
docs/evals/curated-recall-suite.json re-bases acceptance on fact-id ground
truth per the Phase 0 decision record).

Usage:
    python3 eval-graph-recall.py                          # BM25 vs semantic
    python3 eval-graph-recall.py --graph                  # BM25 vs graph
    python3 eval-graph-recall.py --queries docs/evals/curated-recall-suite.json
    python3 eval-graph-recall.py --json out.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))


def _load_budget_recall():
    spec = importlib.util.spec_from_file_location(
        "budget_recall", BIN_DIR / "budget-recall.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_INSIGHTS = Path.home() / ".nock-brain" / "insights.json"

# Phase 0 suite. M* avoid their ground token on purpose; C* controls
# contain it (BM25 baseline sanity).
DEFAULT_QUERIES = [
    ["M1", "how are customer payments handled", "token:stripe"],
    ["M2", "voice transcription mixing up agent names", "token:deepgram"],
    ["M3", "text to speech provider quota", "token:elevenlabs"],
    ["M4", "secret leak scanning in CI pipelines", "token:gitleaks"],
    ["M5", "agent liveness monitoring", "token:heartbeat"],
    ["M6", "hosting platform downtime incident", "token:railway"],
    ["C1", "gitleaks scan status", "token:gitleaks"],
    ["C2", "railway outage recovery", "token:railway"],
    ["C3", "deepgram transcription bug", "token:deepgram"],
]


def ground_matches(fact: dict, ground: str) -> bool:
    if ground.startswith("id:"):
        return str(fact.get("id", "")) == ground[3:]
    token = ground[6:] if ground.startswith("token:") else ground
    return token.lower() in str(fact.get("content", "")).lower()


def first_hit_rank(items: list, ground: str):
    for i, f in enumerate(items, 1):
        if ground_matches(f, ground):
            return i
    return None


def run_mode(br, query: str, args, graph: bool, semantic: bool):
    t0 = time.perf_counter()
    selection = br.select_recall(
        query, args.facts, args.budget,
        insights_file=args.insights if args.insights.exists() else None,
        graph_expand=graph, semantic=semantic,
    )
    secs = time.perf_counter() - t0
    if selection is None:
        return [], [], secs
    return selection["included"], selection["results"], secs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS)
    parser.add_argument("--budget", type=int, default=800)
    parser.add_argument("--graph", action="store_true",
                        help="Variant = graph expansion instead of semantic")
    parser.add_argument("--queries", type=Path,
                        help="JSON list of [label, query, ground] where "
                             "ground is 'token:x' or 'id:x'")
    parser.add_argument("--json", type=Path, help="write per-query detail")
    args = parser.parse_args()

    if not args.facts.exists():
        print(f"No fact store at {args.facts}.", file=sys.stderr)
        sys.exit(1)

    br = _load_budget_recall()
    queries = (json.loads(args.queries.read_text())
               if args.queries else DEFAULT_QUERIES)
    variant = "graph" if args.graph else "semantic"
    print(f"baseline: flat BM25  |  variant: {variant}  |  "
          f"budget {args.budget}\n")

    rows = []
    hits_base = hits_var = scored = 0
    print(f"{'Q':<4}{'OFF inj':>8} {'hit@inj':>8} | {'ON inj':>7} "
          f"{'hit@inj':>8} {'added':>6} {'+tgt':>5} | {'off_s':>6} {'on_s':>6}")
    for label, query, ground in queries:
        off_inj, off_all, off_t = run_mode(br, query, args, False, False)
        on_inj, on_all, on_t = run_mode(
            br, query, args, graph=args.graph,
            semantic=not args.graph)
        off_ids = {f.get("id") for f in off_inj}
        added = [f for f in on_inj if f.get("id") not in off_ids]
        added_tgt = [f for f in added if ground_matches(f, ground)]
        off_rank = first_hit_rank(off_inj, ground)
        on_rank = first_hit_rank(on_inj, ground)
        scored += 1
        hits_base += 1 if off_rank else 0
        hits_var += 1 if on_rank else 0
        dash = lambda v: str(v) if v else "-"
        print(f"{label:<4}{len(off_inj):>8} {dash(off_rank):>8} | "
              f"{len(on_inj):>7} {dash(on_rank):>8} {len(added):>6} "
              f"{len(added_tgt):>5} | {off_t:>6.2f} {on_t:>6.2f}")
        rows.append({
            "label": label, "query": query, "ground": ground,
            "off": {"injected": len(off_inj), "hit_rank": off_rank,
                    "hit_rank_full": first_hit_rank(off_all, ground),
                    "secs": round(off_t, 3)},
            "on": {"injected": len(on_inj), "hit_rank": on_rank,
                   "hit_rank_full": first_hit_rank(on_all, ground),
                   "secs": round(on_t, 3)},
            "added_to_injection": len(added),
            "added_targets": len(added_tgt),
        })

    print(f"\nhits in injected set: baseline {hits_base}/{scored}, "
          f"{variant} {hits_var}/{scored}")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))
        print(f"detail -> {args.json}")


if __name__ == "__main__":
    main()
