"""Tests for hard-deleting sensitive fact material across local stores."""
import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_purge_fact_apply_removes_pattern_from_facts_events_notes_and_vault(tmp_path):
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    (vault / "facts").mkdir(parents=True)

    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-leaky"}],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-keep"}],
        },
    ]))
    events.write_text(
        json.dumps({"id": "event-leaky", "content": "leaked-secret-value"}) + "\n" +
        json.dumps({"id": "event-keep", "content": "safe memory"}) + "\n"
    )
    (notes / "s1.md").write_text("- leaked-secret-value\n- safe memory\n")
    (vault / "facts" / "leaky.md").write_text("leaked-secret-value\n")
    (vault / "facts" / "keep.md").write_text("safe memory\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "removed 1 fact" in result.stdout
    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "leaked-secret-value" not in events.read_text()
    assert "event-keep" in events.read_text()
    assert "leaked-secret-value" not in (notes / "s1.md").read_text()
    assert "safe memory" in (vault / "facts" / "keep.md").read_text()
