# Skeptical review — the #47–#93 fix train

Scope: verification-cache / sidecar / signing / revocation / recall / consolidation,
with a specific hunt for **silent** recall degradation (wrong or missing results with
no error surfaced). Every claim below was traced to the cited lines and, where noted,
reproduced. Findings are ordered by severity.

Bottom line up front: the crypto core (`_sign.py`, `_verify_cache.py`) and the
supersession/revocation layer are genuinely well-built and I could not break them. The
real defects are on the **fail-open boundary** — two load paths that are contracted to
degrade gracefully but instead *raise*, and on the hook path a raise means recall
silently returns nothing.

---

## 1. HIGH — A byte-corrupt `facts.json` crashes recall silently; `load_facts` violates its "never raises" contract

**Files:** `bin/_facts.py:82-85` (root cause); reached on the hook path via
`bin/_storeback.py:165-166` → `bin/budget-recall.py:676-677`. Same blind spot in the
health tool at `bin/nockbrain-health.py:26-29` (`load_json`, called at line 229).

REPO-MAP §4 states a non-negotiable: *"`load_facts` never raises; corrupt store → `[]`
+ stderr line."* The handler does not deliver that:

```python
# bin/_facts.py:81-86
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"{label}: skipped malformed fact store ({exc})", file=sys.stderr)
    return []
```

`path.read_text(encoding="utf-8")` raises `UnicodeDecodeError` on invalid UTF-8 bytes.
`UnicodeDecodeError` subclasses **`ValueError`**, not `OSError` and not
`json.JSONDecodeError`, so it escapes the `except` and propagates out of `load_facts`.
The catch only covers *valid-UTF-8-but-invalid-JSON*; a byte-corrupt store slips past.

**Failure scenario (silent recall outage):** a `facts.json` truncated mid-multibyte
character. This is realistic because several store writers are non-atomic and emit
non-ASCII with `ensure_ascii=False` — e.g. `purge-fact.py` (`secure_write_json` →
`_store.secure_write_text`, write-then-chmod, not atomic), `apply-promotion-batch.py:148`
(`facts_path.write_text(...)`), `ingest-curated-memory.py:243-245`, and the rebuild
staging merge at `rebuild-store.py:247-249` — so a crash mid-write can leave a torn tail.
On the next recall the exception propagates through `JsonStore.load_facts` →
`budget-recall._load` (neither guards it), the process dies, and `hooks/memory-inject.sh`
(designed to always exit 0, stderr → `hook-errors.log`) swallows the traceback and emits
`{}`. Recall then produces **nothing** — indefinitely, until a human notices — instead of
the contracted `[] + stderr` degrade. This is exactly the "corrupt store → silent
degradation" mode the review targets.

**Compounding:** the operator's own detector shares the blind spot and is worse.
`nockbrain-health.py`'s `load_json` (lines 26-29) wraps *no* exception at all, so it
crashes on a byte-corrupt **or** a plainly malformed (`JSONDecodeError`) `facts.json`.
So when the store is corrupt, the tool meant to *surface* the silent recall failure is
itself down. (Within `rebuild-store` this is fail-closed — a health-subprocess crash
aborts the promote — so live is protected there; the gap is the manual/diagnostic path
and the live hook.)

**Verified:** class hierarchy (`issubclass(UnicodeDecodeError, ValueError)` = True,
`…, OSError)` = False), and reproduced end-to-end: a file containing
`b'[{"id":"x","content":"caf\xe9 '` makes both `_facts.load_facts` and
`_storeback.JsonStore(p).load_facts()` raise `UnicodeDecodeError` rather than return `[]`.
The invalid-JSON path *is* covered and tested (`tests/test_fact_store.py:29`); there is no
test for non-UTF-8 bytes.

**Fix:** catch `ValueError` (or add `UnicodeDecodeError`) at `_facts.py:83`; give
`nockbrain-health.load_json` an equivalent guard.

**Attribution note:** this handler predates the #47–#93 train — it is a *left-behind*
gap in the fail-open contract, not a regression the train introduced. It is in scope as a
live silent-degradation path, and the train's non-atomic writers make the trigger more
reachable.

---

## 2. MEDIUM — A corrupt vector sidecar crashes semantic recall instead of degrading to BM25

**Files:** `bin/_embed.py:174-188` (`load_sidecar` validation/except gaps) →
`bin/_dense_recall.py:126` (and `_embed.py:179`); propagates through the unguarded
`bin/budget-recall.py:811` (`_dense_recall.fuse`) and `:853` (`select_recall`).

REPO-MAP §6 non-negotiable: *"BM25 is the floor, always. Every optional tier degrades to
the seeds unchanged."* The `_dense_recall` module docstring repeats that corrupt sidecar
rows degrade silently. `load_sidecar` does not fully back that up:

