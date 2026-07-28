# E2 — Single SQLite Store: Design

**Date:** 2026-07-28
**Status:** Draft for review (Mira: operate-side; Kevin: final gate)
**Plan:** E2 of `2026-07-26-mira-brain-enhancement-plan.md` (v2), carrying E6's schema in the same migration per the decision log.
**Builds:** Fable (base repo). **Operates/cutover:** Mira on fleet-02, post-command-center-refactor, fail-closed.

---

## 1. What this replaces

Today the store is a monolithic `facts.json` (fleet-02: ~3.9 MB, 1,913 facts) rewritten wholesale by every nightly distill and every supersede/purge/dedup apply. BM25 is computed in Python over the whole store on each recall. Vectors live in a separate `embeddings.npz` sidecar keyed by fact id that can drift from the facts file. Backups are full-file copies. A reader that overlaps a writer sees a torn world (mitigated only by write-then-rename timing).

Target: **one SQLite database** (`~/.nock-brain/brain.db`, WAL mode, `0600`) holding facts, insights, and embeddings. `facts.json`, the vault, and `graph.json` become **derived exports** — same audit artifacts, no longer the source of truth.

## 2. Load-bearing finding: the migration requires ZERO re-signing

The v2 plan scoped "attestation must define canonical row serialization — re-sign migration required" as E2's highest risk. **That risk is retired.** The attestation signs canonical JSON of the fact's *values* — core `{id, kind, content}`, the evidence anchor, and parent hashes (`_sign.py`) — not the bytes of the container that stores them. If the DB stores the same values, `verify_fact` recomputes the same hashes.

Verified 2026-07-28 with a live test: sign a fact (HMAC path), round-trip it through SQLite-style TEXT columns (structured fields as compact JSON, scalars as scalars), restore, verify → `valid`.

Consequences:
- The migration is verify → copy → verify, with the **original attestations preserved verbatim**. The tamper-evidence chain is never broken; `signed_at` history is untouched.
- The fidelity requirement collapses to *lossless value round-trip*, enforceable by tests (§10) and by strict whole-store verification on the migrated DB — if any value changed in transit, verification fails and the migration fails closed.

## 3. Non-goals

- **No recall-algorithm changes.** BM25 scoring, RRF fusion, graph expansion, budget capping, classifier behavior: byte-identical results are the acceptance bar (§9). FTS5 is introduced later as an *optimization inside the same contract*, not with this migration.
- **No schema-semantics changes** beyond making existing optional fields (`valid_at`, `invalid_at`, `superseded_by`, …) first-class columns. E6's semantics already shipped in base.
- **No cutover in this workstream.** Base ships the backend, the migrator, and the parity harness. Mira's cutover is a separate, receipt-gated operation after the command-center refactor.

## 4. Schema (v1)

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE meta (
  key TEXT PRIMARY KEY, value TEXT
); -- schema_version=1, store_uuid, migrated_from_sha256, migrated_at

CREATE TABLE facts (
  id                  TEXT PRIMARY KEY,
  kind                TEXT NOT NULL,
  content             TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'current',
  confidence          REAL,
  source              TEXT,             -- fleet scoping; NULL reads as default brain
  source_date         TEXT,
  valid_at            TEXT,             -- E6 window (ISO-8601)
  invalid_at          TEXT,
  superseded_by       TEXT,             -- supersession link
  superseded_at       TEXT,
  supersession_reason TEXT,
  session             TEXT,
  session_anchor      TEXT,
  created_at          TEXT,
  last_seen_at        TEXT,
  evidence            TEXT,                        -- canonical JSON when present (signed anchor)
  attestation         TEXT,                        -- verbatim JSON envelope when present
  extra               TEXT NOT NULL DEFAULT '{}'   -- all unmodeled fields, lossless
);
-- (P2 refinement: evidence/attestation are nullable — a fact that never had a
-- key must not gain one on reload; fabricated keys would be a fidelity bug.)
CREATE INDEX facts_status_kind ON facts(status, kind);
CREATE INDEX facts_superseded_by ON facts(superseded_by);

CREATE TABLE insights (   -- synthesize.py output, same promotion story
  id TEXT PRIMARY KEY, doc TEXT NOT NULL             -- whole insight as JSON v1
);

