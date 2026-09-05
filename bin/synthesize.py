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
Haiku-distill: it enriches the insight prose from bounded, recorded inputs.
Publication always validates and signs the result before atomic replacement;
the source store must already verify under its existing signing key.

Usage:
    python3 synthesize.py                          # defaults: ~/.nock-brain/{facts,insights}.json
    python3 synthesize.py --facts ./facts.json --output ./insights.json
    python3 synthesize.py --threshold 0.3 --min-cluster 2
    python3 synthesize.py --kinds correction,bug   # only consolidate these kinds
    python3 synthesize.py --llm                     # opt-in Haiku-distill (subscription path)
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess  # nosec B404 - only invokes the trusted local `claude` CLI, no shell
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _scrub import JUDGE_PROMPT_MARKERS, scrub_secrets
from _facts import fact_currently_valid, fact_source, fill_source_date, malformed_fact_reason
from _sign import canonical_fact_hash, resolve_signing_key, sign_facts, verify_facts
from _store import secure_mkdir, secure_replace_bytes

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_OUTPUT = Path.home() / ".nock-brain" / "insights.json"
DEFAULT_THRESHOLD = 0.3
DEFAULT_MIN_CLUSTER = 2
# Members below this confidence never enter clustering: recurrence of low-value
# noise is not a lesson. 0.6 sits between the [STATUS] extraction tier (0.5)
# and the weakest durable inferred kind (architecture, 0.7), so status-grade
# noise is excluded while every durable extraction stays eligible.
DEFAULT_CONFIDENCE_FLOOR = 0.6
# Opt-in LLM (Haiku-distill) synthesizer. "haiku" is the CLI alias for the cheap
# Haiku tier; `claude -p` runs on the Claude Code subscription (NOT the metered
# API), so the LLM-distill carries no per-call spend.
DEFAULT_LLM_MODEL = "haiku"
DEFAULT_LLM_TIMEOUT = 60.0  # seconds per cluster before falling back to heuristic
# Bound LLM spend: enrich only the top-N strongest recurrences with Haiku; the
# long tail stays heuristic. Keeps the full insight set complete while capping
# calls (the store has ~270 clusters — re-distilling all nightly is wasteful).
DEFAULT_LLM_TOP = 40
MAX_INPUTS = 25
MAX_INPUT_CHARS = 300
SYNTHESIS_SCHEMA = "nockbrain-synthesis/v1"

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "this", "that", "we", "you", "i", "he", "she", "they", "our", "us", "claude",
    "code", "kevin", "mira", "not", "no", "so", "if", "then", "than", "into",
    "out", "up", "down", "over", "after", "before", "about",
}


