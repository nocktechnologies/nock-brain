#!/usr/bin/env python3
"""Budget-aware memory recall: retrieve facts within a token budget.

Returns a curated summary of relevant past-session facts that fits
within a configurable token cap.

Usage:
    python3 budget-recall.py "what did we decide about content strategy"
    python3 budget-recall.py --budget 800 "status of the audit"
    python3 budget-recall.py --budget 1500 --include-superseded "pricing history"
    python3 budget-recall.py --strict-verify "..."   # only signed+valid facts
"""
# Deferred annotations keep this script runnable on Python 3.9 (stock macOS
# /usr/bin/python3): the recall hook resolves plain `python3` from PATH, and
# PEP 604 unions in def signatures are a def-time TypeError before 3.10.
from __future__ import annotations

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

from _facts import RECALL_ITEM_FIELDS, fact_currently_valid, fact_source, load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_INSIGHTS = Path.home() / ".nock-brain" / "insights.json"
CHARS_PER_TOKEN = 4
DEFAULT_BUDGET = 1000
MAX_BUDGET = 1500
MIN_CONFIDENCE = 0.7

QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "did", "do", "does", "for", "from", "had", "has", "have",
    "how", "i", "in", "is", "it", "me", "of", "on", "or", "our",
    "please", "remind", "show", "tell", "that", "the", "their", "this",
    "to", "us", "was", "we", "were", "what", "when", "where", "which",
    "who", "why", "with", "you", "your",
}

# --- Per-batch diversity cap (N8142) ---------------------------------------
# A post-scoring SELECTION constraint (scoring itself is untouched): cap how
# many facts that share the SAME source_date may sit in the front of a recall
# result. A single bulk import (e.g. the 1650-fact 2026-05-19 backfill, 66% of
# the store) otherwise term-matches almost any generic/recency query and crowds
# the top-K — recency decay alone does not fix it because durable-kind facts
# barely decay and sheer volume wins. This caps any one import's footprint
# regardless of kind. Env-configurable via NOCKBRAIN_MAX_PER_DATE; 0 (or any
# value <= 0) disables the cap entirely (legacy/unbounded behavior).
DEFAULT_MAX_PER_DATE = 4

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
    # Synthesized insights inherit a FROZEN source_date (the day they were first
    # distilled — often the historical dump), so a 180-day half-life lets stale
    # insights bury recent raw facts in recall. Decay them faster so recent work
    # surfaces (N8392: the May-19 dump was 85% of insights and dominated recall).
    "insight": 45.0,
    "identity": 100000.0,  # ~never decays
}
# Used for any kind not in the table above (and as a safe middle ground).
DEFAULT_HALF_LIFE_DAYS = 60.0
# Floor so a very old fact never decays fully to zero (it would be unrankable
# even when it is the only term match). Keeps recency a tie-breaker, not a wall.
MIN_RECENCY_FACTOR = 0.01

# --- Bulk-date down-weight (A3) ---------------------------------------------
# A single bulk import (the 2026-05-19 backfill: ~65% of the store) term-matches
# almost any query. Per-kind recency decay does not fix it — durable kinds barely
# decay — and the post-scoring per-date diversity cap only REORDERS the front of
# the result; the bulk date still floods the candidate pool and stays ~45% of
# recall-eligible facts. This applies a mild, SCORE-level penalty to facts whose
# source_date is OVER-REPRESENTED in the *candidate corpus* of a query, so no
# single date can monopolize ranking. Conservative by construction: it triggers
# ONLY above a share threshold and is floored, so a normal date (a few % of the
# corpus) is untouched and a legitimately-relevant old fact is down-weighted,
# never crushed. Env overrides: NOCKBRAIN_BULK_DATE_THRESHOLD (share above which
# a date is penalized; <=0 or >=1 disables) and NOCKBRAIN_BULK_DATE_MIN_FACTOR.
BULK_DATE_SHARE_THRESHOLD = 0.25
BULK_DATE_MIN_FACTOR = 0.5


