#!/usr/bin/env python3
"""Export NockBrain memory artifacts as an Obsidian-compatible vault.

The vault is a derived, auditable view. JSON stores remain the source of truth.

Beyond the flat facts/sessions/review dump, the vault is wired as a real
entity knowledge graph ("the human window"): facts link to the agents,
projects, people, and concepts they mention, and each entity note backlinks
the facts that reference it. Fact content is captured from raw tool output,
so accidental bracket pairs (e.g. bash ``[[ -n $x ]]`` tests) are neutralized
before rendering so Obsidian does not interpret them as live wikilinks.
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


# --- Known-entity registry -------------------------------------------------
# Deliberately a small curated registry, not NLP. Matched case-insensitively
# against fact content on word boundaries. The canonical (lowercase) name is
# the wikilink target / note filename; the entity note title is title-cased.
KNOWN_AGENTS = [
    "mira", "mar", "kit", "wren", "mason", "cooper", "beck", "rook", "iris",
    "holden", "pierce", "ellis", "scout", "herald", "hollis", "warden", "ash",
    "vale", "hammer", "forge", "alastair", "talon", "refine", "crane", "slate",
    "kimi", "smith", "tinker",
]
KNOWN_PROJECTS = [
    "nockcc", "nocklock", "nockguard", "nockbrain", "nock-brain", "jobcost",
    "nexus", "meridian", "fulcrum", "voice", "terminal", "beltline",
]
KNOWN_PEOPLE = ["kevin", "keith"]

# Concept vocabulary for the light keyword pass. Fact `kind` is always emitted
# as a concept too (see entities_for_fact), so this list is the keyword layer.
CONCEPT_KEYWORDS = [
    "security", "dispatch", "merge", "identity", "provenance", "recall",
    "audit", "handoff", "telegram", "privacy", "decision", "directive",
    "correction", "architecture", "bug", "test", "deploy",
]

# kinds that are surfaced as decision notes (and tagged as such).
DECISION_KINDS = {"decision", "directive", "correction"}

ENTITY_KIND_FOLDER = {
    "agent": "agents",
    "project": "projects",
    "person": "people",
    "concept": "concepts",
}


def load_facts(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def neutralize_brackets(text: str) -> str:
    """Defuse accidental wikilink/bracket pairs captured from tool output.

    Fact content is raw — it routinely contains bash ``[[ -n $x ]]`` tests and
    ``[[wikilink]]``-shaped strings copied from other docs. Left untouched,
    Obsidian renders these as live (broken) links. We split every ``[[`` and
    ``]]`` pair with a zero-noise space so the literal text survives but no
    wikilink is created. Real graph links live only in the generated
    "## Links" / "## Mentioned in" sections, never in rendered content.
    """
    return text.replace("[[", "[ [").replace("]]", "] ]")


def _word_boundary_pattern(name: str) -> re.Pattern[str]:
    # Allow internal hyphens (e.g. "nock-brain") while still anchoring on
    # word-ish boundaries so "scout" does not match "scoutmaster".
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


_AGENT_PATTERNS = {name: _word_boundary_pattern(name) for name in KNOWN_AGENTS}
_PROJECT_PATTERNS = {name: _word_boundary_pattern(name) for name in KNOWN_PROJECTS}
_PEOPLE_PATTERNS = {name: _word_boundary_pattern(name) for name in KNOWN_PEOPLE}
_CONCEPT_PATTERNS = {name: _word_boundary_pattern(name) for name in CONCEPT_KEYWORDS}


def entities_for_fact(fact: dict[str, Any]) -> dict[str, list[str]]:
    """Return the de-duplicated known entities a fact mentions, by kind.

    Keys: "agent", "project", "person", "concept". Each value is an ordered,
    de-duplicated list of canonical (lowercase) entity names.
    """
    content = fact.get("content", "") or ""

    def matches(patterns: dict[str, re.Pattern[str]]) -> list[str]:
        return [name for name, pat in patterns.items() if pat.search(content)]

    concepts = matches(_CONCEPT_PATTERNS)
    kind = (fact.get("kind", "") or "").lower()
    # The fact kind is itself a first-class concept; keep order stable and unique.
    if kind and kind not in concepts:
        concepts.insert(0, kind)

    return {
        "agent": matches(_AGENT_PATTERNS),
        "project": matches(_PROJECT_PATTERNS),
        "person": matches(_PEOPLE_PATTERNS),
        "concept": concepts,
    }


def is_decision(fact: dict[str, Any]) -> bool:
    return (fact.get("kind", "") or "").lower() in DECISION_KINDS


def fact_note_name(fact: dict[str, Any]) -> str:
    return f"{fact.get('source_date', 'undated')}-{slugify(fact.get('id', 'fact'))}.md"


def fact_link_target(fact: dict[str, Any]) -> str:
    """Wikilink target for a fact note (filename stem, no extension)."""
    return fact_note_name(fact)[:-3]


def fact_note(fact: dict[str, Any], links: dict[str, list[str]]) -> str:
    evidence = fact.get("evidence", [])
    first = evidence[0] if evidence else {}
    source_anchor = f"{fact.get('source_file', '')}:{first.get('line', '')}".strip(":")
    kind = fact.get("kind", "fact")
    status = fact.get("status", "")

    lines = [
        "---",
        f"id: {fact.get('id', '')}",
        f"kind: {kind}",
        f"status: {status}",
        f"confidence: {fact.get('confidence', '')}",
        f"source_date: {fact.get('source_date', '')}",
        "---",
        "",
        f"# {str(kind).title()}",
        "",
        neutralize_brackets(fact.get("content", "") or ""),
        "",
        f"Source: {source_anchor}",
        "",
        "## Links",
    ]

    link_targets: list[str] = []
    for entity_kind in ("agent", "project", "person", "concept"):
        for name in links.get(entity_kind, []):
            link_targets.append(name)
    if link_targets:
        lines.append("Entities: " + " ".join(f"[[{name}]]" for name in link_targets))
    else:
        lines.append("Entities: none detected.")

    tags = [f"#{slugify(str(kind))}"] if kind else []
    if status:
        tags.append(f"#status/{slugify(str(status))}")
    if is_decision(fact):
        tags.append("#decision")
    lines.append("")
    lines.append("Tags: " + " ".join(tags) if tags else "Tags: none.")

    lines.append("")
    lines.append("## Evidence")
    if evidence:
        for ev in evidence:
            lines.append(f"- {ev.get('path', '')}:{ev.get('line', '')}")
    else:
        lines.append("- No evidence recorded.")
    return "\n".join(lines) + "\n"


def write_fact_notes(
    facts: list[dict[str, Any]],
    vault: Path,
    fact_links: dict[str, dict[str, list[str]]],
) -> list[Path]:
    facts_dir = vault / "facts"
    secure_mkdir(facts_dir)
    written = []
    for fact in facts:
        name = fact_note_name(fact)
        path = facts_dir / name
        links = fact_links.get(fact.get("id", ""), {})
        secure_write_text(path, fact_note(fact, links), encoding="utf-8")
        written.append(path)
    return written


def session_links_for_facts(facts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each canonical entity name -> sorted unique session ids mentioning it."""
    sessions: dict[str, set[str]] = {}
    for fact in facts:
        session = fact.get("session") or ""
        if not session:
            continue
        ents = entities_for_fact(fact)
        for names in ents.values():
            for name in names:
                sessions.setdefault(name, set()).add(session)
    return {name: sorted(ids) for name, ids in sessions.items()}


