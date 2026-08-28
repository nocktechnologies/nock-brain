"""Sidecar cache of verified attestation signatures for the recall hot path.

Verifying every fact's attestation inside budget-recall (the OWASP F5 closure)
costs ~160us per Ed25519 signature — ~0.4-0.8s over a 2,500-fact store, paid on
EVERY recall for inputs that almost never change between recalls, blowing the
memory-inject hook's <2s budget. This cache remembers which signature
determinations have already been made, so each one is verified once per
content change instead of once per recall. (The offline auditor,
verify-facts.py, deliberately never uses it: an audit always does the full
cryptographic pass.)

What a cache entry means — and what stays uncached:

- A signature-op determination is recorded as an opaque digest binding the
  exact proof: an HMAC over (alg, key_id, signature, signed payload [, status
  for non-VALID]) KEYED under the verifying key material — see
  _sign.cache_digest. The payload embeds the attestation's committed
  fact/source hashes and the CURRENT parent hashes, so editing a fact's
  attested content, its evidence, a parent, the signature itself, or rotating
  the key all change the digest -> miss -> a real signature verification. The
  HMAC keying is what makes a forged sidecar useless without read access to
  the key file (see the threat model below).
- VALID proofs use the historical preimage (no status field) so existing
  sidecars keep hitting. PARENT_SUSPECT and signature-fail TAMPERED bind the
  status into the HMAC so an attacker-writable sidecar cannot upgrade a
  cached failure to VALID by rewriting the digest set. A later store change
  that repairs ancestry changes the payload's parent hashes -> miss ->
  re-verify, which is the required direction.
- The content-hash comparisons in _sign.verify_fact (recomputing the fact's
  canonical hash against the committed hash — the check that actually catches
  F5 content poisoning) run on EVERY recall regardless of cache state; a hit
  only skips the redundant public-key operation. A tampered fact is therefore
  detected immediately, warm cache or not.
- UNSIGNED results are never cached (no signature to digest). Committed-hash
  TAMPERED results are not cached either — those checks run every recall.

Store stamp: the sidecar records the store file's (mtime_ns, size), captured
BEFORE the store is read, as informational metadata only — it is NOT a hard
invalidation key. Digests are individually content-bound (each HMAC covers that
fact's committed hashes + current parent hashes), so a still-valid fact hits
even after an unrelated rewrite of facts.json (append, supersede, consolidate,
purge). Only the facts that actually changed miss and re-verify. A stamp
mismatch marks the handle dirty so save() can record the new stamp and prune.

What still discards the whole digest set (fail closed: doubt costs speed, never
safety): unreadable, oversized, malformed, wrong version, or wrong key. A
missing sidecar is a clean cold start, not doubt.

Growth bound: a dirty save persists only digests that were hit() or add()ed
during THIS run. Purged or pre-edit digests are not live this run, so they
drop instead of accumulating forever across store rewrites.

Concurrent saves: save() is load-modify-replace with no lock. Two recalls
racing a store rewrite (hook + budget-recall, or two Claude sessions) used
to last-writer-wins: a stale v1 handle would os.replace over a v2 sidecar
and the next recall would pay a cold verification. save() now re-stats the
store immediately before os.replace and skips when the stamp no longer
matches self.store_sig — the stale writer keeps its in-memory set but does
not clobber the newer sidecar. When the stamp still matches, save() re-reads
the sidecar and unions digests: within one store stat the set is append-only
(VALID and status-bound non-VALID share it; union cannot upgrade a failure
because non-VALID HMACs bind the status). Across stamps, skip-not-union
preserves the prune bound.

Unwritable store: load_for_store probes the sidecar directory once per
handle. If the directory is read-only or full, the handle stays usable
in memory (hit/add still skip redundant pubkey ops for the rest of this
recall and later recalls in the same process) but save() does not touch
disk. At most one diagnostic is printed per process — never one line per
recall — so a stuck store cannot fill hook-errors.log.

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
fact content, so scrub/purge parity is unaffected: purging rewrites facts.json
(the store stamp changes, which is informational). The purged fact is not
hit-or-added this run, so its digest is pruned on the dirty save.
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

# Process-level degradation for an unwritable sidecar. A read-only or full
# store directory would otherwise print `could not save verification cache`
# on every recall (and append that line to hook-errors.log forever). After
# the first diagnostic, further save failures this process are silent; the
# digest set is kept in _memory so a later load_for_store in this process
# still hits.
_save_warned = False
_memory: dict[tuple, set[str]] = {}


def cache_path_for(store_path: Path) -> Path:
    return store_path.with_name(store_path.name + ".verified-cache.json")


class VerifiedSignatureCache:
    """The digest set for one store file, plus the bookkeeping to persist it.

    `hit`/`add` are the only calls on the verification hot loop; `save` runs
    once per store load and rewrites the sidecar only when something changed
    (a new signature determination recorded, an untrustworthy sidecar being
    replaced, or the store stamp moving — informational, but we persist the
    new stamp and prune to this run's live set). Digests are opaque: VALID
    proofs and status-bound non-VALID proofs share the same set. A save
    whose store stamp has moved is skipped so a stale writer cannot clobber
    a newer sidecar; a same-stamp save unions on-disk digests."""

    def __init__(self, path: Path, key_id: str, alg: str,
                 store_sig: dict, digests: "set[str]", dirty: bool = False,
                 store_path: "Path | None" = None):
        self.path = Path(path)
        self.key_id = key_id
        self.alg = alg
        self.store_sig = store_sig  # {"mtime_ns": int, "size": int} — metadata
        self.digests = set(digests)
        self._dirty = dirty
        self._live: "set[str]" = set()  # hit or added this run; prune target
        self.store_path = Path(store_path) if store_path is not None else None
        self._writable = True  # load_for_store clears this after a failed probe

    def hit(self, digest: str) -> bool:
        if digest in self.digests:
            self._live.add(digest)
            return True
        return False

    def add(self, digest: str) -> None:
        if digest not in self.digests:
            self.digests.add(digest)
            self._dirty = True
        self._live.add(digest)

    def save(self) -> None:
        """Persist the digest set (atomic replace, 0600). A failure is a
        one-line stderr note, NEVER an exception: budget-recall calls this
        unguarded on the recall hot path, so any propagating error would crash
        recall instead of degrading to slow-but-working verification. Catches
        Exception (not just OSError) so a serialization or filesystem surprise
        cannot escape; KeyboardInterrupt/SystemExit still propagate.

        A dirty save keeps only digests hit() or add()ed this run, so a store
        rewrite cannot accumulate stale entries for facts that are gone.

        Last-writer-wins is not safe across a store rewrite: a handle that
        loaded v1 would otherwise os.replace {v1-stat, v1-live} over a v2
        sidecar. Re-stat the store immediately before replace and skip when
        it no longer matches self.store_sig. When it still matches, re-read
        the sidecar and union — append-only within one stat, and sound with
        status-bound non-VALID digests in the same set because they are
        opaque HMACs that cannot hit as VALID."""
        if not self._dirty:
            return
        if self.store_path is not None and _store_sig(self.store_path) != self.store_sig:
            return
        self.digests = set(self._live)
        self.digests |= _peer_digests(self.path, self.key_id, self.alg,
                                      self.store_sig)
        if not self._writable:
            _stash_memory(self)
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
            # Re-stat immediately before replace: the store can still move
            # between the earlier check and here (the window the race lives in).
            if (self.store_path is not None
                    and _store_sig(self.store_path) != self.store_sig):
                return
            os.replace(tmp, self.path)
            tmp = None  # replaced; nothing to clean up
            _drop_memory(self)
        except Exception as exc:  # noqa: BLE001 - never crash recall on cache save
            self._writable = False
            _stash_memory(self)
            _warn_save(self.path, exc)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def load_for_store(store_path: Path, verify_key) -> "VerifiedSignatureCache | None":
    """Cache handle for `store_path` verified under `verify_key`.

    MUST be called before the store file is read: the store stamp is captured
    here as sidecar metadata (not an invalidation key). A store rewritten
    between this stat and the read records a stale stamp; the next recall
    notices the mismatch, rewrites the metadata, and prunes — still the safe
    direction. Returns None (caching off) when verification is off or the
    store is not statable.

    Probes sidecar-directory writability once here so save() does not pay a
    failed mkstemp on every recall. Unwritable: one diagnostic, in-memory
    caching for the rest of this process (union any previously stashed
    digests), no crash."""
    if verify_key is None:
        return None
    store_path = Path(store_path)
    try:
        st = store_path.stat()
    except OSError:
        return None
    store_sig = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
    path = cache_path_for(store_path)
    writable = _probe_writable(path.parent)
    if not writable:
        _warn_unwritable(path)
    digests, dirty = _load_digests(path, verify_key, store_sig)
    mem = _memory.get(_memory_key(store_path, verify_key.key_id, verify_key.alg))
    if mem:
        digests = set(digests) | mem
    cache = VerifiedSignatureCache(path, verify_key.key_id, verify_key.alg,
                                   store_sig, digests, dirty,
                                   store_path=store_path)
    cache._writable = writable
    return cache


def _load_digests(path: Path, verify_key, store_sig: dict) -> "tuple[set[str], bool]":
    """(digests, dirty). Empty on cryptographic/structural doubt — a MISSING
    sidecar is a clean cold start (dirty=False, nothing to rewrite);
    unreadable, oversized, malformed, wrong version, or wrong key marks dirty
    so save() replaces the untrustworthy sidecar even if this run proves no
    new signatures.

    A store-stamp mismatch is NOT doubt: the digest set is retained (each
    entry is self-authenticating) and dirty is set so save() records the new
    stamp and prunes to this run's live set.

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
            and isinstance(doc.get("digests"), list)
            and all(isinstance(d, str) for d in doc["digests"])
        ):
            # Store stamp is informational: retain the set even when it moved.
            dirty = doc.get("store") != store_sig
            return set(doc["digests"]), dirty
        return set(), True
    except Exception:  # noqa: BLE001 - fail closed to full verification, never crash
        return set(), True


