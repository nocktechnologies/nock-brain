# Customer store and reviewed imports

Kevin approved continuing after the read-only Explorer milestone. This next
increment supplies a fresh customer store and explicit, reviewed ingestion.
It remains in the shared engine repository and does not install or alter the
fleet, harness, hooks, settings, or any existing home-directory store.

## Customer flow

`consumer-brain.py init --store /new/absolute/path` creates an empty JSON store
and a new local UUID and signing identity. The destination must not exist;
an existing directory, including an empty one, is never adopted. Its parent
must already exist. All customer directories are 0700 and files 0600.

`consumer-brain.py propose --store PATH --format markdown|claude-jsonl
--source FILE [--source FILE ...]` reads only explicitly selected regular
files and writes a bounded proposal. Markdown means customer-curated bullet
notes; JSONL preserves conversation roles and the existing privacy fences.
The output identifies a full SHA-256 proposal digest. `review --store PATH
--proposal DIGEST` shows the complete candidates and provenance as escaped
JSON, without terminal control sequences. `apply --store PATH --proposal
DIGEST` is the explicit acceptance action; there is no automatic approval.

Proposals bind the customer identity, signing key, and current authoritative
facts generation. A changed proposal or stale store is rejected. Apply signs
only new facts using `_sign.sign_facts`, verifies before atomic publication,
and retains prior records and signatures. Repeating an identical import does
not duplicate facts. No raw transcript, tool payload, model call, automatic
source discovery, telemetry, scheduler, or shared private memory is needed.

## Boundaries

- An explicit customer manifest distinguishes these stores from fleet data.
  No customer may impersonate a registered fleet machine; `KNOWN_MACHINES`
  and all existing extraction defaults remain unchanged.
- Reuse the existing classification, authority, secret-scrubbing and JSONL
  privacy functions. Customer fact construction may differ where the fleet
  constructor assumes a registered machine or has prefix-truncated IDs.
- Customer identity and source snapshot hashes belong in signed evidence.
  IDs cover the complete sanitized content and kind plus customer UUID.
  Classification is heuristic, not comprehensive conversation understanding.
- Customer writers share one lock and compare the captured generation just
  before replacement. There is no transaction with unrelated legacy writers;
  mixing legacy writers into this store is unsupported. Nonempty lifecycle
  sidecars and SQLite cutover are refused rather than risking resurrection.
- Corrupt, unsigned, malformed, or foreign existing facts fail closed. Only
  the customer key and explicitly selected store are used. Environment
  signing paths, fleet identity, home data, and optional tiers have no effect.
- Inputs and artifacts have fixed size/count limits; reject symlinks, special
  files, hard-linked internal files, malformed JSON and non-finite numbers.
  Source drift is detected during capture. Errors do not echo source text or
  key material. Failed initialization may leave an incomplete private
  directory; it is not a usable customer store and must not be adopted.
- Secret scrubbing is a pattern-based filter, not a guarantee that notes have
  no sensitive information. Review remains necessary. HMAC fallback stores
  shared verification secret material in the `.pub` file; both key files are
  private. No public-key-security claim is made for that fallback.

The Explorer remains read-only and can inspect this store with `--store`.
Corrections and forgetting, a packaged launcher, standalone hook setup, and
real customer extraction/recall evaluation remain subsequent milestones.
