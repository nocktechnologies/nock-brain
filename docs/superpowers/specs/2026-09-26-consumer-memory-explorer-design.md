# Consumer Memory Explorer: first milestone

Date: 2026-09-26
Status: proposed for user review; implementation has not started
Base reviewed: `d41e699` on the public repository's `main`

## Intent and product boundary

The user wants a human to open a local application, see the decisions and
facts an agent remembers, inspect their sources, and eventually correct them.
Brain should remain independently usable and be included in Command/Terminal
and school offerings. Customer distribution must carry no fleet memory or
production credentials and development must not alter the running fleet.

Keep one shared engine repository. Establish consumer release and data
boundaries rather than fork the engine into a second implementation. A
versioned consumer release is a later acceptance milestone, not a claim about
the current public branch.

Current separation is code versus data: this public repository contains the
engine; runtime data is outside the checkout and has a separate private store
repository. No separate consumer Brain repository, GitHub release, or version
tag was found in the reviewed accounts. A fresh clone does not ship the live
store, but existing commands default to the current user's `~/.nock-brain`.
Testing a fresh checkout on an operator's machine is therefore not data
isolation by itself.

## Alternatives considered

1. **Shared engine plus optional local viewer (selected).** Makes the real
   store visible independently and gives paid applications a reusable service
   boundary. Requires a small local HTTP surface.
2. **Viewer only inside Command.** Reuses its browser shell, but would require
   Command for a basic Brain task and does not establish a standalone product.
3. **Separate consumer engine fork.** Separates release cadence immediately,
   but creates duplicate signing, recall, and privacy fixes. Isolated stores
   and versioned releases provide the needed separation without that drift.

Command's MemoryEntry/Decision records and Brain's signed fact store are
distinct sources. The Explorer reads the selected Brain store; it does not
silently copy records into Command or imply its pages already show these facts.

## First implementation scope

Ship a read-only local Memory Explorer with:

- A synthetic demo that works without the harness, Command, Claude, model
  credentials, network access, or any existing memory store.
- Searchable, paginated facts and insights, filtered by kind and lifecycle
  state. Include superseded entries on explicit selection.
- Detail view with complete stored content, dates, confidence as a stored
  score, evidence references, verification state, and supersession links.
- A recall preview using the real classifier and production BM25 selection
  function, with the selected items, rendered text, approximate token cost,
  and verification/degradation notices.
- A store summary distinguishing missing, empty, unreadable, and readable
  stores, with a clearly labeled snapshot time and explicit Refresh action.

The preview is labeled **BM25 preview** with semantic and graph tiers off.
It is a replay of selection under displayed settings, not a record of what a
running agent received. Real injection history requires future instrumentation.
Show classifier eligibility separately so a user can distinguish a matching
fact from a prompt the automatic hook would skip.

No edits, approvals, supersessions, purges, transcript ingestion, scheduler
installation, hooks installation, model downloads, or settings changes happen
from this viewer. These are separate implementation slices below.

## Launch and data isolation

Follow the current script-oriented repository structure; do not add a package
framework or change the hook-reachable import closure just to host the UI.
Proposed commands:

```sh
python3 bin/explore-memory.py --demo
python3 bin/explore-memory.py --store /absolute/path/to/customer-brain
```

With no mode, print these choices and exit without reading a store. `--demo`
and `--store` are mutually exclusive. Demo constructs clearly fictitious
records and a disposable signing identity in an owner-only temporary
directory. It never falls back to the home store, scans transcript directories,
or imports existing environment key paths. Closing the server cleans up its
owned temporary files. Source fixtures and screenshots must be synthetic.

`--store` selects one directory at process start. The UI displays its identity
and never permits arbitrary filesystem paths in HTTP requests. V1 supports
the authoritative JSON store only. Refuse an explicit SQLite selection or a
SQLite cutover marker with a clear unsupported-backend message; do not present
possibly stale JSON as the active SQLite store.

All preview work runs against a bounded temporary snapshot of the selected
facts, insights, revocations and verification material. Read the selected
store's `signing-key.pub`, or an explicit `--verify-key` path; never read or
copy its private `signing-key` or inherit the developer's default identity.
Legacy HMAC verification files contain symmetric material and receive the
same owner-only scratch permissions. There is no signing or key creation in a
selected store. Verification material is never an API response. With no
verification file, show that verification is unavailable.

Existing recall helpers can write verification caches and degradation logs.
Those writes must land only in temporary scratch space. Sanitize the recall
child's NOCKBRAIN configuration so sidecars, keys, insights and backend
selection cannot resolve to another store. Use the existing classifier and
`budget-recall.select_recall`; do not implement a second ranker.

Capture file identities before and after snapshotting and retry once on drift;
if input changes again, report that a stable snapshot could not be obtained.
This does not claim a transaction across independent live writers. Report
read/parse errors visibly rather than representing them as a healthy empty
store. Browsing, refreshing and previewing must leave source bytes and mtimes
unchanged and create no files in the selected store.

## Local application and UI

