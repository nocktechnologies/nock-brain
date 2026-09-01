# Mira Brain — Enhancement Plan

**Date:** 2026-07-26 · **v2:** 2026-07-27 — Mira's redline (`2026-07-27-mira-brain-enhancement-plan-REDLINE.md`) folded in
**Status:** **Approved** — Kevin green-lit the E5b privacy call and the signed-store mutations on 2026-07-27 (see Decision log). Build split: Fable builds tooling in base nock-brain; Mira pin-bumps and operates on fleet-02.
**Scope:** `/home/nock/Dev/mira-brain` + `/home/nock/.nock-brain` on nock-fleet-02, plus the recall hooks in the crm-mira harness
**Basis:** Live inspection of fleet-02 on 2026-07-26, compared against nock-brain `main` @ `d3995b2` and the mid-2026 agent-memory landscape

> **⚠ Seat note (2026-08-31): every `fleet-02` in this doc means `kevins-linux`.**
> The 2026-08-27 seat migration re-homed the resident agent; fleet-02 is retired
> and no longer a valid `NOCKBRAIN_MACHINE` — it raises at mint. This plan is
> still **Approved** with E3/E7/E5b outstanding, so its scope paths and the E3
> CPU-measurement instruction target `kevins-linux`. The dated inspection
> figures and decision-log rows below are left as recorded.

---

## Current-state snapshot (2026-07-26)

