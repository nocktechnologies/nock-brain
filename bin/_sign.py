"""Signed, tamper-evident fact provenance for the NockBrain memory store (N8068).

OWASP audit finding F5: any local process can edit ``facts.json`` and poison
what gets injected into an agent's context. There was no cryptographic
integrity. This module adds a signed *fact envelope* so tampering is detectable.

Design (Mar Sinclair's spec):

- Sign each fact's CORE content (``id`` + ``kind`` + ``content``) and its source
  anchor (the evidence pointer) under a deterministic canonicalization (sorted
  keys, no whitespace drift) so signatures are stable across re-serialization.
- Prefer Ed25519 via ``cryptography``; fall back to HMAC-SHA256 (stdlib only)
  when ``cryptography`` is not importable. The import is graceful: installs
  without ``cryptography`` still sign and verify, just with the HMAC algo. The
  algorithm in force is recorded on every attestation via ``alg`` + ``key_id``.
- Derived facts carry ``parent_fact_ids``; the signature covers the fact hash
  PLUS the canonical hashes of those parents (Merkle-style ancestry), so a
  changed or revoked parent makes the child verify as ``parent-suspect``.

The attestation envelope added to each fact::

    "attestation": {
        "fact_id":            <str>,            # the fact's id at sign time
        "canonical_fact_hash":<sha256 hex>,     # over {id, kind, content}
        "source_hash":        <sha256 hex>,     # over the evidence anchor
        "alg":                "ed25519"|"hmac-sha256",
        "key_id":             <str>,            # fingerprint of the signing key
        "signature":          <hex>,            # over the signed payload
        "parent_fact_ids":    [<str>, ...],     # Merkle ancestry (may be empty)
        "signed_at":          <iso8601 utc>,
    }

The private key is NEVER logged and NEVER serialized into facts.json.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# bin/ has no package structure; import sibling helpers by adding bin/ to path.
import sys

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_mkdir, secure_write_text  # noqa: E402

# --- graceful cryptography import -------------------------------------------
# If cryptography is importable we sign with Ed25519; otherwise we fall back to
# HMAC-SHA256 from the stdlib. The fallback is permanent (not removed even when
# cryptography is added to requirements) so the product never hard-depends on it.
try:  # pragma: no cover - exercised via monkeypatch in tests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    _HAVE_CRYPTOGRAPHY = True
except Exception:  # pragma: no cover - import-failure path
    _HAVE_CRYPTOGRAPHY = False

ALG_ED25519 = "ed25519"
ALG_HMAC = "hmac-sha256"

DEFAULT_STORE_DIR = Path.home() / ".nock-brain"
DEFAULT_KEY_PATH = DEFAULT_STORE_DIR / "signing-key"
DEFAULT_PUB_PATH = DEFAULT_STORE_DIR / "signing-key.pub"

# Domain-separation prefix keeps these signatures from being valid in any other
# context that might reuse the same key bytes.
_DOMAIN = b"nockbrain-fact-v1\n"


# --- canonicalization --------------------------------------------------------
def _canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, UTF-8.

    Stable across re-serialization so a fact that round-trips through json
    dump/load produces an identical signing payload."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_fact_core(fact: dict[str, Any]) -> dict[str, Any]:
    """The CORE content a signature commits to: id + kind + content.

    These three define what the fact *asserts*. Mutating any of them (the F5
    attack: poisoning the injected claim) changes this hash and breaks the
    signature."""
    return {
        "id": fact.get("id", ""),
        "kind": fact.get("kind", ""),
        "content": fact.get("content", ""),
    }


def source_anchor(fact: dict[str, Any]) -> Any:
    """The provenance anchor: the evidence pointer(s) {event_id, path, line}.

    Kept distinct from the core so the verifier can later distinguish a tampered
    claim from a tampered provenance trail."""
    return fact.get("evidence", [])


def canonical_fact_hash(fact: dict[str, Any]) -> str:
    return _sha256_hex(_canonical_json(canonical_fact_core(fact)))


def source_hash(fact: dict[str, Any]) -> str:
    return _sha256_hex(_canonical_json(source_anchor(fact)))


def _signed_payload(fact_hash: str, src_hash: str, parent_hashes: list[str]) -> bytes:
    """The exact bytes that get signed.

    Includes the fact hash, the source hash, and the SORTED canonical hashes of
    every parent fact (Merkle-style). Sorting makes ancestry order-independent;
    a parent's hash is computed from the parent fact's own core, so editing a
    parent changes the child's payload -> child verifies parent-suspect."""
    body = {
        "fact_hash": fact_hash,
        "source_hash": src_hash,
        "parent_hashes": sorted(parent_hashes),
    }
    return _DOMAIN + _canonical_json(body)


