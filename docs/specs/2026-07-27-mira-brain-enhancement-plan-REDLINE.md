# Mira Brain — Enhancement Plan · MIRA REDLINE

**Date:** 2026-07-27
**Redlines:** `2026-07-26-mira-brain-enhancement-plan.md` (same directory) — read that first; this marks only what I'd change.
**Basis:** my review + a **live measurement run 2026-07-27**: a conservative LLM contradiction pass over the 265 currently-live `decision`+`correction` facts (2 of 8 kinds), plus a structural read of the store.

Redline conventions: ~~strikethrough~~ = remove · **[+ …]** = add/replace · `NOTE:` = rationale.

---

## Why this redline exists — what the measurement changed

The plan's current-state table cites **"superseded: 5"** and E6 leans on it ("only pays off if supersession happens at volume"). That 5 is misleading, and the whole plan should be read in that light:

- **5 is the *marking* rate, not the *supersession* rate.** In just the 265 live decision/correction facts I found **≥5 distinct real supersessions that were never marked**, producing **16 currently-live facts that are stale** — each one a wrong answer sitting in recall. That's 2 of 8 fact kinds; the true store-wide rate is higher.
- **Duplication multiplies staleness.** 12 of those 16 are *one* decision (the `mara-nockos` surface requirement) extracted into 12 near-identical facts. Dedup is a first-class need, not a footnote.
- **The store can't even link a supersession.** `status` is a bare flag; there is no `superseded_by`/`invalidated_by` field. Even the 5 *marked* facts don't record what replaced them.
- **Live kicker:** the single most recent unmarked supersession in the store is the voice/text→harness-delivery rule Kevin corrected me on *today*. The store literally cannot tell me I was wrong.

Evidence file: `/tmp/nb-contradictions-found.json` (16 confirmed, 5 borderline, 265 analyzed).

---

## Redline

### Current-state snapshot (§ table)

~~| Curated / superseded | 47 / 5 |~~
**[+ | Curated / superseded (MARKED) | 47 / 5 — but "5 superseded" is the MARKING rate. Measured 2026-07-27: ≥5 distinct unmarked supersessions + 16 stale-but-live facts in the decision+correction slice alone (2 of 8 kinds). True supersession activity ≥2× the marked count. |]**
**[+ NEW ROW | Supersession link | NONE — bare status flag, no `superseded_by`/`invalidated_by`; marked facts don't record their replacement. |]**
**[+ NEW ROW | Extraction duplication | HIGH — one real event = 12 near-identical live facts; dedup is load-bearing, not cosmetic. |]**

### Sequencing (§ summary table + "Recommended path")

~~E3 and E4 in parallel~~ → **[+ E4 (denoise) BEFORE E3 (embedder). ]**

`NOTE:` The doc's own datapoint — semantic recall ranked the ground-truth fact *worse* (113th → 139th) — reads as a noisy IDF pool, not a weak embedder. 722/1,913 facts are git-derivable merge noise. Denoise first, re-run `mira-recall-suite.json`, and the 0.6–1.5 GB embedder may prove unnecessary on a 14-agent box. Don't pay CPU for an embedder to out-shout noise E4 deletes for free.

~~**Recommended path:** E1 now → E2 (with E6's schema riding the same migration) → E3 and E4 in parallel → E5 → E7.~~
**[+ Revised path: E1 now → E2 (+ E6 schema **+ supersession-link field**, one migration) → **E4 denoise → re-eval** → E3 reranker (embedder only if the eval earns it) → E7 → **E5b**. E5a rides E4 (below). ]**

### E5 — LLM distillation — **REFRAME, don't reject** (split into E5a / E5b)

`NOTE:` The measurement says distinct-event volume is modest (~5/265) but fact-level pollution and duplication are the real cost. So the cheap structural half captures most of the immediate backlog value with **zero new dependency and zero privacy exposure** — do it first.

**[+ E5a (cheap; ride E4, no external dependency):**
1. **Dedup** near-identical extractions (the 12→1 `mara-nockos` case is the archetype).
2. **Add the supersession-LINK field** (`superseded_by` / `invalidated_by`).
3. **One-time cleanup** of the 16 measured stale facts (human-gated — Kevin's the gate on the signed store).
This is the bulk of today's backlog value, shippable now. **]**

**[+ E5b (the nightly LLM contradiction pass):** still worth it going-forward for contradictions structure can't catch — but it's the **lower-priority half** now, not the headline. **]**

~~First API dependency and recurring cost in the memory path~~
**[+ Money gate is likely MOOT: the nightly distill already runs Haiku via the `claude -p` subscription (zero metered spend). If E5b uses that same path, only the PRIVACY con — transcripts leaving the box for the call — remains. That privacy call is the single open decision (below). ]**

### E6 — bi-temporal schema

~~Only pays off if supersession actually happens at volume — which depends on E5's contradiction detection.~~
**[+ Supersession ALREADY happens at volume; it is just unmarked (measured ≥2× the recorded rate). E6 pays off immediately, independent of E5. Land the schema **and** the supersession-link field with E2's one migration; backfill the 16 measured cases as the first closed validity intervals. ]**

---

## Endorsed as written (no change)

- **E1** — pin bump · retention · eval gate. Ship now; free + reversible.
- **E2** — single SQLite store (FTS5 + sqlite-vec). The load-bearing migration; parallel-run old-vs-new recall before cutover, exactly as the doc says.
- **E3 reranker** — take it regardless (cheap, helps BM25-only too). Only the *embedder* becomes eval-gated-speculative under this redline.
- **E7** — MCP pull path + progressive disclosure. Last, hybrid with push. No change.

---

## The one decision for Kevin

**E5b privacy.** Is LLM contradiction detection over transcripts — running on the *existing* zero-metered `claude -p` subscription path — worth transcripts leaving the box for that call? Or keep the brain fully local and rely on **E5a's structural fixes + marking discipline** to hold staleness down?

Everything else in this redline is mine to execute on your go. This one is yours.

*Prepared by Mira, 2026-07-27. Figures from a live measurement on nock-fleet-02; nothing applied to the signed store without Kevin's gate.*
