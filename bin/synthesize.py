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
upgrade without touching the clustering or I/O. `--llm` turns on the opt-in
Haiku-distill: it enriches only the insight prose; identity/provenance fields
stay heuristic and deterministic.

Usage:
    python3 synthesize.py                          # defaults: ~/.nock-brain/{facts,insights}.json
    python3 synthesize.py --facts ./facts.json --output ./insights.json
    python3 synthesize.py --threshold 0.3 --min-cluster 2
    python3 synthesize.py --kinds correction,bug   # only consolidate these kinds
    python3 synthesize.py --llm                     # opt-in Haiku-distill (subscription path)
"""
import argparse
import hashlib
import json
import re
import subprocess  # nosec B404 - only invokes the trusted local `claude` CLI, no shell
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
# Opt-in LLM (Haiku-distill) synthesizer. "haiku" is the CLI alias for the cheap
# Haiku tier; `claude -p` runs on the Claude Code subscription (NOT the metered
# API), so the LLM-distill carries no per-call spend.
DEFAULT_LLM_MODEL = "haiku"
DEFAULT_LLM_TIMEOUT = 60.0  # seconds per cluster before falling back to heuristic
# Bound LLM spend: enrich only the top-N strongest recurrences with Haiku; the
# long tail stays heuristic. Keeps the full insight set complete while capping
# calls (the store has ~270 clusters — re-distilling all nightly is wasteful).
DEFAULT_LLM_TOP = 40

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


def _call_claude(prompt: str, model: str, timeout: float) -> str:
    """Run one headless Claude prompt via the local ``claude -p`` CLI; return its
    stripped stdout, or ``""`` on any failure.

    Uses the Claude Code subscription path (``claude -p``), NOT the metered
    Anthropic API — so the LLM-distill carries no per-call spend. The prompt is
    passed as a fixed argv element (no shell), so cluster text cannot inject a
    command.
    """
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell, trusted local CLI
            ["claude", "-p", "--model", model, prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def make_claude_synthesizer(model: str = DEFAULT_LLM_MODEL,
                            timeout: float = DEFAULT_LLM_TIMEOUT):
    """Build an opt-in LLM synthesizer for :func:`synthesize_cluster`.

    The returned callable ``(cluster, heuristic_content) -> str`` reads a cluster
    of recurring facts and returns ONE consolidated lesson sentence — enriching
    only the insight's prose. It returns ``""`` on any failure (empty, too-short,
    or errored call) so ``synthesize_cluster`` owns the single fallback path.
    Identity and provenance fields are never touched by the LLM.
    """
    def _synth(cluster: list[dict], heuristic_content: str) -> str:
        members = "\n".join(f"- {f.get('content', '')[:300]}" for f in cluster[:25])
        prompt = (
            "These notes are the same recurring lesson from past work sessions. "
            "Write ONE clear, specific sentence (max 45 words) stating the durable, "
            "reusable lesson — what to do or avoid next time. Output only the "
            "sentence, no preamble or quotes.\n\n" + members
        )
        cleaned = " ".join(_call_claude(prompt, model, timeout).split())
        return cleaned if len(cleaned) >= 12 else ""
    return _synth


def synthesize_cluster(cluster: list[dict], synthesizer=None) -> dict:
    """Turn a cluster of recurring same-kind facts into one consolidated insight.

    The heuristic version (default, ``synthesizer=None``) summarizes by recurrence
    + shared theme + the most recent member. Passing an opt-in ``synthesizer``
    callable ``(cluster, heuristic_content) -> str`` enriches ONLY the
    human-readable ``content``; every identity/provenance field stays heuristic
    and deterministic, so an LLM can never change an insight's identity or corrupt
    the store. Any synthesizer failure (exception or empty result) falls back to
    the heuristic content.
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

    heuristic_content = (
        f"Recurring {kind} (seen {n}x{', ' + date_range if date_range else ''}): "
        f"{theme}. Most recent: {latest[:160]}"
    )

    content = heuristic_content
    synthesized_by = "heuristic"
    if synthesizer is not None:
        try:
            enriched = synthesizer(cluster, heuristic_content)
        except Exception:  # nosec B110 - synthesis must never break the pipeline
            enriched = None
        if isinstance(enriched, str) and enriched.strip():
            content = enriched.strip()
            synthesized_by = "llm"

    return {
        "id": insight_id(kind, theme, source_ids),
        "kind": "insight",
        "tier": "synthesized",
        "of_kind": kind,
        "recurrence": n,
        "theme": theme,
        "content": content,
        # which path wrote `content` — heuristic (deterministic) or llm (enriched).
        "synthesized_by": synthesized_by,
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
    synthesizer=None, llm_top: int | None = None,
) -> list[dict]:
    """Consolidate current facts into insights. Only clusters with at least
    min_cluster members (a genuine recurrence) become insights. An optional
    ``synthesizer`` callable enriches each insight's prose (see
    :func:`synthesize_cluster`); ``None`` (default) uses the heuristic. When a
    synthesizer is given, ``llm_top`` bounds enrichment to the N strongest
    recurrences (the highest-value lessons); the long tail stays heuristic.
    ``llm_top=None`` enriches every cluster."""
    active = [f for f in facts if f.get("status", "current") != "superseded"]
    if kinds:
        active = [f for f in active if f.get("kind") in kinds]

    by_kind: dict[str, list[dict]] = {}
    for f in active:
        by_kind.setdefault(f.get("kind", "fact"), []).append(f)

    # Collect every qualifying cluster, then rank strongest-first so LLM
    # enrichment targets the top recurrences within a bounded call budget.
    clusters = [
        cluster
        for kind_facts in by_kind.values()
        for cluster in cluster_kind(kind_facts, threshold)
        if len(cluster) >= min_cluster
    ]
    clusters.sort(key=len, reverse=True)

    insights = []
    for rank, cluster in enumerate(clusters):
        use = synthesizer if (
            synthesizer is not None and (llm_top is None or rank < llm_top)
        ) else None
        insights.append(synthesize_cluster(cluster, use))
    return insights


