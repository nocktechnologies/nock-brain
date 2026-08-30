#!/usr/bin/env python3
"""Surface stale-but-live facts by pairing topic-overlapping facts across time
(E5b: the nightly contradiction pass).

The 2026-07-27 live-store measurement found real supersessions that were never
marked: a later correction reverses an earlier decision, but both stay live and
the earlier one keeps winning recall. This tool finds those pairs:

1. **Structural pairing** (dependency-free, always runs): live facts of the
   watched kinds are paired when their contents share topic (token-Jaccard in
   [--min-overlap, --max-overlap)) — pairs AT or above --max-overlap are
   near-duplicates and belong to dedup-facts.py, not here. The later fact is
   the presumed superseder. A >=1 day source-date gap is required for a
   CONFIRMED classification; same-date pairs are at most borderline (the
   measured counting convention).
2. **LLM verdict layer** (opt-in ``--llm``): each candidate pair is judged by
   Haiku via the local ``claude -p`` CLI — the Claude Code subscription path,
   NOT the metered API, exactly like synthesize.py's enrichment. Fact content
   is re-scrubbed for secrets and truncated before it leaves the store; note
   this sends FACT content (post-privacy-fence material), never raw
   transcripts. Verdicts: yes -> confirmed (with date gap) / borderline
   (without), no -> dropped, unclear or any failure -> borderline.

Propose-only by design: this tool NEVER writes the store. It emits a review
queue (contradiction-candidates.json + .md) whose proposed actions are
supersede-fact.py commands — applying stays a deliberate, human-gated step.

Usage:
    python3 detect-contradictions.py                       # structural pass
    python3 detect-contradictions.py --llm                 # + Haiku verdicts
    python3 detect-contradictions.py --kinds decision,correction
    python3 detect-contradictions.py --queue-dir /path/review
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import content_tokens, fact_currently_valid
from _scrub import scrub_secrets
from _store import secure_mkdir, secure_write_json, secure_write_text
from _storeback import resolve_store
from _scrub import JUDGE_PROMPT_MARKERS
from synthesize import DEFAULT_LLM_MODEL, DEFAULT_LLM_TIMEOUT, _call_claude

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_KINDS = ("decision", "correction", "directive")
DEFAULT_MIN_OVERLAP = 0.3
# Pairs at/above this are near-duplicates — dedup-facts.py's job, not a
# contradiction. Matches dedup's DEFAULT_MIN_SIMILARITY.
DEFAULT_MAX_OVERLAP = 0.85
DEFAULT_MAX_PAIRS = 200

_VERDICT_RE = re.compile(r"verdict:\s*(yes|no|unclear)", re.IGNORECASE)
_CONTENT_LIMIT = 300


def _parse_date(value: object) -> "datetime | None":
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _live(fact: object, kinds: "tuple[str, ...]") -> bool:
    return (
        isinstance(fact, dict)
        and fact.get("status") != "superseded"
        and fact.get("kind") in kinds
        and fact_currently_valid(fact)
    )


def find_candidates(
    facts: "list[dict]",
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    max_overlap: float = DEFAULT_MAX_OVERLAP,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    kinds: "tuple[str, ...]" = DEFAULT_KINDS,
    stats: "dict | None" = None,
) -> "list[dict]":
    """Pair live facts that share topic but are not near-duplicates.

    Pairing is ACROSS the watched kinds (a correction reversing a decision is
    exactly the target). The later-dated fact is the presumed superseder; with
    equal or unparseable dates the pair still surfaces with date_gap_days=0,
    which caps its classification at borderline. Ranked by overlap; capped at
    ``max_pairs`` with the drop count reported (no silent caps)."""
    pool = sorted(
        (f for f in facts if _live(f, kinds)),
        key=lambda f: str(f.get("id", "")),
    )
    tokens = [content_tokens(f.get("content")) for f in pool]

    candidates: "list[dict]" = []
    for i in range(len(pool)):
        if not tokens[i]:
            continue
        for j in range(i + 1, len(pool)):
            if not tokens[j]:
                continue
            overlap = len(tokens[i] & tokens[j]) / len(tokens[i] | tokens[j])
            if not min_overlap <= overlap < max_overlap:
                continue
            a, b = pool[i], pool[j]
            date_a, date_b = _parse_date(a.get("source_date")), _parse_date(b.get("source_date"))
            if date_a is not None and date_b is not None:
                gap = abs((date_b - date_a).days)
                if date_b < date_a:
                    a, b = b, a
            else:
                gap = 0
            candidates.append(
                {"earlier": a, "later": b, "overlap": round(overlap, 3), "date_gap_days": gap}
            )

    candidates.sort(
        key=lambda c: (-c["overlap"], str(c["earlier"].get("id", "")), str(c["later"].get("id", "")))
    )
    if stats is not None:
        stats["dropped_pairs"] = max(0, len(candidates) - max_pairs)
        stats["max_pairs"] = max_pairs
    if len(candidates) > max_pairs:
        dropped = len(candidates) - max_pairs
        print(
            f"detect-contradictions: dropped {dropped} candidate pair(s) over "
            f"--max-pairs={max_pairs} (kept strongest overlap)",
            file=sys.stderr,
        )
        candidates = candidates[:max_pairs]
    return candidates


def _judged_text(fact: dict) -> str:
    scrubbed, _ = scrub_secrets(str(fact.get("content", "")))
    return f"[{fact.get('kind', 'fact')} · {fact.get('source_date', 'undated')}] " + scrubbed[:_CONTENT_LIMIT]


def make_claude_judge(model: str = DEFAULT_LLM_MODEL,
                      timeout: float = DEFAULT_LLM_TIMEOUT):
    """Build the opt-in ``claude -p`` judge: ``(early, late) -> raw verdict``.

    Runs on the Claude Code subscription path (no metered spend), mirroring
    synthesize.py. Returns ``""`` on any failure, which classify() treats as
    borderline — the pass degrades, never crashes."""
    def _judge(early: str, late: str) -> str:
        prompt = (
            JUDGE_PROMPT_MARKERS[1] + "\n"
            "Does the LATER one contradict or replace the EARLIER one, such that "
            "the earlier fact is now stale and should be marked superseded?\n"
            "Answer on the first line exactly `VERDICT: yes`, `VERDICT: no`, or "
            "`VERDICT: unclear`, then one short reason sentence.\n\n"
            f"EARLIER: {early}\n\nLATER: {late}"
        )
        return _call_claude(prompt, model, timeout)
    return _judge


def classify(candidates: "list[dict]", judge=None,
             llm_top: "int | None" = None) -> "list[dict]":
    """Turn candidate pairs into review rows.

    Without a judge every pair is ``unreviewed`` (structural evidence only).
    With a judge: yes -> confirmed when the date gap is >=1 day, else
    borderline (same-date convention); no -> dropped; unclear, unparseable, or
    a failed call -> borderline.

    ``llm_top`` bounds the judge to the strongest N candidates (they arrive
    sorted by overlap); the rest classify structurally as unreviewed rather
    than being dropped. Without a bound, a large store means hundreds of
    sequential claude -p calls and the nightly can never finish — the F2
    silent-death mode."""
    rows: "list[dict]" = []
    for index, c in enumerate(candidates):
        earlier, later = c["earlier"], c["later"]
        row = {
            "earlier_id": earlier.get("id", ""),
            "later_id": later.get("id", ""),
            "earlier_kind": earlier.get("kind", ""),
            "later_kind": later.get("kind", ""),
            "earlier_date": earlier.get("source_date", ""),
            "later_date": later.get("source_date", ""),
            "earlier_content": str(earlier.get("content", ""))[:_CONTENT_LIMIT],
            "later_content": str(later.get("content", ""))[:_CONTENT_LIMIT],
            "overlap": c["overlap"],
            "date_gap_days": c["date_gap_days"],
            "proposed_action": (
                f"python3 bin/supersede-fact.py {earlier.get('id', '')} "
                f"--by {later.get('id', '')} "
                f"--reason 'superseded by later {later.get('kind', 'fact')} "
                f"({later.get('source_date', 'undated')})'"
            ),
        }
        judged = judge is not None and (llm_top is None or index < llm_top)
        if not judged:
            row["classification"] = "unreviewed"
            row["verdict_source"] = "structural"
        else:
            raw = judge(_judged_text(earlier), _judged_text(later)) or ""
            match = _VERDICT_RE.search(raw)
            verdict = match.group(1).lower() if match else "unclear"
            if verdict == "no":
                continue
            if verdict == "yes" and c["date_gap_days"] >= 1:
                row["classification"] = "confirmed"
            else:
                row["classification"] = "borderline"
            row["verdict_source"] = "llm"
            row["judge_raw"] = " ".join(raw.split())[:200]
        rows.append(row)
    return rows


def write_queue(rows: "list[dict]", queue_dir: Path, *, llm: bool,
                llm_top: "int | None" = None,
                stats: "dict | None" = None) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    counts = {"confirmed": 0, "borderline": 0, "unreviewed": 0}
    for row in rows:
        counts[row["classification"]] += 1
    doc = {
        "generated_at": generated_at,
        "llm": llm,
        "llm_top": llm_top,
        "max_pairs": stats.get("max_pairs") if stats else None,
        "dropped_pairs": stats.get("dropped_pairs", 0) if stats else 0,
        "candidate_count": len(rows),
        "counts": counts,
        "candidates": rows,
    }
    secure_mkdir(queue_dir)
    secure_write_json(queue_dir / "contradiction-candidates.json", doc, indent=2)

    lines = [
        "# Contradiction candidates",
        "",
        f"Generated: {generated_at} · verdicts: {'llm (claude -p)' if llm else 'structural only'}",
        f"{counts['confirmed']} confirmed · {counts['borderline']} borderline · "
        f"{counts['unreviewed']} unreviewed",
        "",
        "Applying is human-gated — run each proposed action deliberately.",
        "",
    ]
    for row in rows:
        lines.append(
            f"## {row['classification'].upper()}: `{row['earlier_id']}` -> `{row['later_id']}` "
            f"(overlap {row['overlap']}, gap {row['date_gap_days']}d)"
        )
        lines.append(f"- earlier [{row['earlier_kind']} · {row['earlier_date']}]: {row['earlier_content']}")
        lines.append(f"- later [{row['later_kind']} · {row['later_date']}]: {row['later_content']}")
        if row.get("judge_raw"):
            lines.append(f"- judge: {row['judge_raw']}")
        lines.append(f"- apply: `{row['proposed_action']}`")
        lines.append("")
    secure_write_text(queue_dir / "contradiction-candidates.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--queue-dir", type=Path, default=None,
                        help="review-queue directory (default: <facts dir>/review)")
    parser.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                        help="comma-separated kinds to watch")
    parser.add_argument("--min-overlap", type=float, default=DEFAULT_MIN_OVERLAP)
    parser.add_argument("--max-overlap", type=float, default=DEFAULT_MAX_OVERLAP)
    parser.add_argument("--max-pairs", type=int, default=DEFAULT_MAX_PAIRS)
    parser.add_argument("--llm", action="store_true",
                        help="judge candidates with Haiku via claude -p (subscription path)")
    parser.add_argument("--llm-top", type=int, default=25,
                        help="judge only the strongest N candidates with the "
                             "LLM (bounds nightly runtime); rest stay "
                             "structural. 0 = judge all")
    parser.add_argument("--model", default=DEFAULT_LLM_MODEL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_LLM_TIMEOUT)
    args = parser.parse_args()

    if not 0.0 < args.min_overlap < args.max_overlap <= 1.0:
        parser.error("--min-overlap and --max-overlap must satisfy 0 < min < max <= 1")
    if args.max_pairs < 1:
        parser.error("--max-pairs must be >= 1")

    store = resolve_store(args.facts)
    if not store.freshness_path.exists():
        print(f"No fact store found ({store.describe()}).", file=sys.stderr)
        sys.exit(1)

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    facts = store.load_facts()
    stats: dict = {}
    candidates = find_candidates(
        facts,
        min_overlap=args.min_overlap,
        max_overlap=args.max_overlap,
        max_pairs=args.max_pairs,
        kinds=kinds,
        stats=stats,
    )
    if args.llm_top < 0:
        parser.error("--llm-top must be >= 0 (0 = judge all)")
    judge = make_claude_judge(args.model, args.timeout) if args.llm else None
    llm_top = None if (not args.llm or args.llm_top == 0) else args.llm_top
    rows = classify(candidates, judge=judge, llm_top=llm_top)

    queue_dir = args.queue_dir or args.facts.parent / "review"
    write_queue(rows, queue_dir, llm=args.llm, llm_top=llm_top, stats=stats)
    counts = {"confirmed": 0, "borderline": 0, "unreviewed": 0}
    for row in rows:
        counts[row["classification"]] += 1
    print(
        f"{len(rows)} candidate(s): {counts['confirmed']} confirmed, "
        f"{counts['borderline']} borderline, {counts['unreviewed']} unreviewed."
    )
    print(f"Queue: {queue_dir / 'contradiction-candidates.json'}")
    print("Store untouched. Apply via the proposed supersede-fact.py commands.")


if __name__ == "__main__":
    main()
