# Four-way model comparison — skeptical code review of nock-brain

**Date:** 2026-08-28/29 · **Repo state:** all runs pinned to `4be09b9` (tip of the #47–#93 fix train)
**Models:** claude-opus-4-7, claude-opus-4-8, claude-opus-5, claude-fable-5
**Protocol:** identical prompt (`prompt.txt`), effort pinned `high`, isolated fresh config dirs (no memory/hooks), `--max-turns 80`, `--dangerously-skip-permissions` in disposable clones, `--output-format json` for usage capture. Ran in parallel on Kevin's Mac, subscription auth.
**Planted control:** nock 9981 — `sidecar_status()` vs `_load_digests()` (bin/_verify_cache.py:516 / :340) disagree on what a usable cache is; health reports "fresh" for a sidecar recall rejects. Known-open, present at the pin, unknown to the models.
**Grading:** one strict referee subagent per review (all four referees ran on Fable — same referee across models; note the Fable-grades-Fable caveat), instructed to re-verify every discrete claim against the pinned repo, reproduce failure scenarios where possible, and check blindly for the control.

## Scored table

| | Opus 4.7 | Opus 4.8 | Opus 5 | Fable |
|---|---|---|---|---|
| **Effort** | `high` (pinned) | `high` (pinned) | `high` (pinned) | `high` (pinned) |
| Findings | 8 | 4 | 9 | 10 |
| Claims graded | 21 | 22 | 19 | 30 |
| Confirmed | 18 | 19 | 18 | 28 |
| Inflated | 1 | 1 | 1 | 0 |
| Partial | 1 | 2 | 0 | 1 |
| Wrong | 1 | 0 | 0 | 1 |
| **Strict accuracy** | 86% | 86% | **95%** | 93% |
| **Control bug** | ✅ found | ❌ *certified area sound* | ✅ found | ✅ found |
| Self-bounded | ✅ 74 turns | ✅ 37 turns | ❌ **hit 80-turn cap** | ✅ 41 turns |
| Wall time | 19.2 min | 13.1 min | 14.9 min | 15.9 min |
| Output tokens | 71K | 126K | 64K | 66K |
| Cache-read tokens | 12.9M | 6.8M | 12.2M | **4.3M** |
| API-equiv cost | $10.94 | $9.91 | $9.84 | $11.92 |

## Unique real findings per model (referee-confirmed)

- **Fable (only):** the resurrection bug — nightly rebuild `merge_facts` is recent-wins by deterministic fact id, and re-extraction re-mints purged/edited/superseded facts as `"current"`; purges resurrect and get auto-re-signed, nothing on the recall path catches it. Rule-zero defeating. Also: `purge-fact --apply` rewrites the store on zero matches, silently destroying loader-dropped records.
- **Opus 5 (only):** the signing-key split-brain — recall verifies against `NOCKBRAIN_SIGNING_*` env keys while sign-facts/verify-facts/rebuild-store silently mint and use a default `~/.nock-brain` key; referee reproduced "recall dark, every surface green" end to end. Also: `consolidate --execute` supersedes without minting signed revocation events (violates a repo non-negotiable).
- **Opus 4.8 (only):** empirically-reproduced runtime crash chains — byte-corrupt facts.json raises `UnicodeDecodeError` through the "never raises" contract into a silent `{}` recall outage; malformed sidecar `IndexError` crashes the *whole* recall past the BM25 floor. Also the promotion `check_chain` null-digest hole.
- **Opus 4.7:** promotion-apply verify gap (UNSIGNED/PARENT_SUSPECT batches marked applied); strongest overlap coverage of the sqlite insights mis-keying and non-atomic-store-write families.
- **Shared:** purge leaves content live in `insights.json` (Opus 5 + Fable); `resolve_store` mis-keying under sqlite marker (4.7 + 5 + Fable); unguarded `nockbrain-health` `load_json` (4.7 + 4.8); non-atomic writes to the authoritative store (4.7 + 4.8 + Fable).

## Read on the results

1. **The Reddit post's accuracy spread did not reproduce.** With a precision-demanding prompt and pinned effort `high`, every model scored 86–95% (the post's best was 75%). Prompting for verification-before-claiming lifts everyone; per-claim accuracy differences at this level are small.
2. **What actually separated the models:** what they *uniquely* found, and whether they *stopped*. Opus 5 could not self-terminate (81/80 turns) — its known residency failure shape, showing up in the harness metric while its output quality was excellent. Fable produced the most verified findings, the broadest+deepest unique discovery, with the lowest context replay (4.3M vs 6.8–12.9M) and clean self-termination; the 2× token rate netted out to only ~1.2× 4.8's dollar cost on this task because of that efficiency.
3. **4.8's temperament quantified:** fewest findings, zero wrong claims, fastest, self-bounded, reproduces what it asserts — and bounded skepticism (it certified the control's area sound). Ideal operational reviewer; not the deepest auditor.
4. **On "maybe 4.7 is the answer":** not on this data. It's solid (found the control) but slowest, with two attribution slips plus one wrong claim, and its unique contribution is the smallest of the four.

## Caveats

- n=1 task, one repo, one run per model. Rhythms and unique-finding luck vary run to run.
- Referees were all Fable (consistent referee ≥ neutral referee was the tradeoff); the Fable review's grade carries a self-grading caveat. Raw REVIEW.md files sit next to this file for independent spot-checks.
- This measures bounded review work, not residency temperament — except the self-termination column, which is the one residency-relevant behavioral readout.

## Action items spawned

Real bugs found by the union of reviews, to be filed as nocks: resurrection/merge bug, signing-key split-brain, purge→insights retention, consolidate-without-revocations, sqlite insights mis-keying, recall crash chains past the BM25 floor, promotion verify gaps, non-atomic authoritative-store writes, zero-match purge destruction.