def _resolve_bulk_date_params() -> tuple[float, float]:
    """Resolve (threshold, floor) for the bulk-date penalty from env, falling
    back to the module defaults. A non-float env value is ignored rather than
    crashing the live recall path."""
    threshold = BULK_DATE_SHARE_THRESHOLD
    floor = BULK_DATE_MIN_FACTOR
    raw = os.environ.get("NOCKBRAIN_BULK_DATE_THRESHOLD", "").strip()
    if raw:
        try:
            threshold = float(raw)
        except ValueError:
            pass
    raw = os.environ.get("NOCKBRAIN_BULK_DATE_MIN_FACTOR", "").strip()
    if raw:
        try:
            floor = float(raw)
        except ValueError:
            pass
    return threshold, floor


def bulk_date_factor(share: float, threshold: float, floor: float) -> float:
    """Mild multiplicative penalty for a fact from an over-represented date.

    `share` is the date's fraction of the candidate corpus. At or below
    `threshold` the factor is 1.0 (untouched). Above it the factor falls
    linearly with the excess share and is clamped at `floor`, so even a date
    that is the overwhelming majority of candidates is down-weighted, not
    erased. A threshold outside (0, 1) disables the penalty entirely."""
    if threshold <= 0 or threshold >= 1 or share <= threshold:
        return 1.0
    excess = share - threshold
    return max(floor, 1.0 - excess)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _normalize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    # Coerce None/empty to "" so a fact whose content is explicitly null never
    # crashes the recall path (.lower() on None) — the live injection path runs
    # this over every candidate fact.
    return [
        _normalize_token(term)
        for term in re.findall(r"[a-z0-9]+", (text or "").lower())
    ]


def _ordered_query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in _tokenize(query):
        if term in QUERY_STOPWORDS or term in terms:
            continue
        terms.append(term)
    return terms


def _query_terms(query: str) -> set[str]:
    """Return content-bearing query terms for ranking.

    Prompt-time recall receives natural-language questions. Ranking on every
    token lets high-frequency scaffolding words ("what", "is", "the", "who")
    match generic session notes and bury the real subject. Drop those words and
    rank on the remaining signal terms. If a query has no signal terms, return
    an empty set rather than recalling keyword noise.
    """
    return set(_ordered_query_terms(query))


def _query_pairs(ordered_terms: list[str]) -> list[tuple[str, str]]:
    return list(zip(ordered_terms, ordered_terms[1:]))


def _default_recall_min_matches(query_terms: set[str]) -> int:
    return 2 if len(query_terms) >= 3 else 1


def _pair_window_count(
    doc: list[str],
    pairs: list[tuple[str, str]],
    max_gap: int = 40,
) -> int:
    if not pairs:
        return 0
    positions: dict[str, list[int]] = {}
    for idx, term in enumerate(doc):
        positions.setdefault(term, []).append(idx)

    count = 0
    for left, right in pairs:
        left_positions = positions.get(left, [])
        right_positions = positions.get(right, [])
        if any(
            abs(rpos - lpos) <= max_gap
            for lpos in left_positions
            for rpos in right_positions
        ):
            count += 1
    return count


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (_normalize_token(match.group(0).lower()), match.start(), match.end())
        for match in re.finditer(r"[a-z0-9]+", text or "", re.IGNORECASE)
    ]