```python
# bin/_embed.py:174-188
try:
    with np.load(path, allow_pickle=False) as archive:
        sidecar = {
            "ids":    [str(i) for i in archive["ids"]],
            "hashes": [str(h) for h in archive["hashes"]],
            "model":  str(archive["model"][0]),      # line 179
            "mat":    archive["mat"].astype(np.float32),
        }
except (OSError, KeyError, ValueError, zipfile.BadZipFile):  # line 182
    return None
if len(sidecar["ids"]) != sidecar["mat"].shape[0]:           # line 184
    return None
```

Two gaps, both yielding an `IndexError` — which is **not** in the line-182 catch set and
is **not** caught anywhere upstream (`_dense_recall.fuse`'s try/except at lines 75-84 only
wraps `get_encoder`/`load_sidecar` and only catches `EmbedUnavailable`; the RRF loop at
119-137 is unguarded; `_maybe_dense_fuse` and `select_recall` add no guard):

- **Trigger A — short `hashes`:** the only length check (line 184) compares `ids` vs
  `mat` rows; `len(hashes)` is never validated. A sidecar whose `hashes` array is shorter
  than `ids` (but `len(ids) == mat.shape[0]`) passes as "usable". Then in the fusion loop,
  `sidecar["hashes"][idx]` at `_dense_recall.py:126` — with `idx` drawn from an argsort
  over all `mat` rows — indexes past the end → `IndexError`.
- **Trigger B — empty `model`:** `str(archive["model"][0])` at line 179 raises
  `IndexError` on an empty `model` array, escaping the line-182 catch directly out of
  `load_sidecar`.

Either way the exception propagates to `select_recall` and **crashes the entire recall**
(not just the semantic tier), the opposite of the "BM25 is the floor" guarantee. Impact is
limited to brains with the semantic tier enabled (`semantic-on` marker + `NOCKBRAIN_SEMANTIC=1`).

**Verified:** read the call chain end to end and confirmed no try/except covers the loop
or the `fuse` call. A *missing* `ids`/`hashes`/`model` key is fine (that is `KeyError`,
caught); the gap is specifically a length/shape mismatch or empty `model` array.

**Fix:** validate `len(hashes) == mat.shape[0]` and `len(model) >= 1` at line 184, and/or
add `IndexError` to the line-182 catch set. (Same partial-coverage weakness applies to the
unguarded `graph_from_facts()` build at `_graph_recall.py` — worth hardening alongside,
though I could not construct an input that makes it raise on otherwise-valid facts.)

---

## 3. LOW (latent) — `ingest-curated-memory.py` bypasses the per-fact signing router and its self-verify is non-blocking

**File:** `bin/ingest-curated-memory.py:236-238`, `:243-245`, `:248-249`, `:281`.

CLAUDE.md's first non-negotiable: *"Never sign facts except through `_sign.sign_facts`"*
(the N9851 trap — a v2-authority fact routed through legacy `sign_fact` verifies as
`TAMPERED` and is silently dropped from recall). This CLI signs curated facts with
`sign_fact` **directly in a loop** rather than `_sign.sign_facts`:

```python
for fact in curated:
    sign_fact(fact, key, facts_by_id={})      # line 237-238 — bypasses per-fact routing
merged = kept + curated
store_path.write_text(json.dumps(merged, …))  # line 243-245 — store written FIRST …
statuses = {verify_fact(f, key, …) for f in curated}  # line 248-249 — … verified AFTER
```

`main()` (line 277-281) only *prints* `verify_statuses`; it never checks them and never
rolls back. Safe **today** only because `build_fact` hardcodes a fixed schema that emits no
v2-only authority field and no frontmatter flows into one. But the two weaknesses compound:
if `build_fact` ever grew a v2-authority field, every curated fact would be legacy-signed →
`TAMPERED` → silently dropped from recall (N9851), and the store at line 243-245 would
already be persisted corrupt with no abort. (The write is also a plain non-atomic
`write_text`, not `secure_write_json`.)

**Verified:** read the routing predicate `_sign.is_v2_claim_fact` and confirmed `sign_fact`
does no v2 routing; confirmed write precedes verify and `main` ignores the statuses.

**Fix:** route through `_sign.sign_facts`; make the self-verify blocking (no write / nonzero
exit on any non-`VALID` status).

---

## 4. LOW (hardening) — `apply-promotion-batch.py` chain check can be holed by a null digest, and a failed post-apply verify wedges re-runs

**File:** `bin/apply-promotion-batch.py:66-76`, `:148`, `:157-165`.

```python
def check_chain(batches):
    prev_digest = None
    for batch in batches:
        parent = batch.get("parent_batch_digest") or None
        if prev_digest is not None and parent != prev_digest:   # line 71
            raise ApplyError(…)
        prev_digest = batch.get("batch_digest")                 # line 76
```

