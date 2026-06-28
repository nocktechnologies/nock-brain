"""Tests for the curated auto-memory ingestion (Finding A5).

Curated Markdown files (one durable fact each) are extracted into the fact store
as signed, high-confidence facts so recall can surface the roster/pricing/etc.
These tests cover frontmatter parsing, fact shape, idempotency, signature
validity, and that the MEMORY.md index is skipped.
"""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_sign():
    spec = importlib.util.spec_from_file_location("_sign", BIN / "_sign.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CURATED = """---
name: project_widget
description: "A widget fact for testing."
metadata:
  node_type: memory
  type: project
---

Body line one with a keyword: pineapple.
Body line two.
"""

INDEX = "# Memory Index\n\n- [thing](thing.md) — blah\n"


@pytest.fixture
def memdir(tmp_path):
    (tmp_path / "project_widget.md").write_text(CURATED, encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(INDEX, encoding="utf-8")
    return tmp_path


@pytest.fixture
def key_paths(tmp_path):
    sign = _load_sign()
    kp = tmp_path / "signing-key"
    pp = tmp_path / "signing-key.pub"
    sign.load_or_create_key(kp, pp, alg=sign.ALG_HMAC)  # deterministic-ish, no crypto dep
    return kp, pp


def _empty_store(tmp_path):
    p = tmp_path / "facts.json"
    p.write_text("[]", encoding="utf-8")
    return p


def test_parse_frontmatter(ingest_curated_memory):
    fm, body = ingest_curated_memory.parse_frontmatter(CURATED)
    assert fm["name"] == "project_widget"
    assert fm["description"] == "A widget fact for testing."
    assert fm["metadata.type"] == "project"
    assert "pineapple" in body
    assert not body.startswith("---")


def test_build_fact_shape(ingest_curated_memory, memdir):
    fact = ingest_curated_memory.build_fact(memdir / "project_widget.md")
    # Required recall/store fields present.
    for field in ("id", "kind", "status", "confidence", "content", "source_date", "evidence"):
        assert field in fact
    assert fact["id"].startswith("curated-")
    assert fact["confidence"] >= 0.9
    assert fact["status"] == "current"
    assert fact["source"] == "curated-memory"
    assert fact["kind"] == "architecture"  # project -> architecture (durable)
    assert "pineapple" in fact["content"]
    assert fact["curated_type"] == "project"


def test_ingest_skips_index_and_signs(ingest_curated_memory, memdir, key_paths, tmp_path):
    kp, pp = key_paths
    store = _empty_store(tmp_path)
    result = ingest_curated_memory.ingest(memdir, store, key_path=kp, pub_path=pp)
    assert result["ingested"] == 1  # MEMORY.md skipped
    assert result["verify_statuses"] == ["valid"]

    facts = json.loads(store.read_text())
    assert len(facts) == 1
    sign = _load_sign()
    key = sign.load_or_create_key(kp, pp, create=False)
    fbid = {f["id"]: f for f in facts}
    assert sign.verify_fact(facts[0], key, facts_by_id=fbid) == sign.VALID
    assert facts[0]["attestation"]["alg"] == key.alg


def test_ingest_is_idempotent(ingest_curated_memory, memdir, key_paths, tmp_path):
    kp, pp = key_paths
    store = _empty_store(tmp_path)
    ingest_curated_memory.ingest(memdir, store, key_path=kp, pub_path=pp)
    r2 = ingest_curated_memory.ingest(memdir, store, key_path=kp, pub_path=pp)
    assert r2["removed_prior_curated"] == 1
    assert r2["ingested"] == 1
    facts = json.loads(store.read_text())
    assert len([f for f in facts if f["id"].startswith("curated-")]) == 1  # no dupes


def test_ingest_preserves_non_curated_facts(ingest_curated_memory, memdir, key_paths, tmp_path):
    kp, pp = key_paths
    store = tmp_path / "facts.json"
    pre = {"id": "abc123", "kind": "decision", "status": "current",
           "confidence": 0.8, "content": "keep me", "source_date": "2026-06-01",
           "evidence": []}
    store.write_text(json.dumps([pre]), encoding="utf-8")
    ingest_curated_memory.ingest(memdir, store, key_path=kp, pub_path=pp)
    facts = json.loads(store.read_text())
    ids = {f["id"] for f in facts}
    assert "abc123" in ids  # pre-existing non-curated fact untouched
    assert any(i.startswith("curated-") for i in ids)
