# Skeptical review of the #47–#93 fix train

## 1. Purge reports success while the authoritative SQLite fact remains recallable

**Severity: critical**

**Citations:** `bin/purge-fact.py:51`, `bin/purge-fact.py:151`, `bin/purge-fact.py:174`, `bin/_storeback.py:318`, `bin/budget-recall.py:674`

**Concrete failure scenario:** After SQLite cutover (`store-v2` plus `brain.db`), `purge-fact.py --apply` still reads and rewrites `facts.json` directly. Recall, however, resolves the same path's directory to `brain.db`. The command can therefore print that a sensitive fact was removed, delete its JSON/cache/vector artifacts, and leave the authoritative database row untouched; the supposedly purged content continues to be injected by recall. This defeats the command's hard-delete/privacy contract, not merely a cache optimization.

**How verified:** I created a temporary cut-over store with the same `delete-me` fact in JSON and SQLite, ran the real purge CLI with `--apply`, and then called the production recall path. The CLI exited 0 and JSON became `[]`, while `SqliteStore.load_facts()` still returned `delete-me` and `budget_recall()` still returned its content.

## 2. Rebuild, signing, and consolidation can mutate a shadow JSON store after SQLite cutover

**Severity: high**

**Citations:** `bin/rebuild-store.py:60`, `bin/rebuild-store.py:552`, `bin/rebuild-store.py:593`, `bin/sign-facts.py:55`, `bin/sign-facts.py:67`, `bin/consolidate-facts.py:272`, `bin/consolidate-facts.py:377`, `bin/_storeback.py:318`

**Concrete failure scenario:** The backend contract makes `brain.db` authoritative once the marker exists, but the rebuild promotion list contains only `facts.json` and derived artifacts; `sign-facts.py` and `consolidate-facts.py` likewise bypass `resolve_store` and operate directly on JSON. A nightly rebuild can successfully promote new, signed JSON and refresh derived views while live recall keeps serving the old database. Manual bulk signing can report success while authoritative DB rows remain unsigned, and consolidation can report supersessions that have no effect on recall. None of these commands surfaces that it modified the non-authoritative copy.

**How verified:** In a temporary marked store I put fact `old` in `brain.db`, staged fact `new`, and invoked the real `promote()` implementation. It reported `facts.json` promoted and JSON contained only `new`; `resolve_store(...).describe()` still selected SQLite and its recall input contained only `old`. I also checked the complete call paths: none of the cited signing or consolidation reads/writes goes through the backend contract.

## 3. Loading `insights.json` in SQLite mode reloads all facts and duplicates recall results

**Severity: high**

**Citations:** `bin/_storeback.py:311`, `bin/_storeback.py:312`, `bin/_storeback.py:318`, `bin/budget-recall.py:674`, `bin/budget-recall.py:865`, `bin/budget-recall.py:884`

**Concrete failure scenario:** `resolve_store()` selects a backend solely from the requested path's parent directory. Consequently `_load(insights.json)` in a marked directory resolves to the same `brain.db` as `_load(facts.json)`, instead of reading `insights.json`. Every matching fact is then prepended as an "insight" and appended again as a fact. The duplicate rows consume the token budget, push legitimate tail facts out, inflate the match count, and suppress the actual insights file without any error.

**How verified:** I built a marked temporary SQLite store containing facts `a` and `b` and a real `insights.json` containing insight `ins`. `select_recall("sqlite pricing", ..., insights_file=...)` returned result IDs `['a', 'b', 'a', 'b']`; `ins` never appeared. The full suite does not cover this composition because backend parity calls `search()` on already-loaded lists rather than the production `_load()`/insights path (`bin/eval-store-parity.py:122`).

## 4. Recall ignores the validity and revocation authority that v2 signatures protect

**Severity: high**

**Citations:** `bin/_sign.py:307`, `bin/_sign.py:309`, `bin/_sign.py:337`, `bin/_facts.py:142`, `bin/_facts.py:143`, `bin/budget-recall.py:428`, `bin/budget-recall.py:434`

**Concrete failure scenario:** Claim-attestation v2 signs `valid_from`, `valid_to`, and `revokes_revision_ids`, but the recall gate checks only the older, differently named `valid_at`/`invalid_at` fields plus mutable `status`. An expired or not-yet-valid v2 claim with `status: current` is therefore injected even though its signed authority window excludes the current time. Likewise, a signed v2 revision's `revokes_revision_ids` has no production consumer outside signing/verification, so the revoked revision is not retired by recall. These are valid signatures producing authorization-incorrect recall, with no warning.

**How verified:** I created a fully valid HMAC-signed v2 claim whose `valid_to` was `2026-08-02T00:00:00.000000Z`, then recalled at 2026-08-28. `verify_fact()` returned `valid`, and the production `budget_recall()` returned the expired claim. A repository-wide reference check found production reads of `revokes_revision_ids` only in `_sign.py`; the recall validity helper reads only `valid_at` and `invalid_at`.

