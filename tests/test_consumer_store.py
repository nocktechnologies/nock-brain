"""Synthetic customer-store boundary and publication tests."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _consumer_store as cs
import _sign


def candidate(manifest, content="A chosen fact", *, kind="decision"):
    store_id = manifest["store_id"]
    source_hash = hashlib.sha256(b"synthetic input").hexdigest()
    return {
        "id": "customer-" + hashlib.sha256(cs.canonical_bytes([store_id, kind, content])).hexdigest(),
        "kind": kind,
        "content": content,
        "confidence": 0.9,
        "scope": "global",
        "status": "current",
        "source": "customer:" + store_id,
        "source_file": "notes.md",
        "source_date": "2026-09-26",
        "created_at": "2026-09-26T12:00:00+00:00",
        "subject": "customer",
        "evidence": [{"store_id": store_id, "key_id": manifest["key_id"],
                      "sha256": source_hash, "path": "notes.md", "line": 1,
                      "event_id": source_hash + ":1"}],
    }


def proposal(store, candidates):
    return store.save_proposal(candidates,
                               [{"path": "notes.md", "sha256": hashlib.sha256(b"synthetic input").hexdigest(),
                                 "format": "markdown"}],
                               {"files": 1, "candidates": len(candidates), "overlong_skipped": 0})


def test_fresh_distinct_stores_and_existing_destinations(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    a = cs.init_store(first)
    b = cs.init_store(second)
    assert a["store_id"] != b["store_id"]
    assert a["key_id"] != b["key_id"]
    for path in (first, second):
        assert path.stat().st_mode & 0o077 == 0
        assert (path / "facts.json").stat().st_mode & 0o077 == 0
        with cs.ConsumerStore(path) as store:
            assert store.facts == []
    with pytest.raises(cs.ConsumerError):
        cs.init_store(first)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")
    with pytest.raises(cs.ConsumerError):
        cs.init_store(dangling)
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir(mode=0o700)
    with pytest.raises(cs.ConsumerError):
        cs.ConsumerStore(incomplete).__enter__()


def test_proposal_apply_signatures_old_attestations_and_idempotency(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        one = candidate(manifest)
        digest = proposal(store, [one])
        assert store.read_proposal(digest)["candidates"] == [one]
        assert store.apply_proposal(digest) == {"added": 1, "skipped": 0, "total": 1}
        old = json.loads((path / "facts.json").read_text())[0]
        assert _sign.verify_fact(old, store._key) == _sign.VALID
        assert old["attestation"]["key_id"] == manifest["key_id"]
        with pytest.raises(cs.ConsumerError, match="stale"):
            store.apply_proposal(digest)
        second = candidate(manifest, "A second fact")
        digest2 = proposal(store, [one, second])
        assert store.apply_proposal(digest2) == {"added": 1, "skipped": 1, "total": 2}
        assert store.facts[0] == old
        digest3 = proposal(store, [one, second])
        assert store.apply_proposal(digest3) == {"added": 0, "skipped": 2, "total": 2}
        assert store.facts[0] == old


def test_tampered_proposal_stale_generation_and_failure_preserve_facts(tmp_path, monkeypatch):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        digest = proposal(store, [candidate(manifest)])
        proposal_path = path / "proposals" / (digest + ".json")
        proposal_path.write_bytes(proposal_path.read_bytes() + b" ")
        with pytest.raises(cs.ConsumerError, match="digest"):
            store.apply_proposal(digest)
        proposal_path.write_bytes(cs.canonical_bytes({"broken": True}))
        with pytest.raises(cs.ConsumerError):
            store.read_proposal(digest)
        proposal_path.unlink()
        digest = proposal(store, [candidate(manifest)])
        before = (path / "facts.json").read_bytes()
        def fail(*_args, **_kwargs):
            raise OSError("synthetic failure")
        monkeypatch.setattr(cs, "secure_replace_bytes", fail)
        with pytest.raises(cs.ConsumerError, match="publication"):
            store.apply_proposal(digest)
        assert (path / "facts.json").read_bytes() == before
    with cs.ConsumerStore(path) as store:
        digest = proposal(store, [candidate(manifest)])
        (path / "facts.json").write_bytes(b"[] ")
        with pytest.raises(cs.ConsumerError, match="changed"):
            store.apply_proposal(digest)


@pytest.mark.parametrize("mutation", [
    lambda fact: fact.update(content="changed"),
    lambda fact: fact.update(source="mira"),
    lambda fact: fact["evidence"][0].update(store_id="foreign"),
    lambda fact: fact.update(confidence=float("nan")),
    lambda fact: fact.update(machine="mac-kevin"),
    lambda fact: fact.update(id="customer-wrong"),
])
def test_invalid_candidates_fail_closed(tmp_path, mutation):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        fact = candidate(manifest)
        mutation(fact)
        with pytest.raises(cs.ConsumerError):
            proposal(store, [fact])
        with pytest.raises(cs.ConsumerError, match="duplicate"):
            proposal(store, [candidate(manifest), candidate(manifest)])


def test_invalid_owned_files_keys_lifecycle_and_symlinks(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        digest = proposal(store, [candidate(manifest)])
        store.apply_proposal(digest)
    facts = path / "facts.json"
    good = facts.read_bytes()
    tampered = json.loads(good)
    tampered[0]["content"] = "poison"
    facts.write_text(json.dumps(tampered))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    facts.write_bytes(good)
    lifecycle = path / "revocations.jsonl"
    lifecycle.write_text("event\n")
    lifecycle.chmod(0o600)
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    lifecycle.unlink()
    marker = path / "store-v2"
    marker.write_bytes(b"")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    marker.unlink()
    pub = path / "signing-key.pub"
    original = pub.read_bytes()
    # HMAC deliberately shares the same secret document in both key files.
    # An unexpected private-key field is invalid for either supported algorithm.
    malformed_public = json.loads(original)
    malformed_public["private_key"] = "0" * 64
    pub.write_text(json.dumps(malformed_public))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    pub.write_bytes(original)
    facts.chmod(0o644)
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    facts.chmod(0o600)
    hardlink = tmp_path / "other-link"
    os.link(facts, hardlink)
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    hardlink.unlink()
    facts.unlink()
    facts.symlink_to(tmp_path / "missing")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_read_regular_special_and_limit_and_environment_isolation(tmp_path, monkeypatch):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(cs.ConsumerError):
        cs.read_regular(fifo, 10)
    source = tmp_path / "notes"
    source.write_bytes(b"abc")
    source.chmod(0o644)
    assert cs.read_regular(source, 3) == b"abc"
    with pytest.raises(cs.ConsumerError):
        cs.read_regular(source, 2)
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(source))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY_PUB", str(source))
    monkeypatch.setenv("NOCKBRAIN_STORE", "sqlite")
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    with cs.ConsumerStore(path) as store:
        digest = proposal(store, [candidate(manifest)])
        assert store.apply_proposal(digest)["added"] == 1
    assert source.read_bytes() == b"abc"


def test_proposal_and_fact_limits(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        with pytest.raises(cs.ConsumerError):
            proposal(store, [candidate(manifest, "x" * 1501)])
        with pytest.raises(cs.ConsumerError):
            proposal(store, [candidate(manifest, str(i)) for i in range(1001)])
        with pytest.raises(cs.ConsumerError):
            store.read_proposal("../facts.json")


def test_provenance_binding_and_immutable_proposal_files(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        fact = candidate(manifest)
        fact["evidence"][0]["sha256"] = "a" * 64
        with pytest.raises(cs.ConsumerError, match="source receipt"):
            proposal(store, [fact])
        digest = proposal(store, [candidate(manifest)])
        proposal_path = path / "proposals" / (digest + ".json")
        original = proposal_path.read_bytes()
        assert proposal(store, [candidate(manifest)]) == digest
        assert proposal_path.read_bytes() == original
        hardlink = tmp_path / "linked-proposal"
        os.link(proposal_path, hardlink)
        with pytest.raises(cs.ConsumerError, match="unsafe"):
            store.read_proposal(digest)
        hardlink.unlink()
        proposal_path.unlink()
        proposal_path.symlink_to(tmp_path / "missing")
        with pytest.raises(cs.ConsumerError):
            store.read_proposal(digest)


def test_publication_skip_and_manifest_failure_preserve_authoritative_bytes(tmp_path, monkeypatch):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        digest = proposal(store, [candidate(manifest)])
        before = (path / "facts.json").read_bytes()
        monkeypatch.setattr(cs, "secure_replace_bytes", lambda *_args, **_kwargs: False)
        with pytest.raises(cs.ConsumerError):
            store.apply_proposal(digest)
        assert (path / "facts.json").read_bytes() == before
    manifest_path = path / "customer.json"
    altered = dict(manifest, key_id="foreign-key")
    manifest_path.write_bytes(cs.canonical_bytes(altered))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_lock_is_persistent_and_private(tmp_path):
    path = tmp_path / "store"
    cs.init_store(path)
    with cs.ConsumerStore(path):
        lock = path / ".customer.lock"
        inode = lock.stat().st_ino
        assert lock.stat().st_mode & 0o077 == 0
    assert lock.stat().st_ino == inode
    with cs.ConsumerStore(path):
        assert lock.stat().st_ino == inode


def test_absolute_sanitized_source_and_multiline_control_content(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    text = "Decision line one\nline two\twith escape \x1b[31m"
    fact = candidate(manifest, text)
    selected = str(tmp_path / "chosen-notes.md")
    fact["source_file"] = "chosen-notes.md"
    fact["evidence"][0]["path"] = selected
    receipt = {"path": selected, "sha256": fact["evidence"][0]["sha256"],
               "format": "markdown"}
    with cs.ConsumerStore(path) as store:
        digest = store.save_proposal([fact], [receipt],
                                     {"files": 1, "candidates": 1, "overlong_skipped": 0})
        assert store.read_proposal(digest)["candidates"][0]["content"] == text
        assert store.apply_proposal(digest)["added"] == 1


def test_malformed_json_and_collection_fields_raise_consumer_error(tmp_path):
    for data in (b'{"number":1e999}', b'{"number":NaN}', b'{"x":1,"x":2}'):
        with pytest.raises(cs.ConsumerError):
            cs._json(data)
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        bad_source = {"path": "/tmp/notes.md", "sha256": "a" * 64, "format": []}
        with pytest.raises(cs.ConsumerError):
            store.save_proposal([], [bad_source],
                                {"files": 1, "candidates": 0, "overlong_skipped": 0})
    (path / "customer.json").write_bytes(cs.canonical_bytes(dict(manifest, algorithm=[])))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_public_observations_cannot_change_additive_base_or_identity(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        first = candidate(manifest, "first fact")
        store.apply_proposal(proposal(store, [first]))
        old = store.facts[0]
        generation = store.generation

        store.facts.clear()
        store.facts[0]["content"] = "poison"
        store.facts[0]["evidence"][0]["store_id"] = "foreign"
        store.manifest["store_id"] = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(AttributeError):
            store.generation = "0" * 64
        with pytest.raises(AttributeError):
            store.manifest = {}
        with pytest.raises(AttributeError):
            store.facts = []
        with pytest.raises(AttributeError):
            store.path = tmp_path / "other-store"

        assert store.facts == [old]
        assert store.manifest == manifest
        assert store.generation == generation
        second = candidate(manifest, "second fact")
        assert store.apply_proposal(proposal(store, [second])) == {
            "added": 1, "skipped": 0, "total": 2}
        assert store.facts[0] == old
    with cs.ConsumerStore(path) as reopened:
        assert len(reopened.facts) == 2
        assert reopened.facts[0] == old
        assert reopened.manifest == manifest


@pytest.mark.parametrize("change", [
    lambda att: att.update(fact_id="customer-wrong"),
    lambda att: att.update(signed_at="bogus"),
    lambda att: att.update(canonical_fact_hash="abc"),
    lambda att: att.update(source_hash=7),
    lambda att: att.update(signature="f"),
    lambda att: att.update(parent_fact_ids=["foreign-parent"]),
    lambda att: att.update(extra="unsigned metadata"),
])
def test_malformed_v1_attestation_metadata_fails_closed(tmp_path, change):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        store.apply_proposal(proposal(store, [candidate(manifest)]))
    facts_path = path / "facts.json"
    facts = json.loads(facts_path.read_bytes())
    change(facts[0]["attestation"])
    facts_path.write_bytes(cs.canonical_bytes(facts))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


@pytest.mark.parametrize("key_name", ["signing-key", "signing-key.pub"])
@pytest.mark.parametrize("bad_document", [
    lambda data: data.rstrip()[:-1] + b', "extra": NaN}',
    lambda data: data.rstrip()[:-1] + b', "extra": 1e999}',
    lambda data: data.rstrip()[:-1] + b', "alg": "hmac-sha256"}',
    lambda data: b'{"alg":',
])
def test_captured_key_json_is_strict(tmp_path, key_name, bad_document):
    path = tmp_path / "store"
    cs.init_store(path)
    key_path = path / key_name
    key_path.write_bytes(bad_document(key_path.read_bytes()))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


@pytest.mark.parametrize("key_name", ["signing-key", "signing-key.pub"])
def test_captured_key_document_shape_and_material_length(tmp_path, key_name):
    path = tmp_path / "store"
    cs.init_store(path)
    key_path = path / key_name
    original = key_path.read_bytes()
    doc = json.loads(original)
    material_field = next(field for field in ("private_key", "public_key", "secret") if field in doc)
    for mutation in ({"extra": "ignored"}, {material_field: "00"}, {"key_id": "foreign"}):
        key_path.write_bytes(cs.canonical_bytes(dict(doc, **mutation)))
        with pytest.raises(cs.ConsumerError):
            with cs.ConsumerStore(path):
                pass
    key_path.write_bytes(original)
    with cs.ConsumerStore(path):
        pass


@pytest.mark.parametrize("algorithm", ["ed25519", "hmac-sha256"])
def test_both_key_algorithms_construct_from_capture_and_reject_mismatch(tmp_path, monkeypatch, algorithm):
    if algorithm == "ed25519" and not _sign._HAVE_CRYPTOGRAPHY:
        pytest.skip("cryptography unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(_sign, "_HAVE_CRYPTOGRAPHY", algorithm == "ed25519")
        path = tmp_path / "store"
        other = tmp_path / "other"
        manifest = cs.init_store(path)
        cs.init_store(other)
        assert manifest["algorithm"] == algorithm
        with cs.ConsumerStore(path) as store:
            digest = proposal(store, [candidate(manifest)])
            assert store.apply_proposal(digest)["added"] == 1
        public_path = path / "signing-key.pub"
        public_doc = json.loads((other / "signing-key.pub").read_bytes())
        public_doc["key_id"] = manifest["key_id"]
        public_path.write_bytes(cs.canonical_bytes(public_doc))
        with pytest.raises(cs.ConsumerError):
            with cs.ConsumerStore(path):
                pass


@pytest.mark.parametrize("key_name", ["signing-key", "signing-key.pub"])
def test_key_leaf_swap_after_capture_never_reopens_outside_path(tmp_path, monkeypatch, key_name):
    path = tmp_path / "store"
    cs.init_store(path)
    key_path = path / key_name
    outside = tmp_path / "outside-key-copy"
    outside.write_bytes(key_path.read_bytes() + b" " * (cs.SMALL_LIMIT + 1))
    original_capture = cs.ConsumerStore._capture
    calls = 0

    def capture_then_swap(store):
        nonlocal calls
        captured = original_capture(store)
        calls += 1
        if calls == 1:
            key_path.unlink()
            key_path.symlink_to(outside)
        return captured

    monkeypatch.setattr(cs.ConsumerStore, "_capture", capture_then_swap)
    monkeypatch.setattr(_sign, "load_or_create_key", lambda *_a, **_k: pytest.fail("legacy private loader reopened path"))
    monkeypatch.setattr(_sign, "load_public_key", lambda *_a, **_k: pytest.fail("legacy public loader reopened path"))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    assert (path / "facts.json").read_bytes() == b"[]\n"


def test_existing_ed25519_key_fails_without_cryptography(tmp_path, monkeypatch):
    if not _sign._HAVE_CRYPTOGRAPHY:
        pytest.skip("cryptography unavailable")
    path = tmp_path / "store"
    cs.init_store(path)
    before = (path / "signing-key").read_bytes()
    monkeypatch.setattr(_sign, "_HAVE_CRYPTOGRAPHY", False)
    with pytest.raises(cs.ConsumerError, match="requires cryptography"):
        with cs.ConsumerStore(path):
            pass
    assert (path / "signing-key").read_bytes() == before
