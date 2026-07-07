#!/usr/bin/env python3
"""Consolidate near-duplicate durable facts accumulated across dates.

Extract-time dedup is exact-match within a single run, so the store slowly
accumulates semantically identical facts extracted on different dates ("we use
Postgres 14" three times over months). This tool generalizes the one-off May-19
consolidation (consolidate-may19.py, N8382): it clusters near-identical CURRENT
facts of durable kinds by normalized-content similarity, keeps the
highest-confidence member as canonical, and flips the rest to
status=superseded with a superseded_by pointer to the canonical fact plus a
supersession_reason. budget-recall already reads superseded_by; this is the
first writer of that pointer at consolidation time.

SELECTION (deterministic, dependency-free):
  - Only status=current facts of the durable kinds (default: architecture,
    config, content, decision, directive, bug). kind=correction is NEVER
    touched, even if requested via --kinds.
  - Content is normalized (lowercased, reference tokens like #123 / PR 45 /
    NOCK-12 / N8382 stripped, punctuation dropped). Unlike the May-19 tool,
    bare numbers are KEPT: "Postgres 14" and "Postgres 15" are different
    claims, not duplicates. Facts with fewer than 3 normalized tokens carry
    too little signal to call near-identical and are left alone.
  - Same-kind facts cluster greedily (single-link) at Jaccard token-set
    similarity >= --similarity (default 0.8 — near-identical wording).
  - A cluster is actionable when it has >= --min-cluster members spanning
    at least 2 distinct source_dates (cross-date accumulation is the gap this
    closes; same-date dupes are extract-time dedup's job — override with
    --include-same-date). Canonical = highest confidence, ties broken by most
    recent source_date, then id.

SAFETY: content is NEVER rewritten — fact signatures commit to id+kind+content
(not status), so a status-only flip preserves attestations. Recall verifies
attestations at load (PR #33): any content rewrite would make facts verify
TAMPERED and silently drop from recall. Losers keep their validity window
(the claim was true; it is merely redundant — the canonical stays current).

Default is a DRY-RUN: prints a summary and writes a reviewable manifest,
mutating nothing. --execute additionally requires
--i-have-reviewed-the-manifest AND a prior dry-run manifest whose selection
still matches the live store exactly — execute never rewrites the manifest,
it applies precisely the loser->canonical mapping the operator reviewed or
refuses (store drift between review and execute invalidates the review).
On a match it backs up the store first, then applies the status flips.

OPS RULE (from the #32/#33 handoff): after any --execute against a live store,
run bin/sign-facts.py then bin/verify-facts.py.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_write_json

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
MANIFEST_NAME = "consolidate-facts-manifest.json"

# Durable kinds eligible for near-dup consolidation. Operational-noise kinds
# (status, dispatch, merge) recur by nature and are handled by recall's noise
# gates, not by supersession. correction is the highest standing-order value
# in the store and is hard-excluded from all consolidation (May-19 precedent).
DEFAULT_KINDS = {"architecture", "config", "content", "decision", "directive", "bug"}
NEVER_TOUCH = {"correction"}

DEFAULT_SIMILARITY = 0.8
DEFAULT_MIN_CLUSTER = 2
MIN_TOKENS = 3

# Reference tokens that vary between extractions of the same underlying fact.
# Deliberately NARROWER than the May-19 normalizer: bare digits are kept so
# version-bearing claims ("Postgres 14" vs "Postgres 15") stay distinct.
_REF_TOKEN = re.compile(r"(#\d+|\bpr[-\s]?\d+\b|\bnock[-\s]?\d+\b|\bn\d+\b)", re.I)

POST_EXECUTE_RULE = (
    "OPS RULE: after any --execute on a live store, run bin/sign-facts.py "
    "then bin/verify-facts.py."
)


def _conf(f: dict) -> float:
    try:
        return float(f.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def normalize_tokens(text: str) -> frozenset[str]:
    """Token set of *text* with volatile reference tokens stripped."""
    text = _REF_TOKEN.sub(" ", text.lower())
    return frozenset(re.findall(r"[a-z0-9]+", text))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_near_duplicates(facts: list[dict], similarity: float) -> list[list[dict]]:
    """Greedy single-link clustering of same-kind facts whose normalized token
    sets overlap at Jaccard >= similarity. Facts with too few normalized
    tokens (< MIN_TOKENS) never cluster."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_kind[f.get("kind", "fact")].append(f)

    clusters: list[list[dict]] = []
    for kind_facts in by_kind.values():
        kind_clusters: list[list[dict]] = []
        tokens = {id(f): normalize_tokens(str(f.get("content", ""))) for f in kind_facts}
        for f in kind_facts:
            ft = tokens[id(f)]
            if len(ft) < MIN_TOKENS:
                continue
            placed = False
            for c in kind_clusters:
                if any(jaccard(ft, tokens[id(m)]) >= similarity for m in c):
                    c.append(f)
                    placed = True
                    break
            if not placed:
                kind_clusters.append([f])
        clusters.extend(kind_clusters)
    return clusters


