# Customer bootstrap implementation plan

**Spec:** `docs/superpowers/specs/2026-09-26-consumer-bootstrap-design.md`

**Goal:** Fresh customer identity, explicit source selection, complete proposal
review, and verified additive publication, usable without the fleet.

**Execution:** subagent-driven-development in the existing isolated
`codex/brain-consumer-explorer` branch. Use mid-tier implementation/review
agents and the strongest final reviewer. No deployments or real data.

## Shared contract

`bin/_consumer_store.py`: `ConsumerError(ValueError)`, `init_store(path: Path)
-> dict`, `ConsumerStore(path)` context manager that owns the writer lock.
Attributes `path`, `manifest`, `facts`, `generation`; methods
`save_proposal(candidates, sources, stats) -> str`,
`read_proposal(digest) -> dict`, `apply_proposal(digest) -> dict`.
`read_regular(path, limit) -> bytes` is a bounded no-follow drift-checked
reader. `canonical_bytes(value) -> bytes` is strict, deterministic ASCII JSON.

Manifest `customer.json`: schema `nockbrain-customer/v1`, `store_id` (UUID),
`key_id`, `algorithm`, `created_at`. Proposal schema
`nockbrain-customer-proposal/v1`: store_id, key_id, generation, candidates,
sources, stats. Filename `proposals/<sha256-of-exact-bytes>.json`.
No automatic time in the proposal envelope so identical inputs are stable.
Limit source files to 16, 8 MiB each, 32 MiB combined; candidate content 1500
characters, at most 1000 candidates; facts/proposal files at most 32 MiB;
manifest/key files at most 16 KiB. Exceeding a source/proposal limit fails
visibly; overlong candidate messages are skipped with a count, never silently
truncated into changed meaning. UTC import date may be explicit internally
for tests; stable candidate timestamps derive from a valid source timestamp
or file mtime, not wall-clock proposal time.

Customer facts: ordinary v1 records with complete-content hash ID prefixed
`customer-`, kind, content, confidence, scope=global, status=current,
source_file, source_date, created_at, subject, source=`customer:<store_id>`.
Explicit source avoids the legacy engine's default agent owner. No `machine` or reserved v2
fields. Signed `evidence` entries include customer `store_id`, `key_id`,
source `sha256`, sanitized path, line, and source-based event identity.
ID = `customer-` + SHA256(canonical_bytes([store_id, kind, content])).
Duplicate new facts may combine evidence within one proposal; existing facts
are skipped by ID without changing their evidence/attestations.

`bin/_consumer_import.py`: `collect_candidates(sources: list[Path],
format: str, manifest: dict) -> tuple[candidates, source_receipts, stats]`.
Consumes store helpers and returns JSON-compatible values. Does not write.
Sources receipts contain sanitized path, raw-file SHA256, format. Stats
include files, candidates, overlong_skipped, plus existing JSONL privacy
counters as useful. No raw text or tool payload persisted.

## Review focus

No default home access; existing destination never adopted; incomplete init
never treated as ready; signing-key mismatch/corruption fails closed; stale
proposals and atomic failure preserve authoritative data; existing signatures
stay unchanged; denylisted/special/drifting input fails; source secrets and
terminal escapes are not exposed in metadata or diagnostics; customer IDs
cannot bypass the fleet mint gate; no claims of comprehensive extraction.

### Task 1: Customer identity, proposals and verified atomic publication

**Read first:** the spec above and Shared contract in this plan (the task
brief includes this pointer). Read CLAUDE.md and REPO-MAP before code.
**Own only:** `bin/_consumer_store.py`, `tests/test_consumer_store.py`.

- [ ] Implement the exact shared API. Explicit absolute paths only, no defaults
  or environment store/key resolution. Init uses exclusive directory creation,
  creates empty `facts.json`, new explicit-path keypair and proposals directory,
  and writes the manifest last. Refuse all existing targets (including dangling
  symlinks) before writing. Parent must exist. Never clean unknown paths.
- [ ] Opening requires a valid canonical UUID manifest and matching local
  signing and verification keys. Refuse a symlink store/owned files, special
  files, hard-linked owned files, unsafe group/world permissions, SQLite marker,
  and nonempty revocations/tombstones. Do not recurse into arbitrary files.
  Bounded regular reads check descriptor identity/stat before and after. Use a
  persistent 0600 flock lock; do not unlink it. Hold it until context exit.
- [ ] Load existing facts strictly; reject duplicates, malformed/unsigned/
  tampered/foreign facts. Verify existing signatures, key identity, and signed
  evidence store ownership. Do not repair or re-sign previous records.
- [ ] Save immutable digest-addressed proposals, validate schema and candidate
  identity/content/evidence/limits with allowlisted fields. Source text is
  untrusted data. Reject NaN/Infinity and malformed shapes. Bind proposal to
  key/store/current facts generation; never accept an arbitrary proposal path.
- [ ] Apply refuses changed digest or stale generation; skip already-existing
  candidate IDs, sign new facts only through `_sign.sign_facts`, verify the
  complete merged list, then atomic 0600 replacement after rechecking captured
  facts/key/manifest/lifecycle marker state. Failure must retain facts bytes.
  Return `{added, skipped, total}`. Applying an already consumed proposal may
  return a stale-proposal error; repeated propose/apply must be idempotent.