# --- key management ----------------------------------------------------------
def key_fingerprint(material: bytes, alg: str) -> str:
    """Short, stable id derived from the key material (never the secret itself
    for HMAC: we fingerprint a one-way hash, so key_id leaks nothing)."""
    digest = hashlib.sha256(alg.encode("utf-8") + b":" + material).hexdigest()
    return f"{alg}:{digest[:16]}"


class SigningKey:
    """A signing/verification key pair. Ed25519 when available, else HMAC.

    Never logs or serializes the private material into facts."""

    def __init__(self, alg: str, *, ed_private=None, ed_public=None, hmac_secret: bytes | None = None):
        self.alg = alg
        self._ed_private = ed_private
        self._ed_public = ed_public
        self._hmac_secret = hmac_secret
        if alg == ALG_ED25519:
            pub_bytes = ed_public.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            self._pub_bytes = pub_bytes
            self.key_id = key_fingerprint(pub_bytes, alg)
        else:
            self._pub_bytes = None
            # Fingerprint a hash of the secret, not the secret, so key_id is safe
            # to embed in every attestation.
            self.key_id = key_fingerprint(hashlib.sha256(hmac_secret).digest(), alg)

    def cache_key_material(self) -> bytes:
        """Secret bytes that key the verification-cache MAC (see cache_digest).

        For HMAC it is the shared secret; for Ed25519 it is the raw public-key
        bytes. This is exactly the material an attacker must be able to READ to
        recompute a cache digest — the same key-file read access that the store
        directory does not by itself grant when the key lives on a protected
        path (NOCKBRAIN_SIGNING_PUB/KEY). key_id alone does NOT suffice: it is a
        truncated one-way fingerprint that is public in every attestation, so
        keying on it would leave the cache forgeable from facts.json alone.
        Never the Ed25519 private key — the verify-only recall path never has
        it, and the public bytes are unrecoverable from signatures."""
        if self.alg == ALG_ED25519:
            return self._pub_bytes
        return self._hmac_secret

    # -- signing/verifying primitives --
    def sign_bytes(self, payload: bytes) -> str:
        if self.alg == ALG_ED25519:
            return self._ed_private.sign(payload).hex()
        return hmac.new(self._hmac_secret, payload, hashlib.sha256).hexdigest()

    def verify_bytes(self, payload: bytes, signature_hex: str) -> bool:
        try:
            sig = bytes.fromhex(signature_hex)
        except (ValueError, TypeError):
            return False
        if self.alg == ALG_ED25519:
            try:
                self._ed_public.verify(sig, payload)
                return True
            except Exception:
                return False
        expected = hmac.new(self._hmac_secret, payload, hashlib.sha256).digest()
        return hmac.compare_digest(expected, sig)


def _generate_key(alg: str | None = None) -> SigningKey:
    use_ed = _HAVE_CRYPTOGRAPHY if alg is None else (alg == ALG_ED25519)
    if use_ed and _HAVE_CRYPTOGRAPHY:
        priv = Ed25519PrivateKey.generate()
        return SigningKey(ALG_ED25519, ed_private=priv, ed_public=priv.public_key())
    # HMAC fallback: 32 bytes of CSPRNG entropy.
    secret = secrets.token_bytes(32)
    return SigningKey(ALG_HMAC, hmac_secret=secret)


def _write_key(key: SigningKey, key_path: Path, pub_path: Path) -> None:
    """Persist private key 0600 and public key 0600 via secure-perm helpers.

    Stored as JSON with the alg recorded so load can reconstruct without
    guessing. Private material is written ONLY to key_path, never logged."""
    if key.alg == ALG_ED25519:
        priv_raw = key._ed_private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_raw = key._ed_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        priv_doc = {"alg": ALG_ED25519, "key_id": key.key_id, "private_key": priv_raw.hex()}
        pub_doc = {"alg": ALG_ED25519, "key_id": key.key_id, "public_key": pub_raw.hex()}
    else:
        priv_doc = {"alg": ALG_HMAC, "key_id": key.key_id, "secret": key._hmac_secret.hex()}
        # HMAC is symmetric; the "public" file records only the verifying alg +
        # key_id + the same secret (verification needs it). It is also 0600.
        pub_doc = {"alg": ALG_HMAC, "key_id": key.key_id, "secret": key._hmac_secret.hex()}

    secure_write_text(key_path, json.dumps(priv_doc, indent=2))
    secure_write_text(pub_path, json.dumps(pub_doc, indent=2))


