#!/usr/bin/env python3
"""Benchmark recall quality with graph expansion OFF vs ON against a live store.

Replicates the production selection path in budget_recall() (BM25 seeds ->
optional graph expansion -> insight-lead dedup -> date diversity cap ->
token-budget truncation) while tracking fact IDs, so results can be scored
against ground truth. Each query carries a distinctive ground-truth token
(e.g. 'stripe') that the query text deliberately does NOT contain — a hit
means recall bridged a vocabulary mismatch, the failure mode keyword ranking
is worst at.

This is an offline benchmark, not a unit test: results depend on the local
fact store. The 2026-07-10 run against a 2,480-fact store is what exposed the
vacuous >=2-shared-terms neighbor gate (see _graph_recall.py) and, after that
fix, showed graph expansion enriches on-topic seeds but cannot rescue queries
whose seeds are wrong-topic — that class needs semantic retrieval.

Usage:
    python3 eval-graph-recall.py
    python3 eval-graph-recall.py --facts ~/.nock-brain/facts.json --budget 800
    python3 eval-graph-recall.py --queries my-queries.json --json out.json

Custom --queries file: JSON list of [label, query, ground_truth_token] items.
"""
import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
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

# (label, query, ground-truth token). M* queries avoid their token on purpose
# (vocabulary mismatch); C* controls contain it (BM25 baseline sanity).
DEFAULT_QUERIES = [
    ["M1", "how are customer payments handled", "stripe"],
    ["M2", "voice transcription mixing up agent names", "deepgram"],
    ["M3", "text to speech provider quota", "elevenlabs"],
    ["M4", "secret leak scanning in CI pipelines", "gitleaks"],
    ["M5", "agent liveness monitoring", "heartbeat"],
    ["M6", "hosting platform downtime incident", "railway"],
    ["C1", "gitleaks scan status", "gitleaks"],
    ["C2", "railway outage recovery", "railway"],
    ["C3", "deepgram transcription bug", "deepgram"],
]


def run_mode(br, all_facts, insights, query, budget, graph_expand):
    """Replicate budget_recall() selection; return (injected, ranked, seeds, secs)."""
    now = datetime.now(timezone.utc)
    terms = br._query_terms(query)
    min_matches = br._default_recall_min_matches(terms)
    t0 = time.perf_counter()
    seeds = br.search(all_facts, query, False, now=now, min_matched_terms=min_matches)
    expanded = br._maybe_graph_expand(all_facts, seeds, query, False, now, graph_expand)
    ins = br.search(insights, query, False, now=now, min_matched_terms=min_matches)
    covered = {sid for i in ins for sid in i.get("source_ids", [])}
    results = ins + [f for f in expanded if f.get("id") not in covered]
    results = br._apply_date_diversity_cap(results, br._resolve_max_per_date(None))
    used = br.estimate_tokens(
        f"Memory recall ({len(results)} matches, budget {budget} tokens):"
    )
    injected = []
    for f in results:
        ft = br.estimate_tokens(br.format_fact(f, terms))
        if used + ft > budget:
            break
        injected.append(f)
        used += ft
    return injected, results, len(seeds), time.perf_counter() - t0


def first_target_rank(items, token):
    for i, f in enumerate(items, 1):
        if token in str(f.get("content", "")).lower():
            return i
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS)
    parser.add_argument("--budget", type=int, default=800)
    parser.add_argument("--queries", type=Path,
                        help="JSON list of [label, query, ground_truth_token]")
    parser.add_argument("--json", type=Path, help="write per-query detail here")
    args = parser.parse_args()

    if not args.facts.exists():
        print(f"No fact store at {args.facts}. Run extract-facts.py first.",
              file=sys.stderr)
        sys.exit(1)

    br = _load_budget_recall()
    verify_key = br._resolve_verify_key()
    all_facts = br._load(args.facts, verify_key=verify_key)
    insights = (br._load(args.insights, verify_key=verify_key)
                if args.insights.exists() else [])
    queries = (json.loads(args.queries.read_text())
               if args.queries else DEFAULT_QUERIES)
    print(f"facts={len(all_facts)} insights={len(insights)} budget={args.budget}\n")

    rows = []
    for label, query, token in queries:
        targets = sum(1 for f in all_facts
                      if token in str(f.get("content", "")).lower())
        off_inj, off_all, off_seeds, off_t = run_mode(
            br, all_facts, insights, query, args.budget, False)
        on_inj, on_all, on_seeds, on_t = run_mode(
            br, all_facts, insights, query, args.budget, True)
        off_ids = {f.get("id") for f in off_inj}
        added = [f for f in on_inj if f.get("id") not in off_ids]
        added_targets = [f for f in added
                         if token in str(f.get("content", "")).lower()]
        rows.append({
            "label": label, "query": query, "token": token,
            "targets_in_store": targets,
            "off": {"seeds": off_seeds, "injected": len(off_inj),
                    "target_rank": first_target_rank(off_inj, token),
                    "target_rank_full": first_target_rank(off_all, token),
                    "secs": round(off_t, 2)},
            "on": {"seeds": on_seeds, "injected": len(on_inj),
                   "target_rank": first_target_rank(on_inj, token),
                   "target_rank_full": first_target_rank(on_all, token),
                   "secs": round(on_t, 2)},
            "graph_added_to_injection": len(added),
            "graph_added_targets": len(added_targets),
        })

    print(f"{'Q':<4}{'targets':>8} | {'OFF seeds':>9} {'inj':>4} {'hit@inj':>8} | "
          f"{'ON inj':>6} {'hit@inj':>8} {'added':>6} {'+tgt':>5} | "
          f"{'off_s':>6} {'on_s':>6}")
    dash = lambda v: str(v) if v else "-"
    for r in rows:
        o, n = r["off"], r["on"]
        print(f"{r['label']:<4}{r['targets_in_store']:>8} | {o['seeds']:>9} "
              f"{o['injected']:>4} {dash(o['target_rank']):>8} | "
              f"{n['injected']:>6} {dash(n['target_rank']):>8} "
              f"{r['graph_added_to_injection']:>6} {r['graph_added_targets']:>5} | "
              f"{o['secs']:>6} {n['secs']:>6}")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))
        print(f"\ndetail -> {args.json}")


if __name__ == "__main__":
    main()
