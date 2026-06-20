#!/usr/bin/env python3
"""Budget-aware memory recall: retrieve facts within a token budget.

Returns a curated summary of relevant past-session facts that fits
within a configurable token cap.

Usage:
    python3 budget-recall.py "what did we decide about content strategy"
    python3 budget-recall.py --budget 800 "status of the audit"
    python3 budget-recall.py --budget 1500 --include-superseded "pricing history"
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import RECALL_ITEM_FIELDS, load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_INSIGHTS = Path.home() / ".nock-brain" / "insights.json"
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = 1000
MAX_BUDGET = 1500
MIN_CONFIDENCE = 0.7

# BM25 parameters (Okapi defaults). k1 controls term-frequency saturation; b
# controls how strongly document length is normalized.
BM25_K1 = 1.5
BM25_B = 0.75

# --- Recency decay (N8069) -------------------------------------------------
# A fact's score is multiplied by an exponential half-life decay on its
# source_date so a stale fact no longer outranks a current one purely on term
# match. Half-lives are PER-KIND: a "status" or "dispatch" line goes stale in
# days, while a "decision" or "directive" stays load-bearing for months. Tune
# these by editing the dict — they are the only knob for the decay curve.
#
# half-life H means score is halved every H days of age:
#     recency_factor = 0.5 ** (age_days / H)
# A very large H (DURABLE_HALF_LIFE) is effectively "never decays".
RECENCY_HALF_LIFE_DAYS: dict[str, float] = {
    # Fast-decaying / point-in-time kinds — yesterday's status is noise today.
    "status": 14.0,
    "dispatch": 14.0,
    "feed": 14.0,
    "merge": 30.0,
    "bug": 45.0,
    # Durable kinds — decisions/directives/corrections/identity stay relevant.
    "decision": 180.0,
    "directive": 180.0,
    "correction": 180.0,
    "architecture": 180.0,
    "insight": 180.0,
    "identity": 100000.0,  # ~never decays
}
# Used for any kind not in the table above (and as a safe middle ground).
DEFAULT_HALF_LIFE_DAYS = 60.0
# Floor so a very old fact never decays fully to zero (it would be unrankable
# even when it is the only term match). Keeps recency a tie-breaker, not a wall.
MIN_RECENCY_FACTOR = 0.01


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _tokenize(text: str) -> list[str]:
    # Coerce None/empty to "" so a fact whose content is explicitly null never
    # crashes the recall path (.lower() on None) — the live injection path runs
    # this over every candidate fact.
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _resolve_now(now: datetime | None = None) -> datetime:
    """Resolve the reference 'now' for recency decay. Injectable for
    deterministic tests: explicit arg > NOCK_BRAIN_NOW env (ISO date/datetime)
    > wall clock. Never a bare datetime.now() buried in the scoring path."""
    if now is not None:
        return now
    env = os.environ.get("NOCK_BRAIN_NOW")
    if env:
        parsed = _parse_date(env)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _parse_date(value) -> datetime | None:
    """Parse a source_date into a datetime, or None if absent/unparseable.
    Accepts 'YYYY-MM-DD', full ISO timestamps, and date objects. Returns None
    for the sentinel 'unknown' or anything we cannot read — callers treat None
    as 'no recency signal' (neutral factor), never as a crash."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text or text.lower() == "unknown":
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None


def recency_factor(fact: dict, now: datetime) -> float:
    """Exponential half-life decay on source_date, with a per-kind half-life.
    Returns a neutral 1.0 for facts with no parseable source_date (backward
    compatible — pre-N8069 facts and 'unknown' dates are never penalized)."""
    parsed = _parse_date(fact.get("source_date"))
    if parsed is None:
        return 1.0
    # Compare in a tz-consistent way: if one side is naive, drop tz from both.
    ref = now
    if parsed.tzinfo is None and ref.tzinfo is not None:
        ref = ref.replace(tzinfo=None)
    elif parsed.tzinfo is not None and ref.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)
    age_days = (ref - parsed).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0  # future-dated or same-day facts are fully fresh
    half_life = RECENCY_HALF_LIFE_DAYS.get(
        str(fact.get("kind", "")).lower(), DEFAULT_HALF_LIFE_DAYS
    )
    if half_life <= 0:
        return 1.0
    return max(MIN_RECENCY_FACTOR, 0.5 ** (age_days / half_life))


def supersession_factor(fact: dict) -> float:
    """Soft penalty for facts that are deprecated-but-not-hard-filtered.

    In the current schema, supersession is expressed ONLY via
    `status == "superseded"`, which `search()` removes outright (a hard
    filter) before scoring — so there is no soft-deprecated tier to penalize
    and this returns 1.0 (a documented no-op hook). If a future fact ever
    carries a soft signal (`deprecated: true`, or a `supersedes`/`superseded_by`
    pointer while still status=current), it is down-weighted but kept rankable.
    We deliberately do NOT invent fields the store does not have."""
    if fact.get("deprecated") is True:
        return 0.4
    # A still-current fact that nonetheless announces it is being superseded by
    # something newer: keep it, but let the newer fact win ties.
    if fact.get("status", "current") == "current" and fact.get("superseded_by"):
        return 0.6
    return 1.0


