# Recall evals

Committed, CI-gated evaluation of nock-brain recall quality. This is the
hardened successor to the throwaway pilot harness described in
`reports/2026-08-22-openviking-recall-pilot.md`; the design it enables is
`reports/2026-08-22-recall-session-hierarchy-design.md` (Phase 1).

## Run it

```bash
python3 bin/recall-eval.py              # print both metrics on the fixture
python3 bin/recall-eval.py --self-test  # also reproduce the cap-lever validity check
python3 bin/recall-eval.py --gate       # CI mode: nonzero exit on regression
python3 bin/recall-eval.py --json       # machine-readable
```

It runs the reconstructed **n=36 gold query set** (`recall-gold-v1.json`)
against a **committed, signed fixture store**
(`tests/fixtures/recall-eval-store.json`) — never the live `~/.nock-brain`
store — driving the real production selection path (`budget-recall.select_recall`).

## The two metrics

**recall (identity)** — the existing metric. Fraction of the 36 gold fact IDs
that land in `select_recall()['included']` (the set the injection hook actually
emits) at the production budget (400) with the production cap
(`max_per_date=4`). This answers "did the right fact get injected at all?"

**companionship (new)** — the metric that makes the session-hierarchy payoff
measurable. Identity recall is blind to it: it checks whether the gold fact
landed, not whether the surrounding context of that working episode came too. A
recalled fact that arrives *alone* is a chunk ripped out of its conversation; a
recalled fact that arrives *with its session* is context-complete memory — the
moat. Baseline is deliberately low: most hits arrive isolated. Reported in two
framings, both over the **same denominator** — the gold hits that both (a) live
in a multi-fact session (≥3 facts / ≥2 siblings) and (b) actually landed:

- `companionship` — the **mean fraction** of each hit's session-siblings that
  arrive with it. This is the design-authoritative number Phase 2 tunes; it is
  the framing that produced the pilot's 7.4% baseline.
- `companionship_hit_rate` — the **share of those hits that arrive with ≥1
  sibling** (the pilot's "15 of 18 arrived isolated" framing → 3/18 = 16.7%).

## Measured fixture baseline

| metric | fixture baseline | note |
|---|---|---|
| recall @400, cap4, semantic-off | **97.2%** (35/36) | hermetic BM25 number CI reproduces |
| companionship (mean fraction) @400 | **14.3%** | over 21 multi-session hits |
| companionship_hit_rate @400 | **33.3%** | 7 of 21 arrive with ≥1 sibling (14 isolated) |
| cap lever (self-test) | **cap2 88.9% → cap4 97.2% = +8.3pt** | reproduces the documented ~8pt lever |
| fixture attestation | **196/196 VALID** | includes 10 v2-authority facts |

The fixture's companionship baseline (~14%) is **not** the pilot's 7.4%. The
pilot measured 7.4% at budget 800 against the full 2,397-fact live store, where a
hit competes with the 636-fact 2026-05-19 backfill for budget; the fixture is a
196-fact slice with every gold session-sibling included by construction and a far
smaller competing corpus, so more siblings fit. What reproduces is the **shape**
(most hits isolated) and the **lever** (the cap defers same-date siblings). Trust
within-config deltas on this fixture, not the absolute number against the pilot's.

## How the CI gate fires

`ci.yml` runs `python3 bin/recall-eval.py --gate` after the unit tests. It exits
non-zero (failing the build) if any of these regress below their committed floor:

- **recall** below `RECALL_FLOOR = 0.90` (baseline 0.972 — headroom for BM25
  jitter across the 3.11/3.12 matrix, trips on a real recall regression);
- **companionship** below `COMPANIONSHIP_FLOOR = 0.05` (baseline ~0.14 — guards
  against a change that collapses hit-context, e.g. re-keying the date cap);
- **attestation**: any fixture fact failing to verify (guards the v2-authority
  signing rule whose violation caused the Aug-2026 40%-exclusion incident);
- **instrument self-test**: the cap lever inverting or shrinking below 5pt, which
  would mean the harness itself is no longer measuring what it claims.

The floors live at the top of `bin/recall-eval.py`. When a technique
(e.g. Phase 2 session companions) is shown to raise companionship with no recall
regression, raise `COMPANIONSHIP_FLOOR` to lock the gain in.

## Hermeticity and signing

The fixture ships **no** `embeddings.npz` sidecar, so the semantic tier is inert
by design and the gate runs **semantic-off**. With no sidecar the semantic path
degrades to BM25 (identical result), so the committed floor is a pure-BM25 number
CI can always reproduce. Signatures **are** verified, against a **disposable
fixture-only key** committed at `tests/fixtures/recall-eval-key.{json,pub}`.
That key is HMAC-SHA256 (portable — no `cryptography` dependency in CI; its
verification is symmetric, so the `.pub` is the same secret). It has **no
relationship to the production signing key** and is safe under `tests/`
(gitleaks-allowlisted; bandit scans `bin/` only).

## Regenerating the artifacts

Both generators read a source store **read-only** and treat the fixture/gold as
**derived** data (ONE SOURCE OF TRUTH: the live store), never a second vault.

```bash
# Rebuild the signed fixture slice (gold + session-siblings + same-date distractors):
python3 bin/build-recall-fixture.py                 # from the live store
python3 bin/build-recall-fixture.py --distractors 10

# Regenerate the stratified gold ID sample (queries stay hand-authored):
python3 bin/gen-recall-gold.py --out docs/evals/recall-gold-v2.json
```

**Known limitation:** `recall-gold-v1.json` is a reconstruction of a **lost**
larger n=90 set (it lived in a prior session scratchpad, was never promoted to
`bin/`, and is gone — see the `_meta` block in the JSON). `gen-recall-gold.py`
picks the stratified IDs mechanically; the paraphrase queries are, and must
remain, **hand-authored** (overlap-guarded), so the set can be extended
deliberately rather than hand-lost again — but n=90 cannot be restored.
