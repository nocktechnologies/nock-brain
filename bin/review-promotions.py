#!/usr/bin/env python3
"""Generate human-gated promotion candidates from memory facts.

This command never edits CLAUDE.md, AGENTS.md, skills, hooks, or identity
files. It only writes a review queue that a human can approve, edit, reject, or
defer.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_mkdir, secure_write_text

ACTIONS = ["approve", "edit", "reject", "defer"]
PROMOTABLE_KINDS = {"decision", "directive", "architecture", "config", "correction"}
MIN_CONFIDENCE = 0.8


def load_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def strip_tag(text: str) -> str:
    return re.sub(r"^\[[A-Z_ -]+\]\s*", "", text).strip()


def proposed_target(fact: dict[str, Any]) -> str:
    content = fact.get("content", "")
    upper = content.upper()
    lower = content.lower()
    if "AGENTS.MD" in upper:
        return "AGENTS.md"
    if "CLAUDE.MD" in upper:
        return "CLAUDE.md"
    if "skill" in lower:
        return "skills/review"
    if "hook" in lower:
        return "hooks/review"
    if "identity" in lower or "invariant" in lower:
        return "identity/review"
    return "review/project-rules.md"


def risk_level(target: str, fact: dict[str, Any]) -> str:
    if target.startswith(("hooks/", "identity/", "skills/")):
        return "high"
    if target in {"CLAUDE.md", "AGENTS.md"}:
        return "medium"
    if fact.get("kind") in {"architecture", "config"}:
        return "medium"
    return "low"


def candidate_id(fact: dict[str, Any], target: str) -> str:
    seed = f"{fact.get('id', '')}:{target}:{fact.get('content', '')[:160]}"
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def candidate_from_fact(fact: dict[str, Any]) -> dict[str, Any] | None:
    if fact.get("status", "current") != "current":
        return None
    if fact.get("kind") not in PROMOTABLE_KINDS:
        return None
    if fact.get("confidence", 0) < MIN_CONFIDENCE:
        return None

    target = proposed_target(fact)
    return {
        "id": candidate_id(fact, target),
        "fact_id": fact.get("id", ""),
        "status": "pending",
        "source_fact_kind": fact.get("kind", ""),
        "proposed_target": target,
        "proposed_text": strip_tag(fact.get("content", "")),
        "confidence": fact.get("confidence", 0),
        "risk_level": risk_level(target, fact),
        "actions": ACTIONS,
        "evidence": fact.get("evidence", []),
    }


def candidates_from_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for fact in facts:
        candidate = candidate_from_fact(fact)
        if not candidate:
            continue
        key = (candidate["proposed_target"], candidate["proposed_text"].lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def render_markdown(candidates: list[dict[str, Any]]) -> str:
    lines = ["# Promotion Candidates", ""]
    if not candidates:
        lines.append("No candidates.")
        return "\n".join(lines) + "\n"

    for candidate in candidates:
        lines.extend([
            f"## {candidate['id']}",
            f"- Target: `{candidate['proposed_target']}`",
            f"- Risk: `{candidate['risk_level']}`",
            f"- Actions: {', '.join(candidate['actions'])}",
            f"- Text: {candidate['proposed_text']}",
            "- Evidence:",
        ])
        for ev in candidate.get("evidence", []):
            lines.append(f"  - {ev.get('path', '')}:{ev.get('line', '')}")
        lines.append("")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a human-gated promotion review queue")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Review output directory")
    args = parser.parse_args(argv)

    if not args.facts.exists():
        print(f"Facts file not found: {args.facts}")
        return 1

    candidates = candidates_from_facts(load_json(args.facts))
    secure_mkdir(args.output)
    secure_write_text(
        args.output / "promotion-candidates.json",
        json.dumps(candidates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    secure_write_text(
        args.output / "promotion-candidates.md",
        render_markdown(candidates),
        encoding="utf-8",
    )
    print(f"Wrote {len(candidates)} promotion candidate(s) to {args.output}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
