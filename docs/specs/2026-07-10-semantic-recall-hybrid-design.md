# Spec: Hybrid Semantic Recall (Embeddings + RRF Fusion + Conditional Rerank)

## Objective
Close the vocabulary-mismatch gap in budget-capped recall: a query phrased
differently from the stored fact ("how are customer payments handled") must
find the fact ("Stripe webhook secret rotated") without sharing its tokens.

Measured on the live 2,480-fact store (2026-07-10, `bin/eval-graph-recall.py`):
BM25 recall hits 3/3 keyword-control queries but only 2/6 paraphrase queries.
The misses split into two classes with different fixes:

- **Class A — retrieved but buried.** The target matched weakly and ranked
  below noisy seeds (rank 63 of 242; rank 17 with injection cut at 9).
  Fix: better fusion/ranking.
- **Class B — never retrieved.** Zero token overlap; the target is absent
  from the candidate list entirely. Fix: dense (semantic) retrieval. Graph
  expansion cannot rescue this class — it anchors on BM25 seeds, and when
  the seeds are wrong-topic its neighbors are wrong-topic drift (verified
  post-#35).

Success: the M-query suite in `bin/eval-graph-recall.py` reaches >=5/6
hit-in-injection with controls staying 3/3, at <1s p50 added hook latency,
with the flag-off path byte-identical to today's recall.

## Assumptions
- The v1 recall spine (BM25 `budget-recall.py`, classifier gate, insight
  lead, diversity cap, token budget, attestation verify) remains authoritative;
  the semantic path is an additive candidate source, same pattern as #35's
  graph layer.
- **No API billing.** Fleet policy (Mar v2 decision): Claude subscription
  only; there is no Anthropic embeddings API. All embedding inference is
  local. OpenAI/hosted embedding options in the pgvector wiki are rejected.
- **Dependency-optional.** Core stays stdlib-only. Semantic recall is an
  opt-in extra: if its deps or model file are missing, recall degrades to
  BM25 silently (stderr note, not failure). numpy is the only hard runtime
  dep of the tier (already present on the dogfood box).
- **Hook process lifecycle is the binding constraint.** `memory-inject.sh`
  spawns a fresh python3 per prompt, so model *cold-start* counts every
  time. This rules out sentence-transformers/torch (2-5s import) and is why
  the wiki's server-oriented "nomic-embed by default" advice does not
  transfer directly. No daemons — nock-brain stays "files + hooks".
- **No database.** At this scale brute-force numpy cosine over an in-memory
  matrix is <5ms at 100k facts (2,480 today = ~3.8MB at 384d). pgvector /
  PGlite / HNSW / SQLite-vec are explicitly out of scope until a store
  exceeds ~100k facts.
- Fact `content` is already capped at 1,500 chars (p99 measured), ~375
  tokens — whole-fact embedding fits every candidate model's window.
  Chunking is out of scope.
- Deploys to every brain checkout the same way (nock-brain, mira-brain,
  mar-hq's brain): per-store sidecar, no GPU assumed, Apple Silicon and
  Linux VPS both supported.

## Design

### D1 — Retrieval: hybrid BM25 + dense, fused with RRF
Two ranked candidate lists per query:

1. **Lexical:** today's `search()` output unchanged (BM25 x confidence x
   recency x supersession, min-matched-terms bar).
2. **Dense:** cosine similarity of the query embedding against the fact
   matrix, multiplied by the SAME confidence/recency/supersession gates
   (reuse the injected functions, as `_graph_recall.expand()` does). No
   term-match bar — that is the point of this list. Top `2 x match_count`
   candidates.

Fuse with Reciprocal Rank Fusion, k=60 (wiki `hybrid-search.md`): RRF needs
no score calibration between BM25 and cosine and is the least-tunable option.
Weighted fusion (favoring lexical for exact-token queries) is a knob to add
only if the eval demands it. Everything downstream — insight lead, covered-id
dedup, date diversity cap, budget truncation — is unchanged.

Graph expansion (#35) composes after fusion, anchoring on the fused list;
with on-topic dense seeds it enriches rather than drifts.

### D2 — Vector store: sidecar file next to facts.json
`~/.nock-brain/embeddings.npz` holding: fact ids, content SHA-256 hashes,
model id + dimension, float32 matrix. Rules:

- **Derived data, never authoritative.** Recall joins by fact id; the fact's
  attestation is still what `--strict-verify` checks. A vector whose hash no
  longer matches its fact's content is ignored and queued for re-embed.
- **Purge parity (privacy).** `purge-fact.py` must delete the vector row in
  the same operation as the fact — embeddings are content-derived and
  recoverable by inversion attacks; a purged fact may not leave a vector
  behind. Same for `rebuild-store.py` (rebuild drops orphans).
- Incremental: `embed-facts.py --backfill` for the initial pass;
  `embed-facts.py --new` embeds facts whose id/hash is absent (cheap enough
  to run from the existing post-session extraction path).

### D3 — Embedding model: decided by a Phase 0 spike, not this spec
Candidates, all local, all Apache/MIT:

| Candidate | Deps | Cold start | Quality prior |
|---|---|---|---|
| model2vec static (e.g. potion-base-8M) | numpy only | ~tens of ms | lowest, but strong for retrieval-at-this-scale |
| all-MiniLM-L6-v2 via ONNX | onnxruntime | ~0.3-0.6s | mid (56 MTEB) |
| nomic-embed-text v1.5 @ 384d (Matryoshka) via ONNX | onnxruntime | ~0.5-1s | highest (62 MTEB) |

Selection criteria, in order: (1) M-suite hit rate on the real store,
(2) total hook latency p50 <1s cold, (3) install weight. The spike runs all
three through `eval-graph-recall.py` and records the decision in this spec.
Model files are version-pinned with checksums at install time; no network
access at recall time.

### D4 — Rerank: conditional Phase 3, not built up front
Hypothesis to test first: RRF fusion alone fixes Class A (a buried target's
dense rank will be high, and RRF sums ranks). Only if the post-fusion eval
still shows burial do we add a cross-encoder rerank stage (ONNX
bge-reranker-class, rescoring only the fused top-20) — it roughly doubles
model weight and latency, so it must earn its place with eval data.

### D5 — Gating and rollout
`NOCKBRAIN_SEMANTIC=1` env flag, exactly the #35 pattern: default off at
first, off-path byte-identical (golden test), flip the default only after
the eval gates pass and a week of dogfood on this store. Missing deps or
sidecar => silent BM25 fallback even when flagged on.

### Out of scope
Query expansion via LLM (latency + billing), PGlite/pgvector, embedding
daemons, TeamOS/source-scoping changes (source scoping already exists in
`search()`), re-ingesting more history, insight-store embedding (revisit
after facts prove out — insights are few and BM25-findable today).

## Phases

- **Phase 0 — model spike (half day).** Bench the three candidates on the
  eval suite + cold-start-in-hook measurement. Deliverable: decision record
  appended here; the losing models never become deps.
- **Phase 1 — embedding store (1 day).** `bin/embed-facts.py` (backfill +
  incremental), sidecar format, hash invalidation, purge/rebuild parity,
  tests (fixture-scale, no model download in CI — inject a stub encoder).
- **Phase 2 — hybrid recall (1-2 days).** `_dense_recall.py` module gated
  from `budget-recall.py` (mirror `_graph_recall.py`'s pure-pass-through
  off-path), RRF fusion, hook wiring, golden off-path test, eval rerun.
  Acceptance: >=5/6 M-suite, 3/3 controls, <1s p50 hook.
- **Phase 3 — conditional rerank.** Only on eval evidence of residual
  Class-A burial.
- **Phase 4 — install + default (half day).** `install.sh` opt-in prompt
  (download model, run backfill), README/SKILL.md docs, default-on decision
  after dogfood burn-in.

## Verification
`bin/eval-graph-recall.py` is the regression benchmark for every phase (it
predates this spec and measured the problem). CI stays green with no model
files: unit tests use a stub encoder; the eval is an offline tool run against
live stores, not a CI job.

## Phase 0 Decision Record (2026-07-11)

Spike run against the live 2,480-fact store; all three candidates embedded
the full store and ran the eval suite through the exact production selection
path. Harness: scratchpad `spike_embed.py` / `spike_eval.py` / `spike_cold.py`
(fresh-process cold starts, 3-5 runs each, Apple Silicon).

### Model decision: potion-base-8M as RAW static embeddings

| Criterion | potion-raw | MiniLM-L6 ONNX | nomic-v1.5 @384 ONNX |
|---|---|---|---|
| M-suite hit@injection (amended pipeline, k=3) | **3/6** | 2/6 | 2/6 |
| Controls | 3/3 | 3/3 | 3/3 |
| Dense rank, M2 paraphrase probe | 3 | 5 | 4 |
| Cold start p50 (fresh process) | **~430ms** | ~710ms | 1.6s first / ~370ms page-warm |
| Store backfill (2,480 facts) | **1.5s** | 4.5min | 18.8min |
| Runtime deps beyond stdlib | **numpy + tokenizers** | + onnxruntime | + onnxruntime |
| Model assets | **~30MB** | ~98MB | ~523MB |

Retrieval quality was statistically indistinguishable across all three on
this corpus — the binding constraint is corpus noise (1,500-char operational
blobs where the answer token is incidental), not model capacity. With quality
tied, potion wins every operational criterion. "Raw" matters: loading via the
model2vec library cost 0.7-2.1s; the model is just a token-embedding matrix,
so runtime encode is `tokenizer.json` + numpy lookup (skip [CLS]/[SEP],
mean-pool, L2-normalize — verified cosine 1.0 parity against model2vec).
Install converts the pinned safetensors to `.npy` once; model2vec is at most
an install-time dep, never a runtime one. Fallback if a quality ceiling ever
binds: MiniLM-ONNX (best M4 dense rank), or quantized nomic (unexplored).

### Design amendments from the spike (bind on Phase 2)

1. **D1 amended — dense gates are FILTER-only.** Multiplying cosine by
   recency/confidence destroys paraphrase recall: cosine lives in a ~0.2-0.6
   band, so a 0.44 recency factor buried a perfect-paraphrase hit (the
   Deepgram/STT fact, raw dense rank 5) below recent noise. Dense candidates
   keep the superseded/validity/min-confidence *filters* only; recency stays
   a lexical-side and selection-time concern.
2. **D1 amended — reserved dense slots.** RRF alone under-serves strong
   dense hits on noisy stores: M2's dense-rank-3 hit fused to 24 (generic
   both-list facts collect two RRF terms), then the date diversity cap
   demoted it to 66 (it was the 9th fact from the 2026-05-19 bulk-import
   date). Guarantee the top-3 dense-only facts a place in the injected set
   (displacing the tail, exempt from the date cap). Measured: +1 M-suite on
   every model; the diversity cap must not apply to reserved slots.
3. **Insight lead needs a cap.** 20 insights prepended on one eval query
   consumed most of the 800-token budget before any fused fact. Phase 2
   should cap the insight lead (e.g. top 5) — micro-eval when implementing.
4. **Eval suite curation.** Two M-queries (M1 payments/stripe, M3
   tts-quota/elevenlabs) have NO genuinely on-topic fact in the store — the
   token-bearing facts are operational blobs that mention the token
   incidentally. Token-presence ground truth mislabels these; semantic
   retrieval is *correct* not to surface them. Phase 2 acceptance re-bases
   on a curated suite (ground truth by fact id, targets verified on-topic):
   >=5/6 becomes "all queries with a verified on-topic target, minus at
   most one".

### Measured store costs (for Phase 1)
256-dim float32 sidecar for 2,480 facts: ~2.5MB. Full re-embed of the store
with potion-raw: seconds — hash-invalidation can be coarse without pain, and
model swaps are cheap enough to re-embed on upgrade rather than migrate.

## Phase 2 Acceptance (2026-07-11)

Hybrid fusion implemented in budget-recall behind `NOCKBRAIN_SEMANTIC` (or
`--semantic`) with all four Phase 0 amendments: filter-only dense gates,
reserved top-3 dense slots exempt from the date diversity cap, insight lead
capped at 5 (`NOCKBRAIN_INSIGHT_LEAD`), and fact-id ground truth
(`docs/evals/curated-recall-suite.json`). The eval now drives the real
`select_recall()` pipeline, not a replica.

Measured on the live 2,480-fact store: curated suite **baseline 6/8 ->
semantic 8/8** (verified-target queries 5/5, controls 3/3) — acceptance was
"all verified-target queries minus at most one". The flagship zero-overlap
paraphrase (S1, Deepgram/STT) goes miss -> hit via its reserved slot; S2
goes miss -> rank 4. Phase 0 suite for continuity: 5/9 -> 6/9 (M1/M3 remain
no-target artifacts; semantic recall is correct not to surface them).
In-process recall latency 0.1-0.7s per query with the mmap'd model.

Insight-cap micro-eval: 8/8 with the cap on or off — the reserved-slot
guarantee carries correctness; the cap's effect is budget allocation (more
fused facts reach injection). Default stays 5.

**Phase 3 (cross-encoder rerank) is NOT triggered:** post-fusion, no
verified-target query shows Class-A burial. Remaining work is Phase 4
(installer wiring: tokenizers dep on the box, default-on decision after
dogfood burn-in — flag stays off by default until then).