def search(facts: list[dict], query: str, include_superseded: bool = False,
           now: datetime | None = None) -> list[dict]:
    """Rank facts against the query with Okapi BM25 — proper token matching with
    IDF (rarer query terms count for more) and document-length normalization.
    This replaces a naive substring-overlap count, which both over-matched
    (e.g. "cat" inside "category") and treated every term as equally important.

    The BM25 relevance is then multiplied by confidence, a per-kind recency
    decay (N8069: stale status facts no longer beat current ones), and a soft
    supersession penalty. `now` is injectable for deterministic tests."""
    if not include_superseded:
        facts = [f for f in facts if f.get("status", "current") != "superseded"]
    facts = [f for f in facts if f.get("confidence", 0) >= MIN_CONFIDENCE]
    if not facts:
        return []

    query_terms = set(_tokenize(query))
    if not query_terms:
        return []

    ref_now = _resolve_now(now)

    # Corpus statistics for BM25, computed over the candidate set.
    docs = [_tokenize(f.get("content", "")) for f in facts]
    n_docs = len(docs)
    avgdl = sum(len(d) for d in docs) / n_docs if n_docs else 0.0
    doc_freq: Counter = Counter()
    for d in docs:
        for term in set(d):
            doc_freq[term] += 1

    results = []
    for f, doc in zip(facts, docs):
        tf = Counter(doc)
        dl = len(doc)
        bm25 = 0.0
        for term in query_terms:
            df = doc_freq.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf[term]
            denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * (dl / avgdl if avgdl else 0))
            if denom > 0:
                bm25 += idf * (freq * (BM25_K1 + 1)) / denom
        if bm25 <= 0:
            continue
        score = (
            bm25
            * f.get("confidence", 0)
            * recency_factor(f, ref_now)
            * supersession_factor(f)
        )
        if score > 0:
            results.append((score, f))

    results.sort(key=lambda x: (-x[0], -x[1].get("confidence", 0)))
    return [f for _, f in results]


def format_fact(f: dict) -> str:
    parts = [f"[{f.get('source_date', 'unknown')}]", f"[{f.get('kind', 'fact').upper()}]"]
    header = " ".join(parts)
    content = str(f.get("content", ""))[:200]
    if f.get("status") == "superseded":
        content = f"[SUPERSEDED] {content}"
    return f"{header}\n{content}"


def _load(path: Path) -> list[dict]:
    return load_facts(path, required_fields=RECALL_ITEM_FIELDS)


_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    """A flag env var is truthy iff its (case-insensitive, stripped) value is in
    {1,true,yes,on}; absent/anything-else is off. Mirrors the gate the design
    requires for NOCKBRAIN_GRAPH_RECALL."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _maybe_graph_expand(all_facts: list[dict], seeds: list[dict], query: str,
                        include_superseded: bool, now: datetime,
                        graph_expand: bool) -> list[dict]:
    """Gate for graph-augmented recall. When `graph_expand` is False this is a
    PURE pass-through: it returns the exact same `seeds` list object, before any
    graph import/build/allocation runs — so the off-path is byte-identical to
    the flat path. When True, it delegates to _graph_recall.expand(), which
    appends graph neighbors (weighted strictly below the weakest seed) using the
    SAME recency/supersession/confidence gates as search()."""
    if not graph_expand:
        return seeds  # additive guarantee: identical object, zero graph work
    if not seeds:
        return seeds
    import _graph_recall  # local import: never loaded on the off-path
    return _graph_recall.expand(
        all_facts, seeds, include_superseded, now,
        recency_factor=recency_factor,
        supersession_factor=supersession_factor,
        min_confidence=MIN_CONFIDENCE,
    )


def budget_recall(query: str, facts_file: Path, budget: int = DEFAULT_BUDGET,
                  include_superseded: bool = False, insights_file: Path | None = None,
                  now: datetime | None = None, graph_expand: bool = False) -> str:
    ref_now = _resolve_now(now)
    if facts_file:
        all_facts = _load(facts_file)
        fact_results = search(all_facts, query, include_superseded, now=ref_now)
        fact_results = _maybe_graph_expand(
            all_facts, fact_results, query, include_superseded, ref_now, graph_expand
        )
    else:
        fact_results = []
    insight_results = search(_load(insights_file), query, include_superseded, now=ref_now) if insights_file else []

    # Consolidated insights lead; drop the raw facts an insight already covers so
    # recall shows the synthesis, not the synthesis plus its own sources.
    covered = {sid for ins in insight_results for sid in ins.get("source_ids", [])}
    fact_results = [f for f in fact_results if f.get("id") not in covered]

    results = insight_results + fact_results
    if not results:
        return ""

    output_lines = [f"Memory recall ({len(results)} matches, budget {budget} tokens):"]
    tokens_used = estimate_tokens(output_lines[0])
    included = 0

    for f in results:
        formatted = format_fact(f)
        fact_tokens = estimate_tokens(formatted)
        if tokens_used + fact_tokens > budget:
            remaining = len(results) - included
            if remaining > 0:
                output_lines.append(f"[...{remaining} more results truncated by budget]")
            break
        output_lines.append(formatted)
        tokens_used += fact_tokens
        included += 1

    output_lines.append(f"[{included} item(s), ~{tokens_used} tokens]")
    return "\n\n".join(output_lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS,
                        help="Synthesized-insight store (surfaced first); optional")
    parser.add_argument("--include-superseded", action="store_true")
    parser.add_argument("--graph", action="store_true",
                        help="Enable graph-augmented recall (default off; also "
                             "via NOCKBRAIN_GRAPH_RECALL=1)")
    args = parser.parse_args()

    budget = min(args.budget, MAX_BUDGET)
    query_str = " ".join(args.query)
    graph_expand = args.graph or _env_truthy("NOCKBRAIN_GRAPH_RECALL")
    result = budget_recall(query_str, args.facts, budget, args.include_superseded,
                           insights_file=args.insights, graph_expand=graph_expand)

    if result:
        print(result)
    else:
        print("No matching facts found.")


if __name__ == "__main__":
    main()