def _relevant_excerpt(content: str, query_terms: set[str] | None, max_chars: int = 220) -> str:
    content = str(content or "")
    if len(content) <= max_chars or not query_terms:
        return content[:max_chars]

    spans = _token_spans(content)
    hits = [(term, start, end) for term, start, end in spans if term in query_terms]
    if not hits:
        return content[:max_chars]

    best: tuple[int, int, int] | None = None
    for _, start, _ in hits:
        window_start = max(0, start - 30)
        window_end = min(len(content), window_start + max_chars)
        terms_in_window = {
            term for term, span_start, _ in spans
            if window_start <= span_start < window_end and term in query_terms
        }
        score = len(terms_in_window)
        candidate = (score, -window_start, window_start)
        if best is None or candidate > best:
            best = candidate

    start = best[2] if best else hits[0][1]
    end = min(len(content), start + max_chars)
    excerpt = content[start:end].strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(content):
        excerpt += "..."
    return excerpt


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
           now: datetime | None = None,
           sources: "set[str] | list[str] | None" = None,
           min_matched_terms: int | None = None) -> list[dict]:
    """Rank facts against the query with Okapi BM25 — proper token matching with
    IDF (rarer query terms count for more) and document-length normalization.
    This replaces a naive substring-overlap count, which both over-matched
    (e.g. "cat" inside "category") and treated every term as equally important.

    The BM25 relevance is then multiplied by confidence, a per-kind recency
    decay (N8069: stale status facts no longer beat current ones), and a soft
    supersession penalty. `now` is injectable for deterministic tests.

    `sources` scopes recall to facts owned by those agents (gbrain-style fleet
    scoping). `None` (the default) means no scoping — exact prior behavior, so
    every existing caller is unaffected. A fact's owner is `fact_source(f)`
    (missing source defaults to DEFAULT_SOURCE)."""
    if not include_superseded:
        facts = [f for f in facts if f.get("status", "current") != "superseded"]
        # Bi-temporal gate: a fact outside its validity window (invalid_at passed,
        # or valid_at not yet reached) is not CURRENT, so it drops from default
        # recall — but stays in the store and returns with include_superseded.
        # Facts with no window bounds are always valid (backward compatible).
        _now = _resolve_now(now)
        facts = [f for f in facts if fact_currently_valid(f, _now)]
    if sources is not None:
        # A bare string is a common caller slip — set("mira") would shatter into
        # {'m','i','r','a'} and silently match nothing. Wrap it.
        allowed = {sources} if isinstance(sources, str) else set(sources)
        facts = [f for f in facts if fact_source(f) in allowed]
    facts = [f for f in facts if f.get("confidence", 0) >= MIN_CONFIDENCE]
    if not facts:
        return []

    ordered_terms = _ordered_query_terms(query)
    query_terms = set(ordered_terms)
    if not query_terms:
        return []
    if min_matched_terms is None:
        min_matched_terms = 1
    query_pairs = _query_pairs(ordered_terms)

    ref_now = _resolve_now(now)

    # Corpus statistics for BM25, computed over the candidate set.
    docs = [_tokenize(f.get("content", "")) for f in facts]
    n_docs = len(docs)
    avgdl = sum(len(d) for d in docs) / n_docs if n_docs else 0.0
    doc_freq: Counter = Counter()
    for d in docs:
        for term in set(d):
            doc_freq[term] += 1

    # Bulk-date down-weight (A3): how much of the candidate corpus each
    # source_date occupies, so an over-represented date (e.g. the May-19 bulk
    # import) gets a mild score penalty and cannot monopolize ranking.
    bulk_threshold, bulk_floor = _resolve_bulk_date_params()
    date_counts: Counter = Counter(str(f.get("source_date", "unknown")) for f in facts)
    date_share = {d: c / n_docs for d, c in date_counts.items()} if n_docs else {}

    results = []
    for f, doc in zip(facts, docs):
        tf = Counter(doc)
        dl = len(doc)
        bm25 = 0.0
        matched_terms = 0
        for term in query_terms:
            df = doc_freq.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            freq = tf[term]
            if freq > 0:
                matched_terms += 1
            denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * (dl / avgdl if avgdl else 0))
            if denom > 0:
                bm25 += idf * (freq * (BM25_K1 + 1)) / denom
        if bm25 <= 0:
            continue
        if matched_terms < min_matched_terms:
            continue
        pair_matches = _pair_window_count(doc, query_pairs)
        coverage_boost = 1.0 + (max(0, matched_terms - 1) * 3.0)
        phrase_boost = 1.0 + (pair_matches * 4.0)
        share = date_share.get(str(f.get("source_date", "unknown")), 0.0)
        score = (
            bm25
            * f.get("confidence", 0)
            * recency_factor(f, ref_now)
            * supersession_factor(f)
            * coverage_boost
            * phrase_boost
            * bulk_date_factor(share, bulk_threshold, bulk_floor)
        )
        if score > 0:
            results.append((score, pair_matches, matched_terms, f))

    results.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3].get("confidence", 0)))
    return [f for _, _, _, f in results]


