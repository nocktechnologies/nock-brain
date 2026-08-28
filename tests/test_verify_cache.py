"""Regression tests for the attestation-verification cache on the recall hot
path (bin/_verify_cache.py). Verifying ~2,500 Ed25519 signatures on every
recall added ~0.4-0.8s per invocation, most of the memory-inject hook's <2s
budget. The sidecar cache remembers already-proven signatures per store; these
tests pin its contract:

- a warm cache skips the signature operations (and ONLY those — the committed-
  hash comparisons still run, so tampering is caught even on a warm cache);
- a store rewrite does not wipe the digest set: unchanged facts stay cache
  hits (each digest is content-bound) and only new/changed facts re-verify;
  a dirty save prunes to digests hit-or-added this run;
- a tampered fact is still detected after a store rewrite — including the
  forged variant where the attacker recomputes the committed hashes and fakes
  the sidecar's store stamp;
- --strict-verify semantics are unchanged: the cache only accelerates the
  VALID determination, never alters a status;
- a stale save cannot clobber a newer sidecar: A loaded v1, B saved v2, A's
  later save skips (re-stat mismatch) so B's digests survive, including
  status-bound non-VALID entries that share the digest set;
- same-stamp concurrent saves union on-disk digests (append-only within one
  store stat) rather than last-writer-wins;
- a forged sidecar cannot bypass verification: the digest is an HMAC keyed
  under key material an attacker without the key file cannot reproduce, so a
  planted digest never hits (even under --strict-verify);
- hostile/corrupt or non-hex inputs fail closed to a full verification pass and
  NEVER crash recall — a pathologically nested sidecar (RecursionError), an
  oversized one, a non-hex signature (surrogate), or a save error all degrade
  gracefully;
- an unwritable sidecar directory degrades to in-memory caching: one
  diagnostic per process, a second recall in the same process still hits,
  and the process does not crash;
- any cache doubt (corrupt sidecar, missing key) fails closed to a full
  verification pass, never to skipped verification.
"""
import importlib
import importlib.util
import json
import os
import stat
import time
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load_verify_cache():
    """Load bin/_verify_cache.py without depending on another test having
    put bin/ on sys.path, and without replacing sys.modules['_verify_cache']
    that budget-recall already imported."""
    spec = importlib.util.spec_from_file_location(
        f"_verify_cache_direct_{id(object())}", _BIN / "_verify_cache.py")
    vc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vc)
    return vc


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