def choose_canonical(members: list[dict]) -> dict:
    """Highest confidence wins; ties break to the most recent source_date,
    then id — fully deterministic across runs."""
    return max(members, key=lambda f: (
        _conf(f), str(f.get("source_date", "")), str(f.get("id", ""))
    ))


def select(rows: list[dict], kinds: set[str] | None = None,
           similarity: float = DEFAULT_SIMILARITY,
           min_cluster: int = DEFAULT_MIN_CLUSTER,
           include_same_date: bool = False) -> dict:
    """Pick the near-duplicate clusters to consolidate. Returns the actionable
    clusters (canonical + supersede list) and flat candidate entries for the
    manifest. Read-only."""
    kinds = (kinds or DEFAULT_KINDS) - NEVER_TOUCH
    eligible = [
        f for f in rows
        if f.get("status", "current") == "current"
        and f.get("kind") in kinds
        and f.get("id")
    ]

    actionable = []
    for cluster in cluster_near_duplicates(eligible, similarity):
        if len(cluster) < min_cluster:
            continue
        dates = {str(f.get("source_date", "")) for f in cluster}
        if len(dates) < 2 and not include_same_date:
            continue
        canonical = choose_canonical(cluster)
        canonical_tokens = normalize_tokens(str(canonical.get("content", "")))
        losers = [f for f in cluster if f is not canonical]
        actionable.append({
            "kind": canonical.get("kind"),
            "canonical": canonical,
            "supersede": losers,
            "similarities": {
                f.get("id"): round(jaccard(
                    normalize_tokens(str(f.get("content", ""))), canonical_tokens), 3)
                for f in losers
            },
        })

    def entry(f: dict, cluster: dict) -> dict:
        return {
            "id": f.get("id"),
            "kind": f.get("kind"),
            "confidence": _conf(f),
            "source_date": f.get("source_date", ""),
            "superseded_by": cluster["canonical"].get("id"),
            "similarity_to_canonical": cluster["similarities"].get(f.get("id")),
            "snippet": str(f.get("content", ""))[:120],
        }

    candidates = [entry(f, c) for c in actionable for f in c["supersede"]]
    return {
        "eligible_total": len(eligible),
        "clusters": actionable,
        "candidates": candidates,
    }


def supersession_map(clusters: list[dict]) -> dict[str, str]:
    """loser fact id -> canonical fact id: the exact mutation --execute
    applies. Works on both live selection clusters (full facts) and manifest
    clusters (summary entries), so the reviewed-vs-live comparison in the
    execute gate is apples-to-apples."""
    return {
        str(f.get("id")): str(c["canonical"].get("id"))
        for c in clusters for f in c.get("supersede", [])
    }


