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
  binding the exact proof: an HMAC over (alg, key_id, signature, signed
  payload) KEYED under the verifying key material — see _sign.cache_digest. The
  payload embeds the attestation's committed fact/source hashes and the CURRENT
  parent hashes, so editing a fact's attested content, its evidence, a parent,
  the signature itself, or rotating the key all change the digest -> miss -> a
  real signature verification. The HMAC keying is what makes a forged sidecar
  useless without read access to the key file (see the threat model below).
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

Threat model: the sidecar lives next to facts.json and is attacker-writable,
so a forged entry must never be able to mint a VALID result. It cannot: the
digest is an HMAC keyed under _sign.SigningKey.cache_key_material() (the raw
Ed25519 public-key bytes, or the HMAC secret), so computing a digest that
hit()s requires READING the key file — the very access that would let an
attacker delete/replace the key and disable verification anyway. A truncated,
public key_id is embedded in every attestation, but that alone is not enough to
key the HMAC, so an attacker with only facts.json (+ sidecar) write access —
the case that matters when the key lives on a protected path via
NOCKBRAIN_SIGNING_PUB/KEY — cannot forge a bypass. (An earlier design keyed the
digest on public inputs only and WAS forgeable in that split-key posture, even
under --strict-verify; the HMAC keying closes it, and CACHE_VERSION was bumped
so pre-fix sidecars are rejected.) The sidecar holds only opaque digests, no
fact content, so scrub/purge parity is unaffected: purging rewrites facts.json,
which trips the freshness guard, and the purged fact's digest drops on the next
save.
"""
# Deferred annotations keep this module importable on Python 3.9 (stock macOS
# /usr/bin/python3): it is reachable from the memory-inject hook's hot path.
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

CACHE_VERSION = 2
FILE_MODE = 0o600

# A legitimate sidecar is bounded by the store size (one ~64-hex digest per
# signed fact); even a 100k-fact store is a few MB. Anything larger is either
# corruption or a hostile file, so we refuse to read it into memory and fall
# back to full verification. Guards against a well-formed-but-giant sidecar
# permanently blowing the recall budget (and against MemoryError on read).
MAX_SIDECAR_BYTES = 64 * 1024 * 1024


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
        one-line stderr note, NEVER an exception: budget-recall calls this
        unguarded on the recall hot path, so any propagating error would crash
        recall instead of degrading to slow-but-working verification. Catches
        Exception (not just OSError) so a serialization or filesystem surprise
        cannot escape; KeyboardInterrupt/SystemExit still propagate."""
        if not self._dirty:
            return
        doc = {
            "version": CACHE_VERSION,
            "alg": self.alg,
            "key_id": self.key_id,
            "store": self.store_sig,
            "digests": sorted(self.digests),
        }
        tmp = None
        try:
            # mkstemp creates the file 0600; close the fd immediately and write
            # by path so a failure in open()/json.dump can never leak the fd.
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=self.path.name + ".", suffix=".tmp")
            os.close(fd)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, separators=(",", ":"))
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, self.path)
            tmp = None  # replaced; nothing to clean up
        except Exception as exc:  # noqa: BLE001 - never crash recall on cache save
            print(f"{self.path}: could not save verification cache ({exc})",
                  file=sys.stderr)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


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
    """(digests, dirty). Empty on ANY doubt — a MISSING sidecar is a clean cold
    start (dirty=False, nothing to rewrite); anything else — unreadable,
    oversized, malformed, mismatched — marks dirty so save() replaces the
    untrustworthy sidecar even if this run proves no new signatures.

    Fails CLOSED without ever raising: budget-recall's _load calls this on the
    hot path, so a hostile/corrupt sidecar must degrade to full verification,
    never crash recall. The broad `except Exception` is deliberate — a deeply
    nested JSON array raises RecursionError (a RuntimeError, NOT a ValueError)
    and a giant file raises MemoryError; both would escape a narrow
    (OSError, ValueError) handler and kill the hook."""
    try:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return set(), False  # no sidecar yet: clean cold start
        if size > MAX_SIDECAR_BYTES:
            return set(), True  # implausibly large: distrust, don't read it in
        # Catch FileNotFoundError again: the sidecar can vanish between stat and
        # read (a concurrent recall/cleanup) — still a clean-missing signal, not
        # doubt, so we don't gratuitously rewrite an empty sidecar.
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set(), False
        doc = json.loads(raw)
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
    except Exception:  # noqa: BLE001 - fail closed to full verification, never crash
        return set(), True
