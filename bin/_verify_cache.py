"""Sidecar cache of verified attestation signatures for the recall hot path.

Verifying every fact's attestation inside budget-recall (the OWASP F5 closure)
costs ~160us per Ed25519 signature — ~0.4-0.8s over a 2,500-fact store, paid on
EVERY recall for inputs that almost never change between recalls, blowing the
memory-inject hook's <2s budget. This cache remembers which signatures have
already been proven, so each one is verified once per content change instead of
once per recall. (The offline auditor, verify-facts.py, deliberately never uses
it: an audit always does the full cryptographic pass.)

What a cache entry means — and what stays uncached:

- Only a POSITIVE signature verification is recorded, as an opaque digest
  binding the exact proof: sha256 over (alg, key_id, signature, signed payload)
  — see _sign.cache_digest. The payload embeds the attestation's committed
  fact/source hashes and the CURRENT parent hashes, so editing a fact's
  attested content, its evidence, a parent, the signature itself, or rotating
  the key all change the digest -> miss -> a real signature verification.
- The content-hash comparisons in _sign.verify_fact (recomputing the fact's
  canonical hash against the committed hash — the check that actually catches
  F5 content poisoning) run on EVERY recall regardless of cache state; a hit
  only skips the redundant public-key operation. A tampered fact is therefore
  detected immediately, warm cache or not.
- TAMPERED / UNSIGNED / PARENT_SUSPECT results are never cached.

Freshness guard: the sidecar records the store file's (mtime_ns, size),
captured BEFORE the store is read. Any mismatch — or any doubt at all:
unreadable, malformed, wrong version, wrong key — discards the whole digest set
and falls back to full verification. Fail closed: doubt costs speed, never
safety.

Threat model: the sidecar lives next to facts.json, so an attacker with write
access there could forge digests. That is the same access that lets them delete
the signing key outright, which already disables verification — attestations
are tamper-*evidence*, not a gate (see _resolve_verify_key in budget-recall).
The sidecar holds only opaque digests, no fact content, so scrub/purge parity
is unaffected: purging rewrites facts.json, which trips the freshness guard,
and the purged fact's digest drops on the next save.
"""
# Deferred annotations keep this module importable on Python 3.9 (stock macOS
# /usr/bin/python3): it is reachable from the memory-inject hook's hot path.
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

CACHE_VERSION = 1
FILE_MODE = 0o600


def cache_path_for(store_path: Path) -> Path:
    return store_path.with_name(store_path.name + ".verified-cache.json")


class VerifiedSignatureCache:
    """The digest set for one store file, plus the bookkeeping to persist it.

    `hit`/`add` are the only calls on the verification hot loop; `save` runs
    once per store load and rewrites the sidecar only when something changed
    (a new signature proven, or an untrustworthy sidecar being replaced)."""

    def __init__(self, path: Path, key_id: str, alg: str,
                 store_sig: dict, digests: "set[str]", dirty: bool = False):
        self.path = Path(path)
        self.key_id = key_id
        self.alg = alg
        self.store_sig = store_sig  # {"mtime_ns": int, "size": int}
        self.digests = set(digests)
        self._dirty = dirty

    def hit(self, digest: str) -> bool:
        return digest in self.digests

    def add(self, digest: str) -> None:
        if digest not in self.digests:
            self.digests.add(digest)
            self._dirty = True

    def save(self) -> None:
        """Persist the digest set (atomic replace, 0600). A failure is a
        one-line stderr note, never an exception: the cache is an optimization
        and the recall path must keep working without it."""
        if not self._dirty:
            return
        doc = {
            "version": CACHE_VERSION,
            "alg": self.alg,
            "key_id": self.key_id,
            "store": self.store_sig,
            "digests": sorted(self.digests),
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=self.path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, separators=(",", ":"))
                os.chmod(tmp, FILE_MODE)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            print(f"{self.path}: could not save verification cache ({exc})",
                  file=sys.stderr)


def load_for_store(store_path: Path, verify_key) -> "VerifiedSignatureCache | None":
    """Cache handle for `store_path` verified under `verify_key`.

    MUST be called before the store file is read: the freshness stat is
    captured here, so a store rewritten between this stat and the read records
    a stale stat and the next recall re-verifies — the safe direction. Returns
    None (caching off) when verification is off or the store is not statable."""
    if verify_key is None:
        return None
    store_path = Path(store_path)
    try:
        st = store_path.stat()
    except OSError:
        return None
    store_sig = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
    path = cache_path_for(store_path)
    digests, dirty = _load_digests(path, verify_key, store_sig)
    return VerifiedSignatureCache(path, verify_key.key_id, verify_key.alg,
                                  store_sig, digests, dirty)


def _load_digests(path: Path, verify_key, store_sig: dict) -> "tuple[set[str], bool]":
    """(digests, dirty). Empty on ANY doubt — missing is a clean cold start;
    unreadable/malformed/mismatched marks dirty so save() replaces the
    untrustworthy sidecar even if this run proves no new signatures."""
    try:
        if not path.exists():
            return set(), False
        doc = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(doc, dict)
            and doc.get("version") == CACHE_VERSION
            and doc.get("key_id") == verify_key.key_id
            and doc.get("alg") == verify_key.alg
            and doc.get("store") == store_sig
            and isinstance(doc.get("digests"), list)
            and all(isinstance(d, str) for d in doc["digests"])
        ):
            return set(doc["digests"]), False
        return set(), True
    except (OSError, ValueError):  # ValueError covers JSONDecodeError
        return set(), True
