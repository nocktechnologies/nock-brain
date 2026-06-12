"""Tests for schema-safe fact-store loading."""
import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def load_fact_store():
    spec = importlib.util.spec_from_file_location("fact_store", REPO / "bin" / "_facts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def valid_fact(fid="fact-1", content="Kevin chose schema validation"):
    return {
        "id": fid,
        "kind": "decision",
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": "2026-06-12",
        "evidence": [{"event_id": "event-1"}],
    }


def test_load_facts_skips_malformed_records_with_stderr_count(tmp_path, capsys):
    fact_store = load_fact_store()
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([
        valid_fact(),
        {"id": "missing-kind", "content": "bad"},
        "not a dict",
    ]))

    facts = fact_store.load_facts(facts_file)

    assert [fact["id"] for fact in facts] == ["fact-1"]
    assert "skipped 2 malformed fact" in capsys.readouterr().err