def build_entity_index(
    facts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[str]]]]:
    """Build the entity registry and the per-fact link map.

    Returns (entities, fact_links) where:
      entities[name] = {"name", "kind", "facts": [fact_link_target, ...]}
      fact_links[fact_id] = {"agent": [...], "project": [...], ...}
    """
    entities: dict[str, dict[str, Any]] = {}
    fact_links: dict[str, dict[str, list[str]]] = {}

    for fact in facts:
        ents = entities_for_fact(fact)
        fact_links[fact.get("id", "")] = ents
        target = fact_link_target(fact)
        for entity_kind, names in ents.items():
            for name in names:
                record = entities.setdefault(
                    name, {"name": name, "kind": entity_kind, "facts": []}
                )
                if target not in record["facts"]:
                    record["facts"].append(target)
    return entities, fact_links


def entity_note(entity: dict[str, Any], session_ids: list[str]) -> str:
    name = entity["name"]
    kind = entity["kind"]
    lines = [
        "---",
        f"entity: {name}",
        f"type: {kind}",
        f"fact_count: {len(entity['facts'])}",
        "---",
        "",
        f"# {name.replace('-', ' ').title()}",
        "",
        f"Type: {kind}",
        "",
        "## Mentioned in",
    ]
    if entity["facts"]:
        for fact_target in entity["facts"]:
            lines.append(f"- [[{fact_target}]]")
    else:
        lines.append("- No facts reference this entity.")
    lines.append("")
    lines.append("## Sessions")
    if session_ids:
        for session in session_ids:
            lines.append(f"- [[{session}]]")
    else:
        lines.append("- No sessions reference this entity.")
    return "\n".join(lines) + "\n"