def _load_key_from_doc(doc: dict[str, Any]) -> SigningKey:
    alg = doc.get("alg")
    if alg == ALG_ED25519:
        if not _HAVE_CRYPTOGRAPHY:
            raise RuntimeError(
                "key is Ed25519 but cryptography is unavailable; cannot load"
            )
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(doc["private_key"]))
        return SigningKey(ALG_ED25519, ed_private=priv, ed_public=priv.public_key())
    if alg == ALG_HMAC:
        return SigningKey(ALG_HMAC, hmac_secret=bytes.fromhex(doc["secret"]))
    raise RuntimeError(f"unknown key alg: {alg!r}")


def load_or_create_key(
    key_path: Path = DEFAULT_KEY_PATH,
    pub_path: Path = DEFAULT_PUB_PATH,
    *,
    alg: str | None = None,
    create: bool = True,
) -> SigningKey:
    """Load the signing key, auto-generating one if absent (when create=True).

    The generated algorithm follows availability: Ed25519 if cryptography is
    importable, else HMAC-SHA256. Pass ``alg`` to force one (used by tests)."""
    key_path = Path(key_path)
    pub_path = Path(pub_path)
    if key_path.exists():
        doc = json.loads(key_path.read_text(encoding="utf-8"))
        return _load_key_from_doc(doc)
    if not create:
        raise FileNotFoundError(f"signing key not found at {key_path}")
    secure_mkdir(key_path.parent)
    key = _generate_key(alg)
    _write_key(key, key_path, pub_path)
    return key


def load_public_key(pub_path: Path = DEFAULT_PUB_PATH) -> SigningKey:
    """Load a verification-only view of the key.

    For Ed25519 this needs only the public key; for HMAC the same secret is
    required to verify (symmetric)."""
    pub_path = Path(pub_path)
    if not pub_path.exists():
        raise FileNotFoundError(f"public key not found at {pub_path}")
    doc = json.loads(pub_path.read_text(encoding="utf-8"))
    alg = doc.get("alg")
    if alg == ALG_ED25519:
        if not _HAVE_CRYPTOGRAPHY:
            raise RuntimeError(
                "public key is Ed25519 but cryptography is unavailable; cannot verify"
            )
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(doc["public_key"]))
        # Verification-only: no private key.
        return SigningKey(ALG_ED25519, ed_private=None, ed_public=pub)
    if alg == ALG_HMAC:
        return SigningKey(ALG_HMAC, hmac_secret=bytes.fromhex(doc["secret"]))
    raise RuntimeError(f"unknown key alg: {alg!r}")


# --- signing facts -----------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parent_hashes(parent_ids: list[str], facts_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """Canonical hash of each parent fact's core, for the Merkle commitment.

    A parent referenced but absent from the store hashes its id alone (so the
    commitment is still defined and a later-added/altered parent will mismatch)."""
    hashes = []
    for pid in parent_ids:
        parent = facts_by_id.get(pid)
        if parent is not None:
            hashes.append(canonical_fact_hash(parent))
        else:
            # Absent parent: commit to a sentinel derived from the id so the
            # child still has a stable, verifiable ancestry hash.
            hashes.append(_sha256_hex(_canonical_json({"absent_parent_id": pid})))
    return hashes


def attest_fact(
    fact: dict[str, Any],
    key: SigningKey,
    *,
    facts_by_id: dict[str, dict[str, Any]] | None = None,
    parent_fact_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return the attestation envelope for ``fact`` signed by ``key``.

    ``parent_fact_ids`` defaults to any already present on the fact's
    attestation, or an explicit ``parent_fact_ids`` field on the fact itself."""
    facts_by_id = facts_by_id or {}
    if parent_fact_ids is None:
        existing = fact.get("attestation", {}).get("parent_fact_ids")
        parent_fact_ids = existing if existing is not None else fact.get("parent_fact_ids", [])
    parent_fact_ids = list(parent_fact_ids or [])

    fact_hash = canonical_fact_hash(fact)
    src_hash = source_hash(fact)
    p_hashes = _parent_hashes(parent_fact_ids, facts_by_id)
    payload = _signed_payload(fact_hash, src_hash, p_hashes)
    signature = key.sign_bytes(payload)

    return {
        "fact_id": fact.get("id", ""),
        "canonical_fact_hash": fact_hash,
        "source_hash": src_hash,
        "alg": key.alg,
        "key_id": key.key_id,
        "signature": signature,
        "parent_fact_ids": parent_fact_ids,
        "signed_at": _now_iso(),
    }


