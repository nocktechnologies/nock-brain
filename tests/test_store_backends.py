"""Contract tests for the store backends (E2 P1/P2): every behavior asserted
once, proven against BOTH JsonStore and SqliteStore. The bar is value-identity,
not byte-identity — attestations sign values, so a lossless value round-trip is
what keeps a signed store verifying without any re-signing."""
import json
import os
import stat
import sys

import pytest

RICH_FACT = {
    "id": "f-rich", "kind": "decision", "status": "current", "confidence": 0.9,
    "content": "unicode café ✓ with \"quotes\" and\nnewlines",
    "source_date": "2026-06-01",
    "evidence": [{"path": "s.md", "line": 3, "nested": {"deep": [1, 2]}}],
    "future_unknown_field": {"anything": ["survives", 42, None]},
    "explicit_null": None,
    "boolean_flag": True,
}
PLAIN_FACT = {
    "id": "f-plain", "kind": "bug", "status": "current", "confidence": 0.7,
    "content": "plain fact with no optional fields", "source_date": "2026-06-02",
    "evidence": [],
}


def _store_pair(storeback, tmp_path, backend):
    facts_path = tmp_path / "facts.json"
    if backend == "json":
        return storeback.JsonStore(facts_path)
    store = storeback.SqliteStore(tmp_path / "brain.db")
    store.create(store_uuid="test-uuid")
    return store


@pytest.fixture(params=["json", "sqlite"])
def any_store(request, storeback, tmp_path):
    return _store_pair(storeback, tmp_path, request.param)


# ── the contract, both backends ──────────────────────────────────────────────
def test_roundtrip_is_value_identical(any_store):
    facts = [dict(RICH_FACT), dict(PLAIN_FACT)]
    any_store.replace_all(facts)
    loaded = any_store.load_facts()
    assert {f["id"]: f for f in loaded} == {f["id"]: f for f in facts}


def test_absent_optional_fields_stay_absent(any_store):
    any_store.replace_all([dict(PLAIN_FACT)])
    loaded = any_store.load_facts()[0]
    for never_set in ("valid_at", "invalid_at", "superseded_by", "attestation"):
        assert never_set not in loaded


def test_replace_all_replaces_not_appends(any_store):
    any_store.replace_all([dict(RICH_FACT), dict(PLAIN_FACT)])
    any_store.replace_all([dict(PLAIN_FACT)])
    assert [f["id"] for f in any_store.load_facts()] == ["f-plain"]


def test_required_fields_filtering_matches_json_semantics(any_store, capsys):
    malformed = {"id": "broken", "content": "missing everything else"}
    any_store.replace_all([dict(PLAIN_FACT), malformed])
    loaded = any_store.load_facts(required_fields={"id", "kind", "content", "status",
                                                  "confidence", "source_date", "evidence"})
    assert [f["id"] for f in loaded] == ["f-plain"]
    assert "skipped 1 malformed" in capsys.readouterr().err


def test_signed_store_verifies_after_roundtrip(any_store, sign_lib, tmp_path):
    key = sign_lib.load_or_create_key(
        tmp_path / "k", tmp_path / "k.pub", alg=sign_lib.ALG_HMAC
    )
    facts = [dict(RICH_FACT), dict(PLAIN_FACT)]
    sign_lib.sign_facts(facts, key)
    any_store.replace_all(facts)
    result = sign_lib.verify_facts(any_store.load_facts(), key)
    assert result["valid"] == result["total"] == 2


def test_snapshot_is_loadable_copy(any_store, storeback, tmp_path):
    any_store.replace_all([dict(PLAIN_FACT)])
    dest = tmp_path / "snaps" / ("copy.db" if any_store.kind == "sqlite" else "copy.json")
    any_store.snapshot(dest)
    copy = (storeback.SqliteStore(dest) if any_store.kind == "sqlite"
            else storeback.JsonStore(dest))
    assert [f["id"] for f in copy.load_facts()] == ["f-plain"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_export_facts_json_reads_back_through_json_store(any_store, storeback, tmp_path):
    facts = [dict(RICH_FACT)]
    any_store.replace_all(facts)
    dest = tmp_path / "export" / "facts.json"
    any_store.export_facts_json(dest)
    assert storeback.JsonStore(dest).load_facts() == facts


def test_store_files_are_private(any_store):
    any_store.replace_all([dict(PLAIN_FACT)])
    assert stat.S_IMODE(any_store.freshness_path.stat().st_mode) == 0o600


# ── backend selection ────────────────────────────────────────────────────────
def test_default_is_json_even_when_db_exists(storeback, tmp_path):
    (tmp_path / "facts.json").write_text("[]")
    storeback.SqliteStore(tmp_path / "brain.db").create()
    store = storeback.resolve_store(tmp_path / "facts.json", env={})
    assert store.kind == "json"


def test_marker_plus_db_selects_sqlite(storeback, tmp_path):
    storeback.SqliteStore(tmp_path / "brain.db").create()
    (tmp_path / "store-v2").touch()
    store = storeback.resolve_store(tmp_path / "facts.json", env={})
    assert store.kind == "sqlite"


def test_marker_without_db_stays_json(storeback, tmp_path):
    (tmp_path / "store-v2").touch()
    assert storeback.resolve_store(tmp_path / "facts.json", env={}).kind == "json"


def test_env_sqlite_selects_sqlite(storeback, tmp_path):
    store = storeback.resolve_store(tmp_path / "facts.json",
                                    env={"NOCKBRAIN_STORE": "sqlite"})
    assert store.kind == "sqlite"


def test_env_json_is_the_kill_switch(storeback, tmp_path):
    storeback.SqliteStore(tmp_path / "brain.db").create()
    (tmp_path / "store-v2").touch()  # cutover happened...
    store = storeback.resolve_store(tmp_path / "facts.json",
                                    env={"NOCKBRAIN_STORE": "json"})
    assert store.kind == "json"  # ...and one env var rolls it back


# ── tool integration: a mutator honors the selected backend ──────────────────
def test_supersede_writes_to_sqlite_when_selected(storeback, tmp_path, monkeypatch):
    """With the cutover marker set, supersede-fact must mutate brain.db and
    leave facts.json untouched — proof the mutators ride the contract."""
    import importlib.util
    from pathlib import Path

    facts = [dict(PLAIN_FACT)]
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    db = storeback.SqliteStore(tmp_path / "brain.db")
    db.create()
    db.replace_all(facts)
    (tmp_path / "store-v2").touch()
    json_before = (tmp_path / "facts.json").read_bytes()

    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    spec = importlib.util.spec_from_file_location("supersede_fact", bin_dir / "supersede-fact.py")
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    monkeypatch.setattr(sys, "argv",
                        ["supersede-fact.py", "f-plain", "--by", "newer",
                         "--facts", str(tmp_path / "facts.json")])
    try:
        sf.main()
    except SystemExit:
        pass

    assert (tmp_path / "facts.json").read_bytes() == json_before
    marked = db.load_facts()[0]
    assert marked["status"] == "superseded"
    assert marked["superseded_by"] == "newer"