- [ ] Test fresh distinct stores, existing/symlink/incomplete targets, bad
  key/facts, environment/home isolation, proposal tampering/stale base,
  duplicate IDs, unchanged old attestations, publication failure and special
  files, signature verification, limits. Synthetic inputs only. Run focused
  tests and all-bin Python floor; self-review and commit only owned files.

### Task 2: Explicit Markdown and JSONL candidate extraction

**Read first:** spec and Shared contract above; CLAUDE.md and REPO-MAP.
**Own only:** `bin/_consumer_import.py`, `tests/test_consumer_import.py`.

- [ ] Implement `collect_candidates` exactly, using Task 1 helpers. Source
  selection is explicit absolute files only, no glob/directory traversal.
  Reject duplicates, symlinks/special files, denylisted source paths (reuse
  ingest-jsonl default denylist), signing-key filenames, `.ssh` and `.gnupg`
  components. Capture bounded immutable bytes with total bound and drift
  check; source bytes/modes/mtimes must stay unchanged.
- [ ] Load existing hyphen modules by explicit sibling file path. Reuse
  `classify_bullet`, `authority_fact_allowed`, `extract_metadata` where needed,
  `_scrub.scrub_secrets/is_structural_noise`, and JSONL `line_events`. Do not
  invoke their CLIs, default path discovery, or `machine_tag`. Do not modify
  fleet extraction APIs. Use a small customer constructor from shared schema.
- [ ] Markdown is explicitly customer-curated notes: only `- ` bullets,
  actor=user, scrub first then structural/classification/authority filters.
  JSONL parses each nonempty line strictly to an object, preserves roles,
  denies sidechains and paired private tool results through `line_events`,
  and NEVER mints facts from tool surfaces. Fail malformed JSON/root/parts
  safely rather than silently importing a partial file. Unknown valid event
  types may be skipped. Avoid unsafe coercion of malformed content to prose.
- [ ] Full sanitized content IDs, signed source hash/customer receipts as in
  Shared contract. Whitelist and scrub path/session metadata (prefer deriving
  event IDs from source hash + line + part index, omit unneeded raw metadata).
  Source dates validate ISO date, else use UTC file mtime. Keep role for
  authority checks; assistant cannot mint decision/directive authority.
  Overlong messages counted/skipped; bounded candidates, dedup exact full
  content/kind only, retain evidence within batch, preserve repeatability.
- [ ] Test tagged and inferred notes, customer first-person decisions,
  assistant authority excluded, tool/sidechain/private payload exclusion,
  scrubbing content and metadata, malformed JSONL/shapes/non-finite numbers,
  source limits/denylists/drift/special files, complete-content ID distinctions
  past 200 chars, duplicate evidence and stable repeated extraction. Test with
  absent/hostile fleet identity and signing env. No real source data.
- [ ] Run focused tests, self-review and commit only owned files.

### Task 3: Customer CLI, guide and end-to-end verification

**Read first:** spec and Shared contract above; CLAUDE.md and REPO-MAP.
**Own only:** `bin/consumer-brain.py`, `tests/test_consumer_cli.py`,
`docs/customer-setup.md`, `docs/REPO-MAP.md`, `README.md`,
`docs/memory-explorer.md`, `examples/customer-notes.md`.

- [ ] CLI `main(argv=None)->int`: subcommands init/propose/review/apply as
  specified. Every command requires `--store`; propose requires `--format`
  and one or more repeated `--source`. Review/apply require a full digest.
  Invoking with no subcommand prints help and
  returns zero before filesystem/store access. Parse errors return nonzero.
  All actual operations consume the shared APIs with no fallback/defaults.
- [ ] Init reports new identity and next steps without key material. Propose
  prints count/digest and exact review command; review emits complete escaped
  ASCII JSON with stats/provenance and explains apply command. Apply reports
  added/skipped/total. Paths and source content cannot inject ANSI/terminal
  controls or shell commands into output. Use JSON output for untrusted values
  and shlex.quote for rendered command args. Errors are concise, safe, nonzero.
  Suppress Python bytecode writing before sibling imports. No browser writes.
- [ ] Add a small synthetic notes example and documented walkthrough: fresh
  destination, selected sources only, review entire proposal, apply digest,
  Explorer launch. Explain algorithm/fallback, private key backup, limits,
  stale re-proposal, heuristic extraction/overlong skips, duplicates/evidence,
  unsupported concurrent legacy writers and correction semantics. Label as
  an evaluation milestone, not a ready customer release. Document REPO-MAP
  module/API contracts and unchanged fleet/Explorer boundaries.
- [ ] Test subprocess CLI no-mode and bad args without home data touched,
  full init/propose/review/apply/Explorer synthetic flow under hostile env,
  no fact writes before apply, safe escaping, key mismatch and stale proposal
  failures, idempotent second import, and source sentinels unchanged.
- [ ] Run focused tests, self-review and commit only owned files. Controller
  runs full pytest, classifier/recall gates, stock 3.9 floor, no-dependency
  synthetic smoke, Bandit/gitleaks, independent final review, and records
  verified results in this plan. Do not merge/push/deploy.
