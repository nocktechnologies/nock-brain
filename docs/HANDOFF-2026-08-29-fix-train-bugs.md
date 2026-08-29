# HANDOFF — fix-train bug backlog (for Grok, nock-brain)

**Written 2026-08-29. You are picking up a batch of already-filed, already-verified
bug nocks. Do NOT re-investigate whether they are real — that work is done.**
Each nock was found by a model code-review, then independently re-verified against
the code by a separate referee that re-executed the failure scenarios. The evidence
is archived in-repo (see below). Your job is to FIX them, not re-litigate them.

## Where these came from

A nine-model bake-off ran the same skeptical review of the `#47–#93` fix train at
commit `4be09b9`. The union of reviews surfaced these bugs; every one below is
referee-CONFIRMED. Full method + scored results:

- `docs/evals/2026-08-28-model-comparison/RESULTS.md`
- `docs/evals/2026-08-28-model-comparison/review-*.md` — the raw per-model reviews
  with the exact `file:line` citations and reproduction steps. **These are your spec.**
  Every finding below traces to citations in those files; use them instead of
  re-deriving the bug.

All line numbers in the reviews are against pinned commit `4be09b9`. Re-anchor to
current `main` before editing (files have moved a little since).

## The backlog (all filed, state=queued, owner=kevin)

Grouped by severity. Each nock's description carries a PROBLEM / SOLUTION /
DONE-SPEC. Take them roughly top-down; the two theme clusters (keys, and the
`status` field) are the highest-leverage.

### Critical

- **N10014** — Nightly rebuild resurrects purged/edited/superseded facts.
  `merge_facts` is recent-wins by deterministic id, and re-extraction re-mints as
  `current`, defeating rule zero. Fix: rebuild must consult tombstones /
  revocations / fact-edits before merge.
- **N10013 + N10017** — Signing-key split-brain (these two are the SAME finding,
  filed twice — dedup them first, keep one). Recall verifies against env keys while
  sign/verify/rebuild silently mint a default `~/.nock-brain` key; nothing detects
  the disagreement, so recall goes dark while every surface reads green. Fix: one
  shared env-aware key-resolver across all four surfaces + a health check comparing
  recall's key identity to the store's attestation.
- **N10015 + N10016** — Mutable `status` field drives recall but lives outside every
  signature (also a duplicate pair — dedup, keep one). This is the root cause behind
  three separate confirmed attacks: rebuild resurrection (N10014), unsigned
  suppression (flip status→superseded), and revocation flip-back (flip
  superseded→current serves revoked content with a VALID signature). Design-level
  fix: bring status (or a status-transition log) under signature, OR make the recall
  path audit `revocations.jsonl` + supersession events the way offline `verify-facts`
  does. Fixing this correctly may subsume parts of N10014.

### High

- **N10018** — Ed25519 verify-cache HMAC is keyed on the PUBLIC key → a store-dir
  writer can forge VALID cache entries. Key the HMAC on secret/per-machine material,
  or drop the pre-verify short-circuit for Ed25519. (Security.)
- **N10019** — `purge-fact --apply` leaves purged content live in `insights.json`
  (and graph.json); recall re-injects it first until the next nightly. Scrub/regen
  derived views in the same transaction.
- **N10020** — v2 claim-authority facts may never rank: recall requires `source_date`,
  which the v2 payload never emits. Emit it at mint (from `source_time`) or teach
  `RECALL_ITEM_FIELDS`/`filter_valid_facts` the v2 shape.
- **N10021** — `--strict-verify` fails OPEN on a missing/unloadable key while its help
  says "Fail closed". In strict mode, an unusable key must be fatal (empty recall +
  loud error).
- **N10022** — v2 temporal window (`valid_from`/`valid_to`) is signed but enforced
  nowhere; expired claims inject as current. Teach `fact_currently_valid` the v2
  fields + wire `revokes_revision_ids` into the revocation audit.
- **N10023** — SQLite-cutover split-brain: purge/rebuild/sign/consolidate/promotion
  all bypass the backend contract, and insights mis-key onto `brain.db`. Route all
  writers through `_storeback.resolve_store`; fix `resolve_store` to honor the
  requested basename. (Latent until `NOCKBRAIN_STORE` flips — fix BEFORE any E2
  cutover.)

### Medium / Low

- **N10025** — `consolidate --execute` supersedes without minting signed revocation
  events (violates the mark-only/revocation non-negotiable). Call
  `record_supersessions` + set `invalid_at` in `apply_supersessions`.
- **N10028** — `purge-fact --apply` on zero matches still rewrites the store,
  destroying loader-dropped records; pattern also matches signature hex.
- **N10031** — Health reports a verify-cache sidecar "fresh" that recall's loader
  rejects (`sidecar_status` vs `_load_digests` divergence). *(This was the planted
  control bug in the bake-off — it's real and was already open as N9981; N10031 is
  the filed fix.)* Share one predicate so freshness means loadable.

## Ground rules

- **Never sign facts except through `_sign.sign_facts`** (the N9851 trap — see
  `docs/REPO-MAP.md` §7). Several of these fixes touch signing; respect the router.
- **Consolidation is mark-only; supersessions mint signed revocation events.** N10025
  and the N10015/16 cluster live right on this invariant — honor it.
- **BM25 is the recall floor.** Fixes in the recall path must degrade to the
  unchanged seed list, never crash it.
- **`facts.json` is authoritative; sidecars/exports are derived.** N10019/N10023 are
  about keeping derived views honest — regenerate, don't hand-edit.
- Read `docs/REPO-MAP.md` first (the repo's own instruction). It maps the pipeline,
  the recall ranking order, and the v1/v2 attestation model these bugs live in.
- Each nock has a DONE-SPEC (a test that must pass). Land the test with the fix.
- When a fix changes a module's contract, update `docs/REPO-MAP.md` in the same PR.

## First moves

1. Dedup N10013/N10017 and N10015/N10016 (keep one of each; the pairs are identical
   findings filed twice).
2. Read `docs/evals/2026-08-28-model-comparison/RESULTS.md` for the shape, then pull
   the specific review file cited by whichever nock you start with.
3. The two clusters — key resolution (N10013/17 + N10018 + N10021) and the `status`
   field (N10014 + N10015/16 + N10025) — each want one coherent design pass rather
   than per-symptom patches. Start there; the standalone highs (N10019, N10020,
   N10022, N10023) are more independent.