def _resolve_max_per_date(explicit: "int | None" = None) -> int:
    """Resolve the per-source_date diversity cap: explicit arg >
    NOCKBRAIN_MAX_PER_DATE env > DEFAULT_MAX_PER_DATE. A non-integer env value
    is ignored (falls through to the default) rather than crashing the live
    recall path. A value <= 0 means 'unlimited' (cap disabled)."""
    if explicit is not None:
        return explicit
    raw = os.environ.get("NOCKBRAIN_MAX_PER_DATE", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_PER_DATE


def _apply_date_diversity_cap(results: list[dict], max_per_date: int,
                              exempt: "frozenset | set | None" = None) -> list[dict]:
    """Post-scoring selection constraint — NOT a re-score and NOT a filter.

    Cap how many facts sharing the SAME source_date may appear in the front of
    the (already score-sorted) result list. Walk the list in score order, keep a
    fact while its source_date is still under the cap, and DEFER any further
    same-date facts to the tail (still in their relative score order). No fact is
    dropped here — the downstream token-budget truncation then naturally sheds
    the deferred tail first, so a single bulk import can no longer dominate the
    top-K of a generic/recency query while a genuinely top-scored same-date fact
    still survives.

    `exempt` fact ids (the semantic tier's reserved dense slots) bypass the cap
    entirely: the Phase 0 spike measured the cap demoting the semantically best
    fact because it was the 9th from a bulk-import date. Exempt facts neither
    count toward a date's quota nor get deferred. Default None/empty is the
    exact pre-semantic behavior.

    max_per_date <= 0 disables the cap (returns the list unchanged)."""
    if max_per_date <= 0 or len(results) <= max_per_date:
        return results
    exempt_ids = exempt or frozenset()
    seen: Counter = Counter()
    kept: list[dict] = []
    deferred: list[dict] = []
    for f in results:
        if f.get("id") in exempt_ids:
            kept.append(f)
            continue
        key = str(f.get("source_date", "unknown"))
        if seen[key] < max_per_date:
            seen[key] += 1
            kept.append(f)
        else:
            deferred.append(f)
    return kept + deferred


# --- Attestation verification on the recall hot path (OWASP F5 / N8068) -----
# The signed fact envelope (_sign.py) made tampering with facts.json
# *detectable*, but detection only ran in the offline verify-facts.py CLI — the
# recall path that actually injects facts into live sessions never checked it,
# so a poisoned store would be injected undetected. Close that loop here: when
# signing has been set up (a key exists), every loaded fact is verified and
# TAMPERED facts are dropped before ranking, with a one-line stderr count (the
# injection hook discards stderr, so the warning can never leak into a
# session). Unsigned facts stay recallable by default — much of the store
# predates signing — but are counted in the warning; --strict-verify fails
# closed and keeps only VALID facts. No key on disk means verification is
# skipped entirely (pre-signing behavior, and _sign/cryptography are never
# imported, keeping the no-key hot path cost-free).
#
# Cost control: at ~160us per Ed25519 signature, full verification of a
# 2,500-fact store adds ~0.4-0.8s to every recall — most of the hook's <2s
# budget. A sidecar cache (_verify_cache) remembers already-proven signatures
# per store, guarded by the store file's (mtime_ns, size), so a recall over an
# unchanged store skips the signature operations but STILL recomputes and
# compares every fact's committed content hashes — tampering is detected even
# on a warm cache, and any cache doubt falls back to full verification.
DEFAULT_SIGNING_KEY = Path.home() / ".nock-brain" / "signing-key"
DEFAULT_SIGNING_PUB = Path.home() / ".nock-brain" / "signing-key.pub"


def _resolve_verify_key():
    """Load the attestation-verification key, or None when signing was never
    set up (None disables verification). Paths are overridable via
    NOCKBRAIN_SIGNING_PUB / NOCKBRAIN_SIGNING_KEY; the public key is preferred,
    the private key (which can also verify) is the fallback.

    A key that exists but cannot be loaded fails OPEN with a stderr note:
    attestations are tamper-*evidence*, not a gate — an attacker who can
    corrupt the key file could as easily delete it, and recall powers a live
    injection hook that must keep working."""
    pub_path = Path(os.environ.get("NOCKBRAIN_SIGNING_PUB", "").strip()
                    or DEFAULT_SIGNING_PUB)
    key_path = Path(os.environ.get("NOCKBRAIN_SIGNING_KEY", "").strip()
                    or DEFAULT_SIGNING_KEY)
    try:
        if pub_path.exists():
            import _sign
            return _sign.load_public_key(pub_path)
        if key_path.exists():
            import _sign
            return _sign.load_or_create_key(key_path, pub_path, create=False)
    except Exception as exc:  # noqa: BLE001 - never break live recall on key trouble
        print(f"budget-recall: cannot load signing key ({exc}); "
              "attestation verification skipped", file=sys.stderr)
    return None


def _verify_filter(facts: list[dict], verify_key, *, label: str,
                   strict: bool = False, cache=None) -> list[dict]:
    """Enforce attestations on loaded facts (see block comment above).

    TAMPERED facts are always excluded. Default mode keeps UNSIGNED and
    PARENT_SUSPECT facts (backward compatible — their own content is unproven
    or intact respectively, not known-poisoned) and counts them in the stderr
    warning; strict mode keeps only VALID. No key -> facts pass unchanged.

    `cache` (a _verify_cache handle) lets verify_fact skip the signature
    operation for already-proven facts; every status is computed identically
    with or without it, so strict-mode semantics are unaffected."""
    if verify_key is None or not facts:
        return facts
    import _sign
    facts_by_id = {f.get("id", ""): f for f in facts}
    counts: Counter = Counter()
    kept: list[dict] = []
    for f in facts:
        status = _sign.verify_fact(f, verify_key, facts_by_id=facts_by_id,
                                   verified_cache=cache)
        counts[status] += 1
        if status == _sign.TAMPERED:
            continue
        if strict and status != _sign.VALID:
            continue
        kept.append(f)
    flagged = [
        (counts[_sign.TAMPERED], "tampered", True),
        (counts[_sign.UNSIGNED], "unsigned", strict),
        (counts[_sign.PARENT_SUSPECT], "parent-suspect", strict),
    ]
    if any(count for count, _, _ in flagged):
        parts = [
            f"{'excluded' if excluded else 'allowed'} {count} {name}"
            for count, name, excluded in flagged if count
        ]
        print(f"{label}: attestation check: " + "; ".join(parts)
              + f" of {len(facts)} fact(s)"
              + ("" if strict else " (--strict-verify excludes unverified)"),
              file=sys.stderr)
    return kept


def format_fact(f: dict, query_terms: set[str] | None = None) -> str:
    parts = [f"[{f.get('source_date', 'unknown')}]", f"[{f.get('kind', 'fact').upper()}]"]
    header = " ".join(parts)
    content = _relevant_excerpt(str(f.get("content", "")), query_terms, max_chars=320)
    if f.get("status") == "superseded":
        content = f"[SUPERSEDED] {content}"
    return f"{header}\n{content}"


def _load(path: Path, *, verify_key=None, strict_verify: bool = False) -> list[dict]:
    cache = None
    if verify_key is not None:
        # Local import keeps the no-key hot path free of any cache cost. The
        # cache handle captures the store's freshness stat BEFORE the store is
        # read (see _verify_cache.load_for_store).
        import _verify_cache
        cache = _verify_cache.load_for_store(path, verify_key)
    facts = load_facts(path, required_fields=RECALL_ITEM_FIELDS)
    kept = _verify_filter(facts, verify_key, label=str(path),
                          strict=strict_verify, cache=cache)
    if cache is not None:
        cache.save()
    return kept


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
        currently_valid=fact_currently_valid,
        query_terms=_query_terms(query),
        tokenize=_tokenize,
    )