def apply_supersessions(rows: list[dict], clusters: list[dict],
                        now: str | None = None) -> int:
    """Flip every cluster loser to status=superseded with a superseded_by
    pointer at the canonical fact. Status-only: id/kind/content/evidence are
    never touched, so attestations stay valid."""
    stamp = now or datetime.now(timezone.utc).isoformat()
    canonical_by_loser = supersession_map(clusters)
    n = 0
    for f in rows:
        canonical_id = canonical_by_loser.get(str(f.get("id")))
        if canonical_id:
            f["status"] = "superseded"
            f["superseded_at"] = stamp
            f["superseded_by"] = canonical_id
            f["supersession_reason"] = (
                f"near-duplicate consolidated by consolidate-facts; "
                f"canonical={canonical_id}"
            )
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Consolidate near-duplicate durable facts across dates: keep the "
            "highest-confidence member as canonical, flip the rest to "
            "status=superseded with superseded_by=<canonical id>. Content is "
            "never rewritten (signatures commit to id+kind+content; recall "
            "verifies attestations, so a rewrite would drop facts as "
            "TAMPERED). Default is a dry-run that only writes a manifest."
        ),
        epilog=POST_EXECUTE_RULE,
    )
    ap.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    ap.add_argument("--manifest", type=Path, default=None,
                    help=f"manifest path (default: <facts dir>/{MANIFEST_NAME})")
    ap.add_argument("--kinds", type=str, default=None,
                    help="comma-separated kinds to consolidate "
                         f"(default: {','.join(sorted(DEFAULT_KINDS))}; "
                         "correction is always excluded)")
    ap.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY,
                    help="Jaccard token-set threshold for near-identical "
                         f"(default: {DEFAULT_SIMILARITY})")
    ap.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER,
                    help=f"minimum cluster size (default: {DEFAULT_MIN_CLUSTER})")
    ap.add_argument("--include-same-date", action="store_true",
                    help="also consolidate clusters whose members share one "
                         "source_date (default: cross-date only)")
    ap.add_argument("--execute", action="store_true",
                    help="apply the reviewed manifest's supersede flips (gated; "
                         "requires a prior dry-run whose manifest still matches "
                         "the live selection; default is dry-run)")
    ap.add_argument("--i-have-reviewed-the-manifest", action="store_true")
    args = ap.parse_args()

    if not args.facts.exists():
        print(f"No fact store at {args.facts}.", file=sys.stderr)
        return 1

    data = json.loads(args.facts.read_text())
    rows = data if isinstance(data, list) else data.get("facts", [])

    kinds = ({k.strip() for k in args.kinds.split(",") if k.strip()}
             if args.kinds else None)
    if kinds and kinds & NEVER_TOUCH:
        print(f"note: {sorted(kinds & NEVER_TOUCH)} excluded — never consolidated.",
              file=sys.stderr)
    sel = select(rows, kinds=kinds, similarity=args.similarity,
                 min_cluster=args.min_cluster,
                 include_same_date=args.include_same_date)

    per_kind: dict[str, int] = defaultdict(int)
    for c in sel["candidates"]:
        per_kind[c["kind"]] += 1
    print(f"store total: {len(rows)}")
    print(f"eligible (current, durable kinds): {sel['eligible_total']}")
    print(f"near-dup clusters: {len(sel['clusters'])}")
    print(f"supersede candidates: {len(sel['candidates'])}  {dict(per_kind)}")
    print(f"corrections touched: "
          f"{sum(1 for c in sel['candidates'] if c['kind'] == 'correction')} (must be 0)")
    print(f"post-consolidation current facts: change by -{len(sel['candidates'])}")

    manifest_path = args.manifest or args.facts.parent / MANIFEST_NAME
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "store": str(args.facts),
        "store_total": len(rows),
        "eligible_total": sel["eligible_total"],
        "params": {
            "kinds": sorted(kinds or DEFAULT_KINDS),
            "similarity": args.similarity,
            "min_cluster": args.min_cluster,
            "include_same_date": args.include_same_date,
        },
        "cluster_count": len(sel["clusters"]),
        "supersede_total": len(sel["candidates"]),
        "clusters": [
            {
                "kind": c["kind"],
                "canonical": {
                    "id": c["canonical"].get("id"),
                    "confidence": _conf(c["canonical"]),
                    "source_date": c["canonical"].get("source_date", ""),
                    "snippet": str(c["canonical"].get("content", ""))[:120],
                },
                "supersede": [
                    {
                        "id": f.get("id"),
                        "confidence": _conf(f),
                        "source_date": f.get("source_date", ""),
                        "similarity_to_canonical": c["similarities"].get(f.get("id")),
                        "snippet": str(f.get("content", ""))[:120],
                    }
                    for f in c["supersede"]
                ],
            }
            for c in sel["clusters"]
        ],
        "candidate_ids": [c["id"] for c in sel["candidates"]],
    }
    if not args.execute:
        secure_write_json(manifest_path, manifest, indent=2, default=str)
        print(f"\nmanifest written: {manifest_path}")
        print("\nDRY-RUN only — nothing mutated. Review the manifest, then re-run with")
        print("  --execute --i-have-reviewed-the-manifest")
        return 0

    # ---- gated execute ----
    # Never rewrites the manifest: execute applies exactly the loser->canonical
    # mapping the operator reviewed, or refuses. Anything else (store drift,
    # changed params, missing manifest) means the review no longer covers what
    # would be applied — back to the dry-run.
    if not args.i_have_reviewed_the_manifest:
        print("\nREFUSING --execute without --i-have-reviewed-the-manifest",
              file=sys.stderr)
        return 2
    try:
        reviewed = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"REFUSING --execute: no reviewable manifest at {manifest_path} "
              f"({exc}) — run the dry-run first.", file=sys.stderr)
        return 2
    reviewed_map = supersession_map(reviewed.get("clusters", []))
    live_map = supersession_map(sel["clusters"])
    if reviewed_map != live_map:
        print("REFUSING --execute: live selection no longer matches the "
              f"reviewed manifest ({len(reviewed_map)} reviewed vs "
              f"{len(live_map)} live supersessions). The store or parameters "
              "changed since the dry-run — re-run it and review the fresh "
              "manifest.", file=sys.stderr)
        return 2
    if any(c["kind"] in NEVER_TOUCH for c in sel["candidates"]):
        print("REFUSING --execute with never-touch kinds in candidates",
              file=sys.stderr)
        return 2
    if not live_map:
        print("nothing to consolidate — no near-duplicate candidates.")
        return 0
    backup = args.facts.with_suffix(
        f".json.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(args.facts, backup)
    print(f"backup written: {backup}")

    n = apply_supersessions(rows, sel["clusters"])
    secure_write_json(args.facts, data, indent=2, default=str)
    current = sum(1 for f in rows if f.get("status", "current") == "current")
    print(f"superseded {n} facts; store now {len(rows)} rows ({current} current).")
    print(POST_EXECUTE_RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