def write_entity_notes(
    entities: dict[str, dict[str, Any]],
    vault: Path,
    session_links: dict[str, list[str]],
) -> dict[str, int]:
    counts = {folder: 0 for folder in ENTITY_KIND_FOLDER.values()}
    # Pre-create folders so the vault structure is stable even with zero hits.
    for folder in ENTITY_KIND_FOLDER.values():
        secure_mkdir(vault / folder)
    for entity in entities.values():
        folder = ENTITY_KIND_FOLDER[entity["kind"]]
        path = vault / folder / f"{slugify(entity['name'])}.md"
        secure_write_text(
            path, entity_note(entity, session_links.get(entity["name"], [])), encoding="utf-8"
        )
        counts[folder] += 1
    return counts


def write_decision_notes(facts: list[dict[str, Any]], vault: Path) -> int:
    """Emit a decisions/ folder: one note per decision/directive/correction fact."""
    decisions_dir = vault / "decisions"
    secure_mkdir(decisions_dir)
    count = 0
    for fact in facts:
        if not is_decision(fact):
            continue
        target = fact_link_target(fact)
        kind = fact.get("kind", "decision")
        text = "\n".join([
            "---",
            f"id: {fact.get('id', '')}",
            f"kind: {kind}",
            f"source_date: {fact.get('source_date', '')}",
            "---",
            "",
            f"# {str(kind).title()}",
            "",
            f"See [[{target}]] for the full fact note.",
            "",
            neutralize_brackets((fact.get("content", "") or "")[:500]),
            "",
            "Tags: #decision " + f"#{slugify(str(kind))}",
        ]) + "\n"
        secure_write_text(decisions_dir / f"{target}.md", text, encoding="utf-8")
        count += 1
    return count


def copy_markdown_dir(src: Path | None, dst: Path) -> int:
    secure_mkdir(dst)
    if not src or not src.exists():
        return 0
    count = 0
    for path in sorted(src.glob("*.md")):
        secure_copyfile(path, dst / path.name)
        count += 1
    return count


def write_index(vault: Path, counts: dict[str, int]) -> None:
    lines = [
        "# NockBrain Vault",
        "",
        f"- Facts: {counts.get('facts', 0)}",
        f"- Sessions: {counts.get('sessions', 0)}",
        f"- Review notes: {counts.get('review', 0)}",
        f"- Decisions: {counts.get('decisions', 0)}",
        f"- Agents: {counts.get('agents', 0)}",
        f"- Projects: {counts.get('projects', 0)}",
        f"- People: {counts.get('people', 0)}",
        f"- Concepts: {counts.get('concepts', 0)}",
        "",
        "This vault is a derived audit view. Keep JSON stores as source of truth.",
        "",
        "## Folders",
        "- [[facts]]",
        "- [[sessions]]",
        "- [[review]]",
        "- [[decisions]]",
        "",
        "## Entities",
        "- [[agents]]",
        "- [[projects]]",
        "- [[people]]",
        "- [[concepts]]",
        "",
    ]
    secure_write_text(vault / "index.md", "\n".join(lines), encoding="utf-8")


def export_vault(
    facts: list[dict[str, Any]],
    vault: Path,
    sessions: Path | None = None,
    review: Path | None = None,
) -> dict[str, int]:
    secure_mkdir(vault)
    entities, fact_links = build_entity_index(facts)
    session_links = session_links_for_facts(facts)

    fact_paths = write_fact_notes(facts, vault, fact_links)
    session_count = copy_markdown_dir(sessions, vault / "sessions")
    review_count = copy_markdown_dir(review, vault / "review")
    entity_counts = write_entity_notes(entities, vault, session_links)
    decision_count = write_decision_notes(facts, vault)

    counts = {
        "facts": len(fact_paths),
        "sessions": session_count,
        "review": review_count,
        "decisions": decision_count,
        **entity_counts,
    }
    write_index(vault, counts)
    return counts


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
