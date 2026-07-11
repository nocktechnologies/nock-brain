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
