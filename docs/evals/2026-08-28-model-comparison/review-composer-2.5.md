# Skeptical review: fix train #47–#93 (verification cache, sidecars, signing, recall, consolidation)

Review date: 2026-08-28. Scope: commits from perf #44 through fix #93 on this branch. Every finding below was traced in source and cross-checked against tests where noted.

---

## 1. Legacy-signed v2 claim-authority facts are excluded from recall with no session-visible error

**Severity:** high

**Citations:**
- `bin/_sign.py:681-688` — `sign_facts` routes v2-authority facts to `sign_claim_fact_v2`; bulk legacy signing is documented as recall-breaking.
- `bin/_sign.py:950-951` — legacy `verify_fact` branch returns `TAMPERED` when any v2-only authority field is present on the fact but the attestation is not v2 schema.
- `bin/budget-recall.py:639-640` — `_verify_filter` drops every `TAMPERED` fact before ranking.
- `bin/resign-v2-authority-facts.py:4-9` — documents that wrongly legacy-signed v2 facts “silently drop out of recall.”

**Failure scenario:** A store still containing v2 fleet/promotion facts that were bulk legacy-signed before #82 (or never repaired via `resign-v2-authority-facts.py --apply`) loads normally, passes health checks on `facts.json`, but recall injects none of those facts. The hook still exits 0; the only signal is a stderr line like `excluded N tampered` appended to `~/.nock-brain/hook-errors.log` (`hooks/memory-inject.sh:86`), which operators rarely read.

**Verification:** Read `verify_fact` routing and `_verify_filter` drop logic. Confirmed by `tests/test_resign_v2_authority_facts.py` (routing fix) and `tests/test_verify_on_recall.py:48-62` (tampered facts excluded). Ran `pytest tests/test_resign_v2_authority_facts.py tests/test_verify_on_recall.py` — all passed.

---

## 2. Stale `insights.json` can hide still-valid source facts from recall

**Severity:** high

**Citations:**
- `bin/budget-recall.py:879-882` — after insight search, every `source_id` listed on a matching insight is removed from `fact_results` before merge.
- `bin/rebuild-store.py:411-417` — documents that a stale derived insights view “keeps injecting what the store no longer says” and that insights rank first.
- `bin/nockbrain-health.py` — no insights freshness check (grep for `insights` in this module returns nothing).

**Failure scenario:** `facts.json` is updated (promotion apply, consolidation, manual edit) but `insights.json` is not regenerated. A query matches an old insight whose `source_ids` still point at live facts. Recall injects the outdated synthesis and **never surfaces the underlying facts**, even when those facts would rank highly on BM25/dense. No error is returned to the session.

**Verification:** Read `select_recall` dedup block. Confirmed intended behavior in `tests/test_budget_recall.py:231-253` (`test_insights_surface_first_and_dedup_their_sources`). Contrasted with `rebuild-store.py:411-428`, which regenerates insights post-promote but is not invoked by other store writers (see finding 4).

---

## 3. Stale embedding rows are skipped silently in semantic recall

**Severity:** medium

**Citations:**
- `bin/_dense_recall.py:126-127` — dense candidate dropped when sidecar `hashes[idx]` ≠ live `content_hash(fact)`; no log line for this branch.
- `bin/_embed.py:67-68` — `content_hash` is SHA-256 of embedded text prefix.
- `bin/rebuild-store.py:433-445` — semantic sidecar sync runs only when `semantic-on` exists; failures are non-gating.

**Failure scenario:** Semantic tier is enabled (`~/.nock-brain/semantic-on` or `NOCKBRAIN_SEMANTIC=1`). A fact’s `content` changes (edit, re-extract, promotion) but `embed-facts.py` is not run. BM25 may still surface the fact, but the dense path skips it on hash mismatch. Paraphrase-heavy queries lose the best semantic match with no user-visible error (only generic “no usable vector sidecar” when the whole file is missing: `_dense_recall.py:86-88`).

