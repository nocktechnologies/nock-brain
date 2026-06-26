#!/usr/bin/env python3
"""Refine sanitized evidence events into v1-compatible memory facts.

The v2 pipeline ingests raw Claude Code JSONL as sanitized events first, then
uses this script to produce the existing facts.json shape consumed by
budget-recall.py. Each fact keeps a compact evidence pointer back to the event
and source line that produced it.

Usage:
    python3 bin/refine-sessions.py --events events.jsonl --facts facts.json --notes-dir notes
"""
import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _scrub import is_structural_noise
from _store import secure_mkdir, secure_write_text

MAX_FACT_CONTENT_CHARS = 1500
TOOL_RESULT_CONFIDENCE_CAP = 0.55

# N8392-A: tool I/O is evidence, never a durable fact. Drop both surfaces so
# the inferred 'merge'/'bug' patterns can't mint facts out of raw command JSON
# (tool_use.input) or raw command/result output (tool_result.content).
NON_FACT_SURFACES = {"tool_use.input", "tool_result.content"}


def load_extract_facts():
    path = BIN_DIR / "extract-facts.py"
    spec = importlib.util.spec_from_file_location("extract_facts", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def event_source_date(event: dict[str, Any]) -> str:
    timestamp = event.get("source", {}).get("timestamp", "")
    if isinstance(timestamp, str) and len(timestamp) >= 10:
        return timestamp[:10]
    path = event.get("source", {}).get("path", "")
    stem = Path(path).stem if path else ""
    return stem if stem else datetime.now(timezone.utc).date().isoformat()


def event_source_file(event: dict[str, Any]) -> str:
    path = event.get("source", {}).get("path", "")
    return Path(path).name if path else ""


def event_anchor(event: dict[str, Any]) -> str:
    source = event.get("source", {})
    path = source.get("path", "")
    line = source.get("line", "")
    if path and line:
        return f"{path}:{line}"
    return path


def event_evidence(event: dict[str, Any]) -> dict[str, Any]:
    source = event.get("source", {})
    return {
        "event_id": event.get("id", ""),
        "path": source.get("path", ""),
        "line": source.get("line", ""),
    }


def cap_fact_content(content: str, max_chars: int = MAX_FACT_CONTENT_CHARS) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    suffix = f"\n[TRUNCATED: original {len(content)} chars; see session_anchor]"
    keep = max(0, max_chars - len(suffix))
    return content[:keep].rstrip() + suffix, True


def fact_from_event(event: dict[str, Any], extract_facts=None) -> dict[str, Any] | None:
    extract_facts = extract_facts or load_extract_facts()
    raw_content = str(event.get("content", ""))
    original_content = raw_content.strip()
    if not original_content:
        return None

    # N8392-A: never mint a fact from raw tool I/O or bus/dump artifacts. The
    # prefix guard catches '=== AGENT MESSAGE' blobs that arrive as message-text
    # events and dodge the surface check; both layers are needed.
    if is_structural_noise(original_content) or event.get("surface") in NON_FACT_SURFACES:
        return None

    content, truncated = cap_fact_content(original_content)

    result = extract_facts.classify_bullet(content)
    if not result:
        return None

    kind, confidence = result
    if not extract_facts.authority_fact_allowed(kind, content, actor=event.get("actor", "")):
        return None
    if event.get("surface") == "tool_result.content":
        confidence = min(confidence, TOOL_RESULT_CONFIDENCE_CAP)
    source_date = event_source_date(event)
    created_at = datetime.now(timezone.utc).isoformat()
    metadata = extract_facts.extract_metadata(content)
    fact = {
        "id": extract_facts.make_id(content, source_date),
        "kind": kind,
        "scope": "global",
        "status": "current",
        "confidence": confidence,
        "content": content,
        "source_file": event_source_file(event),
        "source_date": source_date,
        "session": event.get("source", {}).get("session_id", ""),
        "session_anchor": event_anchor(event),
        "created_at": created_at,
        "last_seen_at": created_at,
        "subject": event.get("actor", ""),
        "evidence": [event_evidence(event)],
        **metadata,
    }
    if truncated:
        fact["evidence_truncated"] = True
        fact["evidence_original_chars"] = len(raw_content)
    return fact


def facts_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extract_facts = load_extract_facts()
    facts = []
    seen = set()

    for event in events:
        fact = fact_from_event(event, extract_facts=extract_facts)
        if not fact:
            continue
        key = extract_facts.normalize_for_dedup(fact["content"])
        if key in seen:
            continue
        seen.add(key)
        facts.append(fact)

    return facts


def render_session_note(events: list[dict[str, Any]]) -> str:
    if not events:
        return "# Session unknown\n"

    session_id = events[0].get("source", {}).get("session_id", "") or "unknown"
    facts = facts_from_events(events)
    lines = [f"# Session {session_id}", "", "## Facts"]

    if facts:
        for fact in facts:
            evidence = fact.get("evidence", [{}])[0]
            anchor = f"{fact.get('source_file', '')}:{evidence.get('line', '')}"
            lines.append(f"- [{fact['kind'].upper()}] {fact['content']} ({anchor})")
    else:
        lines.append("- No durable facts extracted.")

    lines.extend(["", "## Evidence Events"])
    for event in events:
        source_file = event_source_file(event)
        line = event.get("source", {}).get("line", "")
        surface = event.get("surface", "")
        kind = event.get("kind", "")
        content = str(event.get("content", "")).replace("\n", " ")
        if len(content) > 240:
            content = content[:237] + "..."
        lines.append(f"- {source_file}:{line} [{surface}/{kind}] {content}")

    return "\n".join(lines) + "\n"


def load_events_jsonl(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def group_events_by_session(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        session_id = event.get("source", {}).get("session_id", "") or "unknown"
        grouped[session_id].append(event)
    return dict(grouped)


def safe_note_name(session_id: str) -> str:
    keep = []
    for char in session_id:
        if char.isalnum() or char in {"-", "_", "."}:
            keep.append(char)
        else:
            keep.append("-")
    name = "".join(keep).strip("-") or "unknown"
    return f"{name}.md"


def write_session_notes(events: list[dict[str, Any]], notes_dir: Path) -> list[Path]:
    secure_mkdir(notes_dir)
    written = []
    for session_id, session_events in sorted(group_events_by_session(events).items()):
        note_path = notes_dir / safe_note_name(session_id)
        secure_write_text(note_path, render_session_note(session_events), encoding="utf-8")
        written.append(note_path)
    return written


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refine sanitized events into memory facts")
    parser.add_argument("--events", type=Path, required=True, help="Sanitized events JSONL")
    parser.add_argument("--facts", type=Path, required=True, help="Output facts JSON file")
    parser.add_argument("--notes-dir", type=Path, required=True, help="Output session notes directory")
    args = parser.parse_args(argv)

    events = load_events_jsonl(args.events)
    facts = facts_from_events(events)

    secure_write_text(args.facts, json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
    notes = write_session_notes(events, args.notes_dir)

    print(f"Wrote {len(facts)} fact(s) to {args.facts}")
    print(f"Wrote {len(notes)} session note(s) to {args.notes_dir}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