def member_confidence(fact: dict) -> float:
    """A fact's confidence for synthesis math; missing/garbage reads as 0.0
    (no evidence of quality contributes no quality)."""
    try:
        return float(fact.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    return ", ".join(sorted(counts, key=lambda term: (-counts[term], term))[:top])


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
            # N10052: never persist the one-shot session — a persisted judge
            # transcript lands under the active config dir's projects/ tree,
            # which rebuild-store scans by default, and the prompt template
            # gets minted back into the store as "facts".
            ["claude", "-p", "--model", model, "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


# Chat-shaped LLM artifacts that must never enter the trusted recall surface.
# The live pollution ("Once you share those, I'll return...") is a model asking
# for input instead of stating a lesson. A shape gate at GENERATION beats a
# post-hoc reject filter, which always trails the next bad shape (F3, Mira).
_CHAT_SHAPE_RE = re.compile(
    r"\b(once you (share|provide|send)|please (share|provide|send|paste)|"
    r"i'?ll (return|provide|give|need)|could you (share|clarify|provide)|"
    r"i (need|would need|don'?t have)|let me know|as an ai|i'?m unable|"
    r"i cannot|to (help|assist) (you )?(with )?(that|this)|here (is|are) the|"
    r"send (me|them|those)|waiting for|based on the (notes|facts) (you|above))",
    re.IGNORECASE,
)


def is_valid_lesson(text: str) -> bool:
    """True iff ``text`` reads like a durable lesson, not a chat reply.

    Rejects empty/too-short output, questions (a lesson asserts, it doesn't
    ask), known chat/meta shapes, and output echoing a registered judge
    template (a marker-carrying "lesson" would re-plant the N10052 leak at
    generation). Deterministic and dependency-free so it runs identically in
    the hook floor."""
    if not isinstance(text, str):
        return False
    if any(marker in text for marker in JUDGE_PROMPT_MARKERS):
        return False
    # Normalize smart quotes/apostrophes and strip surrounding quotes FIRST:
    # a quote-wrapped question ends with '"' not '?', and a curly-apostrophe
    # refusal (’: "i’ll return") evades an ASCII-apostrophe regex.
    normalized = (text.replace("\u2019", "'").replace("\u2018", "'")
                      .replace("\u201c", '"').replace("\u201d", '"')
                      .strip().strip("\"'").strip())
    if len(normalized) < 12:
        return False
    if "?" in normalized:  # a lesson asserts; it never asks
        return False
    if _CHAT_SHAPE_RE.search(normalized):
        return False
    return True


def make_claude_synthesizer(model: str = DEFAULT_LLM_MODEL,
                            timeout: float = DEFAULT_LLM_TIMEOUT):
    """Build an opt-in LLM synthesizer for :func:`synthesize_cluster`.

    The returned callable ``(inputs, heuristic_content) -> str`` reads the
    bounded, recorded inputs and returns ONE consolidated lesson sentence. It returns ``""`` on any failure (empty, too-short,
    or errored call) so ``synthesize_cluster`` owns the single fallback path.
    Input receipts and conservative coverage are computed outside the model.
    """
    def _synth(cluster: list[dict], heuristic_content: str) -> str:
        # Re-scrub before the content leaves the store: facts are scrubbed at
        # extraction, but older/imported facts may predate a pattern. Scrub
        # BEFORE truncation so a secret straddling the 300-char cut cannot
        # survive as a recognizable fragment.
        members = "\n".join(
            f"- {scrub_secrets(f.get('content', ''))[0][:MAX_INPUT_CHARS]}"
            for f in cluster[:MAX_INPUTS]
        )
        prompt = (
            JUDGE_PROMPT_MARKERS[0] + " "
            "Write ONE clear, specific sentence (max 45 words) stating the durable, "
            "reusable lesson — what to do or avoid next time. Output only the "
            "sentence, no preamble or quotes.\n\n" + members
        )
        cleaned = " ".join(_call_claude(prompt, model, timeout).split())
        # Shape-gate at generation: a chat-shaped artifact falls back to the
        # deterministic heuristic content rather than polluting recall.
        return cleaned if is_valid_lesson(cleaned) else ""
    return _synth


def _normalized_text(text: str) -> str:
    return " ".join(text.split())


def source_events(cluster: list[dict]) -> list[dict]:
    """Distinct scoped events, with every supporting fact ID associated to each.

    A fact citing several event IDs supports each of those events; overlapping
    citations do not create another occurrence. Only facts with no event IDs
    fall back to location anchors or identical text on the same date/source.
    """
    groups = {}
    for fact in cluster:
        anchors = fact.get("evidence", [])
        events = sorted({str(a["event_id"]) for a in anchors
                         if isinstance(a, dict) and a.get("event_id")})
        locations = sorted({(str(a["path"]), str(a["line"])) for a in anchors
                            if isinstance(a, dict) and a.get("path") and a.get("line")})
        if events:
            identities = [("event", event) for event in events]
        elif locations:
            identities = [("anchor", tuple(locations))]
        else:
            identities = [("text", str(fact.get("source_date", "")),
                           _normalized_text(fact.get("content", "")))]
        for identity in identities:
            key = (fact_source(fact), fact.get("kind"), identity)
            groups.setdefault(key, {})[fact["id"]] = fact
    result = []
    for key, members in groups.items():
        ordered = sorted(members.values(), key=lambda f: (
            str(f.get("source_date", "")), member_confidence(f), str(f["id"])), reverse=True)
        result.append({"event_id": key[2][1] if key[2][0] == "event" else None,
                       "facts": ordered})
    return sorted(result, key=lambda e: (str(e["facts"][0].get("source_date", "")),
                                        str(e["facts"][0]["id"]), e["event_id"] or ""), reverse=True)


def event_lineage(events: list[dict]) -> list[dict]:
    """Signed event-to-fact associations; fallback groups have no event ID."""
    return [{"event_id": event["event_id"],
             "representative_id": event["facts"][0]["id"],
             "source_ids": sorted(f["id"] for f in event["facts"])} for event in events]


def representative_inputs(events: list[dict]) -> list[dict]:
    """Distinct recent facts, one per date per round, up to the fixed input cap.

    A fact spanning many events is supplied once. When it already represents
    another event, prefer that event's next distinct supporting fact.
    """
    by_date = {}
    seen = set()
    for event in events:
        fact = next((f for f in event["facts"] if f["id"] not in seen), None)
        if fact is not None:
            seen.add(fact["id"])
            by_date.setdefault(str(fact.get("source_date", "")), []).append(fact)
    selected = []
    round_index = 0
    while len(selected) < MAX_INPUTS:
        row = [by_date[d][round_index] for d in sorted(by_date, reverse=True)
               if round_index < len(by_date[d])]
        if not row:
            break
        selected.extend(row[:MAX_INPUTS - len(selected)])
        round_index += 1
    return selected


def synthesize_cluster(cluster: list[dict], synthesizer=None) -> dict:
    """Summarize bounded source events; lineage never implies full coverage."""
    events = source_events(cluster)
    selected = representative_inputs(events)
    # The exact bounded, scrubbed text passed to any synthesizer is also what
    # the heuristic sees. Hash both this text and the original signed source.
    inputs = []
    input_receipts = []
    for fact in selected:
        clean = scrub_secrets(fact.get("content", ""))[0]
        text = clean[:MAX_INPUT_CHARS]
        inputs.append({**fact, "content": text})
        input_receipts.append({
            "id": fact["id"], "source_date": fact.get("source_date", ""),
            "fact_hash": canonical_fact_hash(fact),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "chars": len(text), "truncated": len(clean) > MAX_INPUT_CHARS,
            "evidence": fact.get("evidence", []),
        })
    kind = selected[0].get("kind", "fact")
    dates = sorted(f.get("source_date", "") for f in selected if f.get("source_date"))
    source_ids = sorted({f["id"] for f in cluster})
    input_ids = [f["id"] for f in selected]
    theme = cluster_theme(inputs)
    latest = max(inputs, key=lambda f: (f.get("source_date", ""), f["id"]))
    n = len(events)
    date_range = ""
    if dates:
        date_range = dates[0] if dates[0] == dates[-1] else f"{dates[0]}..{dates[-1]}"
    content = (f"Recurring {kind} ({n} distinct events; {len(inputs)} sampled inputs"
               f"{', ' + date_range if date_range else ''}): "
               f"{theme}. Most recent: {latest['content'][:160]}")
    synthesized_by = "heuristic"
    if synthesizer is not None:
        try:
            enriched = synthesizer(inputs, content)
        except Exception:  # nosec B110 - optional enrichment falls back to signed heuristic
            enriched = None
        if isinstance(enriched, str) and is_valid_lesson(enriched):
            content = enriched.strip()
            synthesized_by = "llm"

    # A one-sentence abstraction is not an exhaustive replacement for 25 facts.
    # Only an untruncated, verbatim source included in the output is covered.
    covered = [f["id"] for f, receipt in zip(selected, input_receipts)
               if not receipt["truncated"] and _normalized_text(f["content"])
               and _normalized_text(f["content"]) in _normalized_text(content)]
    # Several citations from one fact establish recurrence, not independent
    # confidence votes. Cap quality by distinct representative facts once each.
    representatives = {e["facts"][0]["id"]: e["facts"][0] for e in events}
    confidence = round(min(0.95, 0.7 + 0.05 * n,
                           sum(member_confidence(f) for f in representatives.values())
                           / len(representatives)), 2)
    lineage = {
        "schema": SYNTHESIS_SCHEMA, "source_ids": source_ids,
        "input_ids": input_ids, "covered_source_ids": covered,
        "source_date": dates[-1] if dates else "", "source_dates": dates,
        "lineage_source_date": max(str(f.get("source_date", "")) for f in cluster),
        "recurrence": n, "source_row_count": len(cluster),
        "source": fact_source(selected[0]), "inputs": input_receipts,
        "events": event_lineage(events),
    }
    return {
        "id": insight_id(kind, theme, source_ids), "kind": "insight",
        "tier": "synthesized", "of_kind": kind, "recurrence": n,
        "theme": theme, "content": content, "synthesized_by": synthesized_by,
        "status": "current", "confidence": confidence,
        "source": lineage["source"], "source_date": lineage["source_date"],
        "source_ids": source_ids, "source_dates": dates, "input_ids": input_ids,
        "covered_source_ids": covered, "evidence": [lineage],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def synthesize(
    facts: list[dict], threshold: float = DEFAULT_THRESHOLD,
    min_cluster: int = DEFAULT_MIN_CLUSTER, kinds: set[str] | None = None,
    synthesizer=None, llm_top: int | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> list[dict]:
    """Consolidate current facts into insights. Only clusters with at least
    min_cluster distinct source events become insights. Repeated extraction
    rows remain lineage, but do not raise recurrence or confidence. Facts below
    ``confidence_floor`` never enter clustering (pass ``0.0`` to include all).
    An optional ``synthesizer`` callable enriches each insight's prose (see
    :func:`synthesize_cluster`); ``None`` (default) uses the heuristic. When a
    synthesizer is given, ``llm_top`` bounds enrichment to the N strongest
    recurrences (the highest-value lessons); the long tail stays heuristic.
    ``llm_top=None`` enriches every cluster."""
    active = [f for f in facts if f.get("status", "current") != "superseded"
              and fact_currently_valid(f)]
    active = [f for f in active if member_confidence(f) >= confidence_floor]
    if kinds:
        active = [f for f in active if f.get("kind") in kinds]

    by_kind: dict[tuple[str, str], list[dict]] = {}
    for f in active:
        by_kind.setdefault((f.get("kind", "fact"), fact_source(f)), []).append(f)

    # Collect every qualifying cluster, then rank strongest-first so LLM
    # enrichment targets the top recurrences within a bounded call budget.
    clusters = [
        cluster
        for kind_facts in by_kind.values()
        for cluster in cluster_kind(kind_facts, threshold)
        if len(source_events(cluster)) >= min_cluster
    ]
    clusters.sort(key=lambda cluster: len(source_events(cluster)), reverse=True)

    insights = []
    for rank, cluster in enumerate(clusters):
        use = synthesizer if (
            synthesizer is not None and (llm_top is None or rank < llm_top)
        ) else None
        insights.append(synthesize_cluster(cluster, use))
    return insights


def _sign_insights(insights: list[dict], *, key=None) -> "list[dict] | None":
    """Sign through the shared v1/v2 router; publication treats None as fatal."""
    key = key or resolve_signing_key()
    return sign_facts(insights, key) if key is not None else None


def validate_insights(insights: list[dict], facts: list[dict]) -> None:
    """Check generated lineage before signing and again after serialization."""
    if not isinstance(insights, list):
        raise ValueError("synthesis must be a list")
    by_id = {f["id"]: f for f in facts}
    seen = set()
    for insight in insights:
        if (not isinstance(insight, dict) or malformed_fact_reason(insight)
                or insight.get("kind") != "insight" or not insight.get("content")
                or insight.get("id") in seen):
            raise ValueError("malformed or duplicate insight")
        seen.add(insight["id"])
        evidence = insight.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != 1:
            raise ValueError("missing synthesis lineage")
        lineage = evidence[0]
        if not isinstance(lineage, dict) or lineage.get("schema") != SYNTHESIS_SCHEMA:
            raise ValueError("invalid synthesis lineage schema")
        for field in ("source_ids", "input_ids", "covered_source_ids", "source_dates",
                      "source_date", "source", "recurrence"):
            if insight.get(field) != lineage.get(field):
                raise ValueError("synthesis lineage differs: " + field)
        source_ids = lineage["source_ids"]
        input_ids = lineage["input_ids"]
        covered = lineage["covered_source_ids"]
        if (not isinstance(source_ids, list) or not source_ids
                or len(source_ids) != len(set(source_ids)) or not set(source_ids) <= by_id.keys()
                or not isinstance(input_ids, list) or not 0 < len(input_ids) <= MAX_INPUTS
                or len(input_ids) != len(set(input_ids)) or not set(input_ids) <= set(source_ids)
                or not isinstance(covered, list) or not set(covered) <= set(input_ids)):
            raise ValueError("invalid synthesis source coverage")
        inputs = lineage.get("inputs", [])
        if [entry.get("id") for entry in inputs] != input_ids:
            raise ValueError("synthesis inputs differ from their receipt")
        for entry in inputs:
            fact = by_id[entry["id"]]
            text = scrub_secrets(fact["content"])[0][:MAX_INPUT_CHARS]
            if (entry.get("fact_hash") != canonical_fact_hash(fact)
                    or entry.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()
                    or entry.get("source_date") != fact["source_date"]
                    or entry.get("evidence") != fact["evidence"]
                    or entry.get("chars") != len(text)
                    or entry.get("truncated") != (len(scrub_secrets(fact["content"])[0]) > MAX_INPUT_CHARS)):
                raise ValueError("synthesis source input changed")
            if entry["id"] in covered and (entry["truncated"] or
                    _normalized_text(fact["content"]) not in _normalized_text(insight["content"])):
                raise ValueError("source is not fully covered by the synthesis")
        dates = sorted(by_id[fid]["source_date"] for fid in input_ids if by_id[fid]["source_date"])
        if lineage["source_dates"] != dates or lineage["source_date"] != (dates[-1] if dates else ""):
            raise ValueError("synthesis freshness differs from supplied inputs")
        groups = source_events([by_id[fid] for fid in source_ids])
        expected_events = event_lineage(groups)
        if (lineage["recurrence"] != len(groups) or lineage.get("events") != expected_events
                or lineage.get("source_row_count") != len(source_ids)):
            raise ValueError("synthesis recurrence differs from source events")
        if lineage.get("lineage_source_date") != max(str(by_id[fid].get("source_date", "")) for fid in source_ids):
            raise ValueError("synthesis lineage freshness differs from sources")


@contextmanager
def _output_lock(output: Path):
    """All invocations share this persistent lock; never unlink its inode."""
    secure_mkdir(output.parent)
    lock_path = output.with_name("." + output.name + ".synthesis.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _output_identity(output: Path):
    """Identify the opened artifact, including same-byte replacement (or absence)."""
    try:
        with output.open("rb") as handle:
            raw = handle.read()
            stat = os.fstat(handle.fileno())
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size,
            hashlib.sha256(raw).digest())


def publish_insights(facts_path: Path, output: Path, *, threshold=DEFAULT_THRESHOLD,
                     min_cluster=DEFAULT_MIN_CLUSTER, kinds=None, synthesizer=None,
                     llm_top=None, confidence_floor=DEFAULT_CONFIDENCE_FLOOR) -> tuple[list[dict], int]:
    """Build, validate, sign and verify before one atomic replacement.

    Competing publications compare output identity under the same persistent
    lock; a slow stale generation cannot replace an intervening publication.
    The source is rechecked immediately before replacement, but independent
    facts writers do not share this lock: this is not a transaction with them.
    No key is created; failures preserve the prior output.
    """
    output = output.resolve()
    if facts_path.resolve() == output:
        raise ValueError("synthesis output cannot replace its source facts")
    with _output_lock(output):
        original_output = _output_identity(output)
    raw = facts_path.read_bytes()
    facts = json.loads(raw)
    if not isinstance(facts, list):
        raise ValueError("source facts must be a list")
    for fact in facts:
        fill_source_date(fact)
        if (malformed_fact_reason(fact) or not isinstance(fact.get("id"), str)
                or not fact["id"] or not isinstance(fact.get("content"), str)):
            raise ValueError("malformed source fact")
    if len({f["id"] for f in facts}) != len(facts):
        raise ValueError("duplicate source fact IDs")
    key = resolve_signing_key(store_dir=facts_path.parent)
    if key is None:
        raise ValueError("signing key unavailable; prior synthesis preserved")
    if verify_facts(facts, key)["valid"] != len(facts):
        raise ValueError("source facts failed strict verification")
    insights = synthesize(facts, threshold, min_cluster, kinds, synthesizer,
                          llm_top, confidence_floor=confidence_floor)
    validate_insights(insights, facts)
    expected_ids = [insight["id"] for insight in insights]
    signed = _sign_insights(insights, key=key)
    if signed is None:
        raise ValueError("synthesis signing failed")
    serialized = json.dumps(signed, indent=2, ensure_ascii=False, allow_nan=False).encode()
    staged = json.loads(serialized)
    validate_insights(staged, facts)
    if [insight["id"] for insight in staged] != expected_ids:
        raise ValueError("signing changed the insight set")
    if verify_facts(staged, key)["valid"] != len(staged):
        raise ValueError("signed synthesis failed strict verification")
    with _output_lock(output):
        if _output_identity(output) != original_output:
            raise ValueError("stale synthesis generation: output changed; newer synthesis preserved")
        if not secure_replace_bytes(output, serialized, before_replace=lambda: facts_path.read_bytes() == raw):
            raise ValueError("source facts changed during synthesis; prior synthesis preserved")
    return staged, len(facts)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthesize facts into insights")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-cluster", type=int, default=DEFAULT_MIN_CLUSTER)
    parser.add_argument("--kinds", type=str, default=None,
                        help="Comma-separated kinds to consolidate (default: all)")
    parser.add_argument("--confidence-floor", type=float,
                        default=DEFAULT_CONFIDENCE_FLOOR,
                        help="Exclude facts below this confidence from "
                             "clustering; insight confidence is also capped at "
                             "the mean member confidence "
                             f"(default: {DEFAULT_CONFIDENCE_FLOOR}, 0 = include all)")
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
    parser.add_argument("--sign", action="store_true",
                        help="Compatibility flag: publication always requires signing and verification.")
    args = parser.parse_args(argv)

    kinds = {k.strip() for k in args.kinds.split(",")} if args.kinds else None
    synthesizer = make_claude_synthesizer(args.model, args.llm_timeout) if args.llm else None
    llm_top = args.llm_top if args.llm_top and args.llm_top > 0 else None
    try:
        insights, fact_count = publish_insights(
            args.facts, args.output, threshold=args.threshold, min_cluster=args.min_cluster,
            kinds=kinds, synthesizer=synthesizer, llm_top=llm_top,
            confidence_floor=args.confidence_floor)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(f"synthesize: publication failed: {exc}", file=sys.stderr)
        return 1

    mode = f"LLM ({args.model})" if args.llm else "heuristic"
    llm_n = sum(1 for i in insights if i.get("synthesized_by") == "llm")
    print(f"Synthesized {len(insights)} insight(s) from {fact_count} fact(s) "
          f"[{mode}; {llm_n} LLM-enriched].")
    for ins in insights[:10]:
        print(f"  [{ins['recurrence']}x] {ins['of_kind']}: {ins['theme']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