def budget_recall(query: str, facts_file: Path, budget: int = DEFAULT_BUDGET,
                  include_superseded: bool = False, insights_file: Path | None = None,
                  now: datetime | None = None, graph_expand: bool = False,
                  max_per_date: "int | None" = None,
                  strict_verify: bool = False, semantic: bool = False) -> str:
    selection = select_recall(
        query, facts_file, budget, include_superseded,
        insights_file=insights_file, now=now, graph_expand=graph_expand,
        max_per_date=max_per_date, strict_verify=strict_verify,
        semantic=semantic,
    )
    if selection is None:
        return ""
    results = selection["results"]
    output_lines = [f"Memory recall ({len(results)} matches, budget {budget} tokens):"]
    for f in selection["included"]:
        output_lines.append(format_fact(f, selection["query_terms"]))
    remaining = len(results) - len(selection["included"])
    if selection["truncated"] and remaining > 0:
        output_lines.append(f"[...{remaining} more results truncated by budget]")
    output_lines.append(
        f"[{len(selection['included'])} item(s), "
        f"~{selection['tokens_used']} tokens]")
    return "\n\n".join(output_lines)


# Default cap on how many synthesized insights may lead a SEMANTIC recall
# result. Measured in the Phase 0 spike: 20 insights prepended on one query
# consumed most of the 800-token budget before any fused fact. Applies only
# when the semantic tier is on, so the flag-off path stays byte-identical.
# Env-tunable; <= 0 disables the cap.
DEFAULT_INSIGHT_LEAD_CAP = 5


