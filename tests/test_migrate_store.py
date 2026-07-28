"""Tests for migrate-store.py and eval-store-parity.py (E2 P2): the fail-closed
JSON -> SQLite build and the parity bar cutover must clear."""
import json
import sys

import pytest

FACTS = [
    {"id": "a", "kind": "decision", "status": "current", "confidence": 0.9,
     "content": "cloudflare dns migration plan", "source_date": "2026-06-01",
     "evidence": [{"path": "s.md", "line": 1}]},
    {"id": "b", "kind": "bug", "status": "current", "confidence": 0.8,
     "content": "race condition in the session handler", "source_date": "2026-06-02",
     "evidence": [], "custom_field": {"keep": True}},
]


def _write_store(tmp_path, facts=None):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(FACTS if facts is None else facts))
    return path


# ── propose vs apply ─────────────────────────────────────────────────────────
def test_propose_reports_and_writes_nothing(migrate_store, tmp_path):
    facts_path = _write_store(tmp_path)
    receipt = migrate_store.run_migration(facts_path, apply=False)
    assert receipt["mode"] == "propose"
    assert receipt["facts"] == 2
    assert not (tmp_path / "brain.db").exists()


def test_apply_builds_db_and_leaves_json_untouched(migrate_store, storeback, tmp_path):
    facts_path = _write_store(tmp_path)
    before = facts_path.read_bytes()
    receipt = migrate_store.run_migration(facts_path, apply=True)

    assert facts_path.read_bytes() == before
    assert receipt["gates"]["hash_set_equal"] is True
    assert receipt["gates"]["value_identical"] is True
    db = storeback.SqliteStore(tmp_path / "brain.db")
    assert {f["id"]: f for f in db.load_facts()} == {f["id"]: f for f in FACTS}
    assert db.meta()["migrated_from_sha256"] == receipt["facts_json_sha256"]
    assert not (tmp_path / "brain.db.staging").exists()


def test_apply_is_idempotent(migrate_store, storeback, tmp_path):
    facts_path = _write_store(tmp_path)
    migrate_store.run_migration(facts_path, apply=True)
    migrate_store.run_migration(facts_path, apply=True)
    assert len(storeback.SqliteStore(tmp_path / "brain.db").load_facts()) == 2


# ── fail-closed gates ────────────────────────────────────────────────────────
def test_malformed_fact_refuses_migration(migrate_store, tmp_path):
    facts_path = _write_store(tmp_path, FACTS + [{"id": "broken"}])
    with pytest.raises(migrate_store.MigrationError, match="malformed"):
        migrate_store.run_migration(facts_path, apply=True)
    assert not (tmp_path / "brain.db").exists()


def test_duplicate_ids_refuse_migration(migrate_store, tmp_path):
    facts_path = _write_store(tmp_path, FACTS + [dict(FACTS[0])])
    with pytest.raises(migrate_store.MigrationError, match="duplicate"):
        migrate_store.run_migration(facts_path, apply=True)


def test_tampered_signed_store_refuses_migration(migrate_store, sign_lib, tmp_path, monkeypatch):
    facts = [dict(f) for f in FACTS]
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC
    )
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    sign_lib.sign_facts(facts, key)
    facts[0]["content"] = "poisoned after signing"
    facts_path = _write_store(tmp_path, facts)
    with pytest.raises(migrate_store.MigrationError, match="strict verify failed"):
        migrate_store.run_migration(facts_path, apply=True)
    assert not (tmp_path / "brain.db").exists()


def test_signed_store_migrates_with_verify_valid(migrate_store, sign_lib, tmp_path, monkeypatch):
    facts = [dict(f) for f in FACTS]
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC
    )
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    sign_lib.sign_facts(facts, key)
    facts_path = _write_store(tmp_path, facts)
    receipt = migrate_store.run_migration(facts_path, apply=True)
    assert receipt["signature_verify"] == "valid"


# ── parity harness ───────────────────────────────────────────────────────────
def test_parity_identical_on_clean_migration(migrate_store, store_parity, tmp_path):
    facts_path = _write_store(tmp_path)
    migrate_store.run_migration(facts_path, apply=True)
    result = store_parity.run_parity(facts_path, tmp_path / "brain.db", None, fuzz=10)
    assert result["identical"] is True
    assert result["queries_run"] >= 1
    assert result["checks"]["recall_parity"] is True


def test_parity_catches_db_drift(migrate_store, store_parity, tmp_path):
    import sqlite3
    facts_path = _write_store(tmp_path)
    migrate_store.run_migration(facts_path, apply=True)
    con = sqlite3.connect(tmp_path / "brain.db")
    con.execute("UPDATE facts SET content = 'silently rewritten' WHERE id = 'a'")
    con.commit(); con.close()

    result = store_parity.run_parity(facts_path, tmp_path / "brain.db", None, fuzz=10)
    assert result["identical"] is False
    assert result["checks"]["value_identical"] is False
    assert result["checks"]["hash_set_equal"] is False


def test_parity_compares_multisets_not_by_id(migrate_store, store_parity, tmp_path):
    """Duplicate ids on one side must not collapse before comparison — a
    changed record hiding behind a shared id has to surface as inequality."""
    facts_path = _write_store(tmp_path)
    migrate_store.run_migration(facts_path, apply=True)
    twin = dict(FACTS[0], custom_note="only in json twin")
    _write_store(tmp_path, FACTS + [twin])  # same id 'a' twice, different values

    result = store_parity.run_parity(facts_path, tmp_path / "brain.db", None, fuzz=0)
    assert result["identical"] is False
    assert result["checks"]["value_identical"] is False


def test_parity_suite_queries_are_used(migrate_store, store_parity, tmp_path):
    facts_path = _write_store(tmp_path)
    migrate_store.run_migration(facts_path, apply=True)
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps([["S1", "cloudflare dns migration", "id:a"]]))
    result = store_parity.run_parity(facts_path, tmp_path / "brain.db", suite, fuzz=0)
    assert result["queries_run"] == 1
    assert result["identical"] is True
