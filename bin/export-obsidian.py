#!/usr/bin/env python3
"""Export NockBrain memory artifacts as an Obsidian-compatible vault.

The vault is a derived, auditable view. JSON stores remain the source of truth.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_copyfile, secure_mkdir, secure_write_text


def load_facts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def fact_note(fact: dict[str, Any]) -> str:
    evidence = fact.get("evidence", [])
    first = evidence[0] if evidence else {}
    source_anchor = f"{fact.get('source_file', '')}:{first.get('line', '')}".strip(":")
    lines = [
        "---",
        f"id: {fact.get('id', '')}",
        f"kind: {fact.get('kind', '')}",
        f"status: {fact.get('status', '')}",
        f"confidence: {fact.get('confidence', '')}",
        f"source_date: {fact.get('source_date', '')}",
        "---",
        "",
        f"# {fact.get('kind', 'fact').title()}",
        "",
        fact.get("content", ""),
        "",
        f"Source: {source_anchor}",
        "",
        "## Evidence",
    ]
    if evidence:
        for ev in evidence:
            lines.append(f"- {ev.get('path', '')}:{ev.get('line', '')}")
    else:
        lines.append("- No evidence recorded.")
    return "\n".join(lines) + "\n"


def write_fact_notes(facts: list[dict[str, Any]], vault: Path) -> list[Path]:
    facts_dir = vault / "facts"
    secure_mkdir(facts_dir)
    written = []
    for fact in facts:
        name = f"{fact.get('source_date', 'undated')}-{slugify(fact.get('id', 'fact'))}.md"
        path = facts_dir / name
        secure_write_text(path, fact_note(fact), encoding="utf-8")
        written.append(path)
    return written


def copy_markdown_dir(src: Path | None, dst: Path) -> int:
    secure_mkdir(dst)
    if not src or not src.exists():
        return 0
    count = 0
    for path in sorted(src.glob("*.md")):
        secure_copyfile(path, dst / path.name)
        count += 1
    return count


def write_index(vault: Path, fact_count: int, session_count: int, review_count: int) -> None:
    text = "\n".join([
        "# NockBrain Vault",
        "",
        f"- Facts: {fact_count}",
        f"- Sessions: {session_count}",
        f"- Review notes: {review_count}",
        "",
        "This vault is a derived audit view. Keep JSON stores as source of truth.",
        "",
        "## Folders",
        "- [[facts]]",
        "- [[sessions]]",
        "- [[review]]",
        "",
    ])
    secure_write_text(vault / "index.md", text, encoding="utf-8")


def export_vault(
    facts: list[dict[str, Any]],
    vault: Path,
    sessions: Path | None = None,
    review: Path | None = None,
) -> dict[str, int]:
    secure_mkdir(vault)
    fact_paths = write_fact_notes(facts, vault)
    session_count = copy_markdown_dir(sessions, vault / "sessions")
    review_count = copy_markdown_dir(review, vault / "review")
    write_index(vault, len(fact_paths), session_count, review_count)
    return {"facts": len(fact_paths), "sessions": session_count, "review": review_count}


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export NockBrain memory as an Obsidian vault")
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, default=None)
    parser.add_argument("--review", type=Path, default=None)
    parser.add_argument("--vault", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.facts.exists():
        print(f"Facts file not found: {args.facts}")
        return 1

    counts = export_vault(load_facts(args.facts), args.vault, args.sessions, args.review)
    print(f"Wrote vault to {args.vault}: {counts}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