## 5. Signed legacy revocations are audited offline but not enforced on the recall hot path

**Severity: high**

**Citations:** `bin/_revoke.py:143`, `bin/_revoke.py:165`, `bin/verify-facts.py:92`, `bin/verify-facts.py:106`, `bin/budget-recall.py:635`, `bin/budget-recall.py:674`, `bin/budget-recall.py:691`

**Concrete failure scenario:** The signed revocation sidecar was introduced specifically to detect a superseded fact whose unsigned `status` is flipped back to `current`. That cross-check is wired only into `verify-facts.py`. Live recall verifies each fact envelope but never loads or audits `revocations.jsonl`, so a resurrected, still-validly-signed legacy fact is injected as current. The revocation subsystem correctly detects the attack offline, but the production surface it is meant to protect does not consult it and emits no revocation error.

**How verified:** I signed a legacy fact, signed a revocation event for its ID, left/flipped the fact's mutable status to `current`, and used the actual modules. `_revoke.audit()` returned `resurrected == ['revoked-old']`, while `budget_recall()` returned the revoked fact's content.

## 6. `--strict-verify` fails open when the verification key is missing or unreadable

**Severity: high**

**Citations:** `bin/budget-recall.py:598`, `bin/budget-recall.py:616`, `bin/budget-recall.py:681`, `bin/budget-recall.py:837`, `bin/budget-recall.py:976`, `hooks/memory-inject.sh:86`

**Concrete failure scenario:** The CLI describes strict mode as fail-closed, but key load failure returns `None`; `_load()` then passes every fact through without verification. `select_recall()` only writes a warning. Thus deleting, corrupting, or misconfiguring the key disables strict verification and allows unsigned or forged facts into recall. In the production hook stderr is redirected to `hook-errors.log`, so the user receives the unverified memory injection rather than the warning.

**How verified:** I pointed both signing-key environment variables at nonexistent paths and invoked `budget_recall(..., strict_verify=True)` over an unsigned fact. The function warned that verification was skipped and returned the unsigned content. This behavior is also explicitly pinned by `tests/test_verify_on_recall.py:155`, so it is not an incidental exception path.

## 7. Consolidation creates supersessions that the signed-revocation audit considers unattested

**Severity: medium**

**Citations:** `bin/consolidate-facts.py:223`, `bin/consolidate-facts.py:225`, `bin/consolidate-facts.py:227`, `bin/consolidate-facts.py:377`, `bin/_revoke.py:225`, `bin/_revoke.py:242`, `bin/_revoke.py:254`

**Concrete failure scenario:** Consolidation directly changes lifecycle fields and writes the store but never calls `record_supersessions()`. Re-running `sign-facts.py` as instructed does not repair this because legacy fact signatures intentionally exclude status/lifecycle metadata. A consolidated loser with no pre-existing event therefore fails `verify-facts.py --strict-revocations`, and no signed event exists to detect its later resurrection. This leaves consolidation outside the revocation invariant implemented for `supersede-fact` and dedup.

**How verified:** I signed two near-duplicate facts, ran the real consolidation dry-run and gated execute, and audited the resulting store with an empty `revocations.jsonl`. The loser was `status: superseded`, both fact signatures remained valid, and `_revoke.audit()` returned that loser's ID in `unattested_superseded`.

## 8. The claimed concurrent cache-save convergence still has a last-writer-wins race

**Severity: low**

**Citations:** `bin/_verify_cache.py:55`, `bin/_verify_cache.py:271`, `bin/_verify_cache.py:272`, `bin/_verify_cache.py:288`, `bin/_store.py:61`, `bin/_store.py:63`

**Concrete failure scenario:** `save()` reads peer digests before entering the atomic writer, but there is no lock or compare-and-swap on the sidecar itself. Two same-stamp writers can both read the old sidecar, each construct a different union, and then atomically replace one another; the later replace loses the earlier writer's digests. This does not forge a verification result, but it invalidates #90's convergence guarantee and causes avoidable full signature verification on later recalls when writers have different live sets (for example, one exits after a partial verification failure).

**How verified:** I created two cache handles for the same unchanged store, added digest `a` to one and `b` to the other, and used a thread barrier at `secure_write_json_atomic()` so both peer reads completed before either replace. The final valid sidecar contained only `['a']` (the winner is scheduling-dependent), proving one completed writer's digest was lost. The existing concurrency test is sequential (`tests/test_verify_cache.py:595` then `tests/test_verify_cache.py:601`), so it cannot exercise this interleaving.

## Overall verdict

The fix train contains substantial useful hardening, and all 684 current tests pass, but it is not safe to treat the layers as closed: SQLite cutover splits authoritative reads from several critical writers and even aliases insights to facts; both generations of signed revocation/validity authority stop short of live recall; strict verification is not actually strict; and consolidation and cache concurrency still sit outside their advertised invariants. The most dangerous pattern is successful-looking maintenance that changes only shadow or derived state while production recall silently continues serving stale, duplicated, expired, revoked, or supposedly deleted facts.
