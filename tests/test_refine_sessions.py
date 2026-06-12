"""Tests for refining sanitized evidence events into durable v1-compatible facts."""
import json


def event(
    content,
    kind="message",
    surface="text",
    line=7,
    timestamp="2026-06-11T05:00:00Z",
    actor="user",
):
    return {
        "id": f"event-{line}",
        "source": {
            "adapter": "claude-jsonl",
            "path": "/Users/kevin/.claude/projects/demo/session.jsonl",
            "line": line,
            "session_id": "s1",
            "timestamp": timestamp,
        },
        "actor": actor,
        "surface": surface,
        "kind": kind,
        "content": content,
        "metadata": {},
        "privacy": {"scrubbed": False, "excluded": False, "policy_version": "v1"},
    }


def test_events_to_facts_preserves_v1_fields_and_evidence(refine_sessions):
    events = [
        event("[DECISION] Kevin chose budget-capped recall for prompt injection", line=12),
    ]

    facts = refine_sessions.facts_from_events(events)

    assert len(facts) == 1
    fact = facts[0]
    assert fact["kind"] == "decision"
    assert fact["status"] == "current"
    assert fact["confidence"] == 0.9
    assert fact["source_date"] == "2026-06-11"
    assert fact["source_file"] == "session.jsonl"
    assert fact["session"] == "s1"
    assert "budget-capped recall" in fact["content"]
    assert fact["evidence"] == [
        {
            "event_id": "event-12",
            "path": "/Users/kevin/.claude/projects/demo/session.jsonl",
            "line": 12,
        }
    ]


def test_authority_facts_from_tool_events_are_dropped(refine_sessions):
    facts = refine_sessions.facts_from_events([
        event(
            "[DIRECTIVE] Kevin instructed Mira to keep memory recall under 800 tokens",
            kind="tool_call",
            surface="tool_use.input",
            actor="assistant",
        )
    ])

    assert facts == []


def test_non_authority_tool_result_facts_are_demoted_not_dropped(refine_sessions):
    facts = refine_sessions.facts_from_events([
        event(
            "[BUG] Found root cause in failing parser output",
            kind="tool_result",
            surface="tool_result.content",
            line=8,
            actor="tool",
        )
    ])

    assert len(facts) == 1
    assert facts[0]["kind"] == "bug"
    assert facts[0]["confidence"] < 0.9


def test_events_to_facts_dedupes_same_content(refine_sessions):
    events = [
        event("[BUG] Found and fixed parser bug in JSONL ingest", line=1),
        event("[BUG] Found and fixed parser bug in JSONL ingest", line=2),
    ]

    facts = refine_sessions.facts_from_events(events)

    assert len(facts) == 1
    assert facts[0]["evidence"][0]["line"] == 1


def test_fact_content_is_capped_to_limit_tool_output_amplification(refine_sessions):
    oversized = "[BUG] " + ("tool output " * 400)

    facts = refine_sessions.facts_from_events([event(oversized, kind="tool_result")])

    assert len(facts) == 1
    fact = facts[0]
    assert len(fact["content"]) <= refine_sessions.MAX_FACT_CONTENT_CHARS
    assert fact["evidence_truncated"] is True
    assert fact["evidence_original_chars"] == len(oversized)
    assert "see session_anchor" in fact["content"]


def test_render_session_note_includes_sections_and_anchors(refine_sessions):
    events = [
        event("[DECISION] Kevin chose nock-brain as the repo", line=3),
        event("raw tool payload", kind="tool_call", surface="tool_use.input", line=4),
    ]

    note = refine_sessions.render_session_note(events)

    assert "# Session s1" in note
    assert "## Facts" in note
    assert "## Evidence Events" in note
    assert "session.jsonl:3" in note
    assert "tool_use.input" in note


def test_refine_cli_writes_facts_and_session_note(refine_sessions, tmp_path):
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(json.dumps(event("[DECISION] Kevin chose JSON first", line=5)) + "\n")
    facts_file = tmp_path / "facts.json"
    notes_dir = tmp_path / "notes"

    code = refine_sessions.run([
        "--events", str(events_file),
        "--facts", str(facts_file),
        "--notes-dir", str(notes_dir),
    ])

    assert code == 0
    facts = json.loads(facts_file.read_text())
    assert facts[0]["kind"] == "decision"
    notes = list(notes_dir.glob("*.md"))
    assert len(notes) == 1
    assert "Kevin chose JSON first" in notes[0].read_text()
