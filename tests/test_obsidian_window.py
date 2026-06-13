"""Tests for the Obsidian entity knowledge-graph export ("the human window").

These cover the graph wiring added on top of the flat facts/sessions/review
dump: entity folders, fact->entity wikilinks, entity->fact backlinks, #tags,
bracket neutralization, and the rebuilt index.
"""
import json


def fact(
    content="Kevin asked Mira to coordinate the NockCC dispatch and merge",
    *,
    fact_id="fact-1",
    kind="dispatch",
    status="current",
    line=5,
    session="s1",
):
    return {
        "id": fact_id,
        "kind": kind,
        "scope": "global",
        "status": status,
        "confidence": 0.9,
        "content": content,
        "source_file": "session.jsonl",
        "source_date": "2026-06-11",
        "session": session,
        "session_anchor": f"/tmp/session.jsonl:{line}",
        "created_at": "2026-06-11T00:00:00Z",
        "last_seen_at": "2026-06-11T00:00:00Z",
        "subject": "user",
        "evidence": [{"event_id": f"event-{line}", "path": "/tmp/session.jsonl", "line": line}],
    }


def _export(export_obsidian, tmp_path, facts):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "s1.md").write_text("# Session s1\n")
    vault = tmp_path / "vault"
    code = export_obsidian.run([
        "--facts", str(facts_file),
        "--sessions", str(sessions_dir),
        "--vault", str(vault),
    ])
    assert code == 0
    return vault


def test_entity_folders_created(export_obsidian, tmp_path):
    vault = _export(export_obsidian, tmp_path, [fact()])
    for folder in ("agents", "projects", "people", "concepts", "decisions"):
        assert (vault / folder).is_dir(), f"missing entity folder: {folder}"
    # Flat structure stays intact.
    for folder in ("facts", "sessions", "review"):
        assert (vault / folder).is_dir(), f"flat folder regressed: {folder}"


def test_fact_links_known_agent_and_agent_backlinks_fact(export_obsidian, tmp_path):
    vault = _export(export_obsidian, tmp_path, [fact()])

    fact_notes = list((vault / "facts").glob("*.md"))
    assert len(fact_notes) == 1
    fact_text = fact_notes[0].read_text()
    # Fact mentioning "Mira" gets a [[mira]] wikilink in its Links section.
    assert "[[mira]]" in fact_text
    assert "[[kevin]]" in fact_text  # person
    assert "[[nockcc]]" in fact_text  # project

    # The agent note exists and backlinks the fact.
    agent_note = vault / "agents" / "mira.md"
    assert agent_note.exists()
    agent_text = agent_note.read_text()
    fact_stem = fact_notes[0].name[:-3]
    assert f"[[{fact_stem}]]" in agent_text
    assert "## Mentioned in" in agent_text


def test_tags_present_for_kind_and_status(export_obsidian, tmp_path):
    vault = _export(export_obsidian, tmp_path, [fact(kind="dispatch", status="current")])
    fact_text = next((vault / "facts").glob("*.md")).read_text()
    assert "#dispatch" in fact_text
    assert "#status/current" in fact_text


def test_accidental_bash_brackets_are_not_live_wikilinks(export_obsidian, tmp_path):
    # Captured tool output: a bash test plus a wikilink-shaped string.
    captured = 'ran [[ -n $TG ]] && echo ok; see [[feedback_some_rule]] note'
    vault = _export(export_obsidian, tmp_path, [fact(content=captured)])
    fact_text = next((vault / "facts").glob("*.md")).read_text()

    # The literal text survives...
    assert "-n $TG" in fact_text
    assert "feedback_some_rule" in fact_text
    # ...but no live [[...]] / ]] / [[ pair from the CONTENT survives.
    # Split the file at "## Links" so generated wikilinks are excluded.
    body = fact_text.split("## Links", 1)[0]
    assert "[[" not in body
    assert "]]" not in body


def test_index_links_entity_folders(export_obsidian, tmp_path):
    vault = _export(export_obsidian, tmp_path, [fact()])
    index = (vault / "index.md").read_text()
    for link in ("[[agents]]", "[[projects]]", "[[people]]", "[[concepts]]", "[[decisions]]"):
        assert link in index, f"index missing {link}"
    # Existing flat links remain.
    for link in ("[[facts]]", "[[sessions]]", "[[review]]"):
        assert link in index


def test_decision_kinds_emit_decision_notes(export_obsidian, tmp_path):
    vault = _export(export_obsidian, tmp_path, [
        fact(fact_id="d1", kind="directive", content="Kevin set a standing order"),
        fact(fact_id="m1", kind="merge", content="merged a routine PR"),
    ])
    decision_notes = list((vault / "decisions").glob("*.md"))
    # Only the directive becomes a decision note; the merge does not.
    assert len(decision_notes) == 1
    text = decision_notes[0].read_text()
    assert "#decision" in text


def test_no_entities_does_not_break_export(export_obsidian, tmp_path):
    # Content with no known entities; concept still derives from kind.
    vault = _export(export_obsidian, tmp_path, [
        fact(fact_id="x1", kind="architecture", content="refactored an internal helper")
    ])
    fact_text = next((vault / "facts").glob("*.md")).read_text()
    assert "## Links" in fact_text
    # kind is emitted as a concept wikilink even with no other entity hits.
    assert "[[architecture]]" in fact_text
    assert (vault / "concepts" / "architecture.md").exists()
