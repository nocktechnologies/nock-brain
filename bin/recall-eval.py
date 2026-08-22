#!/usr/bin/env python3
"""Committed recall eval: run the reconstructed n=36 gold query set against a
COMMITTED, SIGNED fixture store and report two metrics.

This is the CI-hardened successor to the throwaway pilot harness (see
reports/2026-08-22-openviking-recall-pilot.md). It drives the REAL production
selection path (`budget-recall.select_recall`), so what it measures is exactly
what the injection hook would emit — but hermetically, against
tests/fixtures/recall-eval-store.json, never the live ~/.nock-brain store.

Two metrics:

  * recall  (identity)      — the existing metric: fraction of gold fact ids that
                              land in select_recall()['included'] (the packer's
                              injected set) at the production budget.
  * companionship  (new)    — of the gold hits that live in a multi-fact session,
                              the mean fraction of their session-siblings that
                              arrive WITH them. This is the metric that makes the
                              session-hierarchy payoff (Phase 2/3) measurable:
                              identity recall cannot see whether a hit came with
                              its surrounding context. Baseline is deliberately
                              low — most hits arrive isolated.

Hermeticity note: the fixture ships NO embeddings.npz sidecar, so the semantic
tier is inert by design and CI runs semantic-OFF. With no sidecar the semantic
path degrades to BM25 (identical result), so the committed floor is a BM25 number
CI can always reproduce. Signatures ARE verified (against the committed
disposable fixture key) so the eval also guards the v2-authority signing rule.

Usage:
    python3 bin/recall-eval.py                 # metrics on the fixture
    python3 bin/recall-eval.py --json          # machine-readable
    python3 bin/recall-eval.py --self-test     # validate the instrument (cap lever)
    python3 bin/recall-eval.py --gate          # CI: exit 1 on regression
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
REPO = BIN_DIR.parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

DEFAULT_STORE = REPO / "tests" / "fixtures" / "recall-eval-store.json"
DEFAULT_GOLD = REPO / "docs" / "evals" / "recall-gold-v1.json"
DEFAULT_KEY = REPO / "tests" / "fixtures" / "recall-eval-key.pub"

# Production operating point (crm-mira/core/hooks/memory-inject.sh).
PROD_BUDGET = 400
PROD_MAX_PER_DATE = 4

# Committed regression floors (see docs/evals/README.md for the rationale).
# Set below the measured fixture baselines with headroom for BM25 noise across
# the CI Python matrix; a real recall regression trips these, day-to-day jitter
# does not.
RECALL_FLOOR = 0.90          # measured baseline 0.972
COMPANIONSHIP_FLOOR = 0.05   # measured baseline ~0.15; guards against a hit-context collapse


def _load_budget_recall():
    spec = importlib.util.spec_from_file_location(
        "budget_recall", BIN_DIR / "budget-recall.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _session_key(f: dict) -> str:
    return str(f.get("session") or f.get("session_anchor") or "")


def _selection(br, query, store, *, budget, max_per_date, semantic, graph):
    return br.select_recall(
        query, store, budget=budget,
        graph_expand=graph, max_per_date=max_per_date, semantic=semantic,
    )


def evaluate(br, store: Path, queries: dict, *, budget, max_per_date,
             semantic, graph):
    """Return recall + companionship + per-query detail for one config."""
    facts = json.loads(store.read_text())
    sib_of_session: dict[str, set] = collections.defaultdict(set)
    for f in facts:
        if f.get("status", "current") != "current":
            continue
        s = _session_key(f)
        if s:
            sib_of_session[s].add(f["id"])

    hits = 0
    comp_scores: list[float] = []
    comp_applicable = 0
    comp_zero = 0
    rows = []
    for gid, query in queries.items():
        sel = _selection(br, query, store, budget=budget,
                         max_per_date=max_per_date, semantic=semantic, graph=graph)
        included = {f["id"] for f in sel["included"]} if sel else set()
        found = gid in included
        hits += 1 if found else 0

        # companionship applies only to gold in a >=3-member session (>=2 siblings)
        sess = _session_key(next((f for f in facts if f["id"] == gid), {}))
        siblings = sib_of_session.get(sess, set()) - {gid}
        row = {"id": gid, "recall_hit": found, "session_siblings": len(siblings)}
        if len(siblings) >= 2:
            comp_applicable += 1
            if found:
                frac = len(siblings & included) / len(siblings)
                comp_scores.append(frac)
                if frac == 0:
                    comp_zero += 1
                row["companionship"] = round(frac, 3)
        rows.append(row)

    n = len(queries)
    recall = hits / n if n else 0.0
    # Two companionship framings, both over the SAME denominator = the multi-
    # session gold hits that actually landed (`companionship_measured_on`):
    #  * companionship        — mean fraction of each hit's siblings co-injected
    #    (the design-authoritative metric Phase 2 tunes; pilot baseline 7.4%).
    #  * companionship_hit_rate — fraction of those hits that arrive with >=1
    #    sibling (the pilot's "15 of 18 arrived isolated" framing; 3/18=16.7%).
    companionship = statistics.mean(comp_scores) if comp_scores else 0.0
    measured = len(comp_scores)
    hit_rate = (measured - comp_zero) / measured if measured else 0.0
    return {
        "config": {"budget": budget, "max_per_date": max_per_date,
                   "semantic": semantic, "graph": graph},
        "n": n,
        "recall": round(recall, 4),
        "recall_hits": hits,
        "companionship": round(companionship, 4),
        "companionship_hit_rate": round(hit_rate, 4),
        "companionship_measured_on": measured,
        "companionship_applicable": comp_applicable,
        "companionship_zero_sibling_hits": comp_zero,
        "rows": rows,
    }


def self_test(br, store: Path, queries: dict) -> dict:
    """Validate the instrument by reproducing the documented cap lever
    (max_per_date=2 materially below >=4) and confirming every fixture fact
    verifies. If the cap lever direction inverts, the harness is broken and any
    downstream technique measurement is untrustworthy."""
    cap2 = evaluate(br, store, queries, budget=PROD_BUDGET, max_per_date=2,
                    semantic=False, graph=True)["recall"]
    cap4 = evaluate(br, store, queries, budget=PROD_BUDGET, max_per_date=4,
                    semantic=False, graph=True)["recall"]
    lever = round(cap4 - cap2, 4)
    ok = cap2 < cap4 and lever >= 0.05
    return {"cap2_recall": cap2, "cap4_recall": cap4, "cap_lever_pts": lever,
            "expect": "cap2 < cap4 by >= 0.05 (pilot measured ~0.083)",
            "pass": ok}


def _verify_all(br, store: Path) -> tuple[int, int]:
    """(valid, total) under the fixture key. Exercises the real verify path."""
    import _sign
    vkey = br._resolve_verify_key()
    facts = json.loads(store.read_text())
    by_id = {f["id"]: f for f in facts}
    valid = sum(1 for f in facts
                if _sign.verify_fact(f, vkey, facts_by_id=by_id) == _sign.VALID)
    return valid, len(facts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", type=Path, default=DEFAULT_STORE)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--key", type=Path, default=DEFAULT_KEY,
                    help="verification public key for the fixture")
    ap.add_argument("--budget", type=int, default=PROD_BUDGET)
    ap.add_argument("--max-per-date", type=int, default=PROD_MAX_PER_DATE)
    ap.add_argument("--semantic", action="store_true",
                    help="enable the semantic tier (inert without a sidecar)")
    ap.add_argument("--no-graph", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="reproduce the cap lever to validate the instrument")
    ap.add_argument("--gate", action="store_true",
                    help="CI mode: nonzero exit on regression below the floors")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.store.exists():
        print(f"fixture store not found: {args.store} "
              f"(run bin/build-recall-fixture.py)", file=sys.stderr)
        return 2
    if not args.gold.exists():
        print(f"gold set not found: {args.gold}", file=sys.stderr)
        return 2

    # Verification ON against the committed fixture key. NOCKBRAIN_SIGNING_PUB is
    # a path env (not secret material); the fixture key is disposable.
    os.environ["NOCKBRAIN_SIGNING_PUB"] = str(args.key)
    os.environ.pop("NOCKBRAIN_SIGNING_KEY", None)
    # Neutralize ambient recall knobs so the measurement depends ONLY on the
    # kwargs below — otherwise a stray export could move either arm of a
    # gate-on/gate-off technique comparison (e.g. Phase 2's
    # NOCKBRAIN_SESSION_COMPANIONS) and make it meaningless.
    for knob in ("NOCKBRAIN_MAX_PER_DATE", "NOCKBRAIN_SEMANTIC",
                 "NOCKBRAIN_GRAPH_RECALL", "NOCKBRAIN_TIER_TAIL",
                 "NOCKBRAIN_SESSION_COMPANIONS"):
        os.environ.pop(knob, None)

    queries = json.loads(args.gold.read_text()).get("queries", {})
    br = _load_budget_recall()

    # Copy the fixture into a scratch dir so the verify-cache side-file
    # (store.json.verified-cache.json) never lands in the repo working tree.
    with tempfile.TemporaryDirectory() as td:
        store = Path(td) / "recall-eval-store.json"
        shutil.copy2(args.store, store)

        valid, total = _verify_all(br, store)
        result = evaluate(br, store, queries,
                          budget=args.budget, max_per_date=args.max_per_date,
                          semantic=args.semantic, graph=not args.no_graph)
        st = self_test(br, store, queries) if (args.self_test or args.gate) else None

    result["fixture_verified"] = {"valid": valid, "total": total}

    if args.json:
        out = {"result": result}
        if st is not None:
            out["self_test"] = st
        print(json.dumps(out, indent=2))
    else:
        c = result["config"]
        print(f"recall-eval  |  n={result['n']}  store={args.store.name}  "
              f"budget={c['budget']} max_per_date={c['max_per_date']} "
              f"semantic={c['semantic']} graph={c['graph']}")
        print(f"  fixture attestation : {valid}/{total} VALID")
        print(f"  recall (identity)   : {result['recall']*100:.1f}%  "
              f"({result['recall_hits']}/{result['n']} gold ids injected)")
        m = result["companionship_measured_on"]
        print(f"  companionship       : {result['companionship']*100:.1f}%  "
              f"(mean sibling-fraction over {m} multi-session hits; "
              f"{result['companionship_zero_sibling_hits']}/{m} arrived isolated)")
        print(f"  companionship hitrate: {result['companionship_hit_rate']*100:.1f}%  "
              f"(share of those {m} hits arriving with >=1 sibling)")
        if st is not None:
            verdict = "PASS" if st["pass"] else "FAIL"
            print(f"  self-test cap lever : cap2={st['cap2_recall']*100:.1f}% "
                  f"cap4={st['cap4_recall']*100:.1f}% "
                  f"(+{st['cap_lever_pts']*100:.1f}pt)  [{verdict}]")

    if args.gate:
        problems = []
        if valid != total:
            problems.append(f"attestation: only {valid}/{total} facts VALID")
        if result["recall"] < RECALL_FLOOR:
            problems.append(
                f"recall {result['recall']*100:.1f}% < floor {RECALL_FLOOR*100:.0f}%")
        if result["companionship"] < COMPANIONSHIP_FLOOR:
            problems.append(
                f"companionship {result['companionship']*100:.1f}% "
                f"< floor {COMPANIONSHIP_FLOOR*100:.0f}%")
        if st and not st["pass"]:
            problems.append(
                f"instrument self-test failed (cap lever "
                f"{st['cap_lever_pts']*100:.1f}pt, expected >= 5pt)")
        if problems:
            print("\nREGRESSION GATE FAILED:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("regression gate: PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
