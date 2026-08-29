# Skeptical review — the #47…#93 fix train

Scope: `cf36ac7`(#89/#47) · `5c6fe0a`(#88/#48) · `a4a51d9`(#90/#49,#50) ·
`40f095f`(#91/#52,#53,#54) · `95d4ed5`(#92) · `2b65935`(#87/#51) · `4be09b9`(#93),
plus the verification-cache / sidecar / signing / recall surface they touch.

Baseline at review time: `684 passed` (`python3 -m pytest -q`), working tree clean at
`4be09b9`. Every finding below was reproduced by executing the shipped code — no
finding rests on reading alone. Reproduction scripts are described inline.

---

## 1. CRITICAL — the signing tools ignore `NOCKBRAIN_SIGNING_KEY/_PUB` that recall honors: recall goes 100% dark while every health surface reports green

**Files**

- `bin/budget-recall.py:602-605` — recall resolves the verifying key from
  `NOCKBRAIN_SIGNING_PUB` / `NOCKBRAIN_SIGNING_KEY`, falling back to `~/.nock-brain/`.
- `bin/sign-facts.py:44-47,63` — the *only* store signer defaults `--key`/`--pub` to
  `_sign.DEFAULT_KEY_PATH`/`DEFAULT_PUB_PATH` (`~/.nock-brain/signing-key[.pub]`) and
  reads no environment at all. `load_or_create_key` is called with the default
  `create=True` (`bin/_sign.py:519-541`), so on a machine without that file it
  silently *mints a second key*.
- `bin/verify-facts.py:51` — the offline auditor has the same default and the same
  blind spot.
- `bin/rebuild-store.py:549-550` — the nightly orchestrator hardcodes
  `key_path = store_dir / "signing-key"` and exposes no `--key` flag, so the
  cron-driven re-sign inherits the same mismatch.
- `bin/nockbrain-health.py:267` — `recall_ready` is `bool(facts) and not bad_facts`;
  attestation status is not an input.

**Failure scenario (executed)**

An operator adopts the protected-key posture that the fix train's own security prose
names as the case that matters (`bin/_verify_cache.py:81-83`, "an attacker with only
facts.json (+ sidecar) write access — the case that matters when the key lives on a
protected path via `NOCKBRAIN_SIGNING_PUB/KEY`"; same claim at `bin/_sign.py:435-438`;
`bin/_revoke.py:215-216` says the env overrides "mirror the recall path"). They export
both vars to a key outside the store directory and run the documented pipeline.

I ran exactly that against a scratch store, with both env vars set for every process:

```
sign-facts:  Signed 1 fact(s) with ed25519 (key ed25519:2d2ad97ba6b0150d)   <- ~/.nock-brain key
protected key id:                              ed25519:0bf480f4bf1b879b     <- the key recall uses
budget-recall stdout: No matching facts found.
budget-recall stderr: ...facts.json: attestation check: excluded 1 tampered of 1 fact(s)
verify-facts rc 0:    valid 1 · TAMPERED 0 · unsigned 0 · parent-suspect 0
nockbrain-health:     - Recall ready: true
                      - Verification cache: present, fresh, writable
```

`sign-facts.py` did not even use a key in the store directory it was pointed at — it
used `~/.nock-brain/signing-key`. Recall then classifies **every fact** TAMPERED and
drops it (`bin/budget-recall.py:639-640`). The single stderr line is written to
`hook-errors.log` and discarded by the hook (`hooks/memory-inject.sh:86`), and the two
surfaces an operator would actually check — `verify-facts.py` (exit 0, all valid,
because it uses the *signing* default) and `nockbrain-health.py` (`recall_ready: true`)
— both report healthy. Memory injection is silently and totally dead.

The verify-cache work in #90/#91 hardened the sidecar specifically for this posture; it
does not work end to end. This is a leftover, not an introduction, but the fix train
restated the assumption in three module docstrings without checking it.

**Note on scope:** `docs/REPO-MAP.md:353-357` lists these vars under "Env vars honored
by recall", so recall-only is arguably the documented contract. That is exactly the
problem — a partially-honored key override with no warning and no divergence check is a
trap, and the security rationale in `_verify_cache.py`/`_sign.py` reads as if the split
posture is supported. Minimum fix: make `sign-facts.py`, `verify-facts.py` and
`rebuild-store.py` resolve keys through one shared helper (`_revoke.resolve_signing_key`
already implements it), and make health compare the store's `attestation.key_id` set
against the key recall would load.

---

## 2. HIGH — under `NOCKBRAIN_STORE=sqlite` (or the `store-v2` marker) recall loads the fact store as its own insight store: every result is emitted twice and `insights.json` is never read

**Files**

- `bin/budget-recall.py:670-674` — `_load()` calls `resolve_store(path)` on whatever
  path it is given.
- `bin/budget-recall.py:865-867` — `_load(insights_file, ...)` goes through that same
  `_load`.
- `bin/_storeback.py:302-320` — `resolve_store` keys off `facts_path.parent`, not the
  filename: `NOCKBRAIN_STORE=sqlite` returns `SqliteStore(store_dir/"brain.db")` for
  *any* path in the store directory, `insights.json` included. The `store-v2` marker
  branch (`:318-319`) does the same with no env var at all.

**Failure scenario (executed)**

Store of 3 facts, `insights.json` deliberately empty, `brain.db` holding the same 3
facts, `store-v2` marker present:

```
NOCKBRAIN_STORE=json    -> results = ['f0','f1','f2']
NOCKBRAIN_STORE=sqlite  -> results = ['f0','f1','f2','f0','f1','f2']
marker only (no env)    -> results = ['f0','f1','f2','f0','f1','f2']
```

Consequences, all silent: the insights tier is inert (real `insights.json` never
opened, so the "insights lead" contract of §6 is void); the token budget is halved by
verbatim duplicates; the `covered` de-dup at `bin/budget-recall.py:881-882` cannot fire
because raw facts carry no `source_ids`; and under `--semantic` the duplicate block is
capped at 5 by `_resolve_insight_lead_cap` while still displacing real facts. Nothing
is logged.

The declared cutover bar does not cover this: `bin/eval-store-parity.py:121-125`
compares only `budget_recall.search(...)` over the fact lists. It never calls
`select_recall`, so the parity harness would pass a cutover into this bug. (Related and
same root cause: the hook hard-exits when `~/.nock-brain/facts.json` is absent —
`hooks/memory-inject.sh:55-58` — so a JSON-free SQLite cutover kills recall outright.)

Fix: `_load` should take the resolved store, not re-resolve per path — insights are a
derived JSON artifact and must always use `JsonStore`.

---

## 3. HIGH — `purge-fact.py --apply` is a "hard delete" that leaves the purged content in `insights.json`, which recall then injects ahead of everything else

**Files**

- `bin/purge-fact.py:2` — "Hard-delete fact material from local NockBrain stores".
- `bin/purge-fact.py:136-146` — the argument surface is `--facts`, `--events`,
  `--notes-dir`, `--vault`, `--sidecar`. There is no `--insights`, and
  `insights.json` / `fact-edits.jsonl` / `revocations.jsonl` appear nowhere in the file.
- `bin/synthesize.py:237-240` — an insight's `content` embeds `Most recent:
  {latest[:160]}`, i.e. up to 160 characters of member-fact content verbatim, plus
  `source_ids` at `:273`.
- `bin/budget-recall.py:884` — `results = insight_results + fact_results`; insights are
  always prepended.
- `bin/edit-fact.py:110,121-122` — `fact-edits.jsonl` rows hold the **FULL** pre- and
  post-edit content ("not a truncated preview", by the file's own docstring).

**Failure scenario (executed)**

Three facts containing `"Jane Doe of Acme Corp asked us to delete her billing dispute
record"`, synthesized into one insight, then purged:

```
purge:  removed 3 fact(s), 0 event(s), 0 note line(s), 0 vault line(s), 0 vector(s)
facts left: 0
recall after purge (included):
  ['Recurring decision (seen 3x, 2026-01-01..2026-03-01): delete, asked, her, record,
    doe. Most recent: Jane Doe of Acme Cor']
insights.json still contains the purged text: True
```

The subject-erasure request completes with exit 0 and a "removed 3 fact(s)" summary
while the content keeps being injected into every session. `rebuild-store.refresh_insights`
(`bin/rebuild-store.py:411-430`) regenerates insights from the promoted store, so a
healthy nightly eventually scrubs `insights.json` — but that is up to 24 h of continued
injection, it is not what the tool reports, and #93 is direct evidence the nightly can
be dead for weeks without anyone noticing. `fact-edits.jsonl` is append-only and never
regenerated, so content there survives indefinitely.

#91 added verified-cache-sidecar purge parity (`bin/purge-fact.py:167-186`) — the same
review pass should have caught the two derived artifacts that actually contain fact
text.

---

## 4. MEDIUM — `consolidate-facts.py --execute` supersedes facts without minting signed revocation events, breaking the stated non-negotiable

**Files**

- `bin/consolidate-facts.py:212-233` — `apply_supersessions` writes `status`,
  `superseded_at`, `superseded_by`, `supersession_reason` and returns. `_revoke` is not
  imported anywhere in the file.
- Compare `bin/dedup-facts.py:255` and `bin/supersede-fact.py:85,116`, which both call
  `record_supersessions(...)` right after `store.replace_all(...)`.
- `CLAUDE.md` non-negotiable: "supersessions mint signed revocation events".

**Failure scenario (executed)**

Two near-duplicate signed facts; `consolidate-facts.py --manifest ... --execute
--i-have-reviewed-the-manifest`:

```
superseded 1 facts; store now 2 rows (1 current).
revocations.jsonl exists: False
verify-facts --strict-revocations:  revocations: 0 attested ... 1 unattested
```

The consequence is the exact hole `_revoke` exists to close (`bin/_revoke.py:3-14`):
nothing signed authorizes this supersession, so flipping `status` back to `current`
restores the fact into recall with no verification failure — the resurrection check has
no event to compare against. Every consolidation also permanently pollutes the
`--strict-revocations` audit with unattested marks, degrading it into noise.

(Verified separately that consolidation is otherwise correctly mark-only: both facts
still verify `valid` in-process after `--execute`.)

---

## 5. LOW — `sidecar_status` reports `fresh: true` for sidecars the loader rejects wholesale, so #92's new reasons can still point health at the wrong thing

**Files**

- `bin/_verify_cache.py:563-572` — freshness is decided on `version` + a typed `store`
  stamp only.
- `bin/_verify_cache.py:401-409` — `_sidecar_header_ok`, which the actual loader uses,
  additionally requires matching `key_id`, matching `alg`, and `digests` being a list of
  strings.

**Failure scenario (executed)** — three sidecars with a correct version and a current
stamp:

| sidecar | `sidecar_status` | `_load_digests` |
|---|---|---|
| foreign `key_id` (post key rotation) | `fresh: True`, `reason: None` | `0 digests, dirty=True` |
| no `digests` key | `fresh: True`, `reason: None` | `0 digests, dirty=True` |
| `digests: [1,2,3]` | `fresh: True`, `reason: None` | `0 digests, dirty=True` |

Health prints `- Verification cache: present, fresh, writable` in all three cases while
recall is cold-starting every time. A dirty `save()` repairs the file on the next
successful recall, so the window is usually short — but it is unbounded on a host where
health is monitored and recall runs rarely (e.g. after a key rotation before the first
prompt). Reusing `_sidecar_header_ok` inside `sidecar_status` closes it.

## 6. LOW — health inspects `facts.json`'s sidecar; recall's sidecar is `store.freshness_path`

`bin/nockbrain-health.py:288-289` calls `sidecar_status(facts_path)` unconditionally,
while `bin/budget-recall.py:689` opens the cache at `store.freshness_path` — `brain.db`
under any SQLite selection (`bin/_storeback.py:191`). After a cutover, health reports
`missing (cold start)` forever about a file recall never writes, and never observes the
sidecar that actually exists. Same root cause as finding 2.

## 7. LOW — `_probe_writable` leaks temp files that the sweeper deliberately refuses to clean

`bin/_verify_cache.py:455-470` creates `.nb-vc-probe.<rand>.tmp` in the store directory
on **every** `load_for_store`, i.e. twice per recall (facts + insights). The window
between `mkstemp` and `os.unlink` is small, but the whole reason `_sweep_stale_tmps`
exists is that the hook's caller SIGKILLs the process (`bin/_verify_cache.py:181-184`) —
and that sweeper explicitly excludes this prefix (`:185-186`, "Probe tmps
(``.nb-vc-probe.``) use a different prefix and are not selected"). Nothing else ever
removes them. Either sweep the probe prefix too, or reuse the existing mkstemp inside
`secure_replace_bytes` instead of a separate probe.

## 8. LOW — #91 turned `_embed`'s bounded temp file into unbounded accumulation

`bin/_embed.py:191-208` previously wrote a fixed `embeddings.npz.tmp`; it now routes
through `bin/_store.py:35-75`, which uses `tempfile.mkstemp(prefix=path.name + ".")`.
`secure_replace_bytes` cleans up in `finally`, but `rebuild-store.refresh_semantic_sidecar`
runs `embed-facts.py` under `subprocess.run(..., timeout=1800)`
(`bin/rebuild-store.py:456`), and a `TimeoutExpired` kill skips Python cleanup. Before
#91 a repeated nightly timeout left one stale file; now it leaves one multi-megabyte
`embeddings.npz.<rand>.tmp` per occurrence, and no sweeper covers that prefix.

## 9. LOW — hygiene

- `bin/_verify_cache.py:107` imports `FILE_MODE` from `_store` and never uses it
  (`# noqa: F401`). `tests/test_verify_cache.py:778-781` pins this with a source-string
  assertion, so a dead import is now a contract test that asserts nothing about behavior.
- `docs/REPO-MAP.md` line counts and file counts went stale across the train:
  `_sign.py` is 1045 lines (map says 977), `bin/` holds 49 `.py` files (map says 50),
  `tests/` holds 43 files and 684 tests (map says 42 / ~507). The prose contracts were
  updated correctly in every PR; only the numbers rotted.

---

## What I checked and found sound

Worth stating explicitly, because the train's core claims do hold up under instrumented
testing:

- **Per-entry retention (#47/#89)** and **status-bound digests (#48/#88)** are
  cryptographically sound. `cache_digest` (`bin/_sign.py:819-859`) is an HMAC keyed
  under `cache_key_material()`, so a forged sidecar cannot mint VALID; the VALID
  preimage omits the status field while non-VALID inserts it before the variable-length
  payload, and every payload begins with a fixed domain prefix (`b"nockbrain-fact-v1\n"`
  / `b"nock-claim-attestation-v2\n"`), so a 5-field VALID preimage can never collide
  with a 6-field non-VALID one. Not bumping `CACHE_VERSION` in #88 was correct.
- The committed-hash comparisons (`bin/_sign.py:967-973`) genuinely run on every
  recall, warm or cold — I confirmed with an instrumented `SigningKey.verify_bytes`
  against the committed fixture: cold = 196 signature ops, warm = 0, and a poisoned
  fact appended to the warm store was still caught (`excluded 1 tampered of 197`) with
  **zero** signature operations, because content tampering short-circuits before the
  cache is consulted.
- The #48 refusal to infer `PARENT_SUSPECT` from a failed child signature
  (`_parent_set_moved`, `bin/_sign.py:588-605`) is the right call, and matters more now
  that failures are cached.
- The #49 save-skip / same-stamp-union protocol (`bin/_verify_cache.py:266-300`) and the
  #52/#53 ordering in `purge-fact` (store rewrite *then* sidecar unlink,
  `bin/purge-fact.py:167-186`) close their races correctly — I traced every interleaving
  of purge-vs-concurrent-recall and found no window where a purged fact's digest is
  recreated.
- `for_store` (`bin/_verify_cache.py:138-157`) does own the stamp-then-load-then-save
  ordering, and `budget-recall._load` is the only caller.

---

## Verdict

The fix train is competent, well-tested work on the layer it set out to fix: the
verification cache is now cryptographically sound, race-correct, growth-bounded and
self-healing, and I could not break it — the #47–#54 cluster is genuinely closed, and
the caching claims in the docstrings are accurate rather than aspirational. What the
train reveals is a *scoping* problem, not a competence one. Every PR reasoned rigorously
inside `_verify_cache.py` while the load-bearing failures sat one call frame out: the
signing toolchain that recall depends on does not honor the very key override the
train's own threat model is written around, and when that split happens all three
observability surfaces (`verify-facts` exit code, `recall_ready`, the new sidecar health
line) report green while recall injects nothing at all (finding 1). The same pattern
repeats at smaller scale — a store-backend selector that silently swallows
`insights.json` and a parity harness that does not exercise `select_recall`
(finding 2), a purge tool that gained verified-cache parity in #91 while still leaving
the purged text in the two artifacts that actually contain it (finding 3), and a
supersession path that skips the attestation the repo calls non-negotiable (finding 4).
Findings 1–4 all share one shape: recall or integrity degrades correctly-by-the-letter
and invisibly-in-practice, because the health story is asserted per-module rather than
verified end to end. My recommendation is to keep the cache work as shipped, treat
finding 1 as a release blocker, and add one hermetic end-to-end test — sign, verify,
health, recall, in a single scratch store under a non-default key path — that would have
caught findings 1, 2 and 6 at once. Nothing here argues for reverting any of #87–#93.
