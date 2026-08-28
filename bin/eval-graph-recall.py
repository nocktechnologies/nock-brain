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
    python3 eval-graph-recall.py --self-test              # cache-warmup A/B check
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


def _select(br, query: str, args, graph: bool, semantic: bool):
    """One production select_recall; no timing. Used for cache warmup and by
    run_mode so both timed arms share a single call site."""
    selection = br.select_recall(
        query, args.facts, args.budget,
        insights_file=args.insights if args.insights.exists() else None,
        graph_expand=graph, semantic=semantic,
    )
    if selection is None:
        return [], []
    return selection["included"], selection["results"]


def run_mode(br, query: str, args, graph: bool, semantic: bool):
    t0 = time.perf_counter()
    included, results = _select(br, query, args, graph, semantic)
    secs = time.perf_counter() - t0
    return included, results, secs


def _warm_then_time(br, query: str, args, graph: bool, semantic: bool):
    """Time both A/B arms from an identically warm verification cache.

    The sidecar is store-level: a cold baseline run writes it, and the
    variant then inherits the ~0.4-0.8s signature saving — which is not a
    graph/semantic effect (issue #51). One discarded dry run per query
    pays that cost before either arm is timed.
    """
    _select(br, query, args, False, False)
    off = run_mode(br, query, args, False, False)
    on = run_mode(br, query, args, graph, semantic)
    return off, on


def _self_test() -> int:
    """Hermetic check that both timed arms skip the cold signature pass.

    Runs the same warm-then-time order as the live harness against the
    committed recall-eval fixture (copied to a temp dir so the sidecar
    never lands in the tree). A cold load must perform signature ops; both
    timed arms must then perform none.
    """
    import os
    import shutil
    import tempfile

    repo = BIN_DIR.parent
    fixture = repo / "tests" / "fixtures" / "recall-eval-store.json"
    key = repo / "tests" / "fixtures" / "recall-eval-key.pub"
    if not fixture.exists() or not key.exists():
        print("self-test: committed fixture or key missing", file=sys.stderr)
        return 2

    import _sign
    calls = {"n": 0}
    real = _sign.SigningKey.verify_bytes

    def counting(self, payload, signature_hex):
        calls["n"] += 1
        return real(self, payload, signature_hex)

    saved_pub = os.environ.get("NOCKBRAIN_SIGNING_PUB")
    saved_key = os.environ.get("NOCKBRAIN_SIGNING_KEY")
    _sign.SigningKey.verify_bytes = counting
    try:
        os.environ["NOCKBRAIN_SIGNING_PUB"] = str(key)
        os.environ.pop("NOCKBRAIN_SIGNING_KEY", None)
        br = _load_budget_recall()
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "facts.json"
            shutil.copy2(fixture, store)
            args = argparse.Namespace(
                facts=store,
                insights=Path(td) / "insights.json",
                budget=800,
            )
            query = "how are customer payments handled"
            calls["n"] = 0
            _select(br, query, args, False, False)
            warmup_ops = calls["n"]
            calls["n"] = 0
            run_mode(br, query, args, False, False)
            off_ops = calls["n"]
            calls["n"] = 0
            run_mode(br, query, args, False, True)
            on_ops = calls["n"]
    finally:
        _sign.SigningKey.verify_bytes = real
        if saved_pub is None:
            os.environ.pop("NOCKBRAIN_SIGNING_PUB", None)
        else:
            os.environ["NOCKBRAIN_SIGNING_PUB"] = saved_pub
        if saved_key is None:
            os.environ.pop("NOCKBRAIN_SIGNING_KEY", None)
        else:
            os.environ["NOCKBRAIN_SIGNING_KEY"] = saved_key

    ok = warmup_ops > 0 and off_ops == 0 and on_ops == 0
    verdict = "PASS" if ok else "FAIL"
    print(f"eval-graph-recall self-test  [{verdict}]")
    print(f"  warmup signature ops : {warmup_ops}")
    print(f"  timed baseline ops   : {off_ops}")
    print(f"  timed variant ops    : {on_ops}")
    print("  both timed arms must be warm (issue #51: verify-cache A/B skew)")
    return 0 if ok else 1


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
    parser.add_argument("--self-test", action="store_true",
                        help="hermetic check: both timed A/B arms see a "
                             "warm verification cache (issue #51)")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(_self_test())

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
        (off_inj, off_all, off_t), (on_inj, on_all, on_t) = _warm_then_time(
            br, query, args, graph=args.graph, semantic=not args.graph)
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
