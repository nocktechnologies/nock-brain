# Customer pilot implementation and merge plan

Base: `2ca107c` on `codex/brain-consumer-explorer` (customer bootstrap plus its
HMAC-compatible test correction). The earlier Explorer and
bootstrap milestones are already on this branch and independently reviewed.

| Work | Owner | Files / interface | Validation |
|---|---|---|---|
| Lifecycle | lifecycle implementer | `_consumer_store.py`, lifecycle tests; `propose_correction(id,digest)`, `propose_forget(id)`, `recover()`, queue/status methods; review/apply accept lifecycle digests | signed immutable cores, generation, crashes, pending erasure, anti-replay |
| Standalone hooks | hook implementer | `_consumer_hooks.py`, `consumer-hook.py`, hook tests; `setup_hooks(store, roots) -> settings_path`, bounded worker adapter | explicit roots, fail-open, strict recall, no private key for recall, no source writes, no auto-apply |
| Customer surface | root | CLI, docs, pilot tests, Python floor closure | complete synthetic customer journey + legacy regression gates |
| Review and release | independent reviewers + root | all changed contracts and branch integration | full pytest, floor, classifier, recall gate, Bandit, gitleaks, GitHub CI, merge |

Independent implementation owns disjoint files. The CLI integration waits for
those contracts. No new dependency, live hook installation, raw customer data,
fleet access, or publication of stores is needed. Implementation agents do not
merge or change shared documentation. Root owns integration and the final merge.

## Completion record

Implementation and independent review are complete. Delivery is split into
Explorer [PR #109](https://github.com/nocktechnologies/nock-brain/pull/109),
customer bootstrap [PR #110](https://github.com/nocktechnologies/nock-brain/pull/110),
and this pilot increment. The first two are merged after GitHub CI passed.
Bootstrap CI exposed an Ed25519-only malformed-key test assumption; its test now
checks a document that is invalid for both Ed25519 and HMAC.

Customer lifecycle and hook implementations received separate independent
reviews and scoped fix reviews. All reported findings were resolved:

- Replaced hardlink publication windows with atomic replacement under the
  customer lock. Recognized orphan files require explicit recovery. Real process
  exits before/after journal, revocation, fact and proposal publication, and
  during cleanup, leave usable recovery paths. Subsequent forgetting removes
  the selected plaintext from recognized store artifacts. The independent
  reviewer additionally passed 16 abrupt-process-exit cases.
- Revocation and recovery-journal size checks happen before publication.
- Correction proposals are signed and self-contained. Discarding their source
  import proposal does not orphan review. Generations include the signed ledger.
- Public-only hook recall validates key documents, file ownership/permissions,
  lifecycle events and recovery state. Unsupported SQLite/purge states refuse
  recall. Parent workers own scratch cleanup; inherited fleet configuration is
cleared, and optional cryptography is pinned from the active interpreter.
- Stop uses a top-level `systemMessage` for a human-visible review notification.
  It never returns additional context that would request another model turn.
  Existing Claude hooks still merge; the pilot guide explicitly explains this.

Local gates: full suite **962 passed**; classifier **10/10**; existing recall
gate **PASS**, **36/36** fixture identities and **196/196** valid attestations;
Bandit 1.8.6 has no findings; staged gitleaks scan has no leaks. Stock Python
3.9 tests exercise generated capture and recall commands plus the complete
customer hook import closure. Synthetic CLI tests cover separate identities,
correction, actual recall changes, forgetting and exact reimport suppression.
Crash regression drivers run in fresh subprocesses with a 10-second deadline,
avoiding raw forks from a threaded pytest process. Their focused suite passed
after that test-only adjustment. A clean Python 3.11 environment also passed
90 hook, pilot and floor tests without optional cryptography.

All development data is synthetic. No fleet data, active hook settings or
customer sources were used. No real customer pass rate or power-loss durability
claim is made. Final GitHub CI and merge are recorded in the pilot PR.