def _resolve_insight_lead_cap() -> int:
    raw = os.environ.get("NOCKBRAIN_INSIGHT_LEAD", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INSIGHT_LEAD_CAP
    except ValueError:
        return DEFAULT_INSIGHT_LEAD_CAP


def _maybe_dense_fuse(all_facts: list[dict], seeds: list[dict], query: str,
                      include_superseded: bool, now: datetime,
                      semantic: bool) -> "tuple[list[dict], frozenset]":
    """Gate for dense (semantic) fusion. When `semantic` is False this is a
    PURE pass-through — same list object, no imports, byte-identical off-path
    (the _maybe_graph_expand pattern). When True, _dense_recall.fuse() RRF-
    merges the BM25 seeds with raw-cosine candidates from the vector sidecar
    and nominates reserved dense slots; any unavailability (deps, model,
    sidecar) degrades silently back to the seeds — BM25 is the floor."""
    if not semantic:
        return seeds, frozenset()
    import _dense_recall  # local import: never loaded on the off-path
    return _dense_recall.fuse(
        all_facts, seeds, query, include_superseded, now,
        min_confidence=MIN_CONFIDENCE,
        currently_valid=fact_currently_valid,
    )


def select_recall(query: str, facts_file: "Path | None",
                  budget: int = DEFAULT_BUDGET,
                  include_superseded: bool = False,
                  insights_file: "Path | None" = None,
                  now: "datetime | None" = None, graph_expand: bool = False,
                  max_per_date: "int | None" = None,
                  strict_verify: bool = False,
                  semantic: bool = False) -> "dict | None":
    """Run the full selection pipeline and return the facts that would be
    injected, as dicts: {results, included, tokens_used, truncated,
    query_terms, reserved_ids}. budget_recall() renders this; the offline
    eval consumes it directly so benchmarks measure production code, not a
    replica. Returns None when nothing matches."""
    ref_now = _resolve_now(now)
    query_terms = _query_terms(query)
    min_matches = _default_recall_min_matches(query_terms)
    verify_key = _resolve_verify_key()
    if strict_verify and verify_key is None:
        print("budget-recall: --strict-verify requested but no signing key "
              "found; attestation verification skipped", file=sys.stderr)
    reserved_ids: frozenset = frozenset()
    if facts_file:
        all_facts = _load(facts_file, verify_key=verify_key,
                          strict_verify=strict_verify)
        fact_results = search(
            all_facts, query, include_superseded,
            now=ref_now, min_matched_terms=min_matches,
        )
        # Dense fusion first (spec D1), graph expansion anchors on the fused
        # list — with on-topic dense seeds it enriches rather than drifts.
        fact_results, reserved_ids = _maybe_dense_fuse(
            all_facts, fact_results, query, include_superseded, ref_now,
            semantic,
        )
        fact_results = _maybe_graph_expand(
            all_facts, fact_results, query, include_superseded, ref_now, graph_expand
        )
    else:
        fact_results = []
    insight_results = (
        search(
            _load(insights_file, verify_key=verify_key,
                  strict_verify=strict_verify),
            query, include_superseded,
            now=ref_now, min_matched_terms=min_matches,
        )
        if insights_file else []
    )
    if semantic:
        lead_cap = _resolve_insight_lead_cap()
        if lead_cap > 0:
            insight_results = insight_results[:lead_cap]

    # Consolidated insights lead; drop the raw facts an insight already covers so
    # recall shows the synthesis, not the synthesis plus its own sources.
    covered = {sid for ins in insight_results for sid in ins.get("source_ids", [])}
    fact_results = [f for f in fact_results if f.get("id") not in covered]

    results = insight_results + fact_results
    if not results:
        return None

    # Diversity cap (post-scoring): keep any single source_date's import from
    # crowding the top of the budget-bounded result. Applied after insight-lead
    # ordering and source dedup, before budget truncation. Reserved dense
    # slots are exempt (Phase 0: the cap demoted the best semantic hit).
    results = _apply_date_diversity_cap(
        results, _resolve_max_per_date(max_per_date), exempt=reserved_ids)

    header = f"Memory recall ({len(results)} matches, budget {budget} tokens):"
    tokens_used = estimate_tokens(header)
    included: list[dict] = []
    truncated = False

    if not reserved_ids:
        # Exact pre-semantic truncation: greedy in order, stop at overflow.
        for f in results:
            fact_tokens = estimate_tokens(format_fact(f, query_terms))
            if tokens_used + fact_tokens > budget:
                truncated = True
                break
            included.append(f)
            tokens_used += fact_tokens
    else:
        # Reserved slots are guaranteed: precommit their token cost, then fill
        # the remaining budget greedily. A reserved fact past the truncation
        # point is appended at the tail (it displaced budget, not order).
        committed: list[dict] = []
        reserved_cost = 0
        for f in results:
            if f.get("id") not in reserved_ids:
                continue
            cost = estimate_tokens(format_fact(f, query_terms))
            if tokens_used + reserved_cost + cost > budget:
                break  # budget cannot hold every reserved slot; keep what fits
            committed.append(f)
            reserved_cost += cost
        committed_ids = {f.get("id") for f in committed}
        tokens_used += reserved_cost
        emitted: set = set()
        for f in results:
            fid = f.get("id")
            if fid in committed_ids:
                if fid not in emitted:
                    included.append(f)
                    emitted.add(fid)
                continue
            fact_tokens = estimate_tokens(format_fact(f, query_terms))
            if tokens_used + fact_tokens > budget:
                truncated = True
                break
            included.append(f)
            tokens_used += fact_tokens
        if truncated:
            for f in committed:
                if f.get("id") not in emitted:
                    included.append(f)
                    emitted.add(f.get("id"))

    return {
        "results": results,
        "included": included,
        "tokens_used": tokens_used,
        "truncated": truncated,
        "query_terms": query_terms,
        "reserved_ids": reserved_ids,
    }


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
    parser.add_argument("--semantic", action="store_true",
                        help="Enable hybrid semantic recall over the vector "
                             "sidecar (default off; also via "
                             "NOCKBRAIN_SEMANTIC=1; degrades to flat BM25 "
                             "when deps/model/sidecar are missing)")
    parser.add_argument("--max-per-date", type=int, default=None,
                        help="Cap facts sharing one source_date in the result "
                             "(default 4; 0 disables; also via "
                             "NOCKBRAIN_MAX_PER_DATE)")
    parser.add_argument("--strict-verify", action="store_true",
                        help="Fail closed: recall only facts whose attestation "
                             "verifies as valid (default also excludes tampered "
                             "facts but still allows unsigned ones; also via "
                             "NOCKBRAIN_STRICT_VERIFY=1)")
    args = parser.parse_args()

    budget = min(args.budget, MAX_BUDGET)
    query_str = " ".join(args.query)
    graph_expand = args.graph or _env_truthy("NOCKBRAIN_GRAPH_RECALL")
    strict_verify = args.strict_verify or _env_truthy("NOCKBRAIN_STRICT_VERIFY")
    semantic = args.semantic or _env_truthy("NOCKBRAIN_SEMANTIC")
    result = budget_recall(query_str, args.facts, budget, args.include_superseded,
                           insights_file=args.insights, graph_expand=graph_expand,
                           max_per_date=args.max_per_date,
                           strict_verify=strict_verify, semantic=semantic)

    if result:
        print(result)
    else:
        print("No matching facts found.")


if __name__ == "__main__":
    main()
