#!/usr/bin/env python3
"""Synthesize recurring facts into higher-level insights — the consolidation
("dreams") layer.

The extract/recall pipeline accumulates raw facts but never steps back to notice
that five corrections are the same lesson. This worker reviews the fact store,
clusters recurring same-kind facts by shared terms, and writes consolidated
INSIGHTS to a separate layer that recall surfaces first. It prevents the store
from becoming "a giant unreadable log."

v1 is heuristic and dependency-free (no model, no network) to keep nock-brain a
clean stdlib-only install. The synthesis step is isolated behind
`synthesize_cluster()` so an LLM-backed synthesizer can drop in as an opt-in
upgrade without touching the clustering or I/O.

Usage:
    python3 synthesize.py                          # defaults: ~/.nock-brain/{facts,insights}.json
    python3 synthesize.py --facts ./facts.json --output ./insights.json
    python3 synthesize.py --threshold 0.3 --min-cluster 2
    python3 synthesize.py --kinds correction,bug   # only consolidate these kinds
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_mkdir, secure_write_json

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_OUTPUT = Path.home() / ".nock-brain" / "insights.json"
DEFAULT_THRESHOLD = 0.3
DEFAULT_MIN_CLUSTER = 2

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "this", "that", "we", "you", "i", "he", "she", "they", "our", "us", "claude",
    "code", "kevin", "mira", "not", "no", "so", "if", "then", "than", "into",
    "out", "up", "down", "over", "after", "before", "about",
}


def tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_kind(facts: list[dict], threshold: float) -> list[list[dict]]:
    """Greedy single-link clustering of same-kind facts by token-set overlap."""
    clusters: list[list[dict]] = []
    token_cache = {id(f): tokenize(f.get("content", "")) for f in facts}
    for f in facts:
        ft = token_cache[id(f)]
        placed = False
        for c in clusters:
            if any(jaccard(ft, token_cache[id(m)]) >= threshold for m in c):
                c.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])
    return clusters


def cluster_theme(cluster: list[dict], top: int = 5) -> str:
    counts: Counter[str] = Counter()
    for f in cluster:
        counts.update(tokenize(f.get("content", "")))
    # Terms shared by the most members read as the theme.
    return ", ".join(term for term, _ in counts.most_common(top))


def insight_id(kind: str, theme: str, source_ids: list[str]) -> str:
    seed = f"{kind}:{theme}:{','.join(sorted(source_ids))}"
    return "ins_" + hashlib.sha256(seed.encode()).hexdigest()[:10]


def synthesize_cluster(cluster: list[dict]) -> dict:
    """Turn a cluster of recurring same-kind facts into one consolidated insight.

    This is the seam for the synthesizer: the heuristic version summarizes by
    recurrence + shared theme + the most recent member. An LLM-backed version
    would implement the same signature, reading the cluster and returning a
    richer insight dict, without changing clustering or storage.
    """
    kind = cluster[0].get("kind", "fact")
    members = sorted(cluster, key=lambda f: f.get("source_date", ""))
    dates = [f.get("source_date", "") for f in members if f.get("source_date")]
    theme = cluster_theme(cluster)
    latest = members[-1].get("content", "")
    n = len(cluster)
    source_ids = [f.get("id", "") for f in cluster if f.get("id")]

    date_range = ""
    if dates:
        date_range = dates[0] if dates[0] == dates[-1] else f"{dates[0]}..{dates[-1]}"

    content = (
        f"Recurring {kind} (seen {n}x{', ' + date_range if date_range else ''}): "
        f"{theme}. Most recent: {latest[:160]}"
    )

    return {
        "id": insight_id(kind, theme, source_ids),
        "kind": "insight",
        "tier": "synthesized",
        "of_kind": kind,
        "recurrence": n,
        "theme": theme,
        "content": content,
        "status": "current",
        "confidence": round(min(0.95, 0.7 + 0.05 * n), 2),
        # source_date (latest member) keeps insights first-class for the recall
        # formatter/search, which key off source_date; source_dates keeps the span.
        "source_date": dates[-1] if dates else "",
        "source_ids": source_ids,
        "source_dates": dates,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def synthesize(
    facts: list[dict], threshold: float = DEFAULT_THRESHOLD,
    min_cluster: int = DEFAULT_MIN_CLUSTER, kinds: set[str] | None = None,
) -> list[dict]:
    """Consolidate current facts into insights. Only clusters with at least
    min_cluster members (a genuine recurrence) become insights."""
    active = [f for f in facts if f.get("status", "current") != "superseded"]
    if kinds:
        active = [f for f in active if f.get("kind") in kinds]

    by_kind: dict[str, list[dict]] = {}
    for f in active:
        by_kind.setdefault(f.get("kind", "fact"), []).append(f)

    insights = []
    for kind_facts in by_kind.values():
        for cluster in cluster_kind(kind_facts, threshold):
            if len(cluster) >= min_cluster:
                insights.append(synthesize_cluster(cluster))
    # Strongest recurrences first.
    insights.sort(key=lambda i: -i["recurrence"])
    return insights


def main():
    parser = argparse.ArgumentParser(description="Synthesize facts into insights")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    parser.add_argument("--kinds", type=str, default=None,
                        help="Comma-separated kinds to consolidate (default: all)")
    args = parser.parse_args()

    if not args.facts.exists():
        print(f"No fact store at {args.facts}. Run extract-facts.py first.", file=sys.stderr)
        sys.exit(1)

    facts = json.loads(args.facts.read_text())
    kinds = {k.strip() for k in args.kinds.split(",")} if args.kinds else None
    insights = synthesize(facts, args.threshold, args.min_cluster, kinds)

    secure_mkdir(args.output.parent)
    secure_write_json(args.output, insights, indent=2, default=str)

    print(f"Synthesized {len(insights)} insight(s) from {len(facts)} fact(s).")
    for ins in insights[:10]:
        print(f"  [{ins['recurrence']}x] {ins['of_kind']}: {ins['theme']}")


if __name__ == "__main__":
    main()