- **Null-digest hole:** if any batch lacks `batch_digest`, line 76 sets `prev_digest =
  None`, and line 71's `if prev_digest is not None` then **skips the next batch's parent
  comparison entirely** — a fail-closed chain gate that a single null digest punches
  through. The chain root is also never anchored to the last-applied digest recorded in
  `state`, so the first fetched batch's `parent_batch_digest` is unchecked. Both require the
  (trusted) NockCC producer to emit an unusual batch, so neither is a live exploit — hence
  LOW/hardening.
- **Wedged re-run on verify failure:** the store is written at line 148, then re-signed and
  strict-verified as subprocesses; the batch is recorded applied only after both pass
  (correct — good). But on a `verify-facts.py` failure (line 157-161) the function returns 1
  **leaving the appended facts in `facts.json`** and only telling the operator to restore the
  backup by hand. Because `state` was not updated, a naive re-run re-applies the same facts →
  `apply_batch` aborts on id-collision (line 86-92). The applier is then stuck aborting every
  run until the operator manually restores. Fail-closed and loud, not silent — but a real
  operational sharp edge.

**Verified:** traced `check_chain`, the additive-only collision guard, and the
record-after-verify ordering; confirmed no auto-rollback on the verify-fail branch.

---

## Areas audited and found sound (not reported as bugs)

Recorded so the clean areas are explicit, not merely unmentioned:

- **`_verify_cache.py` (the whole #44/#47/#48/#49/#50/#51/#52/#53/#54/#88/#90/#91/#92
  train).** The cache cannot mint or drop a fact without key-file read access: the
  committed-content-hash comparisons in `verify_fact` (`_sign.py:972-977`, and the v2
  `payload` check at `:939`) run on **every** recall *before* any cache consultation, so a
  poisoned fact is `TAMPERED` regardless of cache state; digests are HMAC-keyed under
  `cache_key_material()`, so a forged sidecar can neither mint `VALID` nor force a `TAMPERED`
  drop. Per-entry retention (#47), status-bound non-VALID digests (#48/#88), the
  re-stat-before-replace concurrency skip (#90, via `_store.secure_write_json_atomic`'s
  `before_replace`), and the store-first-then-unlink purge ordering (#91) are all internally
  consistent.
- **N9851 routing (#82).** `sign_facts` routes per fact via `is_v2_claim_fact`; `sign-facts.py`,
  `resign-v2-authority-facts.py`, and `edit-fact.py` (which refuses v2 + Merkle-parent facts)
  all honor it. `#88`'s second commit correctly stopped inferring `PARENT_SUSPECT` from a bare
  child-signature failure (`att["signature"]` is attacker-mutable) and now requires independent
  ancestry evidence via `_parent_set_moved`.
- **Revocation / supersession layer.** Consolidation is mark-only (`dedup-facts`,
  `consolidate-facts`, `supersede-fact` write only lifecycle fields, never the signed core);
  resurrection detection, the key-rotation ring, and the single `blocking_findings`
  exit-invariant hold; `backfill-revocations` never writes `facts.json`; `consolidate-facts`
  excludes `correction` and refuses on manifest drift.
- **Recall ranking.** Optional-tier off-paths are pure pass-throughs (identical seed object,
  no imports); recency never multiplies cosine (dense gates are filter-only); non-finite /
  stale / orphan dense rows are excluded; graph neighbors rank strictly below the weakest
  seed; the date-diversity cap defers rather than drops and exempts reserved ids; the
  reserved-slot budget precommit does not double-count.
- **Python 3.9 floor.** All twelve hook-reachable modules carry `from __future__ import
  annotations` (`_verify_cache.py` has it at line 93, after its long docstring) and the
  floor-test closure list still matches. Full suite: **684 passed, 0 skipped**.

---

## Overall verdict

The fix train is, on the whole, careful and defensible work: the parts most people would
expect to be fragile — the attacker-writable verification-cache sidecar, the v1/v2 signing
split, and the attested-supersession machinery — are the parts that held up best under
adversarial reading, with the security-relevant invariants (content-hash-before-cache,
HMAC-keyed digests, mark-only consolidation, fail-closed revocation exits) all intact and
well-tested. The genuine weakness is not in the clever code but on its unglamorous edges:
two load paths (`_facts.load_facts` for a byte-corrupt store, `_embed.load_sidecar` for a
malformed vector sidecar) are contracted to *degrade* but actually *raise*, and because the
recall hook is engineered to swallow every error and exit 0, a raise there converts to a
**silent, open-ended recall outage** rather than the intended graceful fallback — the exact
class of failure this system's whole fail-open doctrine exists to prevent. Neither is a
data-integrity or security hole, and neither was introduced by the train's headline changes,
but Finding 1 in particular deserves a prompt one-line fix (widen the exception) plus a
non-UTF-8 regression test, since it defeats a stated non-negotiable and blinds the health
detector at the same time. Fix those exception boundaries and the train is in solid shape.
