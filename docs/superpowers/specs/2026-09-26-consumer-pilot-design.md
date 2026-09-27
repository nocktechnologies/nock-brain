# Customer pilot: setup, capture, recall and memory control

The customer bootstrap and quiet Memory Explorer are implemented. This milestone
makes a standalone customer pilot usable without Nock Harness. The user authorized
implementation through review and merge. No live fleet/customer data or active
Claude settings will be used during development.

## Experience

`consumer-brain.py` keeps explicit absolute store paths. `status` provides store
counts and next commands; `pending` lists digest-bound proposals; `discard` removes
one reviewed proposal. `open` launches the existing read-only Explorer.

`setup-hooks --store ... --transcript-root ...` creates a private settings file in
the chosen store. The printed `claude --settings FILE` command opts that session
into customer hooks. It does not rewrite global/project settings. The generated
commands use the current interpreter and absolute script paths. Moving the
checkout or interpreter requires regenerating settings. Roots are explicit,
existing directories; no home discovery or recursive import occurs.

A separate customer hook returns Claude's documented UserPromptSubmit
`hookSpecificOutput.additionalContext`. It recalls verified current customer
facts through production BM25 in private scratch storage, with optional tiers
disabled, a bounded subprocess, no inherited fleet configuration, and no writes
to the source store. Stop reads only an allowlisted transcript and saves pending
proposals. It never approves facts. Failures never block Claude and expose only
fixed safe diagnostics. The final assistant response may appear in the transcript
only at a later Stop; the hook does not synthesize unverifiable transcript rows.

## Memory control

`correct --fact ID --replacement-proposal DIGEST` creates a lifecycle proposal
using exactly one replacement candidate. `forget --fact ID` proposes erasure.
Both use the existing review/apply digest workflow. Signed cores stay immutable;
correction adds a newly signed fact, marks the old fact superseded and records a
customer-key-signed revocation. Forget physically removes the selected record
and clears content-bearing pending proposals. A minimal signed revocation retains
only identifiers/timing/action metadata. Reimport skips revoked IDs.

Proposals bind both facts and revocation generations. All multi-file lifecycle
publication must fail closed: revocation precedes retirement/removal. Interrupted
operations have an explicit recoverable state and `recover` command; no silent
partial success or destructive automatic recovery on read. Recovery is bounded,
validates captured keys and signed records/events, and never follows source paths.
Derived customer insights/caches are unsupported and must not undermine forgetting.
Original transcripts, backups, shell output and open Explorer snapshots are
outside erasure scope. Refresh/close Explorer after erasure.

## Boundaries and validation

Keep fleet installer/hook, signing formats, classifier, BM25 ordering and JSON
authority unchanged. Use standard library plus existing optional cryptography.
New customer hook closure is acknowledged and executed under Python 3.9.
Init includes a private `.gitignore` to discourage accidental store commits.
Synthetic end-to-end scenarios cover capture -> review -> apply -> recall,
correction, forgetting, replay suppression, interruption, and store isolation.
No claim of stable release or customer-tested quality until real pilot feedback.

Official contracts consulted: [Claude hooks](https://code.claude.com/docs/en/hooks)
and [CLI settings flag](https://code.claude.com/docs/en/cli-reference).