CREATE TABLE embeddings (
  fact_id  TEXT PRIMARY KEY REFERENCES facts(id),
  model_id TEXT NOT NULL,
  dim      INTEGER NOT NULL,
  vector   BLOB NOT NULL                             -- float32 little-endian
);
```

`extra` is the losslessness guarantee: any fact field the schema does not model round-trips through it, so a future field added by a newer writer survives an older reader. Export reconstructs value-identical fact dicts (key order may differ; no consumer, including `_sign`, is order-sensitive).

FTS5 (`facts_fts`) and sqlite-vec (`vec0`) virtual tables are **deferred accelerators**, added behind the same store contract after parity cutover. Both are feature-detected at open; absence is never an error. (FTS5 confirmed present on the dev Mac and fleet-02's `python3` on 2026-07-28; the hook's stock-macOS `/usr/bin/python3` must be re-checked at install time.)

## 5. Store abstraction and backend selection

A new `bin/_storeback.py` defines the contract both backends implement:

```
load_facts() -> list[dict]          # value-identical to today's load
iter_live_facts(kinds=None) -> ...
upsert_facts(facts) / mark(fact_id, fields)   # lifecycle-field mutations
replace_all(facts)                  # rebuild-store promotion path
load_insights() / save_insights(...)
embeddings_get/put(...)
snapshot(dest_path)                 # backup (JSON: copy; SQLite: VACUUM INTO)
export_facts_json(dest_path)        # the derived audit artifact
```

- `JsonStore` wraps today's `_facts.load_facts` + `_store.secure_write_json` — behavior-identical, the default.
- `SqliteStore` implements the same contract on `brain.db` (stdlib `sqlite3` only).
- **Selection (P2 refinement — presence alone never cuts over):** JSON is the default even when `brain.db` exists. SQLite engages only when *explicitly* selected: `NOCKBRAIN_STORE=sqlite`, or the `store-v2` marker file next to an existing `brain.db` (the deliberate cutover artifact from §8). `NOCKBRAIN_STORE=json` always forces JSON — the reversible kill-switch, same doctrine as `NOCKBRAIN_LIVE_RECALL`.

Every `bin/` script that opens `facts.json` moves to the contract. The long tail (exports, purge, health, dedup, detect-contradictions, supersede) is mechanical; recall (`budget-recall`) is the carefully-parity-tested one.

**Hook-path constraints (unchanged doctrine):** modules reachable from `memory-inject.sh` stay Python-3.9-importable and stdlib-only. `sqlite3` is stdlib. sqlite-vec and FTS5 are optional accelerators with automatic fallback to the existing pure-Python BM25/dense paths. `tests/test_python_floor.py` extends to pin this.

## 6. Concurrency

WAL mode gives the recall hook a consistent snapshot while the distill writes — strictly better than today's whole-file rewrite window. Writers serialize on SQLite's single-writer lock; every existing writer already runs inside the distill window or as a deliberate CLI action, so contention is nil in practice. `busy_timeout` set to 2s on the hook path so a pathological overlap degrades to empty recall, never a hang.

**Why degrade-to-empty and not a JSON fallback** (Mira's §6 review question, answered): two reasons. First, in WAL mode *readers are never blocked by writers* — a reader takes a consistent snapshot; `SQLITE_BUSY` on the read path is confined to rare checkpoint-contention edges, so the scenario is nearly theoretical. Second, post-cutover `facts.json` is a *nightly export* — up to ~24 h stale. A silent fallback to it would quietly serve stale memory, which is precisely the anti-pattern the A4 fix retired from the hook ("no silent fallback"; the store's own thesis is that stale facts are worse than none). The failure stays *visible* (stderr note, same contract as every other hook degradation) and empty.

## 7. Privacy

- File modes via the existing `_store` helpers: directory `0700`, `brain.db` and every snapshot `0600`.
- **Purge semantics change and must be called out:** deleting rows leaves recoverable bytes in SQLite freelists and WAL. `purge-fact.py --apply` on the SQLite backend therefore runs `DELETE` → `PRAGMA wal_checkpoint(TRUNCATE)` → `VACUUM`, and the health report gains a check that a purge was followed by a vacuum. JSON export files regenerated after purge, same as today.
- No content leaves the box in any part of E2.

## 8. Migration procedure (`bin/migrate-store.py`, fail-closed)

Propose-by-default, mirroring dedup/detect conventions. `--apply` performs, inside the distill window on fleet-02:

1. Strict whole-store verify of `facts.json` (existing `verify-facts.py --strict`). Abort on any non-`valid`.
2. Build `brain.db.staging` from the JSON (facts + insights + embeddings sidecar import), `0600`.
3. Strict whole-store verify **of the staging DB** through `SqliteStore.load_facts()` — proves value-fidelity cryptographically (§2).
4. Equality gates: row count == fact count; per-fact `canonical_fact_hash` set identical between JSON and DB; embeddings count match.
5. `fsync` + atomic rename to `brain.db`. **`facts.json` is not deleted or demoted** — the backend selector still prefers... JSON until the cutover flag (below). Record `migrated_from_sha256` in `meta`.
6. Receipt: the `"store_migration"` block (`{"facts": N, "hash_set_equal": true, "verify": "valid", "db_sha256": ...}`) extends the **same single distill-receipt schema** that already carries `dedup` / `eval` / `gates` / `verdict` — one receipt document per distill, never a parallel second schema. `migrate-store.py` also emits the block standalone on stdout for ad-hoc runs.

**Cutover** is a separate deliberate step after the parallel-run bar (§9) is met: set `NOCKBRAIN_STORE=sqlite` (or write the `store-v2` marker), watch one full distill cycle + receipt, then demote `facts.json` to a nightly export. **Rollback at any point:** unset the flag — the JSON path never stopped being valid; or `export_facts_json` regenerates it from the DB losslessly.

## 9. Parallel-run acceptance (the bar cutover must clear)

Run on fleet-02 against the live store, both backends loaded side by side:

1. **Recall parity:** `mira-recall-suite.json` (all 9) plus a generated fuzz set (≥200 queries sampled from fact contents): identical ranked id lists and identical injected-token counts from both backends. Not "close" — identical; the backends share the scoring code and differ only in IO.
2. **Verification parity:** whole-store strict verify identical (`valid` counts equal, zero tampered/parent-suspect on both).
3. **Health parity:** `nockbrain-health.py` counts equal.
4. **Soak (revised per Mira's operate-side review):** ≥7 consecutive nightly distills with green receipts — covering one full weekly cycle so weekly-cadence interactions (nightly contradiction pass × recall over a week's shape of traffic) are exercised, not just three quiet nights. The distill still promotes JSON while a post-distill `migrate-store.py --apply` refresh keeps `brain.db` in lockstep (idempotent rebuild from the promoted JSON). Distill-writes-through-the-contract comes with the P4 cutover, not before.

## 10. Testing (base repo, TDD)

- **Contract tests run against both backends** via a parametrized fixture — every store-layer behavior asserted once, proven twice.
- **Fidelity property test:** randomized facts (unicode, nested evidence, unknown extra fields, absent optionals) sign → store → load → strict-verify `valid`, dict equality.
- **Parity harness** (`eval-store-parity.py`): the §9 checks as a runnable tool, so Mira's parallel-run is a command, not a procedure.
- **Floor tests:** `sqlite3` importable at 3.9; no new hard imports on the hook-reachable set; FTS5/vec absence degrades silently.
- **Purge test:** after purge+vacuum, the deleted content is absent from the raw DB bytes.

## 11. Rollout

| Phase | What | Owner |
|---|---|---|
| P1 | `_storeback.py` contract + `JsonStore` (no behavior change), scripts moved onto it | Fable |
| P2 | `SqliteStore` + `migrate-store.py` + parity harness + receipts | Fable |
| P3 | Mira: migrate `--apply` on fleet-02, parallel-run soak (§9), receipts to thread | Mira |
| P4 | Cutover flag post-refactor; `facts.json` demoted to nightly export | Mira (Kevin gate) |
| P5 | Accelerators behind the contract: FTS5 index, sqlite-vec, `VACUUM INTO` snapshot rotation | Fable |

P1+P2 are pure base-repo work and start immediately; nothing touches fleet-02 until P3.

## 12. Open questions

1. **Hook interpreter FTS5:** fleet-02's `python3` has FTS5; the *hook* resolves whatever `python3` is on PATH in its environment — verify at P3, though nothing in P1–P4 depends on FTS5.
2. **Insights/embeddings/graph residency (P2 refinement):** all three are *derived, regenerable* artifacts (`synthesize.py`, `embed-facts.py --backfill`, `export-graph.py`), so the v1 contract covers the authoritative data only — facts. The `insights`/`embeddings` tables ship in the schema but stay unpopulated, and the file artifacts remain the live sources, until P5.
3. **Snapshot cadence:** `VACUUM INTO` per distill vs per promotion — Mira's retention policy (E1) owns this; the store just provides `snapshot()`.

---

*Review requested: Mira (operate-side — especially §6 busy-timeout, §8 receipt fields, §9 soak length), then Kevin's gate on P3 timing relative to the command-center refactor.*
