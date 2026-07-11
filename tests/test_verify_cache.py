"""Regression tests for the attestation-verification cache on the recall hot
path (bin/_verify_cache.py). Verifying ~2,500 Ed25519 signatures on every
recall added ~0.4-0.8s per invocation, most of the memory-inject hook's <2s
budget. The sidecar cache remembers already-proven signatures per store; these
tests pin its contract:

- a warm cache skips the signature operations (and ONLY those — the committed-
  hash comparisons still run, so tampering is caught even on a warm cache);
- any store mutation invalidates the cache via the (mtime_ns, size) guard;
- a tampered fact is still detected after invalidation — including the forged
  variant where the attacker recomputes the committed hashes and fakes the
  sidecar's freshness stat;
- --strict-verify semantics are unchanged: the cache only accelerates the
  VALID determination, never alters a status;
- any cache doubt (corrupt sidecar, missing key) fails closed to a full
  verification pass, never to skipped verification.
"""
import importlib
import json
import stat

import pytest


def signable_fact(fid, content, kind="decision", source_date="2026-07-01"):
    return {
        "id": fid,
        "kind": kind,
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": source_date,
        "evidence": [{"event_id": f"ev-{fid}", "path": "session.jsonl", "line": 1}],
    }


def write_facts(tmp_path, facts):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


def sidecar_for(facts_file):
    return facts_file.with_name(facts_file.name + ".verified-cache.json")


@pytest.fixture()
def signing_key(sign_lib, tmp_path, monkeypatch):
    """A real signing key in tmp, with budget-recall's key resolution pointed
    at it (overriding the conftest no-key isolation)."""
    key_path = tmp_path / "signing-key"
    pub_path = tmp_path / "signing-key.pub"
    key = sign_lib.load_or_create_key(key_path, pub_path)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(key_path))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(pub_path))
    return key


@pytest.fixture()
def verify_calls(budget_recall, monkeypatch):
    """Count signature operations on the recall hot path. budget-recall's lazy
    `import _sign` resolves through sys.modules (bin/ is on sys.path once the
    budget_recall fixture has loaded), so patching that instance's SigningKey
    class counts exactly the verify_bytes calls recall performs."""
    sign_hot = importlib.import_module("_sign")
    calls = {"n": 0}
    real = sign_hot.SigningKey.verify_bytes

    def counting(self, payload, signature_hex):
        calls["n"] += 1
        return real(self, payload, signature_hex)

    monkeypatch.setattr(sign_hot.SigningKey, "verify_bytes", counting)
    return calls


# --- the cache hit path -------------------------------------------------------
def test_cache_hit_skips_signature_verification(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls):
    facts = [signable_fact(f"f-{i}", f"ed25519 rollout note {i} approved")
             for i in range(3)]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)

    first = budget_recall.budget_recall("ed25519 rollout approved", facts_file)
    assert verify_calls["n"] == 3  # cold: one signature op per signed fact
    sidecar = sidecar_for(facts_file)
    assert sidecar.exists()
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    verify_calls["n"] = 0
    second = budget_recall.budget_recall("ed25519 rollout approved", facts_file)
    assert verify_calls["n"] == 0  # warm: zero signature ops
    assert second == first  # cache changes cost, never results


def test_no_signing_key_creates_no_sidecar(budget_recall, tmp_path):
    # conftest points key resolution at nonexistent paths -> verification (and
    # therefore caching) is off entirely.
    facts_file = write_facts(
        tmp_path, [signable_fact("f-1", "ed25519 rollout was approved")])
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "approved" in out
    assert not sidecar_for(facts_file).exists()


def test_unsigned_only_store_creates_no_sidecar(
        budget_recall, signing_key, tmp_path):
    # A key exists but nothing verifies VALID (e.g. insights.json today):
    # no digests to remember, so no sidecar churn.
    facts_file = write_facts(
        tmp_path, [signable_fact("f-1", "ed25519 rollout was approved")])
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "approved" in out
    assert not sidecar_for(facts_file).exists()