# --- store mutation retains unrelated digests ---------------------------------
def test_store_mutation_does_not_wipe_unrelated_digests(
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
    # Store stamp moved, but each digest is content-bound: only the new fact
    # misses and re-verifies. The two unchanged facts stay cache hits.
    assert verify_calls["n"] == 1

    verify_calls["n"] = 0
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert verify_calls["n"] == 0  # fully warm again, including the new fact


def test_append_retains_cached_digests_of_unchanged_facts(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls):
    """Issue #47: rewriting facts.json must not discard cached digests of
    unchanged facts. Each digest is bound to that fact's current content and
    parents, so a one-fact append re-verifies only the new fact; the old
    facts stay hits and their digests are still in the sidecar after save."""
    facts = [signable_fact("f-1", "ed25519 rollout was approved"),
             signable_fact("f-2", "ed25519 rollout owner is mira")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)
    budget_recall.budget_recall("ed25519 rollout", facts_file)  # warm

    sidecar = sidecar_for(facts_file)
    warm_digests = set(json.loads(sidecar.read_text(encoding="utf-8"))["digests"])
    assert len(warm_digests) == 2
    warm_stamp = json.loads(sidecar.read_text(encoding="utf-8"))["store"]

    new = signable_fact("f-3", "ed25519 rollout gained a runbook")
    sign_lib.sign_fact(new, signing_key, facts_by_id={})
    write_facts(tmp_path, facts + [new])
    # The rewrite moved the store stamp — under the old wholesale-wipe this
    # discarded both cached digests. It must not.
    new_st = facts_file.stat()
    assert {"mtime_ns": new_st.st_mtime_ns, "size": new_st.st_size} != warm_stamp

    verify_calls["n"] = 0
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "runbook" in out
    assert "approved" in out
    assert verify_calls["n"] == 1  # only the appended fact

    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    saved_digests = set(saved["digests"])
    assert warm_digests <= saved_digests  # old facts' digests retained
    assert len(saved_digests) == 3  # plus the new fact's digest
    assert saved["store"] == {"mtime_ns": new_st.st_mtime_ns,
                              "size": new_st.st_size}

    verify_calls["n"] = 0
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert verify_calls["n"] == 0


def test_dirty_save_prunes_digests_not_live_this_run(
        budget_recall, sign_lib, signing_key, tmp_path):
    """A facts.json rewrite that DROPS a fact must not keep accumulating its
    digest: the dirty save after the stamp change persists only hit-or-added
    digests from this run."""
    keep = signable_fact("f-keep", "ed25519 rollout was approved")
    drop = signable_fact("f-drop", "ed25519 rollout owner is mira")
    sign_lib.sign_facts([keep, drop], signing_key)
    facts_file = write_facts(tmp_path, [keep, drop])
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    sidecar = sidecar_for(facts_file)
    warm_digests = set(json.loads(sidecar.read_text(encoding="utf-8"))["digests"])
    assert len(warm_digests) == 2

    write_facts(tmp_path, [keep])  # drop f-drop; store stamp changes
    budget_recall.budget_recall("ed25519 rollout", facts_file)
    saved_digests = set(json.loads(sidecar.read_text(encoding="utf-8"))["digests"])
    assert len(saved_digests) == 1
    assert saved_digests <= warm_digests


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
    # signature work. The intact fact hits its retained digest — a store
    # rewrite is not a wholesale wipe — so verify_bytes is never reached.
    assert verify_calls["n"] == 0


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


def test_peer_digests_refuses_oversized_sidecar(tmp_path, monkeypatch):
    """_peer_digests must not read a sidecar larger than MAX_SIDECAR_BYTES.
    A well-formed oversized file would otherwise union its digests (and
    can MemoryError on the save path)."""
    vc = _load_verify_cache()
    monkeypatch.setattr(vc, "MAX_SIDECAR_BYTES", 64)
    sidecar = tmp_path / "facts.json.verified-cache.json"
    store_sig = {"mtime_ns": 1, "size": 1}
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": store_sig,
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    assert sidecar.stat().st_size > 64
    assert vc._peer_digests(sidecar, "k", "ed25519", store_sig) == set()


def test_sidecar_status_oversized_is_not_fresh(tmp_path, monkeypatch):
    """sidecar_status must not read a sidecar larger than MAX_SIDECAR_BYTES.
    A well-formed oversized file would otherwise look fresh."""
    vc = _load_verify_cache()
    monkeypatch.setattr(vc, "MAX_SIDECAR_BYTES", 64)
    facts = tmp_path / "facts.json"
    facts.write_text("[]", encoding="utf-8")
    st = facts.stat()
    sidecar = vc.cache_path_for(facts)
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": st.st_mtime_ns, "size": st.st_size},
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    assert sidecar.stat().st_size > 64
    status = vc.sidecar_status(facts)
    assert status["present"] is True
    assert status["fresh"] is False


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
    vc = _load_verify_cache()
    cache = vc.VerifiedSignatureCache(
        tmp_path / "facts.json.verified-cache.json", "k", "ed25519",
        {"mtime_ns": 1, "size": 1}, set(), dirty=True)
    monkeypatch.setattr(vc.json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    cache.save()  # must not raise
    assert "could not save verification cache" in capsys.readouterr().err


def test_save_failure_warns_only_once(tmp_path, capsys, monkeypatch):
    """Issue #50: an unwritable sidecar must not print on every save()."""
    vc = _load_verify_cache()
    monkeypatch.setattr(vc.json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    for _ in range(2):
        cache = vc.VerifiedSignatureCache(
            tmp_path / "facts.json.verified-cache.json", "k", "ed25519",
            {"mtime_ns": 1, "size": 1}, set(), dirty=True)
        cache.add("digest")
        cache.save()
    err = capsys.readouterr().err
    assert err.count("could not save verification cache") == 1


# --- concurrent save: stale writer must not clobber a newer sidecar (#49) -----
def test_stale_v1_save_does_not_clobber_v2_sidecar(tmp_path):
    """Issue #49: recall A starts against store v1; a consolidation rewrites
    the store to v2; recall B verifies v2 and saves; A's later save must not
    write {v1-stat, v1-live} over B. Without a re-stat-before-replace, B's
    digests vanish and the next recall pays a cold verification.

    Digests here are opaque strings: VALID and status-bound PARENT_SUSPECT/
    TAMPERED share one set (#48). Both of B's entries must survive."""
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    sidecar = vc.cache_path_for(store)

    store.write_text("v1", encoding="utf-8")
    st1 = store.stat()
    sig_v1 = {"mtime_ns": st1.st_mtime_ns, "size": st1.st_size}

    cache_a = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519", sig_v1, set(), dirty=True, store_path=store)
    cache_a.add("digest-a-v1")

    # Store rewrite: different size so the stamp cannot collide.
    store.write_text("v2-rewritten-by-consolidation", encoding="utf-8")
    st2 = store.stat()
    sig_v2 = {"mtime_ns": st2.st_mtime_ns, "size": st2.st_size}
    assert sig_v2 != sig_v1

    cache_b = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519", sig_v2, set(), dirty=True, store_path=store)
    cache_b.add("digest-b-valid")
    cache_b.add("digest-b-parent-suspect")  # status-bound non-VALID, same set
    cache_b.save()

    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(saved["digests"]) == {"digest-b-valid", "digest-b-parent-suspect"}
    assert saved["store"] == sig_v2

    cache_a.save()  # the stale v1 write — must skip, not clobber

    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(saved["digests"]) == {"digest-b-valid", "digest-b-parent-suspect"}
    assert saved["store"] == sig_v2
    assert "digest-a-v1" not in saved["digests"]


def test_same_stamp_save_unions_peer_digests(tmp_path):
    """Within one store stat, digests are append-only. Two concurrent writers
    each prune to their own live set; save() re-reads the sidecar and unions
    so neither drops the other's adds. VALID and status-bound non-VALID
    entries share the set: union is a set of opaque HMACs and cannot upgrade
    a failure to VALID (#48)."""
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    sidecar = vc.cache_path_for(store)
    store.write_text("stable", encoding="utf-8")
    st = store.stat()
    sig = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}

    cache_b = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519", sig, set(), dirty=True, store_path=store)
    cache_b.add("digest-b-valid")
    cache_b.add("digest-b-parent-suspect")
    cache_b.save()

    cache_a = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519", sig, set(), dirty=True, store_path=store)
    cache_a.add("digest-a-valid")
    cache_a.add("digest-a-tampered")
    cache_a.save()

    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(saved["digests"]) == {
        "digest-a-valid", "digest-a-tampered",
        "digest-b-valid", "digest-b-parent-suspect",
    }
    assert saved["store"] == sig


# --- unwritable sidecar: in-memory only, one diagnostic per process (#50) -----
def test_unwritable_sidecar_warns_once_and_caches_in_memory(
        budget_recall, sign_lib, signing_key, tmp_path, verify_calls, capsys,
        monkeypatch):
    """Issue #50: a read-only or full store directory must not print
    `could not save verification cache` on every recall, and the process
    must still warm in memory so the second recall skips signature ops."""
    vc = importlib.import_module("_verify_cache")
    monkeypatch.setattr(vc, "_save_warned", False)
    monkeypatch.setattr(vc, "_memory", {})
    monkeypatch.setattr(vc, "_probe_writable", lambda directory: False)

    facts = [signable_fact(f"f-{i}", f"ed25519 rollout note {i} approved")
             for i in range(3)]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)

    first = budget_recall.budget_recall("ed25519 rollout approved", facts_file)
    err1 = capsys.readouterr().err
    assert "approved" in first
    assert verify_calls["n"] == 3  # cold
    assert "verification cache is unwritable" in err1
    assert err1.count("verification cache is unwritable") == 1
    assert not sidecar_for(facts_file).exists()  # never persisted

    verify_calls["n"] = 0
    second = budget_recall.budget_recall("ed25519 rollout approved", facts_file)
    err2 = capsys.readouterr().err
    assert second == first
    assert verify_calls["n"] == 0  # in-memory warm
    assert "unwritable" not in err2
    assert "could not save" not in err2


# --- for_store owns the cache lifecycle (#53) ---------------------------------
class _DummyKey:
    key_id = "k"
    alg = "ed25519"


def test_for_store_stats_before_calling_loader(tmp_path, monkeypatch):
    """The stamp-then-read order is in for_store, not a call-site docstring.
    load_fn runs only after load_for_store has captured the store stamp."""
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    store.write_text("[]", encoding="utf-8")
    order = []

    real = vc.load_for_store

    def wrapped(path, key):
        order.append("cache")
        return real(path, key)

    monkeypatch.setattr(vc, "load_for_store", wrapped)

    def loader():
        order.append("store")
        return json.loads(store.read_text(encoding="utf-8"))

    with vc.for_store(store, _DummyKey(), loader) as (cache, facts):
        order.append("body")
        assert facts == []
        assert cache is not None

    assert order[:2] == ["cache", "store"], (
        "for_store must capture the store stamp before invoking load_fn"
    )
    assert order[2] == "body"


def test_for_store_saves_on_exit_even_when_body_raises(tmp_path, monkeypatch):
    """for_store.__exit__ owns save(); a raising body must still persist."""
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    store.write_text("[]", encoding="utf-8")
    saved = {"n": 0}
    real_save = vc.VerifiedSignatureCache.save

    def counting_save(self):
        saved["n"] += 1
        return real_save(self)

    monkeypatch.setattr(vc.VerifiedSignatureCache, "save", counting_save)

    with pytest.raises(RuntimeError, match="boom"):
        with vc.for_store(store, _DummyKey(), lambda: []) as (cache, facts):
            assert cache is not None
            raise RuntimeError("boom")

    assert saved["n"] == 1


def test_budget_recall_does_not_own_cache_lifecycle():
    """Recall must not re-choreograph load_for_store / save; for_store owns it."""
    src = (_BIN / "budget-recall.py").read_text(encoding="utf-8")
    assert "for_store(" in src
    assert "cache.save(" not in src
    assert "load_for_store(" not in src


# --- sidecar file lifecycle (#52) --------------------------------------------
def test_save_sweeps_stale_tmp_but_keeps_young_concurrent_writer(tmp_path):
    """SIGKILL of the hook leaves `{sidecar}.XXXXXX.tmp` behind. save() must
    sweep those, but must not delete a .tmp young enough to belong to a
    concurrent writer (#90)."""
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    store.write_text("[]", encoding="utf-8")
    sidecar = vc.cache_path_for(store)
    stale = tmp_path / (sidecar.name + ".stale.tmp")
    young = tmp_path / (sidecar.name + ".live.tmp")
    probe = tmp_path / ".nb-vc-probe.not-ours.tmp"
    stale.write_text("leftover", encoding="utf-8")
    young.write_text("in-flight", encoding="utf-8")
    probe.write_text("probe", encoding="utf-8")
    old = time.time() - (vc.STALE_TMP_AGE_SEC + 30)
    os.utime(stale, (old, old))

    cache = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519",
        {"mtime_ns": 1, "size": 1}, set(), dirty=False, store_path=store)
    cache.save()  # not dirty: sweep still runs, no new sidecar is written

    assert not stale.exists(), "stale leftover tmp must be swept"
    assert young.exists(), "a young tmp may belong to a concurrent writer"
    assert probe.exists(), "probe tmps use a different prefix and must stay"


