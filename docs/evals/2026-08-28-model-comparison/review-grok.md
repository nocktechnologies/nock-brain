# Review of the #47–#93 fix train

Skeptical pass over the verification-cache, sidecar, signing, recall, and
consolidation work (roughly #47 through #93: `cf36ac7` … `4be09b9`). Every
finding below is checked against the current tree. Line numbers are 1-based
in that tree. Findings that are only documented fail-open, or that cannot
change recall results, are omitted.

The cache/sidecar series (#47–#92) is internally careful: content-hash checks
still run on a warm cache, non-VALID digests bind status into the HMAC, dirty
saves prune, stale writers skip on stamp change, purge rewrites the store
before unlinking the sidecar. The serious holes are one layer out — v2
claims never taught to recall, revocation never on the hot path, and live
store writes that are still non-atomic.

---

## 1. Canonical v2 claim facts never enter recall

**Severity:** high

**Where:** `bin/_facts.py:16`, `bin/_facts.py:36-42`, `bin/_facts.py:50-68`,
`bin/budget-recall.py:676-677`, `tests/test_claim_attestation_v2.py:35-63`,
`bin/apply-promotion-batch.py:93-99`

**Failure:** A correctly v2-signed claim (`verify_fact` → `VALID`) is written
to `facts.json`. The next hook recall loads with `RECALL_ITEM_FIELDS`, which
requires `source_date`. The v2 records this repo constructs
(`make_claim_fact`) have `source_time` / `valid_from` and no `source_date`.
`claim_payload_v2` never mentions `source_date`. `apply-promotion-batch`
stamps only `machine` and `applied_at`.

`filter_valid_facts` skips the claim as malformed, prints `skipped N
malformed fact record(s)` on stderr, and the hook appends that to
`hook-errors.log` and injects nothing for that fact. Existing v1 facts still
rank. The operator sees a healthy signed store and a silent hole where the
promoted claim should have been.

This is the leftover N9851-class *recall* hole: #82 fixed *signing* so v2
facts verify `VALID`; the hot path still refuses them unless they also carry
v1 recall fields.

**Verified:** Read `RECALL_ITEM_FIELDS` vs `make_claim_fact()` field-for-field.
`malformed_fact_reason` is `field not in fact` — not “empty” — so a missing
key is enough. `nockbrain-health` uses `REQUIRED_FACT_FIELDS` (also includes
`source_date`) on the raw JSON, so a v2-only store also flips `recall_ready`
false, but apply-promotion-batch writes live and bypasses that gate.

---

## 2. v2 authority windows are ignored

**Severity:** high

**Where:** `bin/_facts.py:131-148`, `bin/_sign.py:307-333`,
`bin/budget-recall.py:427-434`

**Failure:** `fact_currently_valid` reads only `valid_at` / `invalid_at`. v2
signs `valid_from` / `valid_to` (`claim_payload_v2`). Grep of
`budget-recall.py`, `_facts.py`, `_dense_recall.py`, and `_graph_recall.py`
shows zero uses of `valid_from` / `valid_to`.

A v2 claim that also has `source_date` (so it survives finding 1), with
`valid_to` in the past or `valid_from` in the future, still ranks and
injects. BM25, dense, and graph all use the same `currently_valid` callback.
The conformance fixture even has an `"expired"` case keyed on `valid_to`.

**Verified:** Read `fact_currently_valid` and `claim_payload_v2` side by side.
The two window vocabularies do not meet.

---

## 3. `apply-promotion-batch` mutates live `facts.json` before sign/verify, then does not roll back

**Severity:** high

**Where:** `bin/apply-promotion-batch.py:145-161`,
`tests/test_apply_promotion_batch.py:79-95`,
`bin/_sign.py:681-703`, `bin/budget-recall.py:623-643`

**Failure:** The applier backups, then `facts_path.write_text(...)` of the
merged store, *then* shells `sign-facts.py` and `verify-facts.py`. On
non-zero it prints “restore from `facts.json.bak-preapply-…`” and returns 1.
It does not restore. `applied-batches.json` is not updated (good). The live
store already contains the new payload facts.

If `sign_facts` raises `ClaimAttestationError` on a malformed v2 claim, or
`verify-facts` exits 2/4, those new facts sit UNSIGNED (or mixed). Default
recall **keeps UNSIGNED**. The next hook injects the batch content with no
session-visible error.

Rerun is worse: `apply_batch` is additive-only, so the already-written ids
collide and the batch is refused. Operator is stuck with a dirty store until
they notice the backup line.

`test_run_aborts_before_write_when_verify_fails` mocks `subprocess.run` to
return 1 and only asserts `applied-batches.json` is absent. It does not
assert `facts.json` is unchanged. The test name is false: the write at
line 148 already happened.

**Verified:** Read `run()` order: backup → write → sign → verify → record
state. Read the test body; there is no `facts.json` assertion after the
failed run.

---

## 4. Recall never audits revocations; the nightly merge can resurrect a superseded fact

**Severity:** high

**Where:** `bin/budget-recall.py:619-643` (no `_revoke` import anywhere in
that file), `bin/rebuild-store.py:156-172`, `bin/rebuild-store.py:226-231`,
`bin/rebuild-store.py:60`, `bin/rebuild-store.py:322-334`,
`bin/extract-facts.py:109-110`, `bin/refine-sessions.py:119-123`,
`bin/_revoke.py:165-168`

**Failure:** S1 (`#64`/`#66`) made flipping `status` back to `current` a
hard `verify-facts` failure (`resurrected`). The injection path never calls
`_revoke.audit`. A current fact that a valid revocation event says is dead
still verifies `VALID` (status is outside the signed core) and is injected.

The nightly makes that concrete without a hand edit:

1. Dedup/supersede marks a fact from the last `--since 3` window.
2. Next `rebuild-store` re-extracts the same transcript. `make_id` is
   `sha256(date + content[:200])[:12]`; refine always mints
   `status: "current"`.
3. `merge_facts` lets recent win on id, replacing the live superseded
   object (lifecycle fields gone).
4. `sign_and_export` runs `sign-facts.py` only — not `verify-facts.py`.
5. `PROMOTE_ARTIFACTS` is `facts.json, sessions, review, vault, graph.json`.
   `revocations.jsonl` is not promoted and not rewritten, so the old event
   remains.
6. `verify-facts` would exit 4. Rebuild never runs it. Recall never checks.
   The revoked fact is injected again.

The anti-amnesia gate is count-only (`staged_count < live_count`). Health
gate ignores revocations. Tests pin “recent wins” as desired
(`test_rebuild_store.py`) and do not cover a superseded collision.

**Verified:** `grep _revoke bin/budget-recall.py` is empty. Read `merge_facts`
(“recent wins on collision”), refine’s always-`current` mint, and
`PROMOTE_ARTIFACTS`. `_revoke.audit`’s `resurrected` list is exactly this
state, and nothing on the hot path consults it.

---

## 5. Live `facts.json` writes are still truncate-then-write; a concurrent hook recall goes empty

**Severity:** high

**Where:** `bin/_store.py:24-32`, `bin/_store.py:91-102`,
`bin/_storeback.py:168-169`, `bin/sign-facts.py:67`,
`bin/apply-promotion-batch.py:148`, `bin/purge-fact.py:174`,
`bin/consolidate-facts.py:377`, `bin/_facts.py:72-85`,
`hooks/memory-inject.sh:86-90`

**Failure:** #91 added `secure_write_json_atomic` (mkstemp + chmod 0600 +
`os.replace`) and used it for the verification-cache sidecar. The
authoritative store still goes through `secure_write_text` →
`Path.write_text` (`mode='w'`), which truncates the file before the new
bytes land. `apply-promotion-batch` does not even use `secure_write_*`.

A hook recall overlapping any live writer (sign-facts, extract, edit, purge,
dedup `--apply`, consolidate `--execute`, apply-promotion-batch) can
`json.loads` an empty or partial file. `load_facts` catches
`JSONDecodeError`, prints one stderr line, and returns `[]`. The hook
discards stderr and emits `{}`. No `recall-degradations.jsonl` row is
written on the JSON backend — that log is SQLite-only.

**Verified:** Read `secure_write_text` (“Write then chmod. Not atomic”).
`JsonStore.replace_all` calls `secure_write_json`. `load_facts` empty-on-error
path. `_record_degradation` is only referenced from `SqliteStore.load_facts`.

---

## 6. `purge-fact --apply` does not touch `insights.json`; purged text can still lead recall

**Severity:** high

**Where:** `bin/purge-fact.py:151-188`, `bin/budget-recall.py:35`,
`bin/budget-recall.py:863-882`, `bin/budget-recall.py:960-961`

**Failure:** GDPR-style purge rewrites facts, events, notes, vault,
embeddings, and the verified-cache sidecar. It never opens `insights.json`.
Default recall loads `~/.nock-brain/insights.json` (argparse default; the
hook does not pass `--insights`) and **prepends** matching insights, then
drops source facts that those insights list.

After a successful purge of fact F, an insight whose `source_ids` include F
and whose content still carries F’s prose still matches and is injected
first. `test_purge_fact.py` never mentions insights.

**Verified:** Read the `--apply` block in `purge-fact.py`; the artifact list
has no insights path. Read `DEFAULT_INSIGHTS` and `select_recall`’s insight
lead + covering step.

---

## 7. `consolidate-facts --execute` supersedes without signed revocations

**Severity:** high

**Where:** `bin/consolidate-facts.py:64` (imports),
`bin/consolidate-facts.py:212-231`, `bin/consolidate-facts.py:377`

**Contrast:** `bin/dedup-facts.py:255` and `bin/supersede-fact.py:85,116`
both call `record_supersessions`.

**Failure:** After the double gate (`--execute --i-have-reviewed-the-manifest`),
losers get `status=superseded` and `superseded_by`. There is no `_revoke`
import and no `revocations.jsonl` append. `apply_supersessions` also never
closes `invalid_at` (dedup/supersede do).

Recall still drops `status=="superseded"`, so this is not an immediate leak.
It is an S1 hole: flipping `status` back to `current` is invisible to
`verify-facts` (no trusted event → not `resurrected`, only
`unattested_superseded`, warn-by-default). Combined with finding 4, a
consolidate of a recent-window fact is undone by the next nightly merge.
`tests/test_consolidate_facts.py` never asserts a revocation sidecar. The
OPS RULE to re-run `sign-facts.py` does not mint events (lifecycle is not
signed).

**Verified:** Read `apply_supersessions` field writes; grep of
`consolidate-facts.py` for `record_supersessions` / `_revoke` is empty.

---

## 8. Ed25519 cache HMAC is keyed with the public key, so a store-dir writer can mint `VALID`

**Severity:** high

**Where:** `bin/_sign.py:431-445`, `bin/_sign.py:819-858`,
`bin/_sign.py:861-892`, `bin/_sign.py:970-1006`,
`bin/_verify_cache.py:74-80`

**Failure:** A cache hit skips only `verify_bytes`. The code (and the repo
map) treat that as safe because the committed-hash comparison still runs.
That stops the original F5 edit (“change `content`, leave the envelope”).
It does not stop:

1. Change `content`.
2. Recompute and write `attestation.canonical_fact_hash` /
   `source_hash` so the hash checks pass.
3. HMAC the new payload under `SigningKey.cache_key_material()` and plant
   that digest in `facts.json.verified-cache.json` with a matching store
   stamp.

`_cached_signature_status` then hits `digest_valid` and returns `VALID`
without a public-key op. The poisoned fact is injected; there is no
“verification skipped” or “allowed unsigned” line.

For Ed25519, `cache_key_material()` returns the **raw public-key bytes**.
Default layout keeps `signing-key.pub` next to `facts.json` at `0600`. A
local process that can write the store can read that pub file. The comments
in `_verify_cache.py:74-80` say this is fine because the same access lets
you delete the key and disable verification. Deleting the key prints
`attestation verification skipped` into `hook-errors.log`. Forging the
cache does not.

The HMAC-keying change (cache v2) did close the earlier “digest is a hash of
public inputs, forgeable from `facts.json` alone” bug. On Ed25519 it
replaced that with “forgeable from `facts.json` + the verifying public
key,” which is the default production layout. Split-key with the pub on a
path the store-dir writer cannot read is actually closed. A store-dir
writer can also delete the key and skip verification entirely (documented
fail-open); forging is the stealthier variant: verification looks on, and
a previously signed fact is rewritten in place rather than joined by a new
UNSIGNED row.

**Verified:** Read `cache_key_material`, `cache_digest`, and `verify_fact`’s
order (hash checks, then `_cached_signature_status`). Confirmed Ed25519
branch returns `self._pub_bytes`. Default paths in `budget-recall.py:588-605`
prefer `~/.nock-brain/signing-key.pub`.

---

## 9. Insight covering drops facts before budget truncation

**Severity:** medium

**Where:** `bin/budget-recall.py:874-882`, `bin/budget-recall.py:900-943`

**Failure:** `covered` is the union of `source_ids` on **every matched
insight**, then those facts are removed, then the token budget truncates
the insight+fact list. Semantic mode caps insights at 5 first; the default
hook path does not (no `NOCKBRAIN_SEMANTIC` unless `semantic-on` exists).

A query that matches ten insights at budget 800 may inject two insights.
Sources of insights 3–10 are already gone from `fact_results`, and those
insights themselves never inject. Those facts are missing with no error.

The same covering set also strips reserved dense ids if they appear in any
matched insight’s `source_ids`. Reserved exemption is only for the date cap
and budget precommit, which then cannot find them.

If `source_ids` is present and `null`, `for sid in None` raises `TypeError`,
`budget-recall` dies, the hook emits `{}`. `synthesize.py` always writes a
list; a hand-edited `insights.json` does not.

**Verified:** Read `select_recall` order: cover → date cap → budget. Covering
has no `isinstance(..., list)` guard.

---

## 10. SQLite cutover paths can empty or crash recall with almost no signal

**Severity:** medium

**Where:** `bin/_storeback.py:314-317`, `bin/_storeback.py:244-246`,
`bin/_storeback.py:149-151`, `bin/_storeback.py:262-265`,
`hooks/memory-inject.sh:54-57`

**Failure:** `NOCKBRAIN_STORE=sqlite` selects `SqliteStore` without checking
that `brain.db` exists. Missing DB → `_record_degradation(..., "db-missing")`
and `return []` with **no stderr** (the `sqlite3.Error` branch at 255-259
does print). An existing empty `brain.db` (`create()` with no rows) returns
`[]` with no degradation row at all.

The hook only checks that `facts.json` exists, then passes that path to
`budget-recall`, which `resolve_store`s it. If the Claude Code hook env has
`NOCKBRAIN_STORE=sqlite` or a `store-v2` marker sits next to a present
`brain.db`, recall reads SQLite and never the JSON the hook just confirmed.

Corrupt JSON *cells* (`evidence` / `attestation` / `extra`) are `json.loads`’d
in `_row_to_fact` with no try/except. `SqliteStore.load_facts` only catches
`sqlite3.Error` around `execute`. A bad cell raises, `for_store` still
`save()`s in `finally`, the hook traceback-logs and emits `{}`. JSON-file
loads never raise (`_facts.load_facts` 72-85).

SQLite is not the production default (P3–P5 cutover is not done). The env
and marker selectors are live on the hook path today.

**Verified:** Read `resolve_store`’s `choice == "sqlite"` branch (no
`exists()` check). Read the missing-DB path: degradation log, no `print`.
Read `_row_to_fact`’s uncaught `json.loads`.

---

## 11. `--strict-verify` / `NOCKBRAIN_STRICT_VERIFY=1` with no usable key still injects everything

**Severity:** medium

**Where:** `bin/budget-recall.py:592-616`, `bin/budget-recall.py:632-633`,
`bin/budget-recall.py:837-839`, `hooks/memory-inject.sh:86-90`,
`tests/test_verify_on_recall.py:155-165`

**Failure:** Missing or unloadable key → `_resolve_verify_key` returns
`None` → `_verify_filter` returns facts unchanged, including TAMPERED
ones. `--strict-verify` only prints `attestation verification skipped` on
stderr. The hook redirects that to `hook-errors.log` and still injects.

This is tested as intentional (`test_strict_verify_without_key_warns_and_still_recalls`).
On the hook path the warn is invisible, so the flag is a lie: you can set
`NOCKBRAIN_STRICT_VERIFY=1` and still inject poisoned facts if the key file
is missing, unreadable, or Ed25519 while stock Python lacks `cryptography`.

**Verified:** Read the `verify_key is None` early return and the strict
warning. Read the hook’s `2>>"$ERROR_LOG"` on the budget-recall invocation.
Read the test that pins “still recalls.”

---

## 12. `edit-fact --revert` skips the v2 and Merkle-parent guards

**Severity:** medium

**Where:** `bin/edit-fact.py:239-252`, `bin/edit-fact.py:261-273` (apply
guards), `bin/edit-fact.py:297-352` (revert: neither guard)

**Failure:** Apply refuses v2 claims and Merkle parents, then re-signs with
legacy `sign_fact`. Revert restores content and calls the same `_resign` →
`sign_fact` with no those checks.

If a fact was edited while legacy, later gained v2 authority fields, then
`--revert`, it is legacy-signed and `verify_fact` returns `TAMPERED`. Default
recall drops it. The revert command prints success.

If a child appeared after the original edit, reverting the parent does not
refuse; children become `PARENT_SUSPECT` (kept by default, dropped under
`--strict-verify`).

Apply-path tests cover refuse-v2 / refuse-parent; revert tests do not.

**Verified:** Read `_apply_edit` vs `_revert`. Only `_apply_edit` calls
`_is_v2_claim` and `_child_ids`. `_resign` always `sign_fact`.

---

## 13. Health reports a verification-cache sidecar “fresh” without checking key or algorithm

**Severity:** low

**Where:** `bin/_verify_cache.py:401-409`, `bin/_verify_cache.py:563-572`,
`bin/nockbrain-health.py:352-368`

**Failure:** `sidecar_status` treats a sidecar as fresh when `version == 2`
and the recorded `(mtime_ns, size)` matches the store. It does not check
`key_id` or `alg`. `_load_digests` does (`_sidecar_header_ok`). After a key
rotation with an unchanged `facts.json` stamp, health says `present, fresh,
writable` while every recall discards the sidecar and cold-verifies (or,
while the stamp still matches, `save()` dirties and rewrites).

Does not change recall results. It can hide a persistent cold-verify that
blows the hook’s <2s budget.

**Verified:** Compared `_sidecar_header_ok` (requires `key_id` and `alg`)
with the `fresh` predicate in `sidecar_status` (does not).

---

## 14. Under SQLite, purge and health target `facts.json.verified-cache.json`, recall uses `brain.db.verified-cache.json`

**Severity:** low

**Where:** `bin/budget-recall.py:689`, `bin/_storeback.py:191`,
`bin/purge-fact.py:167-179`, `bin/nockbrain-health.py:288-289`

**Failure:** `for_store` stamps `store.freshness_path`. For `SqliteStore`
that is `brain.db`, so the live sidecar is `brain.db.verified-cache.json`.
Purge always `unlink_for_store(args.facts)` (the JSON path). Health always
`sidecar_status(facts_path)`. After cutover, a matching purge unlinks the
wrong file; health reports the JSON sidecar; recall keeps using the SQLite
one. Digests are still content-bound, so this does not resurrect a purged
fact. GDPR “drop the sidecar” and health “fresh/unwritable” are wrong for
the cache that is actually hot. JSON default is wired correctly.

**Verified:** `SqliteStore.freshness_path = self.db_path`. Purge and health
take the `--facts` / `facts.json` path only. E2 is not cut over; this is
latent until someone sets `NOCKBRAIN_STORE=sqlite` or drops a `store-v2`
marker.

---

## Overall verdict

The #47–#92 cache work closed the bugs it named: per-entry retention, status-bound
non-VALID digests, stamp-skip of stale writers, same-stamp union, unwritable
degrade-once, purge-before-unlink, oversized/unreadable distinct from stale.
I did not find a remaining path where a warm cache skips the committed-hash
check or where unioning opaque HMACs upgrades a cached failure to `VALID`
without key material. What the train did not close — and in a few places
papered over — is the rest of the hot path. v2 claim-authority facts can
verify `VALID` and still never rank (`source_date` gate) or can outlive the
window they signed (`valid_from`/`valid_to` vs `valid_at`/`invalid_at`).
Revocation is an offline auditor; recall and the nightly merge do not honor
it, so a superseded fact from the `--since 3` window comes back `current`.
`apply-promotion-batch` and every other live `facts.json` writer still
truncate-in-place, and the one atomic writer #91 added was only wired to the
sidecar. Default recall still fail-opens to UNSIGNED and to “no key,” so the
hook can inject wrong or empty memory with nothing in the session and only a
line in `hook-errors.log`. Treat the cache series as locally competent and
the signing/recall/consolidation contracts around it as unfinished.