# --- store mutation invalidates ------------------------------------------------
def test_store_mutation_invalidates_cache(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls):
    facts = [signable_fact("f-1", "ed25519 rollout was approved"),
             signable_fact("f-2", "ed25519 rollout owner is mira")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm

    new = signable_fact("f-3", "ed25519 rollout gained a runbook")
    sign_lib.sign_fact(new, signing_key, facts_by_id={})
    write_facts(tmp_path, facts + [new])

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "runbook" in out
    # (mtime_ns, size) guard tripped -> the whole store re-verifies.
    assert verify_calls["n"] == 3

    verify_calls["n"] = 0
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert verify_calls["n"] == 0  # cache re-warmed under the new stat


def test_tampered_fact_detected_after_cache_invalidation(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls, capsys):
    good = signable_fact("f-good", "ed25519 rollout was approved for signing")
    bad = signable_fact("f-bad", "ed25519 rollout budget was zero dollars")
    sign_lib.sign_facts([good, bad], signing_key)
    facts_file = write_facts(tmp_path, [good, bad])
    budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm
    capsys.readouterr()

    # The F5 attack, now against a warm cache: edit content, keep attestation.
    bad["content"] = "ed25519 rollout budget was one million dollars"
    write_facts(tmp_path, [good, bad])

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert "million" not in out
    assert "approved for signing" in out
    assert "excluded 1 tampered" in err
    # The tampered fact fails the committed-hash comparison BEFORE any
    # signature work; only the intact fact reaches verify_bytes.
    assert verify_calls["n"] == 1


def test_forged_hashes_and_faked_guard_still_detected(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    """The strongest forgery short of rewriting the sidecar digests: tamper the
    content, RECOMPUTE the attestation's committed hashes so the fact
    self-hashes clean, keep the stale signature, and copy the store's current
    stat into the sidecar so the freshness guard passes. The cached digest
    binds the signed payload (which embeds the committed hashes), so the doctored
    fact misses the cache, gets a real verification, and fails it."""
    good = signable_fact("f-good", "ed25519 rollout was approved for signing")
    bad = signable_fact("f-bad", "ed25519 rollout budget was zero dollars")
    sign_lib.sign_facts([good, bad], signing_key)
    facts_file = write_facts(tmp_path, [good, bad])
    budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm
    capsys.readouterr()

    bad["content"] = "ed25519 rollout budget was one million dollars"
    bad["attestation"]["canonical_fact_hash"] = sign_lib.canonical_fact_hash(bad)
    bad["attestation"]["source_hash"] = sign_lib.source_hash(bad)
    write_facts(tmp_path, [good, bad])

    sidecar = sidecar_for(facts_file)
    doc = json.loads(sidecar.read_text(encoding="utf-8"))
    st = facts_file.stat()
    doc["store"] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
    sidecar.write_text(json.dumps(doc), encoding="utf-8")

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert "million" not in out
    assert "approved for signing" in out
    assert "excluded 1 tampered" in err


# --- strict-verify semantics unchanged -----------------------------------------
def test_strict_verify_unaffected_by_cache(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls, capsys):
    signed = signable_fact("f-signed", "ed25519 rollout was approved")
    sign_lib.sign_fact(signed, signing_key)
    unsigned = signable_fact("f-unsigned", "ed25519 rollout needs a runbook")
    facts_file = write_facts(tmp_path, [signed, unsigned])

    # Warm the cache with a default (non-strict) recall.
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    capsys.readouterr()

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file,
                                      strict_verify=True)
    err = capsys.readouterr().err
    assert "approved" in out
    assert "runbook" not in out  # still fails closed on unsigned
    assert "excluded 1 unsigned" in err
    # Statuses are computed identically from cache: the signed fact's VALID
    # came from the warm cache, no signature op needed even in strict mode.
    assert verify_calls["n"] == 0


# --- fail closed on cache doubt --------------------------------------------------
def test_corrupt_sidecar_falls_back_to_full_verification(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls):
    facts = [signable_fact("f-1", "ed25519 rollout was approved"),
             signable_fact("f-2", "ed25519 rollout owner is mira")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    first = budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm

    sidecar = sidecar_for(facts_file)
    sidecar.write_text("{ this is not json", encoding="utf-8")

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert out == first
    assert verify_calls["n"] == 2  # doubt -> full verification, not skipped

    # ...and the untrustworthy sidecar was replaced with a valid one.
    json.loads(sidecar.read_text(encoding="utf-8"))
    verify_calls["n"] = 0
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert verify_calls["n"] == 0


def test_rotated_key_discards_cache(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls,
        monkeypatch, capsys):
    facts = [signable_fact("f-1", "ed25519 rollout was approved")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm
    capsys.readouterr()

    # Rotate to a different key: the sidecar's key_id no longer matches, so
    # its digests are discarded and verification runs for real (and fails —
    # the facts were signed by the old key -> tampered under the new one).
    rotated_dir = tmp_path / "rotated"
    rotated_dir.mkdir()
    sign_lib.load_or_create_key(rotated_dir / "signing-key",
                                rotated_dir / "signing-key.pub")
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(rotated_dir / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB",
                       str(rotated_dir / "signing-key.pub"))

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert verify_calls["n"] == 1  # cache rejected, real verification ran
    assert "approved" not in out
    assert "excluded 1 tampered" in err