def test_unlink_for_store_removes_sidecar(tmp_path):
    vc = _load_verify_cache()
    store = tmp_path / "facts.json"
    store.write_text("[]", encoding="utf-8")
    sidecar = vc.cache_path_for(store)
    sidecar.write_text("{}", encoding="utf-8")
    assert vc.unlink_for_store(store) is True
    assert not sidecar.exists()
    assert vc.unlink_for_store(store) is False  # already gone, never raises


# --- atomic write + schema type guards (#54) ---------------------------------
def test_cache_save_uses_store_atomic_write(tmp_path, monkeypatch):
    """save() must go through _store.secure_write_json_atomic, not a private
    mkstemp copy. A future fsync-before-replace fix lands in one place."""
    vc = _load_verify_cache()
    called = []

    def fake(path, value, **kwargs):
        called.append(path)
        return True

    monkeypatch.setattr(vc, "secure_write_json_atomic", fake)
    store = tmp_path / "facts.json"
    store.write_text("[]", encoding="utf-8")
    sidecar = vc.cache_path_for(store)
    cache = vc.VerifiedSignatureCache(
        sidecar, "k", "ed25519",
        vc._store_sig(store), set(), dirty=True, store_path=store)
    cache.add("digest")
    cache.save()
    assert called == [sidecar]