Use a small stdlib Python service bound exclusively to `127.0.0.1`, an
automatically allocated port, and bundled HTML/CSS/JavaScript assets. It runs
only while the operator has launched the viewer; engine recall has no server
dependency. Print the local URL; `--open` optionally opens the browser.

The first screen has a store/demo label, health summary, search and filters,
and a memory list. Selecting a record opens a detail pane. A Recall Preview
tab accepts a prompt and displays what the selected snapshot returns. Missing
and empty stores have explicit states; the demo remains available by a new
demo launch rather than mixing records into a customer store.

Treat stored content as untrusted text. Do not render raw HTML, load remote
assets, or follow arbitrary evidence paths. Evidence anchors are visible as
text; opening original transcripts is outside v1. Use a 50-row page with a
100-row maximum, a 2,000-character preview query, a 16-KiB request-body limit,
a 32-MiB per-input-file limit and a 64-MiB total snapshot limit. Preview has a
five-second timeout. Exceeding a limit produces a visible error; it must never
silently truncate a source store and present an incomplete result as complete.

Protect the local HTTP surface with a random per-launch capability, exact
Host/Origin checks where applicable, no permissive CORS, and no-store responses.
Keep capability material out of access logs. Serve only fixed API routes and
bundled assets, not a generic directory/file server. Error messages and request
logs must not print fact contents, queries or key material. Browser responses
must exclude private-key fields and unrelated files from the selected directory.

The paid applications may later consume the same structured service contract
or shared view; embedding, authentication between applications, remote stores,
and multiple agent stores are not part of this first local milestone.

## Compatibility and independent repairs

Preserve all existing CLI defaults and fleet behavior during the Explorer
slice. Preserve signatures and use the existing verification state machine.
No changes to a deployed seat, the private data repository, runtime settings,
or signing keys are part of this work.

The direct Claude hook and harness transport are different consumers. Both
harnesses currently read `systemMessage` as their subprocess protocol; the
resident harness then inserts that text through `additionalContext`. Repair
the standalone hook in a separate slice with a compatible output/adapter
strategy and contract tests for both consumers. A blanket replacement of
`systemMessage` would break the existing harness reader.

The remaining review findings remain required work rather than disappearing
behind the new UI:

| Follow-on slice | Acceptance requirement |
|---|---|
| Standalone bootstrap | Fresh customer identity and store work without impersonating a fleet machine; known fleet identities and retired-seat mint rules remain enforced. |
| Capture and freshness | Explicit source selection, initial ingest, and opt-in refresh scheduling; visible success/failure; no silent imports during installation. |
| Extraction and evaluation | Human-authored synthetic conversations cover first-person decisions and corrections, along with negatives and stale decisions; no shared-marker quality claim. |
| Correction and forgetting | Audited lifecycle actions reuse engine signing/revocation contracts; a documented deletion/retention policy covers backups and edit history before complete-deletion claims. |
| Consumer release | Versioned code-only artifact, synthetic demos, fresh-machine install/upgrade smoke, privacy scans and verified standalone injection. |

Read-only inspection can ship for evaluation before these are complete. It
must not be marketed as a finished stable consumer distribution on that basis.

## Verification and acceptance

1. A clean demo launch reads no user memory, creates no home-store files,
   makes no network requests, and works without the harness or credentials.
2. Browsing and filtering expose expected synthetic decisions, facts,
   corrections, insights and superseded records with useful empty states.
3. For the displayed budget/configuration and identical snapshot, preview
   agrees with the production classifier and `select_recall` output.
4. Missing, corrupt, unsigned, tampered and revoked synthetic inputs are
   displayed accurately; tampered/revoked items are not promoted into preview
   as valid memories. No key and a corrupt key are distinguishable.
5. Sentinel tests prove the viewer neither reads an unrelated home store nor
   changes selected-store content, mtimes, caches or logs.
6. Host/origin/capability, path traversal, HTML injection, request bounds and
   preview timeout tests cover the actual HTTP boundary.
7. Browser verification covers list/detail navigation, keyboard access,
   filtering, preview, refresh and narrow screens using synthetic records.
8. Existing unit tests, classifier smoke, recall gate and Python-floor checks
   pass. Update `docs/REPO-MAP.md` with each implemented contract change.

## Discovery evidence and limits

Checked on 2026-09-26 against `d41e699`: GitHub reports the engine repository
public and the fleet store repository private. No engine tags/releases were
returned. The current tracked tree contains no root production facts file.
All 196 recall-fixture contents have the synthetic record form.

Gitleaks 8.30.1 found no findings under the repository's current policy.
A second scan without the broad tests/docs exclusions found four matches:
two copies of the documented disposable fixture HMAC key and two occurrences
of the intentionally fake Stripe token in a redaction test. These were
reviewed in context. This is a current tracked-tree check, not a complete
historical or semantic privacy audit. Public code still contains fleet naming
and operational references; customer-neutral configuration is separate work.

## Review handoff

The proposed first delivery is this read-only, demo-safe Explorer in the
existing public engine repository. After design review, write the executable
implementation plan, then build and verify in the isolated worktree. Later
slices introduce customer bootstrap and management actions without duplicating
the engine or exposing fleet data.
