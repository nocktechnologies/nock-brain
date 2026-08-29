# Skeptical review of the #47–#93 fix train

## Critical — post-cutover purge leaves the supposedly purged fact in live recall

`purge-fact.py --apply` always reads and rewrites the JSON file (`bin/purge-fact.py:151`, `bin/purge-fact.py:174`) and deletes the verification-cache sidecar associated with that JSON path (`bin/purge-fact.py:167`, `bin/purge-fact.py:179`).  It never resolves the selected store backend or deletes from `brain.db`.  Recall, however, selects SQLite whenever `NOCKBRAIN_STORE=sqlite` (`bin/_storeback.py:316`) or the `store-v2` marker and database are present (`bin/_storeback.py:318`), and `budget-recall` then reads from that selected store (`bin/budget-recall.py:673`, `bin/budget-recall.py:677`).

Failure scenario: after SQLite cutover, an operator runs the documented hard-delete command for a sensitive fact.  The command reports success, removes it from `facts.json`, and drops the JSON verification-cache sidecar, but the row remains in `brain.db` and continues to be injected by recall.  This is both a failed deletion and a wrong-result recall failure.

Verified by creating the same recallable fact in `facts.json` and a selected `brain.db`, running `purge-fact.py --apply`, then loading through `budget-recall`: JSON was `[]`, while SQLite recall still returned `remove-me`.  The existing purge tests cover the JSON path only; they do not select SQLite.

## High — a normal rebuild never updates the database selected by recall

The rebuild pipeline treats only `facts.json` as the live source: it merges from that file (`bin/rebuild-store.py:552`), promotes only the artifacts listed at `bin/rebuild-store.py:60`, and its promotion map contains no `brain.db` (`bin/rebuild-store.py:379`).  After promotion, its derived-artifact refreshes also explicitly consume `facts.json` (`bin/rebuild-store.py:594`; see the semantic invocation at `bin/rebuild-store.py:450`).  In contrast, the selector makes `brain.db` authoritative after cutover (`bin/_storeback.py:316`, `bin/_storeback.py:318`) and recall loads the selected backend (`bin/budget-recall.py:673`, `bin/budget-recall.py:677`).

Failure scenario: once `NOCKBRAIN_STORE=sqlite` or `store-v2` is enabled, the nightly rebuild can successfully sign and promote new/updated JSON facts, yet every recall continues reading the old database.  New facts are silently missing and superseded or corrected facts can remain current until an operator separately runs the migrator.  The rebuild summary reports promotion success, so it provides no indication that the recall source was untouched.

Verified by tracing the only promotion list and backend-selection/load path above.  `tests/test_rebuild_store.py` exercises JSON promotion and derived refreshes, but contains no SQLite-selected rebuild case.

## High — signed v2 claim validity windows are never enforced by recall

The v2 authority contract validates and signs `valid_from` and `valid_to` (`bin/_sign.py:307`, `bin/_sign.py:309`, `bin/_sign.py:332`, `bin/_sign.py:333`).  Default recall instead applies the older bi-temporal gate (`bin/budget-recall.py:427`, `bin/budget-recall.py:434`), whose implementation reads only `valid_at` and `invalid_at` (`bin/_facts.py:142`, `bin/_facts.py:143`).  It has no v2-window branch.

Failure scenario: a correctly signed v2 decision whose `valid_to` elapsed yesterday, or whose `valid_from` is tomorrow, has neither legacy field.  It is treated as open-ended and can be injected as a current fact.  This produces stale or not-yet-effective authority with no warning and does not require tampering.

Verified with a real v2 signature and an expired `valid_to`: `verify_fact` returned `valid`, and `budget_recall.search(..., now=after_valid_to)` returned the fact.  The v2 contract tests verify that the fields are signed, while the bi-temporal tests cover only the legacy field names.

## High — an attacker can silently suppress a valid v2 authority fact by changing `status`

The v2 payload intentionally omits `status` and says authority must instead be retired by a separately signed revocation (`bin/_sign.py:274`, `bin/_sign.py:275`); the returned signed payload likewise has no `status` member (`bin/_sign.py:320`).  Verification recomputes only that payload before accepting the signature (`bin/_sign.py:936`, `bin/_sign.py:939`, `bin/_sign.py:946`).  But recall independently drops every fact whose mutable `status` is `superseded` (`bin/budget-recall.py:427`, `bin/budget-recall.py:428`).

Failure scenario: a process that can alter the store changes a valid v2 authority fact from `current` to `superseded`, without changing its attestation.  Verification still returns `valid`, so the tamper filter emits no diagnostic; then default recall silently omits the fact.  This bypasses the stated signed-revocation rule and is precisely a missing-fact failure in the signing/recall boundary.

Verified with a real signed v2 fact: after only `status = "superseded"`, `verify_fact` returned `valid` and `budget_recall.search` returned no match.  This behavior also follows directly from the two cited branches.

## High — some SQLite open failures escape the degradation path and become an untracked empty hook result

`SqliteStore.load_facts` records a missing database (`bin/_storeback.py:244`, `bin/_storeback.py:245`) and catches query-time `sqlite3.Error` (`bin/_storeback.py:249`, `bin/_storeback.py:255`, `bin/_storeback.py:258`).  It calls `_connect()` before that `try` (`bin/_storeback.py:248`), however, and `_connect()` can throw from either `sqlite3.connect` or the `PRAGMA` (`bin/_storeback.py:193`, `bin/_storeback.py:196`).  The hook does not check the recall command's exit status: it captures stdout (`hooks/memory-inject.sh:86`) and returns `{}` when it is empty (`hooks/memory-inject.sh:87`, `hooks/memory-inject.sh:88`); `set -e` is not enabled (`hooks/memory-inject.sh:11`).

Failure scenario: a selected `brain.db` is replaced by a directory, becomes inaccessible, or otherwise makes `sqlite3.connect` fail.  The exception bypasses `_record_degradation`, the command produces no stdout, and the hook silently injects no recall.  The stack trace is only appended to the private hook log; the health degradation counter remains clean.

Verified by making the selected `brain.db` path a directory.  `SqliteStore.load_facts()` raised `sqlite3.OperationalError: unable to open database file`, and no `recall-degradations.jsonl` was created.  Focused relevant tests passed (`125` claim/cache/store/purge tests and `49` rebuild/health/bitemporal tests), confirming this is an untested error boundary rather than a failing baseline test.

Overall verdict: the fix train improves individual JSON-path cache and signing cases, but its cross-layer contracts are not coherent enough for a cutover.  In particular, the selected SQLite store diverges from rebuild and purge writers, and v2 authority semantics diverge from the recall filter; both yield silent wrong or missing memory.  I would block production SQLite/v2-authority rollout until the authoritative-store operations and recall validity/revocation rules share one enforced contract, and until every SQLite read failure is durably surfaced.