def _store_sig(store_path: Path) -> "dict | None":
    """Current (mtime_ns, size) of the store, or None if it is not statable."""
    try:
        st = Path(store_path).stat()
    except OSError:
        return None
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def _peer_digests(path: Path, key_id: str, alg: str, store_sig: dict) -> "set[str]":
    """On-disk digests from a same-stamp sidecar, else empty.

    Concurrent same-stat writers each prune to their own live set; unioning
    the already-persisted sidecar keeps the other writer's append-only adds.
    A different store stamp, a foreign key/alg/version, or any read/parse
    failure returns empty so we persist our own live set and do not
    re-accumulate another generation's pruned entries (#47). VALID and
    status-bound non-VALID digests share the set (#48); union is still a
    set of opaque HMACs — a PARENT_SUSPECT digest cannot hit as VALID."""
    try:
        path = Path(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return set()
        if size > MAX_SIDECAR_BYTES:
            return set()  # implausibly large: distrust, don't read it in
        # Sidecar can vanish between stat and read (concurrent recall/cleanup).
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        doc = json.loads(raw)
    except Exception:  # noqa: BLE001 - merge is best-effort; write our live set
        return set()
    if not (
        isinstance(doc, dict)
        and doc.get("version") == CACHE_VERSION
        and doc.get("key_id") == key_id
        and doc.get("alg") == alg
        and doc.get("store") == store_sig
        and isinstance(doc.get("digests"), list)
        and all(isinstance(d, str) for d in doc["digests"])
    ):
        return set()
    return set(doc["digests"])


def _probe_writable(directory: Path) -> bool:
    """True iff we can create (and remove) a temp file in `directory`.

    os.access is not enough: a full disk, a 0555 directory, and a quota
    miss all look the same here — we cannot persist the sidecar."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(directory),
                                   prefix=".nb-vc-probe.", suffix=".tmp")
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _memory_key(store_path: Path, key_id: str, alg: str) -> tuple:
    try:
        ident = str(Path(store_path).resolve())
    except OSError:
        ident = str(store_path)
    return (ident, key_id, alg)


def _stash_memory(cache: VerifiedSignatureCache) -> None:
    if cache.store_path is None:
        return
    key = _memory_key(cache.store_path, cache.key_id, cache.alg)
    _memory.setdefault(key, set()).update(cache.digests)


def _drop_memory(cache: VerifiedSignatureCache) -> None:
    if cache.store_path is None:
        return
    _memory.pop(_memory_key(cache.store_path, cache.key_id, cache.alg), None)


def _warn_once(message: str) -> None:
    global _save_warned
    if _save_warned:
        return
    _save_warned = True
    print(message, file=sys.stderr)


def _warn_unwritable(path: Path) -> None:
    _warn_once(
        f"{path}: verification cache is unwritable; "
        "caching in-memory only this process",
    )


def _warn_save(path: Path, exc: BaseException) -> None:
    _warn_once(
        f"{path}: could not save verification cache ({exc}); "
        "further save failures this process will be silent",
    )


def sidecar_status(store_path: Path) -> dict:
    """present / fresh / writable for nockbrain-health.

    A missing sidecar is a cold start, not an outage. An uncreated
    parent directory is the same case: writable is False (nowhere to
    persist) but flagged is False (not an outage). An unwritable
    existing parent is the silent degradation this surfaces: every new
    process re-verifies. Fresh means the sidecar's recorded store stamp
    matches the current store (mtime_ns, size) — informational after
    per-entry retention. Does not affect recall_ready."""
    store_path = Path(store_path)
    path = cache_path_for(store_path)
    present = path.is_file()
    parent = path.parent
    parent_exists = parent.is_dir()
    writable = parent_exists and _probe_writable(parent)
    fresh = False
    if present:
        current = _store_sig(store_path)
        try:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = None
            if size is not None and size <= MAX_SIDECAR_BYTES:
                try:
                    raw = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    raw = None
                if raw is not None:
                    doc = json.loads(raw)
                    fresh = (
                        isinstance(doc, dict)
                        and doc.get("version") == CACHE_VERSION
                        and current is not None
                        and doc.get("store") == current
                    )
        except Exception:  # noqa: BLE001 - unreadable sidecar is not fresh
            fresh = False
    return {
        "path": str(path),
        "present": present,
        "fresh": fresh,
        "writable": writable,
        "flagged": parent_exists and not writable,
    }
