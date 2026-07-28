#!/usr/bin/env python3
"""Collapse near-identical extractions of one real event into a single
canonical fact (E5a).

Repeated extraction of the same decision produces near-identical live facts
that all compete in recall ranking (the measured archetype: one surface-rule
decision extracted into 12 near-identical facts). This tool clusters
near-duplicates, nominates a canonical survivor, and — only on --apply —
marks the rest superseded with a superseded_by link back to the canonical.

Mark-only by design: the signed core (id + kind + content + evidence) is
never touched, so existing attestations stay valid. Duplicates keep their
own evidence and stay queryable via include_superseded; their bi-temporal
window is closed (invalid_at) so recall stops surfacing them as current.

Propose is the default and never mutates the store — it writes a review
queue (dedup-candidates.json + .md), mirroring propose-facts.py. Applying
is the deliberate, human-gated step.

Usage:
    python3 dedup-facts.py                          # propose to <store>/review
    python3 dedup-facts.py --queue-dir /path/review # custom queue location
    python3 dedup-facts.py --apply                  # mark duplicates superseded
    python3 dedup-facts.py --min-similarity 0.9 --kind decision
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import content_tokens, fact_currently_valid, jaccard
from _store import secure_mkdir, secure_write_json, secure_write_text
from _storeback import resolve_store

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_MIN_SIMILARITY = 0.85

# Shared token helpers live in _facts so dedup and contradiction pairing agree
# on what "same content" means.
normalize_tokens = content_tokens
similarity = jaccard


def _confidence(fact: dict) -> float:
    try:
        return float(fact.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def choose_canonical(members: "list[dict]") -> dict:
    """Deterministic survivor: curated first, then highest confidence, then the
    earliest source_date (the original event), then smallest id."""
    return sorted(
        members,
        key=lambda f: (
            not str(f.get("id", "")).startswith("curated-"),
            -_confidence(f),
            str(f.get("source_date") or "9999-12-31"),
            str(f.get("id", "")),
        ),
    )[0]


def _live(fact: Any) -> bool:
    return (
        isinstance(fact, dict)
        and fact.get("status") != "superseded"
        and fact_currently_valid(fact)
    )


def find_clusters(
    facts: "list[dict]",
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    kind: str = "",
) -> "list[dict]":
    """Cluster live facts whose normalized contents are near-identical.

    Single-link within a kind (facts of different kinds never cluster, even
    with identical text — a directive and a decision assert different things).
    Returns [{"kind", "canonical", "duplicates"}] sorted by canonical id."""
    by_kind: "dict[str, list[dict]]" = {}
    for fact in facts:
        if not _live(fact):
            continue
        fact_kind = str(fact.get("kind", ""))
        if kind and fact_kind != kind:
            continue
        by_kind.setdefault(fact_kind, []).append(fact)

    clusters: "list[dict]" = []
    for fact_kind in sorted(by_kind):
        members = sorted(by_kind[fact_kind], key=lambda f: str(f.get("id", "")))
        tokens = [normalize_tokens(f.get("content")) for f in members]
        parent = list(range(len(members)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(members)):
            if not tokens[i]:
                continue
            for j in range(i + 1, len(members)):
                if not tokens[j]:
                    continue
                union = tokens[i] | tokens[j]
                if len(tokens[i] & tokens[j]) / len(union) >= min_similarity:
                    parent[find(i)] = find(j)

        groups: "dict[int, list[dict]]" = {}
        for i, fact in enumerate(members):
            groups.setdefault(find(i), []).append(fact)
        for group in groups.values():
            if len(group) < 2:
                continue
            canonical = choose_canonical(group)
            duplicates = sorted(
                (f for f in group if f is not canonical),
                key=lambda f: str(f.get("id", "")),
            )
            clusters.append(
                {"kind": fact_kind, "canonical": canonical, "duplicates": duplicates}
            )

    return sorted(clusters, key=lambda c: str(c["canonical"].get("id", "")))


def apply_clusters(clusters: "list[dict]", stamp: "str | None" = None) -> int:
    """Mark every duplicate superseded_by its cluster's canonical, in place.

    Touches only lifecycle fields (status, superseded_*, invalid_at) — never
    the signed core — so attestations survive. Mirrors supersede-fact.py's
    window rule: overwrite a missing OR future-dated invalid_at so the close
    takes effect now; never push an already-past close later."""
    stamp = stamp or datetime.now(timezone.utc).isoformat()
    marked = 0
    for cluster in clusters:
        canonical_id = cluster["canonical"].get("id", "")
        for fact in cluster["duplicates"]:
            fact["status"] = "superseded"
            fact["superseded_at"] = stamp
            fact["superseded_by"] = canonical_id
            fact["supersession_reason"] = f"dedup: near-duplicate of {canonical_id}"
            if not fact.get("invalid_at") or fact["invalid_at"] > stamp:
                fact["invalid_at"] = stamp
            marked += 1
    return marked


def _snippet(fact: dict, limit: int = 160) -> str:
    return str(fact.get("content", ""))[:limit]


def write_queue(clusters: "list[dict]", queue_dir: Path, min_similarity: float) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    doc = {
        "generated_at": generated_at,
        "min_similarity": min_similarity,
        "cluster_count": len(clusters),
        "duplicate_count": sum(len(c["duplicates"]) for c in clusters),
        "clusters": [
            {
                "kind": c["kind"],
                "canonical_id": c["canonical"].get("id", ""),
                "canonical_content": _snippet(c["canonical"]),
                "duplicate_ids": [d.get("id", "") for d in c["duplicates"]],
                "duplicates": [
                    {
                        "id": d.get("id", ""),
                        "content": _snippet(d),
                        "source_date": d.get("source_date", ""),
                        "similarity_to_canonical": round(
                            similarity(c["canonical"].get("content"), d.get("content")), 3
                        ),
                    }
                    for d in c["duplicates"]
                ],
            }
            for c in clusters
        ],
    }
    secure_mkdir(queue_dir)
    secure_write_json(queue_dir / "dedup-candidates.json", doc, indent=2)

    lines = [
        "# Dedup candidates",
        "",
        f"Generated: {generated_at} · min similarity: {min_similarity}",
        f"{len(clusters)} cluster(s), {doc['duplicate_count']} duplicate(s) proposed for supersession.",
        "",
        "Review, then apply with: `python3 bin/dedup-facts.py --apply`",
        "",
    ]
    for c in doc["clusters"]:
        lines.append(f"## {c['canonical_id']} [{c['kind']}] — keep")
        lines.append(f"> {c['canonical_content']}")
        lines.append("")
        for d in c["duplicates"]:
            lines.append(
                f"- supersede `{d['id']}` [{d['source_date']}] (sim {d['similarity_to_canonical']})"
            )
            lines.append(f"  > {d['content']}")
        lines.append("")
    secure_write_text(queue_dir / "dedup-candidates.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--queue-dir", type=Path, default=None,
                        help="review-queue directory (default: <facts dir>/review)")
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY)
    parser.add_argument("--kind", default="", help="only cluster facts of this kind")
    parser.add_argument("--apply", action="store_true",
                        help="mark duplicates superseded (default: propose only)")
    args = parser.parse_args()

    # Jaccard lives in [0, 1]; a non-positive threshold would cluster every
    # same-kind fact together — catastrophic under --apply. Reject up front.
    if not 0.0 < args.min_similarity <= 1.0:
        parser.error("--min-similarity must be in (0, 1]")

    store = resolve_store(args.facts)
    if not store.freshness_path.exists():
        print(f"No fact store found ({store.describe()}).", file=sys.stderr)
        sys.exit(1)

    facts = store.load_facts()
    clusters = find_clusters(facts, min_similarity=args.min_similarity, kind=args.kind)

    if not clusters:
        print("No dedup candidates found.")
        return

    duplicate_count = sum(len(c["duplicates"]) for c in clusters)
    if args.apply:
        marked = apply_clusters(clusters)
        store.replace_all(facts)
        print(f"Marked {marked} duplicate fact(s) superseded across {len(clusters)} cluster(s).")
        for c in clusters:
            print(f"  keep {c['canonical'].get('id', '')} [{c['kind']}] "
                  f"← supersedes {len(c['duplicates'])} duplicate(s)")
        return

    queue_dir = args.queue_dir or args.facts.parent / "review"
    write_queue(clusters, queue_dir, args.min_similarity)
    print(f"Proposed {duplicate_count} duplicate(s) in {len(clusters)} cluster(s).")
    print(f"Queue: {queue_dir / 'dedup-candidates.json'}")
    print("Store untouched. Apply with: dedup-facts.py --apply")


if __name__ == "__main__":
    main()
