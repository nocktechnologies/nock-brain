#!/usr/bin/env python3
"""Extract durable facts from Claude Code session transcripts.

Reads markdown transcript files (from memsearch or any directory), identifies
decisions, corrections, directives, and architecture facts, and writes
structured JSON.

Each fact carries: id, kind, scope, status, content, source_file,
source_date, session_anchor, confidence, created_at.

Usage:
    python3 extract-facts.py                         # defaults
    python3 extract-facts.py --dir ./transcripts     # custom input
    python3 extract-facts.py --since 2026-05-18      # recent only
    python3 extract-facts.py --output ./my-facts.json
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts
from _scrub import scrub_secrets
from _store import secure_mkdir, secure_write_json

DEFAULT_DIRS = [
    Path.home() / ".memsearch" / "memory",
    Path.home() / ".nock-brain" / "transcripts",
]
DEFAULT_OUTPUT = Path.home() / ".nock-brain" / "facts.json"

TAGGED_PATTERNS = [
    (r"\[DECISION\]", "decision", 0.9),
    (r"\[DIRECTIVE\]", "directive", 0.9),
    (r"\[CORRECTION\]", "correction", 0.9),
    (r"\[MERGE\]", "merge", 0.9),
    (r"\[DISPATCH\]", "dispatch", 0.9),
    (r"\[ARCHITECTURE\]", "architecture", 0.9),
    (r"\[BUG\]", "bug", 0.9),
    (r"\[CONFIG\]", "config", 0.9),
    (r"\[CONTENT\]", "content", 0.9),
    (r"\[STATUS\]", "status", 0.5),
]

AUTHORITY_KINDS = {"decision", "directive", "correction"}
USER_AUTHORITY_RE = re.compile(r"\b(?:user|kevin|founder|owner)\b", re.IGNORECASE)

INFERRED_PATTERNS = [
    (r"(?:user|kevin|founder|owner)\b.{0,30}\b(?:decided|approved|locked|chose|picked|selected|wants?)\b", "decision", 0.8),
    (r"(?:user|kevin|founder|owner)\b.{0,30}\b(?:directed|told|asked|said|instructed|requested)\b.{5,}", "directive", 0.75),
    (r"(?:user|kevin|founder|owner)\b.{0,30}\b(?:corrected|caught|pushed back|wrong|mistake)\b", "correction", 0.85),
    (r"\b(?:merged|admin[- ]merge)\b.{0,20}\bPR\s*#\d+", "merge", 0.85),
    (r"\bPR\s*#\d+\b.{0,20}\b(?:merged|admin[- ]merge)\b", "merge", 0.85),
    (r"\b(?:dispatched|activated|assigned)\b.{0,30}\b(?:agent|worker|builder|reviewer)\b", "dispatch", 0.8),
    (r"\b(?:schema|migration|refactor|redesign|architecture)\b.{0,30}\b(?:changed|updated|added|removed|split|merged)\b", "architecture", 0.7),
    (r"\b(?:bug|regression)\b.{0,30}\b(?:found|fixed|caused|root cause)\b", "bug", 0.75),
    (r"\bfixed?\b.{0,20}\b(?:bug|crash|failure|error|leak|bypass)\b", "bug", 0.7),
]

SKIP_PATTERNS = [
    r"^Claude Code executed (?:pipeline|heartbeat|repeated)",
    r"^(?:All|No new|HEARTBEAT_OK)",
    r"^State (?:snapshots?|remained)",
    r"^Telegram polling (?:remained|consistently|failed)",
    r"^Claude Code wrote (?:compaction|checkpoint)",
    r"Claude Code (?:wrote|composed|posted|created|performed)\b.{0,20}(?:diary|Diary)",
    r"^Diary entry",
    r"^Claude Code ran pipeline",
    r"^Claude Code (?:verified|checked) (?:actual )?time",
    r"^Claude Code (?:set up|configured) (?:cron|heartbeat)",
    r"^Claude Code (?:read|loaded|fetched) (?:identity|config|handoff|diary|private|invariants)",
    r"^Claude Code (?:sent|replied|responded|messaged|forwarded) (?:via )?(?:Telegram|voice)",
    r"^Session (?:booted|started|continued)",
    r"^Claude Code (?:acknowledged|confirmed) (?:receipt|the message)",
    r"^Claude Code (?:marked|read) (?:message|msg)",
    r"^Claude Code confirmed\b",
    r"^Claude Code (?:noted|reported) (?:time|status|state|pipeline)",
    r"Ran compaction checkpoint",
    r"compaction checkpoint.{0,30}(?:written|snapsh)",
    r"^Claude Code (?:updated|wrote) (?:Telegram|offset|state)",
]

PR_RE = re.compile(r"PR\s*#(\d+)")


def make_id(content: str, date: str) -> str:
    return hashlib.sha256(f"{date}:{content[:200]}".encode()).hexdigest()[:12]


def classify_bullet(text: str) -> tuple[str, float] | None:
    for skip in SKIP_PATTERNS:
        if re.search(skip, text, re.IGNORECASE):
            return None
    for pattern, kind, conf in TAGGED_PATTERNS:
        if re.search(pattern, text):
            return kind, conf
    for pattern, kind, conf in INFERRED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return kind, conf
    return None


def authority_fact_allowed(kind: str, text: str, actor: str | None = None) -> bool:
    if kind not in AUTHORITY_KINDS:
        return True
    if actor is not None:
        return actor == "user"
    return bool(USER_AUTHORITY_RE.search(text))


def extract_metadata(text: str) -> dict:
    meta = {}
    prs = PR_RE.findall(text)
    if prs:
        meta["pr_numbers"] = [int(p) for p in prs]
    return meta


def parse_file(filepath: Path, since_date: str | None = None) -> list[dict]:
    facts = []
    file_date = filepath.stem
    if since_date and file_date < since_date:
        return facts

    text = filepath.read_text(encoding="utf-8")
    current_session = ""
    current_anchor = ""

    for line in text.splitlines():
        if line.startswith("## Session"):
            current_session = line.replace("## ", "").strip()
        elif line.startswith("<!-- session:"):
            current_anchor = line.strip()
        elif line.startswith("- "):
            bullet = line[2:].strip()
            bullet, _ = scrub_secrets(bullet)
            result = classify_bullet(bullet)
            if result:
                kind, confidence = result
                if not authority_fact_allowed(kind, bullet):
                    continue
                meta = extract_metadata(bullet)
                fact = {
                    "id": make_id(bullet, file_date),
                    "kind": kind,
                    "scope": "global",
                    "status": "current",
                    "confidence": confidence,
                    "content": bullet,
                    "source_file": filepath.name,
                    "source_date": file_date,
                    "session": current_session,
                    "session_anchor": current_anchor[:200] if current_anchor else "",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **meta,
                }
                facts.append(fact)
    return facts


def normalize_for_dedup(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(claude code|the|a|an|was|were|is|are|has|had|have)\b", "", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:100]


def dedupe(facts: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for f in facts:
        key = normalize_for_dedup(f["content"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def find_transcript_dir() -> Path | None:
    for d in DEFAULT_DIRS:
        if d.exists() and any(d.glob("*.md")):
            return d
    return None


def main():
    parser = argparse.ArgumentParser(description="Extract facts from session transcripts")
    parser.add_argument("--dir", type=Path, default=None,
                        help="Directory with markdown transcript files")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--since", type=str, default=None, help="YYYY-MM-DD")
    args = parser.parse_args()

    transcript_dir = args.dir or find_transcript_dir()
    if not transcript_dir or not transcript_dir.exists():
        print("No transcript directory found.", file=sys.stderr)
        print("Checked: " + ", ".join(str(d) for d in DEFAULT_DIRS), file=sys.stderr)
        print("Use --dir to specify a directory with .md transcript files.", file=sys.stderr)
        sys.exit(1)

    secure_mkdir(args.output.parent)

    all_facts = []
    for md_file in sorted(transcript_dir.glob("*.md")):
        file_facts = parse_file(md_file, args.since)
        all_facts.extend(file_facts)

    all_facts = dedupe(all_facts)

    existing = []
    if args.output.exists():
        existing = load_facts(args.output)
        existing_ids = {f["id"] for f in existing}
        new_facts = [f for f in all_facts if f["id"] not in existing_ids]
        all_facts = existing + new_facts
    else:
        new_facts = all_facts

    secure_write_json(args.output, all_facts, indent=2, default=str)

    by_kind = {}
    for f in all_facts:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1

    print(f"Total facts: {len(all_facts)} ({len(new_facts)} new)")
    for kind, count in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"  {kind}: {count}")


if __name__ == "__main__":
    main()