| Metric | Value |
|---|---|
| Facts | 1,913 (all Ed25519-signed, strict verify in distill) |
| Kind mix | 722 merge · 349 directive · 239 bug · 215 architecture · 170 decision · 95 correction · 85 dispatch · 37 content |
| Curated / superseded (marked) | 47 / 5 — **the 5 is the marking rate, not the supersession rate.** Measured 2026-07-27: ≥5 distinct unmarked supersessions and 16 stale-but-live facts in the decision+correction slice alone (2 of 8 kinds); true activity ≥2× the marked count |
| Supersession link | **None** — bare status flag; no `superseded_by`/`invalidated_by`, so even marked facts don't record their replacement |
| Extraction duplication | **High** — one real event became 12 near-identical live facts (the `mara-nockos` surface rule); dedup is load-bearing, not cosmetic |
| Insights (synthesized) | 133 |
| Session notes | 163 |
| Code pin | `d493c9d` (#43) — 3 commits behind base `main` |
| Semantic tier | Provisioned (potion-base-8M + 2.4 MB `embeddings.npz`) but **off** — own eval showed no gain |
| Live recall path | BM25 + graph-neighbor expansion, push-injected via `UserPromptSubmit` hook |
| Backups | 61 timestamped full-copy generations, no rotation |
| Automation | Nightly receipt-bound `mira-nockbrain-distill` (fail-closed: verify → rebuild → verify → shrink-guard → atomic restore) |

Enhancements below keep their original numbering; the **v2 revised order** (per Mira's redline and the 2026-07-27 measurement) is in the Sequencing summary. Headline changes in v2: E4 denoise moves *before* E3 (embedder becomes eval-gated), E5 splits into E5a (structural, pre-E2) and E5b (LLM contradiction pass), and E6 pays off immediately rather than depending on E5.

---

## E1 — Operational hardening: pin bump, backup retention, eval gate

**Replaces:** nothing — tightens what exists. This is the zero-schema-change batch.

**Current.** The distill runner pins `EXPECTED_SOURCE_COMMIT = d493c9d`. That predates three base fixes: #44 (attestation-signature cache — today recall re-verifies up to 1,913 Ed25519 signatures on the hot path of every triggered prompt), #46 (non-finite similarity guard in dense recall), #55 (reserved dense slots 5 → 3 per the two-store sweep). Backups accumulate without rotation (61 generations of facts/graph/review/sessions/vault ≈ hundreds of MB and growing). `mira-recall-suite.json` (9 ground-truthed queries) exists but only runs when someone remembers to run it.

**Change.**
1. Bump the pin to `d3995b2` and redeploy the mira-brain checkout to match.
2. Retention policy: keep 14 daily + 8 weekly generations; archive older sets to cold storage before pruning.
3. Run the recall suite inside the nightly distill; emit hit-ranks into the receipt. Start warn-only, promote to fail-closed once thresholds are calibrated.

**Pros**
- Immediate latency win on every recall-triggered prompt (signature cache).
- Correctness fixes (non-finite guard) with zero behavior redesign.
- Recall quality becomes a monitored invariant instead of a one-time measurement.
- Disk growth capped; no data model changes; fully reversible.

**Cons**
- Pin bump requires editing the release runner and re-verifying the checkout — the fail-closed check rejects any mismatch until both sides agree (that's the feature, but it means a coordinated deploy).
- Eval gate can false-alarm as the store grows (absolute ranks drift naturally). Mitigate: warn-only first, gate on *relative* regression.
- Retention deletes history. Mitigate: archive before prune; never prune the pre-curation / pre-archive snapshots.

---

## E2 — Single SQLite store (FTS5 + sqlite-vec)

**Replaces:** the 3.9 MB monolithic `facts.json`, the separate `embeddings.npz` sidecar, full-file rewrite on every mutation, and full-copy backups.

**Current.** Every nightly distill and every `supersede`/`purge` rewrites the whole JSON file. BM25 is computed in Python over the entire store per recall. Vectors live in a sidecar keyed by fact id that can drift from the facts file. Backups are whole-file copies.

**Change.** One SQLite database in WAL mode: a `facts` table, an FTS5 index (BM25 becomes a SQL query), a `vec0` virtual table (sqlite-vec) for embeddings, and an `insights` table. RRF fusion of the two rankings runs in-process. JSON, vault, and graph.json become derived exports for audit — same artifacts, no longer the source of truth. `bin/_store.py` already abstracts store IO, so the swap is contained to the store layer plus per-script call sites.

**Pros**
- Incremental, transactional writes — distill appends instead of rewriting 4 MB nightly.
- Facts and vectors can never drift (same transaction).
- Hybrid recall becomes one indexed query; scales past 10k facts without touching recall code.
- Snapshots via `VACUUM INTO` (or Litestream streaming replication) replace 61 full-copy generations.
- Supersession/purge become UPDATEs — no rewrite races.

**Cons**
- Attestation today signs canonical JSON bytes. A DB row needs a defined canonical serialization, and the migration must re-sign all 1,913 facts. This is the riskiest step — do it in the distill window with the shrink-guard active and a parallel-run period comparing old/new recall outputs.
- sqlite-vec is the first native-extension dependency on the hook path (which today runs stock `python3`). Keep a BM25-only fallback when the extension is absent — same degrade-gracefully contract the semantic tier already has.
- Every `bin/` script that opens `facts.json` needs the store-layer call swap; the long tail (exports, purge, health) is boring but real work.

---

## E3 — Embedder upgrade + cross-encoder reranker

**Replaces:** potion-base-8M static embeddings (and the current decision to leave the semantic tier off).

**Current.** The semantic tier was provisioned on Jul 11 but `semantic-on` is absent — correctly, because Mira's own eval showed it didn't help (e.g. S1: ground-truth fact ranked 113th flat, 139th with semantic on). potion-base-8M is a ~30 MB static-embedding distill; it cannot rank meaning, only token pools. Live recall is BM25 + graph expansion.

**Change.** Swap to a current small local embedder — **EmbeddingGemma-300M** (~622 MB, ~69.7 MTEB English) or **Qwen3-Embedding-0.6B** (~70.7 MTEB-eng-v2), both Apache-2.0 — re-backfill vectors, re-enable the tier. Add a small cross-encoder reranker over the fused top-50 before the injection cutoff. Re-run `mira-recall-suite.json` before flipping anything on; the harness to prove or kill this already exists.

**Pros**
- Paraphrase recall that actually works — this addresses the exact failure mode that justified turning the tier off.
- Reranker fixes precision at the cutoff that matters (the ~10 injected slots), which RRF alone can't.
- Still fully local: no API keys, no services — the privacy story is unchanged.
- Decision stays evidence-driven: the eval suite gates enablement.

**Cons**
- Footprint jumps from ~30 MB to 0.6–1.5 GB, and inference needs onnxruntime or torch on a box running 14-agent workloads. Nightly backfill is easy; the constraint is per-prompt query embedding + rerank inside the hook's ~2 s budget. Measure on fleet-02 CPU first; if rerank of top-50 exceeds ~400 ms, shrink to top-25.
- Full re-embedding backfill invalidates the existing sidecar/cache.
- If the eval says the gain is marginal again, stop at the reranker (which helps BM25-only recall too) and skip the embedder.

---

## E4 — Memory taxonomy: episodic / semantic / procedural, with decay

**Replaces:** the flat, kind-labeled, everything-lives-forever store.

**Current.** 722 of 1,913 facts are merge records — git-derivable operational noise that competes in BM25 ranking against the 170 decisions that recall exists to serve. Only 5 facts have ever been superseded. Nothing tracks whether a fact has ever been recalled.

**Change.** Adopt the split that's now standard across memory systems:
- **Episodic** (merge, dispatch, status): TTL decay → auto-archive tier, searchable on miss but out of default ranking.
- **Semantic** (decision, architecture, bug): durable, default recall set.
- **Procedural** (directive, correction): standing rules — formalizes the `feedback-rule-recall.py` path that already exists in the harness as a first-class tier.
Track recall hits per fact; reinforce frequently-used facts, decay never-used ones. One-time LLM-assisted, human-gated reclassification pass over the existing 1,913.

**Pros**
- Recall precision improves without touching the ranker — less noise in the IDF pool.
- Store growth decouples from fleet activity volume.
- Archive-not-delete preserves the audit trail (consistent with existing archive practice).
- Procedural tier turns a side-channel script into a designed part of the system.

**Cons**
- TTL thresholds are judgment calls; occasionally an archived merge fact *is* the answer ("when did we merge X?"). Mitigate: fall through to archive-tier search when the primary set misses.
- Usage tracking adds a write to the recall hot path — batch it (append counts at session end), never synchronous.
- The reclassification pass needs Mira's review time; route it through the existing promotion-review queue rather than inventing a new gate.

---

## E5 — LLM-backed distillation — **v2: split into E5a / E5b**

**Replaces:** regex/heuristic fact extraction and heuristic insight clustering — *reframed* by the 2026-07-27 measurement: distinct-event supersession volume is modest (~5/265), but fact-level pollution and duplication are the real cost. The cheap structural half captures most of the immediate backlog value with zero new dependency and zero privacy exposure, so it ships first.

**Current.** Extraction is tag-matching (`[DECISION]` → 0.9) plus pattern-matched language ("user decided…" → 0.7–0.85). Synthesis clustering already has an **opt-in LLM path in production**: `synthesize.py --llm` runs Haiku via `claude -p` on the Claude Code subscription (zero metered spend) to enrich insight prose. Contradiction detection doesn't exist — the store cannot tell an agent it was corrected.

### E5a — structural (approved; rides ahead of E2)
1. **Dedup** near-identical extractions (the 12→1 `mara-nockos` case is the archetype). Zero schema dependency, so it lands *before* E2 and shrinks the migration surface.
2. **Supersession-link field** (`superseded_by` / `invalidated_by`) so marked facts record their replacement.
3. **One-time cleanup** of the 16 measured stale facts — executed inside the fail-closed distill pipeline under Kevin's gate.

### E5b — nightly LLM contradiction pass (approved; lower priority, lands last)
Run new transcript deltas through Haiku with structured outputs, emitting contradiction candidates into the human-gated review queue as supersession proposals. **Costing (grounded, per Mira's correction):** implemented on the existing `claude -p` subscription path (`synthesize.py` pattern) — zero metered spend; the metered Batch API is fallback only if that path proves unsuitable (longer prompts, structured-output needs). Heuristic extraction stays as the offline fallback — the fail-closed philosophy is unchanged.

**Pros**
- E5a alone clears most of today's measured backlog (duplication + unmarked staleness) with no external dependency.
- Contradiction detection is the biggest *correctness* win available: the README's own thesis is that stale decisions are worse than no memory.
- Zero metered spend on the subscription path; nightly cadence means zero impact on prompt latency.
- Proposals stay human-gated; the model never rewrites the store directly.

**Cons**
- E5b sends transcript content off-box. The existing privacy fences (denied paths, private-payload drops, secret redaction) must run **before** the `claude -p` call — they already run at ingest, so ordering is enforceable, but this needs explicit verification in review. *(Kevin accepted this trade-off 2026-07-27.)*
- The `claude -p` path is proven for insight-prose enrichment only; its fit for a structured contradiction pass is a design assumption to validate early.
- Nondeterminism vs. the pinned-commit reproducibility story. Mitigate: log prompts + raw outputs as distill receipts; keep the shrink-guard and strict signature verify exactly as they are.

---

## E6 — Bi-temporal fact schema

**Replaces:** boolean supersession (`status` + reason).

**Current.** A fact is either live or superseded. There's no record of *when* a fact was true, so "what was our deploy policy in May?" is unanswerable, and un-superseding after an incorrect merge of direction is manual archaeology.

**Change.** Add `valid_from` / `valid_to` / `invalidated_by` (distinguishing event time from ingest time — the Graphiti/Zep insight, adopted as a schema rather than a platform). `supersede-fact` closes an interval instead of flipping a flag. Default recall filters to currently-valid; audit queries can time-travel. Backfill `valid_from` from `source_date` for existing facts.

**Pros**
- Point-in-time queries; supersession becomes reversible and auditable.
- A signed **and** time-versioned fact ledger is a trust story none of the commercial memory products have.
- Nearly free if the schema lands during the E2 migration (one migration, one re-sign).

**Cons**
- ~~Only pays off if supersession happens at volume~~ **v2: supersession already happens at volume — it's just unmarked (measured ≥2× the recorded rate). E6 pays off immediately, independent of E5b.** Land the schema *and* the supersession-link field with E2's single migration; backfill the 16 measured cases as the first closed validity intervals.
- Every consumer — recall filters, exports, vault, graph — must learn the validity filter; miss one and superseded facts leak back into recall.
- Backdated `valid_from` for 1,913 existing facts is approximate. Accept the fuzziness; document it.

---

## E7 — Pull path: MCP server + progressive disclosure

**Replaces:** nothing — augments push-only injection.

**Current.** Memory reaches Mira exactly one way: the `UserPromptSubmit` hook classifies the prompt and, if it fires, injects up to ~10 facts (1,000–1,500 tokens). Mid-task, she cannot ask her own memory anything. `session_anchor` drill-back pointers exist on every capped fact but are dead ends — nothing can traverse them.

**Change.** Expose the store as a local MCP server: `memory_search`, `memory_expand` (follows anchors to session notes), `memory_propose_supersession` (writes go to the review queue, never directly to the store). Shrink hook injection to compact titles + anchors (~200 tokens); Mira expands what's relevant on demand. This matches where the platform went — Anthropic's native memory tool + context editing exist precisely because pull-based memory saves tokens and improves long-task performance.

**Pros**
- Large token savings on every triggered prompt; the context window carries pointers, not payloads.
- Memory becomes usable mid-task and mid-investigation, not only at prompt boundaries.
- Anchors finally do their job.
- Write path stays human-gated by construction.

**Cons**
- Pull depends on the model choosing to call the tool. Keep push for classifier-triggered cases — hybrid, not replacement — or high-signal recalls will silently stop happening.
- A new always-on service on a box whose memory jobs are deliberately sandboxed. Run it socket-activated, read-only by default, same hardening profile as the distill unit.
- Tool results enter context as data; the harness's prompt-injection-guard hygiene must extend to memory tool output.

---

## Sequencing summary

| # | Enhancement | Effort | Risk | Depends on | Payoff |
|---|---|---|---|---|---|
| E1 | Pin bump · retention · eval gate | S | Low | — | Immediate latency + monitored quality |
| E5a | Dedup · link field · 16-fact cleanup | S–M | Low (human-gated) | — (pre-E2 by design) | Clears measured backlog; shrinks E2 migration |
| E2 | SQLite store | M–L | **Highest** (re-sign migration) | — | Foundation for the rest |
| E6 | Bi-temporal schema + link field | S–M | Low (rides E2's migration) | E2 | Time-travel audit, reversible supersession — pays off immediately |
| E4 | Taxonomy + decay | M | Low–Med | E2 | Precision + bounded growth; gates E3 |
| E3 | Reranker (embedder eval-gated) | M | Medium (CPU budget) | E4 re-eval | Precision at the injection cutoff |
| E7 | MCP pull path | M | Medium (new service) | E2 | Token savings + mid-task memory |
| E5b | Nightly contradiction pass | M | Medium (privacy — accepted) | E5a; `claude -p` path | Going-forward staleness hygiene |

**Recommended path (v2):** E1 now → E5a dedup (pre-E2, approved) → E2 (+ E6 schema + supersession-link field, one migration) → E4 denoise → re-run `mira-recall-suite.json` → E3 reranker (embedder only if the eval earns it) → E7 → E5b.

---

## Decision log

| Date | Decision | By |
|---|---|---|
| 2026-07-27 | Redline accepted in full: marking-rate reframe, E4-before-E3 (embedder eval-gated), E5a/E5b split, E6 immediate | Fable + Mira (msgs #36219/#36403) |
| 2026-07-27 | **E5b privacy: approved.** Fenced transcript deltas may go off-box via the `claude -p` subscription path (`synthesize.py` pattern); metered Batch API is fallback only | Kevin |
| 2026-07-27 | **Signed-store mutations: approved.** 12→1 `mara-nockos` dedup + cleanup of the 16 measured stale facts; execution stays inside the fail-closed distill pipeline with strict signature verification | Kevin |
| 2026-07-27 | Build split: Fable builds tooling in base `nock-brain`; Mira pin-bumps and operates against the store on fleet-02 | Kevin |

---

*v1 prepared for Mira's review from a live fleet-02 inspection on 2026-07-26. v2 folds in Mira's redline (its live measurement of 265 decision/correction facts) and Kevin's 2026-07-27 approvals. As of v2, nothing has been applied to the signed store; E5a tooling is in build.*