**Verification:** Read dense filter loop; confirmed no stderr on per-row stale skip. `tests/test_dense_recall.py` covers non-finite sims (#46) but not stale-hash logging.

---

## 4. `apply-promotion-batch.py` refreshes neither insights nor embeddings after a successful apply

**Severity:** medium

**Citations:**
- `bin/apply-promotion-batch.py:148-167` — after writing `facts.json`, only `sign-facts.py` and `verify-facts.py` run; no `synthesize.py` or `embed-facts.py`.
- `bin/rebuild-store.py:411-428` — `refresh_insights` (post-promote insight regen).
- `bin/rebuild-store.py:433-463` — `refresh_semantic_sidecar` (post-promote embed sync).

**Failure scenario:** Operator applies one or more promotion batches. New facts are signed and verified, but `insights.json` and `embeddings.npz` remain from the pre-apply store. Recall combines findings 2 and 3: stale insights suppress sources; semantic tier uses stale or missing vectors for new facts.

**Verification:** Read `apply-promotion-batch.py` end-to-end and compared artifact refresh list to `rebuild-store.py` post-promote hooks.

---

## 5. `consolidate-facts --execute` has the same derived-view gap

**Severity:** medium

**Citations:**
- `bin/consolidate-facts.py:377-380` — writes `facts.json` after supersession; prints OPS rule to run `sign-facts.py` / `verify-facts.py`, nothing about insights or embeddings.
- `bin/consolidate-facts.py:46-47` — OPS rule text.

**Failure scenario:** Near-duplicate consolidation superseded losers. Canonical facts remain, but matching insights may still reference superseded ids or outdated themes. Insight-led dedup (`budget-recall.py:879-882`) can continue to hide raw facts or surface wrong synthesis until manual `synthesize.py --sign`.

**Verification:** Read execute path; no call to insight or embed tooling.

---

## 6. Default recall still injects `parent-suspect` derived facts

**Severity:** medium

**Citations:**
- `bin/budget-recall.py:623-625` — default mode keeps `PARENT_SUSPECT` (only `TAMPERED` is always dropped).
- `bin/budget-recall.py:639-643` — strict mode drops non-`VALID`.
- `bin/_sign.py:1000-1005` — `PARENT_SUSPECT` when parent core drifted or absent.

**Failure scenario:** Parent fact tampered or removed; child attestation still cryptographically consistent with committed child hashes but Merkle ancestry is broken. Default recall injects the child as reference material. `--strict-verify` / `NOCKBRAIN_STRICT_VERIFY=1` excludes it, but the hook does not enable strict mode (`hooks/memory-inject.sh:86`).

**Verification:** `tests/test_verify_on_recall.py:111-126` explicitly expects child kept in default mode and excluded under strict.

---

## 7. Signing-key load failure disables attestation verification entirely (fail-open)

**Severity:** medium

**Citations:**
- `bin/budget-recall.py:598-616` — documents fail-open; any load exception prints stderr and returns `None`.
- `bin/budget-recall.py:632-633` — `verify_key is None` → `_verify_filter` returns facts unchanged.
- `hooks/memory-inject.sh:86` — stderr redirected to log, not the session.

**Failure scenario:** `signing-key.pub` exists but is corrupt/unreadable (or Ed25519 key loaded without `cryptography`). Recall runs with verification off: tampered signed facts, bogus attestations, and poisoned content all rank normally. Same behavior as having no key (`tests/test_verify_on_recall.py:135-152`).

**Verification:** Read `_resolve_verify_key` and `_verify_filter` early return; confirmed by `test_no_signing_key_skips_verification`.

---

## 8. Malformed fact records are dropped at load with stderr only

**Severity:** medium

**Citations:**
- `bin/_facts.py:16` — `RECALL_ITEM_FIELDS` (used on recall load) does not include `id` or `evidence`.
- `bin/_facts.py:62-68` — malformed records skipped; one stderr line per load.
- `bin/budget-recall.py:677` — recall load uses `RECALL_ITEM_FIELDS`.
- `hooks/memory-inject.sh:86` — budget-recall stderr → `hook-errors.log`.

**Failure scenario:** Facts missing any of `kind`, `status`, `confidence`, `content`, or `source_date` are removed from the recall corpus. A partial store corruption or bad ingest shrinks recall with no session error; operators must notice `skipped N malformed fact record(s)` in the hook error log.

**Verification:** Read `filter_valid_facts` and `_load`; contrast with `REQUIRED_FACT_FIELDS` at `_facts.py:15` (full store contract includes `id` and `evidence`).

---

## 9. E2 SQLite selection can recall a different corpus than `facts.json` on disk

**Severity:** low (documented not cut over; latent for cutover)

**Citations:**
- `bin/_storeback.py:302-319` — `resolve_store`: `store-v2` marker + existing `brain.db` selects `SqliteStore` even when the path argument is `facts.json`.
- `bin/budget-recall.py:673-677` — `_load` always calls `resolve_store(path)` then `store.load_facts(...)`.
- `docs/REPO-MAP.md` — JSON authoritative; E2 SQLite not cut over (`NOCKBRAIN_STORE=json` kill switch).

**Failure scenario:** After cutover, `brain.db` is updated but `facts.json` export lags. Recall (and verification-cache sidecar keyed to `brain.db` via `SqliteStore.freshness_path`) serves DB contents while operators inspecting `facts.json` see a different fact set. No error if both files exist.

**Verification:** Read `resolve_store` and `JsonStore` / `SqliteStore` freshness paths in `_storeback.py:160-163, 189-191`.

---

## 10. Unwritable verification-cache directory: silent per-process re-verify cost

**Severity:** low

**Citations:**
- `bin/_verify_cache.py:324-328` — unwritable sidecar dir → in-memory cache only, one stderr diagnostic per process.
- `bin/nockbrain-health.py:354-357` — flags `VERIFICATION CACHE UNWRITABLE`.
- `tests/test_health_degradations.py:221` — `recall_ready` remains `True` when cache is unwritable.

**Failure scenario:** Read-only or full `~/.nock-brain` (or sidecar parent). Every new hook process pays full Ed25519 verification (~0.4–0.8s on a 2.5k-fact store per module header). Recall results stay correct; performance budget may be blown intermittently. Health warns, but recall is not blocked.

**Verification:** Read `_probe_writable` / `_warn_unwritable`; ran `tests/test_verify_cache.py` unwritable test and health test above (pass).

---

## What the fix train got right (no finding — verified)

These were specifically checked and did **not** show recall-correctness bugs:

- **Content poisoning vs warm cache:** `verify_fact` recomputes committed hashes before any cache short-circuit (`bin/_sign.py:970-977`, `939-940`). `tests/test_verify_cache.py` tamper-after-rewrite tests pass.
- **Sidecar forgery:** Cache digests are HMAC-keyed under key material (`bin/_sign.py:836-858`, `CACHE_VERSION = 2` in `bin/_verify_cache.py:109`). Forged digests do not produce `VALID` hits (`tests/test_verify_cache.py` forgery tests).
- **Concurrent cache save races:** Stale writer skips when store stamp moved (`bin/_verify_cache.py:269-270`, `288-293`); same-stamp union (`bin/_verify_cache.py:421-452`). Regression tests pass.
- **`sign_facts` v2 routing (#82):** Per-fact routing in `bin/_sign.py:696-702` matches `tests/test_resign_v2_authority_facts.py`.
- **Dense non-finite sims (#46):** Corrupt rows sorted last and excluded (`bin/_dense_recall.py:105-121`).

---

## Overall verdict

The #47–#93 fix train is **materially stronger on verification-cache correctness** than the pre-#47 design: content-hash checks are not skipped on cache hits, sidecar forgery is closed, concurrent-save clobbering is handled, and PARENT_SUSPECT caching is status-bound. I did not find a warm-cache path that admits tampered **content** into ranked results.

The remaining risk is not “cache returns wrong VALID” but **silent recall shrinkage and stale derived layers**: unrepaired N9851 v2 facts are dropped as `TAMPERED` without session-visible errors; stale `insights.json` actively suppresses source facts; stale `embeddings.npz` quietly removes semantic candidates; and several store writers (`apply-promotion-batch`, `consolidate-facts`) update `facts.json` without the derived-view refresh that `rebuild-store.py` already knows is necessary. Operational discipline (resign-v2, sign-facts, synthesize --sign, embed-facts, read `hook-errors.log`) is still part of the correctness contract—the code fixes made the crypto path trustworthy, but production recall quality still depends on keeping sidecars and derived JSON in sync with the authoritative store.
