#!/usr/bin/env python3
"""Gated fact extraction — PROPOSE new facts for review instead of auto-writing.

This is the gated companion to extract-facts.py. It runs the same extraction,
but instead of merging new facts straight into the live store it writes them to
a review QUEUE (proposed-facts.json + .md) tagged status="proposed". The live
facts.json is never touched. A human or agent then runs approve-proposals.py to
release approved proposals into the store.

The point: close the hand-curation gap (automated extraction) WITHOUT giving up
the review gate that keeps the signed store trustworthy. Extraction proposes;
approval — a deliberate, reversible step — is what writes.

Usage:
    python3 propose-facts.py --dir <transcripts>            # propose new facts
    python3 propose-facts.py --since 2026-06-01             # window the scan
    python3 propose-facts.py --queue /path/proposed.json    # custom queue path

Mirrors the gated review-queue pattern in review-promotions.py (candidates as
JSON + markdown with explicit actions), but for fact diffs rather than doc
promotions.
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts
from _store import secure_mkdir, secure_write_json, secure_write_text

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_QUEUE = Path.home() / ".nock-brain" / "proposed-facts.json"
PROPOSAL_ACTIONS = ["approve", "edit", "reject", "defer"]


def _load_module(name: str):
    """Load a hyphenated sibling script as a module (cached, no package structure)."""
    mod_name = name.replace("-", "_")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, BIN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def extract_candidates(transcript_dir: Path, since: str | None) -> list[dict]:
    """Run the existing extraction logic over the transcript dir."""
    extract_facts = _load_module("extract-facts")
    candidates: list[dict] = []
    for md_file in sorted(transcript_dir.glob("*.md")):
        candidates.extend(extract_facts.parse_file(md_file, since))
    return extract_facts.dedupe(candidates)


def new_proposals(candidates: list[dict], live_facts: list[dict], queued: list[dict]) -> list[dict]:
    """Keep only candidates not already in the live store or the queue."""
    known = {f.get("id") for f in live_facts} | {p.get("id") for p in queued}
    fresh = []
    for c in candidates:
        if c.get("id") in known:
            continue
        proposal = dict(c)
        # Make the proposal a fully-valid fact so it round-trips through the
        # strict load_facts() gate: parse_file omits `evidence`, so default it.
        proposal.setdefault("evidence", [])
        proposal["status"] = "proposed"
        proposal["proposed_at"] = _now()
        proposal["actions"] = list(PROPOSAL_ACTIONS)
        fresh.append(proposal)
        known.add(c.get("id"))
    return fresh


def render_markdown(proposals: list[dict]) -> str:
    """Render the proposal queue as a human-readable review markdown."""
    lines = [f"# Proposed facts (review queue) — {len(proposals)} pending", ""]
    if not proposals:
        lines.append("_No new proposals. The live store already covers everything extracted._")
        return "\n".join(lines) + "\n"
    for p in proposals:
        content = str(p.get("content", "")).strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "…"
        lines.append(f"## [{p.get('kind', '?')}] {p.get('id', '')}")
        lines.append(f"- confidence: {p.get('confidence', '')}")
        lines.append(f"- source_date: {p.get('source_date', '')}")
        lines.append(f"- content: {content}")
        lines.append(f"- actions: {', '.join(p.get('actions', []))}")
        lines.append("")
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> int:
    """Extract candidates and queue the genuinely-new ones as proposals; the live store is never touched."""
    parser = argparse.ArgumentParser(description="Propose new facts for gated review (never auto-writes the store)")
    parser.add_argument("--dir", type=Path, default=None, help="Directory with markdown transcripts")
    parser.add_argument("--since", type=str, default=None, help="YYYY-MM-DD lower bound")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS, help="Live fact store (read-only here)")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="Proposal queue output (.json; .md alongside)")
    args = parser.parse_args(argv)

    extract_facts = _load_module("extract-facts")
    transcript_dir = args.dir or extract_facts.find_transcript_dir()
    if not transcript_dir or not transcript_dir.exists():
        print("No transcript directory found. Use --dir.", file=sys.stderr)
        return 1

    live_facts = load_facts(args.facts) if args.facts.exists() else []
    queued = load_facts(args.queue) if args.queue.exists() else []

    candidates = extract_candidates(transcript_dir, args.since)
    fresh = new_proposals(candidates, live_facts, queued)

    # Append to (not overwrite) any existing queue so multiple runs accumulate.
    merged_queue = queued + fresh
    secure_mkdir(args.queue.parent)
    secure_write_json(args.queue, merged_queue, indent=2, default=str)
    secure_write_text(
        args.queue.with_suffix(".md"),
        render_markdown(merged_queue),
        encoding="utf-8",
    )

    print(f"Proposed {len(fresh)} new fact(s); queue now holds {len(merged_queue)} pending.")
    print(f"Live store ({args.facts}) untouched. Review: {args.queue.with_suffix('.md')}")
    print("Release with: approve-proposals.py --approve-all  (or --approve <id>)")
    return 0


def main() -> int:
    """CLI entry point."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