def test_verify_cache_does_not_redeclare_file_mode():
    src = (_BIN / "_verify_cache.py").read_text(encoding="utf-8")
    assert "from _store import FILE_MODE" in src
    assert "FILE_MODE = 0o600" not in src


def test_load_digests_rejects_boolean_version(tmp_path, monkeypatch):
    """JSON true == 1 in Python. A boolean version must not satisfy an
    integer CACHE_VERSION (the trap is latent at version 2; pin it at 1)."""
    vc = _load_verify_cache()
    monkeypatch.setattr(vc, "CACHE_VERSION", 1)
    sidecar = tmp_path / "facts.json.verified-cache.json"
    store_sig = {"mtime_ns": 1, "size": 1}
    sidecar.write_text(json.dumps({
        "version": True,
        "alg": "ed25519", "key_id": "k",
        "store": store_sig,
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    digests, dirty = vc._load_digests(sidecar, _DummyKey(), store_sig)
    assert digests == set()
    assert dirty is True


def test_load_digests_float_store_stamp_is_not_fresh(tmp_path):
    """JSON 1.0 == 1 in Python. A float mtime_ns/size must not match an int stamp.
    Digests stay (stamp is informational); dirty is set so save rewrites."""
    vc = _load_verify_cache()
    sidecar = tmp_path / "facts.json.verified-cache.json"
    store_sig = {"mtime_ns": 1, "size": 1}
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": 1.0, "size": 1.0},
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    digests, dirty = vc._load_digests(sidecar, _DummyKey(), store_sig)
    assert digests == {"deadbeef"}
    assert dirty is True


def test_peer_digests_rejects_float_store_stamp(tmp_path):
    vc = _load_verify_cache()
    sidecar = tmp_path / "facts.json.verified-cache.json"
    store_sig = {"mtime_ns": 1, "size": 1}
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": 1.0, "size": 1.0},
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    assert vc._peer_digests(sidecar, "k", "ed25519", store_sig) == set()


def test_sidecar_status_float_store_stamp_is_not_fresh(tmp_path, monkeypatch):
    """Use a small int stamp so 1.0 == 1 is the trap, not float64 precision."""
    vc = _load_verify_cache()
    facts = tmp_path / "facts.json"
    facts.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(vc, "_store_sig", lambda path: {"mtime_ns": 1, "size": 1})
    sidecar = vc.cache_path_for(facts)
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": 1.0, "size": 1.0},
        "digests": ["deadbeef"],
    }), encoding="utf-8")
    status = vc.sidecar_status(facts)
    assert status["present"] is True
    assert status["fresh"] is False