def sign_fact(
    fact: dict[str, Any],
    key: SigningKey,
    *,
    facts_by_id: dict[str, dict[str, Any]] | None = None,
    parent_fact_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Sign ``fact`` in place: attach the attestation envelope and return it."""
    fact["attestation"] = attest_fact(
        fact, key, facts_by_id=facts_by_id, parent_fact_ids=parent_fact_ids
    )
    return fact


def sign_facts(facts: list[dict[str, Any]], key: SigningKey) -> list[dict[str, Any]]:
    """Sign every fact in a store.

    Two-pass so parent hashes commit to the parents' CORE (independent of
    signing order). Parent hashing uses ``canonical_fact_hash`` which does not
    depend on the attestation, so a single pass over a stable map is correct."""
    facts_by_id = {f.get("id", ""): f for f in facts if isinstance(f, dict)}
    for fact in facts:
        if isinstance(fact, dict):
            sign_fact(fact, key, facts_by_id=facts_by_id)
    return facts


# --- verification ------------------------------------------------------------
# Verification status constants.
VALID = "valid"
TAMPERED = "tampered"
UNSIGNED = "unsigned"
PARENT_SUSPECT = "parent-suspect"

# Domain separation for verification-cache digests (distinct from _DOMAIN so a
# cache digest can never be confused with signable material). The version
# suffix is bumped whenever the digest scheme changes so stale sidecars are
# rejected wholesale by _verify_cache.CACHE_VERSION; v2 switched the digest
# from a plain sha256 of public inputs to an HMAC keyed under the verifying key
# material (see cache_digest) to close a sidecar-forgery bypass.
_CACHE_DOMAIN = b"nockbrain-verify-cache-v2"


def is_cacheable_signature(signature_hex: Any) -> bool:
    """True iff ``signature_hex`` is a hex string verify_bytes could parse.

    Gate for the cache path: a non-str or non-hex signature (attacker-writable
    facts.json can carry either — a JSON number, a list, or a string with a
    lone surrogate/NUL) must skip caching and fall straight through to
    verify_bytes, which returns False -> TAMPERED. Mirrors verify_bytes'
    ``bytes.fromhex`` exactly, so a signature is cacheable iff it is verifiable,
    and cache_digest never sees bytes that would crash ``.encode`` or make its
    field encoding ambiguous."""
    if not isinstance(signature_hex, str):
        return False
    try:
        bytes.fromhex(signature_hex)
        return True
    except ValueError:
        return False


def cache_digest(key: SigningKey, signature_hex: str, payload: bytes) -> str:
    """Digest naming one successful signature verification, for the recall hot
    path's sidecar cache (_verify_cache). It binds everything the proof
    depended on — algorithm, key, signature, and the exact signed payload
    (which itself embeds the committed fact/source hashes and the CURRENT
    parent hashes) — so any change to any of them yields a different digest,
    a cache miss, and a real verification.

    It is an HMAC keyed under ``key.cache_key_material()`` (NOT a bare hash of
    public inputs): a sidecar is attacker-writable, and every non-keyed input
    here (alg, the public key_id fingerprint, the signature, the payload) is
    computable by anyone who can read facts.json. Keying the digest under the
    verifying key material means a forged sidecar entry cannot mint a VALID
    result without read access to the key file — restoring the intended
    'forging the cache needs the same access as replacing the key' property
    even when the key sits on a protected path. ``signature_hex`` is a
    caller-validated hex string (see is_cacheable_signature), so the NUL-joined
    field encoding is unambiguous (hex/alg/fingerprint bytes contain no NUL and
    the variable-length payload comes last)."""
    preimage = b"\0".join([
        _CACHE_DOMAIN,
        key.alg.encode("utf-8"),
        key.key_id.encode("utf-8"),
        signature_hex.encode("utf-8"),
        payload,
    ])
    return hmac.new(key.cache_key_material(), preimage, hashlib.sha256).hexdigest()


def verify_fact(
    fact: dict[str, Any],
    key: SigningKey | None,
    *,
    facts_by_id: dict[str, dict[str, Any]] | None = None,
    verified_cache=None,
) -> str:
    """Verify a single fact's attestation. Returns one of the status constants.

    - UNSIGNED: no attestation present (backward-compat: still loads elsewhere).
    - TAMPERED: the fact's own core or source anchor no longer matches the
      signed hashes, or the signature does not verify under the key.
    - PARENT_SUSPECT: the fact itself is intact, but a parent fact's current
      core no longer matches what the child committed to (Merkle break).
    - VALID: signature verifies and all committed hashes match.

    ``verified_cache`` (a _verify_cache.VerifiedSignatureCache, or anything
    with hit/add) short-circuits ONLY the public-key signature operation, for
    (key, signature, payload) triples this store has already proven VALID. The
    committed-hash comparisons below run unconditionally either way, so a
    tampered fact is still caught with a warm cache; only VALID results are
    ever recorded."""
    facts_by_id = facts_by_id or {}
    att = fact.get("attestation")
    if not isinstance(att, dict) or not att.get("signature"):
        return UNSIGNED
    if key is None:
        # No key to verify against -> cannot affirm; treat as tampered/unverifiable.
        return TAMPERED

    # 1. Recompute the fact's own hashes from current content and compare to the
    #    committed values. This catches the F5 content-poisoning attack.
    current_fact_hash = canonical_fact_hash(fact)
    current_src_hash = source_hash(fact)
    if current_fact_hash != att.get("canonical_fact_hash"):
        return TAMPERED
    if current_src_hash != att.get("source_hash"):
        return TAMPERED

    # 2. Recompute the signed payload using the committed hashes + CURRENT parent
    #    hashes, and verify the signature. If the signature itself fails the
    #    fact's own bytes were tampered (or wrong key) -> TAMPERED.
    parent_ids = list(att.get("parent_fact_ids", []))
    parent_hashes_now = _parent_hashes(parent_ids, facts_by_id)
    payload_now = _signed_payload(
        att["canonical_fact_hash"], att["source_hash"], parent_hashes_now
    )
    if key.alg != att.get("alg"):
        # Algorithm mismatch between key and attestation -> cannot have produced it.
        return TAMPERED
    digest = None
    if verified_cache is not None and is_cacheable_signature(att["signature"]):
        digest = cache_digest(key, att["signature"], payload_now)
        if verified_cache.hit(digest):
            return VALID
    if key.verify_bytes(payload_now, att["signature"]):
        if digest is not None:
            verified_cache.add(digest)
        return VALID

    # 3. Signature failed even though the fact's OWN committed hashes still match
    #    its current content (checked in step 1). The signed payload is
    #    fact_hash + source_hash (both taken from the committed attestation) +
    #    parent_hashes. Since the first two are the committed values, the only
    #    remaining variable that could have changed is the parent set -> the
    #    break is in ancestry. With parents that is PARENT_SUSPECT (a parent was
    #    edited/revoked); with no parents the signature itself is bad -> TAMPERED.
    if parent_ids:
        return PARENT_SUSPECT
    return TAMPERED


def verify_facts(
    facts: list[dict[str, Any]],
    key: SigningKey | None,
) -> dict[str, Any]:
    """Verify a whole store. Returns counts + per-fact statuses.

    Result shape::
        {"valid": int, "tampered": int, "unsigned": int, "parent_suspect": int,
         "total": int, "statuses": [{"id":..., "status":...}, ...]}"""
    facts_by_id = {f.get("id", ""): f for f in facts if isinstance(f, dict)}
    counts = {VALID: 0, TAMPERED: 0, UNSIGNED: 0, PARENT_SUSPECT: 0}
    statuses = []
    for fact in facts:
        if not isinstance(fact, dict):
            counts[TAMPERED] += 1
            statuses.append({"id": None, "status": TAMPERED})
            continue
        status = verify_fact(fact, key, facts_by_id=facts_by_id)
        counts[status] += 1
        statuses.append({"id": fact.get("id", ""), "status": status})
    return {
        "valid": counts[VALID],
        "tampered": counts[TAMPERED],
        "unsigned": counts[UNSIGNED],
        "parent_suspect": counts[PARENT_SUSPECT],
        "total": len(facts),
        "statuses": statuses,
    }
