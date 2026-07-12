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
- a forged sidecar cannot bypass verification: the digest is an HMAC keyed
  under key material an attacker without the key file cannot reproduce, so a
  planted digest never hits (even under --strict-verify);
- hostile/corrupt or non-hex inputs fail closed to a full verification pass and
  NEVER crash recall — a pathologically nested sidecar (RecursionError), an
  oversized one, a non-hex signature (surrogate), or a save error all degrade
  gracefully;
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


# --- forged sidecar cannot bypass verification (the key-material MAC) ----------
def test_forged_digest_cannot_bypass_strict_verify(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    """The strongest attack: an adversary who can read facts.json and write its
    directory but does NOT hold the signing key. They plant a poisoned fact with
    recomputed committed hashes (so it self-hashes clean), an arbitrary
    signature, and a forged sidecar digest, matching the freshness stat. Because
    cache_digest is an HMAC keyed under key material the attacker cannot read,
    no digest they write can hit — the poison fails real verification and is
    excluded even under --strict-verify."""
    good = signable_fact("f-good", "ed25519 rollout was approved for signing")
    sign_lib.sign_fact(good, signing_key)
    key_id = good["attestation"]["key_id"]
    alg = good["attestation"]["alg"]

    poison = signable_fact("f-poison", "ed25519 rollout budget is one million dollars")
    poison["attestation"] = {
        "fact_id": "f-poison",
        "canonical_fact_hash": sign_lib.canonical_fact_hash(poison),
        "source_hash": sign_lib.source_hash(poison),
        "alg": alg, "key_id": key_id, "signature": "deadbeef",
        "parent_fact_ids": [], "signed_at": "2026-01-01T00:00:00+00:00",
    }
    facts_file = write_facts(tmp_path, [good, poison])

    # Attacker forges the sidecar. Even given the OLD public-only digest formula
    # (sha256 of alg/key_id/signature/payload), no entry can match the HMAC the
    # verifier now computes, so any planted digest is dead weight.
    st = facts_file.stat()
    sidecar_for(facts_file).write_text(json.dumps({
        "version": 2, "alg": alg, "key_id": key_id,
        "store": {"mtime_ns": st.st_mtime_ns, "size": st.st_size},
        "digests": ["0" * 64, "f" * 64],  # attacker's best guesses
    }), encoding="utf-8")

    out = budget_recall.budget_recall("ed25519 rollout budget", facts_file,
                                      strict_verify=True)
    err = capsys.readouterr().err
    assert "one million dollars" not in out  # forgery did not bypass
    assert "approved for signing" in out
    assert "excluded 1 tampered" in err


# --- hostile / non-hex inputs fail closed, never crash recall -----------------
def test_deeply_nested_sidecar_does_not_crash_recall(
        budget_recall, sign_lib, signing_key, tmp_path):
    """A corrupt/hostile sidecar whose JSON is pathologically nested makes
    json.loads raise RecursionError (a RuntimeError, not ValueError). It must
    fail closed to full verification, not escape and crash the recall hook."""
    facts = [signable_fact("f-1", "ed25519 rollout was approved")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    sidecar_for(facts_file).write_text("[" * 20000 + "]" * 20000, encoding="utf-8")

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "approved" in out  # recall still works; no traceback
    # The untrustworthy sidecar was replaced with a valid one.
    json.loads(sidecar_for(facts_file).read_text(encoding="utf-8"))


def test_oversized_sidecar_is_refused(
        budget_recall, sign_lib, signing_key, tmp_path, monkeypatch):
    """A well-formed but implausibly large sidecar is refused unread (guards the
    hook budget and MemoryError), degrading to full verification."""
    vc = importlib.import_module("_verify_cache")
    monkeypatch.setattr(vc, "MAX_SIDECAR_BYTES", 512)
    facts = [signable_fact("f-1", "ed25519 rollout was approved")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    sidecar_for(facts_file).write_text(
        json.dumps({"version": 2, "digests": ["a" * 64] * 1000}), encoding="utf-8")

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "approved" in out  # oversized sidecar ignored, recall works


def test_non_hex_signature_is_tampered_not_a_crash(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    """A fact whose attestation signature is a non-hex string (here a lone
    surrogate, valid JSON) must be treated as TAMPERED — as it was before the
    cache existed — not crash cache_digest's str.encode on the hot path."""
    good = signable_fact("f-good", "ed25519 rollout was approved for signing")
    evil = signable_fact("f-evil", "ed25519 rollout is fine honestly")
    sign_lib.sign_facts([good, evil], signing_key)
    evil["attestation"]["signature"] = "\ud800deadbeef"  # non-hex, lone surrogate
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([good, evil]), encoding="utf-8",
                          errors="surrogatepass")

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert "fine honestly" not in out  # excluded, not injected
    assert "approved for signing" in out
    assert "excluded 1 tampered" in err  # and no traceback surfaced


def test_save_failure_does_not_raise(sign_lib, tmp_path, capsys, monkeypatch):
    """save() must degrade to a stderr note, never raise — budget-recall calls
    it unguarded on the hot path. Force a non-OSError from json.dump."""
    vc = importlib.import_module("_verify_cache")
    cache = vc.VerifiedSignatureCache(
        tmp_path / "facts.json.verified-cache.json", "k", "ed25519",
        {"mtime_ns": 1, "size": 1}, set(), dirty=True)
    monkeypatch.setattr(vc.json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    cache.save()  # must not raise
    assert "could not save verification cache" in capsys.readouterr().err