def main():
    parser = argparse.ArgumentParser(description="Synthesize facts into insights")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    parser.add_argument("--kinds", type=str, default=None,
                        help="Comma-separated kinds to consolidate (default: all)")
    parser.add_argument("--llm", action="store_true",
                        help="Enrich insight prose with a cheap LLM (Haiku via "
                             "`claude -p`, subscription path — no metered spend). "
                             "Heuristic stays the default; identity/provenance "
                             "fields are never LLM-touched.")
    parser.add_argument("--model", type=str, default=DEFAULT_LLM_MODEL,
                        help=f"Model for --llm (default: {DEFAULT_LLM_MODEL})")
    parser.add_argument("--llm-timeout", type=float, default=DEFAULT_LLM_TIMEOUT,
                        help="Per-cluster LLM timeout in seconds "
                             f"(default: {DEFAULT_LLM_TIMEOUT})")
    parser.add_argument("--llm-top", type=int, default=DEFAULT_LLM_TOP,
                        help="With --llm, enrich only the N strongest recurrences; "
                             f"the rest stay heuristic (default: {DEFAULT_LLM_TOP}, "
                             "0 = no cap)")
    args = parser.parse_args()

    if not args.facts.exists():
        print(f"No fact store at {args.facts}. Run extract-facts.py first.", file=sys.stderr)
        sys.exit(1)

    facts = json.loads(args.facts.read_text())
    kinds = {k.strip() for k in args.kinds.split(",")} if args.kinds else None
    synthesizer = (make_claude_synthesizer(args.model, args.llm_timeout)
                   if args.llm else None)
    llm_top = args.llm_top if args.llm_top and args.llm_top > 0 else None
    insights = synthesize(facts, args.threshold, args.min_cluster, kinds,
                          synthesizer, llm_top)

    secure_mkdir(args.output.parent)
    secure_write_json(args.output, insights, indent=2, default=str)

    mode = f"LLM ({args.model})" if args.llm else "heuristic"
    llm_n = sum(1 for i in insights if i.get("synthesized_by") == "llm")
    print(f"Synthesized {len(insights)} insight(s) from {len(facts)} fact(s) "
          f"[{mode}; {llm_n} LLM-enriched].")
    for ins in insights[:10]:
        print(f"  [{ins['recurrence']}x] {ins['of_kind']}: {ins['theme']}")


if __name__ == "__main__":
    main()
