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


def test_load_facts_non_utf8_returns_empty_not_raise(tmp_path, capsys):
    """N10024/N10030: a byte-corrupt store must not raise through the
    never-raises contract (UnicodeDecodeError is a ValueError)."""
    fact_store = load_fact_store()
    facts_file = tmp_path / "facts.json"
    facts_file.write_bytes(b"\xff\xfe not utf-8")

    assert fact_store.load_facts(facts_file) == []
    assert "skipped malformed fact store" in capsys.readouterr().err


def test_load_facts_truncated_json_returns_empty_not_raise(tmp_path, capsys):
    fact_store = load_fact_store()
    facts_file = tmp_path / "facts.json"
    facts_file.write_text("{")

    assert fact_store.load_facts(facts_file) == []
    assert "skipped malformed fact store" in capsys.readouterr().err


def test_filter_valid_facts_fills_source_date_from_source_time():
    """N10020: v2 claims omit source_date; recall requires it to rank."""
    fact_store = load_fact_store()
    rows = fact_store.filter_valid_facts(
        [{
            "id": "v2-1",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin approved the bounded memory build",
            "source_time": "2026-08-04T15:00:00.000000Z",
            "evidence": [],
        }],
        required_fields=fact_store.RECALL_ITEM_FIELDS,
    )
    assert len(rows) == 1
    assert rows[0]["source_date"] == "2026-08-04"
