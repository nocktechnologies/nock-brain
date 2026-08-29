# Skeptical review of the #47–#93 fix train

Scope: verification-cache, sidecar, signing, recall and consolidation code
touched by PRs #47 through #93 (roughly `cf36ac7` → `4be09b9`), reading the
current tree at HEAD (`4be09b9`) and only reporting things I can point at in
the code.

Nothing I found is a smoking gun in the code the fix train actually rewrote:
`_verify_cache.py`, `_sign.py`'s signing routing (#82), `_store.secure_replace_bytes`
(#91), `purge-fact.py`'s ordering (#91), the health text (#92) and the machine
enum (#93) all hold up. What follows is the residue: two adjacent latent bugs
that the train's scope did not reach, one legitimate ordering hole around
concurrent-with-purge saves, and a handful of small correctness/robustness
issues in the surrounding code the train touched or relies on.

---

## 1. `_load(insights_file)` returns FACTS, not insights, whenever SQLite is engaged  — severity: high (latent)

**Files**: `bin/budget-recall.py:670–692`, `bin/_storeback.py:302–320`.

**Failure scenario**: `select_recall` calls `_load(insights_file, ...)`
(`budget-recall.py:865`). `_load` does `store = resolve_store(path)`
(line 674). `resolve_store` computes `store_dir = facts_path.parent` and
`db_path = store_dir / DB_FILENAME` and — if `NOCKBRAIN_STORE=sqlite` OR the
`store-v2` marker + `brain.db` are present next to the file — returns
`SqliteStore(db_path)`, **discarding the path argument entirely**
(`_storeback.py:311–319`). `store.load_facts()` then returns rows from the
`facts` table in `brain.db`, not from `insights.json`. The "insights lead"
in `select_recall` (line 862–884) will then prepend up to 5 facts that also
appear in the fact-results list and drop any fact whose id is in the fake
"insights"' `source_ids` (empty, because these rows have no `source_ids`),
so the practical effect is: insights are silently replaced by duplicated
facts, and semantics of the insights-first ordering collapse.

**Verification**: read `resolve_store` in `_storeback.py:310–320` — the
`facts_path` argument only survives through the JsonStore paths (line 315,
line 320); every SqliteStore branch (lines 316, 317, 319) constructs from
`db_path`, not from the caller's path. `budget-recall.py:865` calls
`_load(insights_file, ...)` unconditionally; there is no per-file backend
override.

Note: it is guarded in practice by "SQLite is not cut over" (`docs/REPO-MAP.md`
§4, §5 migrate). Kill switch `NOCKBRAIN_STORE=json` keeps this dead. But
the fix train specifically hardened this recall path and did not address
this, and a single wrong `NOCKBRAIN_STORE=sqlite` env var flips it live
with zero warning.

---

## 2. `nockbrain-health.py:26–29` `load_json` crashes on the very outage it is supposed to surface — severity: high

**File**: `bin/nockbrain-health.py:26–29`.

```
def load_json(path: Path | None, default: Any) -> Any:
    if not path or not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
```

**Failure scenario**: any I/O error or `json.JSONDecodeError` propagates.
`build_report` calls this on `facts_path` (line 229) and `stats_path`
(line 230). A truncated / corrupt `facts.json` (concurrent-writer half-write,
disk-full, or a tampering event mid-write) makes `nockbrain-health.py`
crash with an unhandled traceback — the exact silent-degradation class
that health is supposed to flag as `recall_ready: false`. Same for a
stats file that lost bytes.

Contrast with `_facts.load_facts` (`bin/_facts.py:72–86`), which does
handle both, and with `SqliteStore.load_facts` (`_storeback.py:240–265`),
which logs a degradation and returns `[]`. Health uses neither.

**Verification**: read the function. No try/except. Both call sites feed
paths that may exist but be malformed.

This one is not from the #47–#93 train; it predates it. Included because
the train sold "silent-degradation visibility" as a first-class property
and this is a hole in that story.

---

## 3. Sidecar path used by health and purge is hardcoded to the JSON backend — severity: medium (latent under kill-switch, wrong today when sqlite is chosen)

**Files**: `bin/nockbrain-health.py:288–289`, `bin/purge-fact.py:167–184`.

**Failure scenario (health)**: `nockbrain-health.py:289` calls
`sidecar_status(facts_path)`. `_verify_cache.cache_path_for` derives
`facts_path.name + ".verified-cache.json"`, so under an active SQLite
backend the ACTUAL sidecar is `brain.db.verified-cache.json` (see
`budget-recall.py:689` — it passes `store.freshness_path`, which is
`brain.db` for `SqliteStore`, per `_storeback.py:191`). Health will always
print "Verification cache: missing (cold start)" or, worse, report the
JSON sidecar left over from before the sqlite switchover as authoritative —
misleading either way.

**Failure scenario (purge)**: `purge-fact.py:167,179` calls
`unlink_for_store(args.facts)` on `facts.json` unconditionally, so under
sqlite the `brain.db.verified-cache.json` sidecar is NOT unlinked and
digests keyed to purged content remain (they are opaque HMACs, so they
cannot mint VALID for the new content — but the "purge removes all
material" contract is broken and the digest set grows monotonically
across purges). Purge itself also only rewrites `facts.json`; the
sqlite table is never touched.

Same guard as #1: currently latent because sqlite is not cut over. The
fix train (#91) explicitly touched the purge/sidecar handshake for the
JSON path and did not extend it.

---

## 4. `_verify_cache.sidecar_status` accepts as "fresh" a sidecar `_load_digests` will reject — severity: low

**Files**: `bin/_verify_cache.py:516–583` (`sidecar_status`) vs.
`bin/_verify_cache.py:401–409` (`_sidecar_header_ok`) and `340–380`
(`_load_digests`).

**Failure scenario**: `sidecar_status` only checks `version` and the
typed `store` stamp (lines 559–572). It does NOT check `key_id`, `alg`,
or that `digests` is a list of strings. A sidecar with matching
version+stamp but a foreign `key_id` (rotated key) will be reported
`fresh: true, reason: None`, and health will print "present, fresh,
writable". But on the next recall, `_load_digests` uses
`_sidecar_header_ok` which rejects it (empty set, dirty=True), so every
recall re-verifies from cold.

**Verification**: `_sidecar_header_ok` requires four fields
(`version`, `key_id`, `alg`, `digests`). `sidecar_status`'s inner check
only compares `version` and requires `recorded` (the typed store) to
be non-None (lines 562–566).

Introduced (or at least extended) in #92, which was the pass that split
the reason into `stale_stamp / oversized / unreadable`. It is a
reporting-only inconsistency; recall correctness is unaffected.

---

## 5. `for_store` save-on-stamp-moved does not stash to `_memory`; the docstring is ambiguous about it — severity: low

**File**: `bin/_verify_cache.py:248–301`.

**Behavior**: `save()` has three exit paths that persist nothing:
(a) `not self._dirty` — nothing to save (line 267–268), fine;
(b) `_store_sig(self.store_path) != self.store_sig` — store moved between
load and save (line 269–270), returns without stashing;
(c) `self._writable` False — sidecar dir unwritable (line 274–276),
stashes to module-level `_memory` so later recalls in the SAME process
still benefit.

Only (c) preserves the run's work. Path (b) discards this run's newly
verified digests entirely — the next recall in the same process re-runs
those signature ops. This is not a correctness bug (digests are content-
bound, so nothing goes wrong), but the docstring at the top of the file
(lines 55–66) says "the stale writer keeps its in-memory set" which
naturally reads as "kept in `_memory`" while it actually only means the
handle's own `self.digests` attribute lives until the recall exits.

**Verification**: trace `save()` — only the `not self._writable` branch
(line 275) calls `_stash_memory(self)`; the `_store_sig` skip (line 270)
returns bare. `_memory` (line 131) is populated exclusively via
`_stash_memory`. Confirmed.

**Why note it**: the fix train's own docstring effort (`docs/REPO-MAP.md`
§4 `_verify_cache` bullet) treats "the stale writer keeps its in-memory
set" as a guarantee. It isn't across processes; it barely is across
recalls in the same process (only for the unwritable branch).

---

## 6. `apply-promotion-batch.py` claims strict verification but does not pass `--strict` — severity: low

**File**: `bin/apply-promotion-batch.py:150–161`.

**Failure scenario**: The block comment at line 150 says "Re-sign and
strict-verify with the store's own toolchain; the batch is recorded
applied ONLY after verification passes." The subprocess actually invoked
(line 153–156) runs `verify-facts.py --facts <path>` — no `--strict`,
no `--strict-revocations`. `verify-facts.py` (`bin/verify-facts.py:151,
166`) returns 0 unless a fact is TAMPERED, so any batch that leaves an
UNSIGNED or PARENT_SUSPECT fact in place is silently marked "applied".

`sign-facts.py` should normally sign every fact and remove the UNSIGNED
case, so the happy path is fine. The failure the missing flag hides is:
a v2-authority fact with a malformed contract that `sign_claim_fact_v2`
raises on — `_sign.sign_facts` (`bin/_sign.py:697–703`) will bubble the
`ClaimAttestationError` out of `sign-facts.py` (no try/except in
`bin/sign-facts.py:63–64`), the subprocess returncode is non-zero, and
`apply-promotion-batch.py` will actually catch that at line 157–161.
So this is a "the comment overstates what the code does" nit, not a
live hole. But under any future change that turns an unsigned fact
into a warning-plus-continue in `sign-facts.py`, the strict verify claim
would silently degrade.

Not from the fix train.

---

## 7. `ingest-curated-memory.py:238` sign_fact bypasses the routing — severity: low

**File**: `bin/ingest-curated-memory.py:236–238`.

**Failure scenario**: The CLAUDE.md non-negotiable ("Never sign facts
except through `_sign.sign_facts`", §7 N9851 trap) is violated here.
Ingest calls `sign_fact(fact, key, facts_by_id={})` directly on every
curated fact. Today, curated markdown extraction (see `_curated_fact` in
this file) does not synthesize any of the v2-only authority fields, so
no curated fact is a v2-claim fact and the legacy sign path is safe.
But the entire N9851 remediation was landed to enforce a single routing
choke point; a change to the curated extractor that started emitting
`memory_id`/`revision_id`/etc. would silently drop those facts from
recall (per `_sign.verify_fact:950–951`), and the self-verify block at
line 248–249 only *reports* the statuses (returned in the summary),
it does not fail the run.

**Verification**: line 238 calls `sign_fact` (not `sign_facts`); line
249 builds a `statuses` set but the caller returns it without acting
on non-VALID entries.

Predates the fix train; the train was about routing INSIDE
`_sign.sign_facts`, not tightening every caller. Still: this is one of
exactly two callers left that go around the choke point (the other is
`edit-fact.py`, which explicitly refuses v2 facts at line 261). Worth
either adding an `assert not is_v2_claim_fact(fact)` here or switching
to `sign_facts([fact], key)`.

---

## 8. `sign-facts.py` uses non-atomic `secure_write_text`; a crash mid-write leaves a truncated store — severity: low

**File**: `bin/sign-facts.py:67`.

`secure_write_text` (`bin/_store.py:24–28`) is documented as "not
atomic" and the fix train (#91) introduced `secure_write_json_atomic` /
`secure_replace_text` / `secure_replace_bytes` explicitly for this class
of hazard. But `sign-facts.py` still uses `secure_write_text`, so if
the process is killed between `path.write_text` and `path.chmod`, or
even mid-write, the fact store is left truncated (or without 0600
perms). Given `sign-facts.py` is the tool that walks the whole store,
this is the highest-consequence remaining non-atomic writer.

`resign-v2-authority-facts.py:239–241` has the same issue.

**Verification**: read `secure_write_text` — `path.write_text` then
`path.chmod`, no rename dance; the docstring warns about this. Compare
to `_verify_cache.save()` which routes through
`secure_write_json_atomic`.

---

## Overall verdict

The fix train reads as a series of small, disciplined corrections in
one narrow corner of the system (the verify-cache sidecar and its
interactions with purge, concurrent recall, signing, and health text),
each with a targeted regression test. Adversarial cases I tried against
the actual code — forged sidecars, mid-purge stale saves,
parent-suspect caching cross-runs, key rotation, stamp mismatches under
concurrent writers, oversized/corrupt sidecars, non-hex signatures —
all behave as advertised, and the tests in `tests/test_verify_cache.py`
and `tests/test_purge_fact.py` cover most of them explicitly. What the
train did NOT cover, and is the real risk surface, is everything a
click outside its scope: the JSON-only sidecar/purge/health code
paths that will break the moment the E2 SQLite backend is engaged
(findings 1 and 3), the health tool that crashes on the outage it is
supposed to detect (finding 2), and a small handful of callers that
still work around the safety choke points the train installed
(findings 7 and 8). Nothing here calls for reverting anything; the
train is net-positive. But the "silent-degradation visibility" story
the train tells itself has a couple of holes big enough to walk
through before any sqlite cutover.
