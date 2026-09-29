# nock-brain Repo Map

Read this file first. It exists so a session can operate on this repo **without
re-reading the whole codebase**. It documents what each file owns, how data
flows, the integrity model, and the sharp edges that have already cut someone.

Maintenance rule: when a PR changes a module's contract (not its internals),
update the relevant section here in the same PR. Function names are used
instead of line numbers on purpose — names rot slower.

Last full regeneration: 2026-08-23 (post-#83, commit `10a85ec`).
Contract updates 2026-08-28: shared key resolver, recall revocation audit,
v2 `source_date`/`valid_from`/`valid_to`, purge tombstones, `resolve_store`
basename, `--strict-verify` fail-closed.
Contract updates 2026-08-29: `load_facts` catches `ValueError` (non-UTF-8);
JSON-path degradations; sidecar shape guards; dense `fuse` crash → BM25;
health reports unreadable `facts.json` instead of crashing; `secure_write_json`
is atomic (N10027); promotion apply is stage-verify-commit with `--strict`
and a state-anchored chain (N10026).
Contract updates 2026-08-31: `KNOWN_MACHINES` is a **mint-only** gate and the
retired `fleet-02` seat no longer mints (§5, §8, §9).
Contract updates 2026-09-05: synthesis publication always signs/verifies and
fails closed without replacing the previous output; signed input/coverage
lineage separates sampled text from event recurrence. Each distinct event ID
retains its supporting fact IDs; input and confidence representatives stay
unique by fact ID. Publication uses an output-scoped lock and stale-generation
comparison, without claiming a transaction with independent fact writers.
Recall suppression is limited to fully included, verified, verbatim coverage (§5–6).
Contract updates 2026-09-26: optional read-only Memory Explorer snapshots an
explicit JSON store or synthetic demo, exposes a capability-protected loopback
API, and runs production BM25 preview in isolated scratch space (§12).
Contract updates 2026-09-26: explicit customer identity, selected-file import,
digest-bound proposal review and additive publication are isolated from fleet
and Explorer writes (§13).
Contract updates 2026-09-27: JSONL ingest unwraps human text from
resident-channel receipts before event creation only when the BEGIN/END frame
marker IDs match and the channel closes with no trailing text; malformed
wrappers and resident-engine prompts cannot mint facts. The operator runbook purges historical wrapper
facts and documents an uninstalled daily curated-memory timer. `purge-fact.py`
`--content-prefix` matches only lstripped fact content, never fact ids or
mid-sentence mentions; it also removes matching decoded JSONL events and
complete channel blocks from session notes and vault files. The wrapper-purge runbook requires a quiesced
fact-store, a timestamped backup, and post-synthesis verification because the
purge replacement is not safe with concurrent writers.
Contract updates 2026-09-27: purge removes a session-note fact only when its
complete rendered bullet exactly matches a removed fact (including its anchor
and truncation), preserving transcript history and near-matches. Review and
vault outputs are regenerated from the cleaned sources.
Contract updates 2026-09-28: JSONL ingest promotes a transcriber result only
when its `--telegram-file-id` argument matches `message.voice.file_id` from a
preceding resident Telegram envelope in the same session, and its numeric
sender ID equals configured `ALLOWED_USER`. Missing/invalid allowlist, NockCC,
other senders, unrelated audio, forged script/interpreter paths, and ambiguous
commands leave the result as `tool_result`. The invocation must be a single
Bash command using the resolved `$MIRA_HOME/scripts/transcribe.py` path
(`$HOME/Dev/mira-home` by default) and bare `python`/`python3` or a Python
executable directly under `/usr/bin`, `/usr/local/bin`, or
`$MIRA_HOME/.venv/bin`.
The producer change is [mira-home #78](https://github.com/kkwills13/mira-home/pull/78),
commit `ed5a5212808ffd13ef4a49c52fad6d2aa01079fc`: its transcriber accepts
`--telegram-file-id`, performs getFile/download internally, and its
`capabilities/wired-tools.md` specifies the absolute-path canonical command.
Its offline downloader tests exercise that argument through transcription;
[CI passed](https://github.com/kkwills13/mira-home/actions/runs/36451638839).
Rollout order: deploy that producer and documented command, configure
`ALLOWED_USER` in the nightly ingest environment, then deploy this consumer.
The two PRs alone do not change the running residence or host environment.
Contract updates 2026-09-28: resident-channel names are casefolded before
routing. Only plain user turns and Telegram frames use actor `user`; NockCC
frames namespace every string sender as `nockcc:<sender>` and missing,
blank, or non-string sender values as `nockcc:unknown`. Other non-engine
channels use `<channel>:unknown`. No NockCC sender can collide with the
first-party actors used by authority extraction.
Contract updates 2026-09-28 (N10935): per-prompt recall's insight tier now
excludes every heuristic-synthesized insight (`synthesized_by ==
"heuristic"`) outright, unconditionally on every call, not only when
semantic — see `bin/budget-recall.py`'s `_exclude_heuristic_synthesis`.
Heuristic insights stay in the store, available to any other reader; only
per-prompt recall excludes them. LLM-enriched insights and legacy insights
without the field are unaffected.

---

## 1. What this is

A memory layer for Claude Code agents: distills session transcripts into
signed, auditable *facts*, consolidates them into *insights*, and auto-injects
the relevant ones into future sessions within a token budget. Pure-stdlib
Python in `bin/` (no package structure — sibling imports via `sys.path`
insert; hyphen-named scripts loaded via `importlib`). One shell hook. One
installer. ~29k lines total.

```
bin/        50 scripts: 12 shared _*.py modules + 38 CLIs
hooks/      memory-inject.sh — the Claude Code UserPromptSubmit hook
tests/      42 files, ~507 tests — several are explicit contracts (see §9)
docs/       specs, ADR, audit, eval suites, tracking
install.sh  one-command setup (+ optional --semantic tier)
```

Two version words that mean different things — do not conflate:
- **"v2 pipeline"** = the Conversation Memory Compiler (raw JSONL → evidence
  events → facts), per ADR-001. A pipeline generation, not a schema change.
- **"v2 attestation"** = the claim-authority signing contract in `_sign.py`
  that coexists with legacy v1 envelopes in the same store. See §7.

---

## 2. On-disk runtime layout (`~/.nock-brain/`)

Everything is 0700 dirs / 0600 files, enforced by `bin/_store.py` and the
installer's permission migration. `facts.json` is authoritative; everything
else is derived or append-only sidecar.

| Path | What it is |
|---|---|
| `facts.json` | **The store.** Authoritative fact list; default target of nearly every CLI |
| `insights.json` | Synthesized recurring-fact insights (from `synthesize.py`); recall surfaces these first |
| `.insights.json.synthesis.lock` | Persistent 0600 publication lock for the default insights output; never unlink while writers can run |
| `events.jsonl` | Sanitized evidence events from raw JSONL ingest |
| `sessions/` | Per-session markdown notes from `refine-sessions.py` |
| `review/` | Human-gated queues: `promotion-candidates`, `dedup-candidates`, `contradiction-candidates` (each `.json` + `.md`) |
| `proposed-facts.json` (+`.md`) | The propose→approve queue (`propose-facts` → `approve-proposals`) |
| `vault/`, `graph.json` | Derived Obsidian vault and Graphify graph — receipt-ledgered projections |
| `signing-key`, `signing-key.pub` | Ed25519 keypair (HMAC-SHA256 fallback when `cryptography` is absent), auto-created |
| `revocations.jsonl` | Signed append-only supersession events (`nockbrain-revocation/v1`) |
| `purged-ids.jsonl` | Tombstones for hard-deleted fact ids; rebuild merge will not re-extract them |
| `fact-edits.jsonl`, `.edit-fact.lock` | Actor-tracked edit history + writer lock for `edit-fact.py` |
| `projection-receipts.jsonl` | applied/ambiguous ledger for every derived-artifact write (`_projection.py`) |
| `recall-degradations.jsonl` | Append-only log of degraded (empty) store reads; surfaced only by health |
| `facts.json.verified-cache.json` | HMAC-keyed cache of proven signatures for the recall hot path |
| `embeddings.npz` | Semantic-tier vector sidecar (derived, never authoritative) |
| `model/` | Pinned potion-base-8M embedding assets (checksum-verified) |
| `venv/` | Semantic-tier virtualenv (numpy + tokenizers + cryptography); system Python never touched |
| `semantic-on` | Zero-byte opt-in marker for the semantic tier; `rm` to disable |
| `applied-batches.json` | Idempotency state for `apply-promotion-batch.py` |
| `hook-errors.log` | The hook's only failure visibility (hook itself always exits 0). Rotated to `hook-errors.log.1` at 1 MiB |
| `brain.db`, `store-v2` | E2 SQLite store + deliberate cutover marker — **not used unless explicitly selected** (§5 migrate) |
| `*.bak-<UTC>` | Timestamped pre-swap backups written by `rebuild-store.py` |

The hook is wired into `~/.claude/settings.json` (backed up + atomically
replaced by the installer).

---

## 3. End-to-end data flow

```
RAW SOURCES: ~/.claude/projects/**/*.jsonl · markdown transcripts
             curated memory dirs · NockCC Memory Curator batches (API)
      │
      ├─(A) JSONL lane:    ingest-jsonl → events.jsonl → refine-sessions → facts
      ├─(B) markdown lane: extract-facts (writes store DIRECTLY, additive)
      │                    or propose-facts → ★approve-proposals★ (the gated twin)
      ├─(C) curated lane:  ingest-curated-memory (signs on write, idempotent per dir)
      └─(D) fleet lane:    apply-promotion-batch (server-curated batches; additive-only,
                           hash-chain verified, sign+verify before recording applied)
      ▼
  ~/.nock-brain/facts.json  ──────────────────────────────── THE STORE
      │
      ├─ SIGN:        sign-facts (routes per fact: legacy v1 vs claim v2 — §7)
      │               verify-facts (exit≠0 on any TAMPERED; audits revocations)
      ├─ CONSOLIDATE: synthesize [--llm] → insights.json
      │               dedup-facts (propose → --apply) · consolidate-facts (manifest
      │               → --execute --i-have-reviewed-the-manifest)
      │               detect-contradictions [--llm] (propose-ONLY) → ★supersede-fact★
      │               (every supersession mints a signed revocation event)
      ├─ PROMOTE:     review-promotions → review queue (★human applies by hand —
      │               there is deliberately no applier for CLAUDE.md/AGENTS.md★)
      ├─ EXPORT:      export-obsidian → vault/ · export-graph → graph.json
      │               (both write projection receipts)
      ├─ SEMANTIC:    fetch-embed-model (once) · embed-facts → embeddings.npz
      └─ RECALL:      hooks/memory-inject.sh → recall-classifier (gate, <50ms)
                      → budget-recall --budget 800 → {"systemMessage": ...}
```

★ = human gate. All consolidation is **mark-only**: the signed core
(id+kind+content+evidence) is never rewritten; losers get `superseded_by` and
closed validity windows.

**The nightly orchestrator** — `rebuild-store.py` (cron `33 3 * * *`,
`--since 3`) runs the whole chain into a **staging dir**, applies a HARD
health gate (abort untouched on any live-secret finding or not-recall-ready),
signs, exports, then backup-and-swaps into live, then regenerates
`insights.json` (`synthesize --sign`) and the semantic sidecar
(`embed-facts`, 1800s timeout, never gates). `--dry-run` can never alter
live. `--replace` skips both the live-store merge and the anti-amnesia
shrink guard — the only intentional way to shrink the store. Windowed merge
is **not** naive recent-wins: tombstones (`purged-ids.jsonl`), fact-edits,
and revocations protect those ids, and a live `status=superseded` fact is
never overwritten by a re-extracted `current` copy.

---

## 4. Shared modules (`bin/_*.py`)

| Module | Owns | Key API |
|---|---|---|
| `_store.py` | Filesystem permission discipline (0700/0600) | `secure_mkdir/write_text/write_json/copyfile`; `secure_write_json` **is** atomic (`secure_write_json_atomic`: mkstemp + chmod 0600 + os.replace); `secure_replace_text` / `secure_replace_bytes` (optional `before_replace` skip); `secure_write_text` stays non-atomic |
| `_channel_frame.py` | Resident-channel envelope grammar shared by ingest and purge | `CHANNEL_FRAME_RE` (12-hex id-bound BEGIN/END frame; unanchored callers use `finditer`, ingest uses `fullmatch`) |
| `_facts.py` | The v1 fact-record contract, defensive loading, bi-temporal validity, agent ownership | `REQUIRED_FACT_FIELDS`, `RECALL_ITEM_FIELDS`, `load_facts` (`on_unreadable` callback on I/O/parse failure), `fill_source_date` (v2 `source_time` → operational `source_date`), `fact_currently_valid` (v1 `valid_at`/`invalid_at` **and** v2 `valid_from`/`valid_to`), `fact_source` (default `"mira"`), `content_tokens`, `jaccard`, `malformed_fact_reason`, `load_jsonl_ids` / `TOMBSTONES_FILENAME` |
| `_scrub.py` | Secret redaction + structural-noise discrimination, shared by EVERY extraction path | `scrub_secrets`, `is_structural_noise` (prefix rules + ONE substring exception: `JUDGE_PROMPT_MARKERS`, checked before the [TAG] escape — N10052), `SECRET_PATTERNS` |
| `_sign.py` (977 L) | Both attestation contracts, keys, canonicalization, the verification state machine | `sign_facts` (per-fact routing), `sign_fact`, `sign_claim_fact_v2` (also fills `source_date` from `source_time`), `is_v2_claim_fact`, `verify_fact` → `VALID/TAMPERED/UNSIGNED/PARENT_SUSPECT`, `verify_facts(..., verified_cache=None)` (caching is a property of verification; the offline auditor passes None), `load_or_create_key`, `resolve_key_paths` / `resolve_signing_key` / `resolve_verify_key` (shared env-aware resolver: CLI > `NOCKBRAIN_SIGNING_KEY`/`_PUB` > store_dir/`~/.nock-brain`), `SigningKey.cache_key_material()` (Ed25519 private bytes or `None` if pub-only), verifier receipts |
| `_revoke.py` | Attested supersession (S1): signed append-only revocation events; resurrection detection | `sign_revocation`, `record_supersessions`, `audit`, `resurrected_ids` (recall's fail-open wrapper), `blocking_findings` (single source of truth for exit status), `resolve_signing_key` (re-export of `_sign.resolve_signing_key`) |
| `_storeback.py` | Store-backend contract: `JsonStore` (default) / `SqliteStore` (`brain.db`, WAL); degradation logging | `resolve_store` (env `NOCKBRAIN_STORE`; `json` = kill switch; sqlite only if marker **and** db exist; **honors basename** — non-`facts.json`/`brain.db` paths stay `JsonStore` so insights/graph never key onto `brain.db`), `load_facts`, `replace_all`, `snapshot`, `export_facts_json` |
| `_verify_cache.py` | Cache of proven signatures for the recall hot path (~0.4–0.8 s saved per recall) | `CACHE_VERSION = 3`; HMAC keyed on `SigningKey.cache_key_material()` (Ed25519 **private** bytes — pub-only keys skip the cache); `for_store(path, key, load_fn)` owns the lifecycle (stamp **then** load_fn, save on exit); `load_for_store`; `unlink_for_store`; `VerifiedSignatureCache`; `sidecar_status` (`reason` is `stale_stamp` / `oversized` / `unreadable` / `rejected` when present but not fresh — freshness means loadable, sharing the loader predicate); forgery needs private-key material |
| `_embed.py` | Semantic-tier encoding + `embeddings.npz` sidecar | `get_encoder`, `sync_sidecar`, `load_sidecar` (returns `None` on ANY problem incl. model mismatch, short hashes, empty model, IndexError), `save_sidecar` (via `_store.secure_replace_bytes`; bin/ path bootstrap like `_verify_cache` so a file-spec load resolves the sibling), `EmbedUnavailable`, `NOCKBRAIN_EMBED_STUB=1` for CI |
| `_dense_recall.py` | RRF fusion of BM25 seeds with dense cosine + reserved slots | `fuse(...)` → `(fused, reserved_ids)`; RRF k=60, dense top 40, 3 reserved slots (empirically pinned) |
| `_graph_recall.py` | Graph-neighbor expansion over the export-graph structure | `expand(...)`; neighbors always strictly below the weakest seed |
| `_projection.py` | Readback receipts for derived-artifact writes (S4) | `write_with_receipt` — "applied" only after hash-verified readback; mismatch = "ambiguous", recorded never raised |
| `_window.py` | Nonce-bound watermarked job windows (S3+S8) for idempotent nightlies | `inputs_digest`, `open_window`, `settle` — **RESERVED, deliberately unwired** (operator call 2026-08-23, PR #86); only its test imports it (§11) |

Per-module invariants worth memorizing:

- `_store.secure_write_text` is **not atomic** (write then chmod). JSON writes
  go through `secure_write_json`, which is `secure_write_json_atomic`
  (mkstemp + chmod 0600 + os.replace) so a kill cannot torn-tail `facts.json`.
  Bytes/text atomic helpers remain `secure_replace_text` /
  `secure_replace_bytes` (`_verify_cache.save`, `_embed.save_sidecar`).
- `_facts`: `source` is deliberately not required (would invalidate pre-scoping
  facts). Bi-temporal bounds default open — a malformed bound never breaks
  recall. `fact_currently_valid` honors v1 `valid_at`/`invalid_at` and v2
  `valid_from`/`valid_to`. `load_facts` never raises; corrupt store (including
  non-UTF-8 bytes / `UnicodeDecodeError`) → `[]` + stderr line. `JsonStore`
  records that as `json-error:` in `recall-degradations.jsonl` via
  `on_unreadable`. v2 facts missing `source_date` get it filled from
  `source_time` so they pass `RECALL_ITEM_FIELDS`.
- `_scrub`: matching is prefix/pattern only — with ONE substring exception,
  `JUDGE_PROMPT_MARKERS` (N10052), which outranks everything: a judge-template
  sentence anywhere in the text is always noise, even `[TAG]`ged. Otherwise a
  leading `[UPPER TAG]` is an escape hatch checked first, so genuine tagged
  facts survive every noise rule. Bare-32-hex pattern is aggressive — it
  redacts git SHAs in legitimate content.
- `_storeback`: a SQLite read must never create an empty `brain.db`
  (`exists()` checked first). Broken db **or** unreadable `facts.json` → `[]`
  + row in `recall-degradations.jsonl` — **the loudest silent-degradation path
  in the system**; only `nockbrain-health.py` surfaces it. Round-trip rule:
  values occupy typed columns only when SQLite affinity returns them unchanged;
  everything else spills to `extra` (so `1` never becomes `1.0`).
- `_verify_cache`: VALID, PARENT_SUSPECT, and signature-fail TAMPERED
  determinations are cached (non-VALID digests bind the status into the HMAC
  so a sidecar rewrite cannot upgrade a failure); a hit skips only the
  public-key op — the content-hash comparison (the actual anti-poisoning
  check) runs on every recall, warm or cold. Store (mtime_ns, size) is
  metadata, not a wipe key: unchanged facts stay hits across rewrites; a
  dirty save prunes to digests hit-or-added this run. A save whose store
  stamp no longer matches is skipped so a concurrent stale writer cannot
  clobber a newer sidecar; a same-stamp save unions on-disk digests
  (append-only within one stat — union of opaque HMACs cannot upgrade a
  cached failure to VALID). An unwritable sidecar directory degrades to
  in-memory caching for the process with at most one stderr diagnostic
  (never one per recall). UNSIGNED and committed-hash TAMPERED are not
  cached.
- `_dense_recall`: recency must NEVER multiply cosine (it buried perfect
  paraphrase matches). All dense gates are filter-only.
- `_graph_recall`: `min_shared_terms >= 2` is a vacuous setting that silently
  filters every possible neighbor; `1` is the only meaningful guard.

---

## 5. CLI scripts by stage

Default store for everything: `~/.nock-brain/facts.json` (override `--facts`).

**Ingest / extract**
| Script | Notes |
|---|---|
| `ingest-jsonl.py` | Raw Claude JSONL → sanitized evidence events. User-turn resident-channel receipts are unwrapped only from a complete matching-ID BEGIN/END frame after casefolding the channel: `engine` is always dropped, Telegram maps to `user`, NockCC maps every non-blank string sender to `nockcc:<sender>` and absent/blank/non-string senders to `nockcc:unknown`, and other channels map to `<channel>:unknown`. JSON objects retain the first recognized text field, JSON arrays are dropped, and JSON scalars/non-JSON bodies retain stripped plain human text; malformed or unknown wrappers are dropped and counted. Since `extract-facts.authority_fact_allowed` accepts only exact actor `user`, NockCC actors cannot mint authority facts. Three privacy fences (path denylist, tool/endpoint denylist, scrubber); denied `tool_use` also denies its paired `tool_result`. A transcribe result becomes a `surface="text"`, `kind="message"`, `actor="user"` event only when one simple Bash invocation uses the resolved `$MIRA_HOME/scripts/transcribe.py` path (default `$HOME/Dev/mira-home`), an allowed Python interpreter, and exactly four tokens `[interpreter, script, --telegram-file-id, file_id]`, with the file ID equal to `message.voice.file_id` from a preceding same-session resident Telegram envelope whose numeric sender ID equals configured `ALLOWED_USER`. Each envelope's file_id authorizes at most one successful promotion: matching invocations retain the session and file ID until their result arrives. Failed, empty, diagnostic-only, or denied results leave the file ID available for retry; the first successful result consumes it, so even outstanding invocations cannot mint a second authoritative message. Missing/invalid allowlist, NockCC, other senders, unmatched or already-consumed audio, forged paths, and ambiguous commands leave the result as `tool_result`. The old three-token audio-path form and compound download commands are rejected. Missing `ALLOWED_USER` on a voice envelope increments `voice_envelopes_seen_allowlist_unset` and emits a stderr warning. Known transcriber diagnostics are removed; `transcribe_results_promoted` / `transcribe_results_dropped` expose the result path. |
| `refine-sessions.py` | events → v1-compatible facts + session notes. 1,500-char content cap; raw `tool_use.input` and `tool_result.content` cannot mint facts. The ingest-only trusted-path transcriber exception is converted to a user message before refinement, so it uses the existing user authority gate; all other tool results stay non-authoritative. Reuses extract-facts' classification rules |
| `extract-facts.py` | Markdown transcripts → facts. Tagged (0.9 conf) + inferred (0.7–0.85) patterns; fleet-activity kinds dropped at classification (#76); `machine_tag()` enforces a **closed machine enum, MINT-ONLY** (`KNOWN_MACHINES` = `mac-kevin`, `kevins-linux`; `fleet-02` retired at the 2026-08-27 seat migration and now raises). Retiring a name blocks new stamps only — facts already carrying a retired `machine` stay readable, verifiable and recallable, because `machine` is in neither attestation payload and no read path consults the enum. Never make it a read filter. **Writes the live store directly** — `propose-facts.py` is the gated twin |
| `propose-facts.py` / `approve-proposals.py` | Same extraction into `proposed-facts.json`; approve releases to store (no re-sign), reject drops. Live store untouched until approval |
| `ingest-curated-memory.py` | Dir of curated markdown → signed high-confidence facts; idempotent (drops+reingests `curated-*` slice). Skips files named `feedback_*` only (the hook's rules half already injects exactly those); `type: feedback` notes without the prefix are still ingested. Bypasses the propose gate by design |

**Signing / integrity**
| Script | Notes |
|---|---|
| `sign-facts.py` | Signs every fact via `_sign.sign_facts` (per-fact v1/v2 routing — §7). No dry-run |
| `verify-facts.py` | Exit ≠0 if any TAMPERED (2), resurrected/invalid revocation (4), `--strict`+unsigned or parent-suspect (3), `--strict-revocations`+unattested (5) |
| `edit-fact.py` | Unique-match content edits, actor-tracked (`fact-edits.jsonl` written before store), re-signs, `--revert`. Refuses on Merkle parents and on v2 claim facts |
| `resign-v2-authority-facts.py` | One-shot N9851 repair (see §7). Dry-run default; idempotent |

**Consolidation / supersession**
| Script | Notes |
|---|---|
| `synthesize.py` | Clusters recurring facts → `insights.json`. `--llm` = Haiku via local `claude -p` (subscription, not metered API); shape-gate rejects chat-shaped output; `_call_claude` passes `--no-session-persistence` so judge transcripts can never re-enter the distill (N10052), prompt built from `JUDGE_PROMPT_MARKERS[0]`; publication always requires an existing key and strictly verified source facts; `--sign` remains a compatibility spelling. `publish_insights` validates lineage, signs only via `_sign.sign_facts`, verifies serialized output, and atomically replaces it only if its initial output identity still matches under the shared persistent output lock. It rechecks source bytes immediately before replacement. Missing/corrupt keys, invalid source/output, signing failures, stale generations, observed source changes and write failures return nonzero and preserve prior output. The source check is not a transaction with independent fact writers; other insight writers must share the lock/publication protocol to receive the concurrency guarantee. Optional model failure may still produce a verified heuristic |
| `dedup-facts.py` | Near-identical extractions of one event → one canonical. Propose default; `--apply` marks losers superseded, signatures survive |
| `consolidate-facts.py` | Cross-date near-dupes of durable kinds. Double-gated: `--execute --i-have-reviewed-the-manifest`, refuses on manifest drift. `correction` kind never touched. `--execute` sets `invalid_at` and mints signed revocation events (`record_supersessions`) — same contract as `dedup-facts` / `supersede-fact`. OPS RULE: re-run `sign-facts.py` after any execute |
| `detect-contradictions.py` | Nightly stale-fact pass, propose-ONLY, never writes the store. Output actions are literal `supersede-fact.py` commands. `--llm` judge sees scrubbed content, prompt built from `JUDGE_PROMPT_MARKERS[1]` (N10052); failures degrade to borderline |
| `supersede-fact.py` | The manual apply-target; mints a signed revocation event via `_revoke` |
| `purge-fact.py` | HARD delete across facts/events/notes/vault/insights/graph/embedding-sidecar/verified-cache sidecar (GDPR-style). Per-fact vault mirrors are selected by parsed frontmatter `id` (never filename) across every vault kind folder; `vault/index.md` and files without parsed frontmatter are never deleted. Matching mirrors are deleted as whole files, and their backlink lines are removed. Every run also selects mirrors whose ids are already in `purged-ids.jsonl`, allowing a failed unlink to finish on retry after the fact is gone. Mirror unlinking runs only after insight and graph writes; a missing mirror counts as deleted, while other unlink failures are listed after the remaining mirrors are attempted and make the command exit nonzero. A session note's `## Facts` entry is removed only when it exactly equals the `refine-sessions.py` renderer's complete bullet for a removed fact, including the source anchor and any truncation; headings inside a matched multiline bullet do not end the section. Transcript history and near-matches remain. Dry-run default. `--apply` with **zero matches does not rewrite** the store (would drop loader-skipped malformed records). `--pattern` matches id+content only (not signature hex); `--content-prefix` matches only lstripped content, never ids or mid-sentence mentions. Channel-prefix cleanup parses JSONL event content and removes only complete ID-bound frames whose opening tag matches the literal requested prefix, as found by `_channel_frame.CHANNEL_FRAME_RE` in session/vault prose; unmatched matching opener lines stay in place and are reported with their files. For INSIGHTS the content match covers the N10052 contaminated-cluster shape (the "Most recent:" excerpt quotes the latest member verbatim); `theme` is deliberately NOT matched (top-5 keyword join, can only fire on coincidence). Summary line reports fact/event/note-block/vault-block/vault-mirror-file/unmatched-opener/session-fact-bullet counts plus insight/graph counts. Matching apply rewrites the fact store **first**, appends tombstones, scrubs `insights.json`/`graph.json`, then unlinks the verified-cache sidecar |

**Recall** (see §6 for exact ranking order)
| Script | Notes |
|---|---|
| `recall-classifier.py` | Exit 0 = recall needed. <50ms, zero imports on purpose. `--test` is the CI smoke |
| `budget-recall.py` (1,003 L) | The retrieval engine; `search()` and `select_recall()` are the production API every eval drives |
| `query-facts.py` | Simple manual filter/search (term-count, not BM25) |
| `brain-check.py` / `brain-think.py` | Agent-facing: "does the brain know this?" verdict + cited-synthesis packet. Both drive the real `budget-recall.search` (dynamic-loaded) so verdicts can't drift from production recall |

**Semantic tier**: `fetch-embed-model.py` (pinned-SHA download, once),
`embed-facts.py` (incremental sidecar sync; `--backfill` full). Run both with
`~/.nock-brain/venv/bin/python3`.

**Export**: `export-obsidian.py` (entity knowledge graph from a curated
registry, not NLP), `export-graph.py` (Graphify JSON). Both receipt-ledgered.

**Health / eval**
| Script | Notes |
|---|---|
| `nockbrain-health.py` | Counts, malformed, privacy redactions, live-secret scan vs `.env` files, degradations, contradiction-queue staleness, verification-cache sidecar (present / fresh / writable / reason; text says `stale stamp` only for `reason=stale_stamp`, and distinct `oversized` / `unreadable` / `rejected` lines otherwise; `VERIFICATION CACHE UNWRITABLE` only when `flagged`, not merely `not writable`; a missing sidecar or missing parent is `missing (cold start)`), signing-key identity vs store attestations (`SIGNING KEY MISMATCH` warns, does not flip `recall_ready`), unreadable `facts.json` (`FACTS UNREADABLE`, `recall_ready` false — never a traceback), `recall_ready`. **The hard-gate input for rebuild-store** |
| `recall-eval.py` | The CI gate: n=36 gold vs committed signed fixture, hermetic (pinned `EVAL_NOW`, semantic inert). Floors: recall 0.90 (measured 0.972), companionship 0.05 (~0.14). Also fails on any fixture attestation failure or an inverted cap-lever self-test |
| `eval-graph-recall.py` | Live-store flat-vs-hybrid benchmark (exploratory, not CI) |
| `eval-store-parity.py` | E2 cutover bar: JSON↔SQLite identical on counts, values, hashes, verification, and ranked recall for every suite+fuzz query |
| `build-recall-fixture.py` / `gen-recall-gold.py` | Maintainer tools. Fixture is scrubbed/re-signed/verified-before-write; gold queries must stay hand-authored (overlap-guarded paraphrases) |

**Migration / one-off**: `migrate-store.py` (builds `brain.db` fail-closed,
zero re-signing, `facts.json` untouched; cutover is a separate deliberate act),
`backfill-source.py` (⚠ writes by default, `--dry-run` opt-in),
`backfill-revocations.py` (mints signed events for legacy supersessions;
never touches facts.json).

**Fleet**: `apply-promotion-batch.py` — pulls `memory-promotion-batch/v1`
artifacts from the NockCC API (`NOCKCC_API_KEY`), validates the parent-digest
hash chain **anchored to `applied-batches.json`** (null `batch_digest` refused),
applies **additive-only** (id collision aborts the batch), stamps `machine`,
writes a candidate, shells `sign-facts` + `verify-facts --strict` (unsigned and
parent-suspect fail), then atomically swaps the candidate into `facts.json`
and records applied only after verification.

**Orchestrator**: `rebuild-store.py` — see §3.

---

**Synthesis evidence contract (`nockbrain-synthesis/v1`):**

- `source_events` counts each distinct scoped event ID, including multiple
  citations in one fact. Overlapping citations associate their fact IDs with
  each event rather than creating an event for the tuple. Only facts without
  event IDs fall back to anchors, then identical text on the same date/source.
  `event_lineage` records each event ID (null for fallback groups), its
  representative, and all supporting fact IDs under signed evidence.
- `representative_inputs` selects at most 25 distinct fact IDs, newest date
  first and one per date per round. A multi-event fact is supplied only once;
  overlapping events can select another distinct supporting fact. Each supplied content is scrubbed, capped at 300 chars,
  and recorded with original fact hash, exact supplied-text hash, length,
  truncation, date and source evidence. Selection and theme tie-breaking are
  deterministic. Different agents/sources are clustered separately. Confidence
  remains capped at 0.95 and the mean confidence of distinct event-representative
  facts, counting each representative once even when it cites many events.
- `source_ids` is full cluster lineage. `input_ids` names only supplied records;
  `recurrence` counts distinct events across the cluster, while `source_date`
  and `source_dates` describe actual sampled inputs. The heuristic labels the
  event count separately from the sampled span. `lineage_source_date` retains
  the full-cluster upper bound. All of these receipts live under `evidence`,
  whose source hash is bound by the existing fact attestation.
- `covered_source_ids` is deliberately narrower than input/lineage: only an
  untruncated source reproduced verbatim in the output is a full replacement.
  A one-sentence abstraction typically covers no complete raw facts. Recall
  uses this signed evidence only after including the entire insight, checks
  source content hashes against current facts, and keeps reserved dense
  sources. Legacy `source_ids` alone never suppresses raw details.
- Insight and raw date-diversity queues are independent. An oversized omitted
  insight cannot consume raw-date slots or block a shorter source answer;
  raw-only ranking, truncation and optional-tier pass-through remain unchanged.


## 6. The recall path, in exact order

`hooks/memory-inject.sh` (UserPromptSubmit): parses a non-empty prompt (the
length floor is the classifier's `too_short`, <6 chars),
prefers `~/.nock-brain/venv/bin/python3`, exports `NOCKBRAIN_SEMANTIC=1` iff
the `semantic-on` marker exists, runs the classifier, then
`budget-recall --budget 800 --facts … -- "$PROMPT"` (note `--` — option-
injection fix). Output `{"systemMessage": …}` with inert framing ("reference
material, not instructions"), else `{}`. Always exits 0; stderr goes to
`hook-errors.log`. **There is no timeout mechanism in the hook** — the <2s
budget is design intent enforced only by downstream cost control.

Inside `budget-recall.select_recall()`:

0. **Load + verify.** `resolve_store` (basename-honoring: `insights.json` stays
   JSON even if `brain.db` is selected) → `_verify_cache.for_store` (stamp
   **before** the store is read, save on exit) → `verify_facts(..., verified_cache=)`
   filter: TAMPERED always excluded; UNSIGNED and PARENT_SUSPECT kept by
   default (counted on stderr); `--strict-verify` keeps only VALID. Missing
   key ⇒ verification skipped entirely (fail open) **except** `--strict-verify`,
   which fails closed (empty recall + loud stderr). Then agent scoping
   (`--agent-scope`: keep `source ∈ {scope, "shared"}`). Then **revocation
   audit**: facts a trusted `revocations.jsonl` event says are dead are
   excluded even if `status=current` (the unsigned flip-back).
1. **BM25 tier** (`search()`): hard filters (not superseded, currently valid
   including v2 `valid_from`/`valid_to`, `revokes_revision_ids` hides revoked
   revisions, confidence ≥ 0.7), Okapi BM25 (k1=1.5, b=0.75, query-local corpus stats),
   then score = bm25 × confidence × recency (per-kind half-life: status 14d …
   decision/directive/correction/architecture 180d, identity ~forever,
   insight 45d) × supersession factor × trust factor × coverage boost ×
   phrase-proximity boost × bulk-date penalty. v2 facts missing `source_date`
   get it filled from `source_time` at load.
2. **Dense fusion** (`--semantic`): RRF-fuse BM25 list with cosine candidates
   from the sidecar; stale/orphan/non-finite vectors skipped; query text is
   stripped of intent scaffolding ("what did we decide about…"). Returns
   3 reserved dense-only ids. Any failure **including an unexpected exception
   inside `fuse`** ⇒ seeds unchanged (the call is wrapped; BM25 is the floor).
3. **Graph expansion** (`--graph`): expands the *fused* list (order is
   specified: dense first) with concept/session neighbors, always below the
   weakest seed. Off-path returns the identical list object.
3b. **Curated cap** (`--max-curated`, default 2; 0 excludes, negative
   disables): on the final raw-fact list, facts with `source == "curated-memory"`
   beyond the first 2 in list order are dropped, so the next-best non-curated facts take
   the freed slots (N10944). Pure filter, no re-score; insights unaffected. Unlike
   the date cap it does NOT exempt reserved dense ids (the ≤2 bound is hard);
   capped-out ids are pruned from `reserved_ids`.
4. **Insights lead**: on every call, semantic or not, every
   heuristic-synthesized insight (`synthesized_by == "heuristic"`, the
   auto-generated "Recurring \<kind\> (N distinct events...)" keyword-bag
   template — see synthesize.py §"Synthesis evidence contract") is excluded
   outright *before* `search()` runs (N10935: the template term-matches
   almost any query on 1-2 generic words, and unconditionally leading
   `fact_results` let a handful of them occupy the whole per-prompt budget
   ahead of genuinely on-topic facts; excluding before scoring also keeps
   their boilerplate terms out of `search()`'s corpus statistics). The
   surviving insights are then searched with the same `search()` as facts.
   LLM-enriched insights (`synthesized_by == "llm"`) and legacy insights
   missing the field are unaffected — only per-prompt recall excludes
   heuristic ones; the store itself, and any other reader of it, is
   untouched. Since a heuristic insight's coverage claim typically rests on
   its "Most recent: \<verbatim excerpt\>" clause, per-prompt coverage
   suppression (below) now in practice only fires for LLM prose that happens
   to quote a source verbatim. Separately, and only when semantic, the whole
   (already-filtered) insight list is further capped at 5. Full lineage
   never suppresses raw facts. Only signed `covered_source_ids` from a
   completely included
   insight can deduplicate unchanged, verbatim source detail; reserved dense
   sources remain included.
5. **Date-diversity cap**: max 4 per `source_date`, independently within the
   insight and raw queues; overflow deferred to each queue's tail, never
   dropped; reserved raw ids exempt. Omitted insights spend no raw date slots.
6. **Token budget**: len/4 estimate, default 1000, hard max 1500; reserved
   ids' cost precommitted, greedy fill. An oversized insight is skipped so a
   shorter source can fit; raw-fact overflow keeps the original truncation.

**BM25 is the floor, always.** Every optional tier degrades to the seeds
unchanged with a stderr note the hook discards.

---

## 7. Integrity model

**Legacy v1 envelope** (`sign_fact`): signs `{id, kind, content}` + evidence +
sorted parent core-hashes, domain `nockbrain-fact-v1`. Does NOT sign status /
confidence / dates — lifecycle marks stay cheap. That gap is closed by
**signed revocations** (`_revoke.py`): every supersession appends a signed
event; a current fact that a valid event says is dead = **resurrected** =
hard verify failure. Merkle ancestry gives `PARENT_SUSPECT` when a parent is
independently observed gone or drifted from its own committed hash; a
signature failure with intact parents is `TAMPERED` (`att["signature"]` is
attacker-mutable, so non-empty `parent_ids` is not ancestry evidence).

**Claim-authority v2** (`sign_claim_fact_v2`, schema
`nock-claim-attestation/v2`): signs the entire authority payload inline —
`memory_id` (stable UUID) + `revision_id` (content-addressed) + confidence,
scope, validity window, `verify_before_act`, `promotion_batch_digest`,
revision DAG (`parent_revision_ids` / `revokes_revision_ids`). Deliberately
NO `status` field: authority is retired only by a separately signed revoking
revision — you cannot un-revoke by editing a string. Strict canonical JSON
(no `-0.0`, no float exponents, byte-exact UTC timestamps). A v2 fact is
VALID or TAMPERED, nothing between.

A fact is claim-authority iff `is_v2_claim_fact()`: carries the v2 schema or
any v2-only authority field. These originate from operator-accepted promotion
batches (`apply-promotion-batch.py`), not transcript distillation.

**⚠ THE N9851 TRAP** (the repo's sharpest edge): `verify_fact`'s legacy
branch calls any fact with v2-only fields TAMPERED. `sign_facts` used to sign
everything legacy — one bulk `sign-facts.py` run silently dropped 105 real
facts out of recall. Fixed by per-fact routing in `_sign.sign_facts` (#82);
`resign-v2-authority-facts.py` repairs already-damaged stores; the CI recall
eval verifies fixture signatures specifically to guard this. **Never sign
facts except through `_sign.sign_facts`.**

**Verifier receipts** (`nock-claim-verifier-receipt/v1`): 12-field exact-set
contract binding one provider session/turn to one claim revision + live
evidence anchor, for the Claim Guard consumer. The signing key must live
outside the model seat.

**Fail-open vs fail-closed doctrine** (memorize this split):
- Fail **open** (recall keeps working): missing signing key (default mode),
  corrupt store, missing/stale sidecars, degraded SQLite reads, job windows,
  every optional recall tier. Unsigned revocations are not trusted, so a
  flip-back of an unattested supersession still depends on `status==superseded`.
- Fail **closed**: v2 claim signing/verification, revocation audits
  (`blocking_findings`), `--strict-verify` with no/unloadable key (empty
  recall), the migrator, promotion-batch chain validation, the rebuild health
  gate, verify-cache trust decisions.

---

## 8. Python floor + environment knobs

CI runs 3.11/3.12, but the hook runs on stock macOS `python3` = **3.9**.
`tests/test_python_floor.py` hardcodes the exact 13-module hook-reachable
closure (`_dense_recall, _embed, _facts, _graph_recall, _projection, _revoke,
_sign, _store, _storeback, _verify_cache, budget-recall, export-graph,
recall-classifier`) and requires `from __future__ import annotations`
everywhere. **Never add an import edge into the hook path without updating
that list and confirming 3.9 compatibility** — 3.10-only syntax has killed
recall in production twice.

Env vars honored by recall (`budget-recall`): `NOCKBRAIN_SEMANTIC`,
`NOCKBRAIN_GRAPH_RECALL`, `NOCKBRAIN_STRICT_VERIFY`, `NOCKBRAIN_STORE`
(`json` = instant SQLite rollback), `NOCKBRAIN_AGENT_SCOPE`,
`NOCKBRAIN_MAX_PER_DATE`, `NOCKBRAIN_INSIGHT_LEAD`, `NOCKBRAIN_SIGNING_KEY`
/`_PUB`, `NOCKBRAIN_UNTRUSTED_FACTOR`, `NOCKBRAIN_BULK_DATE_*`; dense/graph
tuning `NOCKBRAIN_RRF_K`, `NOCKBRAIN_DENSE_TOP`, `NOCKBRAIN_RESERVED_SLOTS`,
`NOCKBRAIN_GRAPH_WEIGHT` etc. Other: `NOCKBRAIN_MACHINE` (closed enum, **mint-only** — read paths
never consult it; retired names raise at mint),
`NOCKBRAIN_EMBED_STUB` (CI), `NOCKBRAIN_EMBED_MODEL_DIR`, `NOCKCC_API_KEY`
(fleet applier). The hook itself sets only `NOCKBRAIN_SEMANTIC`.

---

## 9. Tests that are contracts (break these consciously or not at all)

- `test_python_floor.py` — the 3.9 closure (§8).
- `test_claim_attestation_v2.py` + `tests/nock_memory_conformance_v1.json` —
  the v2 wire format. The conformance JSON's canonical bytes hash to a
  hardcoded digest; any byte change breaks it deliberately.
- `test_ci_hardening.py` — static assertions over `ci.yml` (SHA-pinned
  actions, pinned pytest/bandit, `bandit -r bin`, gitleaks via pinned
  `go install`, dependabot present).
- `test_store_backends.py` — every store behavior asserted once, proven for
  both backends; pins backend-selection semantics incl. the kill switch.
- `test_revocations.py` — resurrection detection, key-rotation ring, the
  single exit-invariant predicate.
- `test_stage1_hardening.py` — the OWASP remediation suite (drives the real
  hook via subprocess).
- `test_recall_eval.py` + committed fixture (`tests/fixtures/
  recall-eval-store.json`, 196 signed facts, disposable HMAC key — the `.pub`
  containing the secret is intentional and gitleaks-allowlisted).
- `test_rebuild_store.py` — health-gate aborts, backup-before-swap, the
  never-shrink-on-merge guard.
- `test_extract_facts.py::test_retired_machine_*` — both halves of the
  machine-enum contract: a retired seat cannot MINT, and facts already stamped
  with one stay `verify --strict`-green and recallable. The second test is what
  stops a later PR turning the enum into a read filter and orphaning every
  fact minted on a decommissioned seat.

CI (`.github/workflows/ci.yml`): pytest → classifier smoke →
`recall-eval.py --gate` → bandit (bin/ only) → gitleaks.

---

## 10. docs/ index

| Doc | Status |
|---|---|
| `specs/nockbrain-v2-conversation-memory-compiler.md` | Founding v2 spec. MVP implemented; the `nockbrain/` package layout it targets does NOT exist (still `bin/` scripts). Privacy section (3 fences) is the densest part |
| `decisions/ADR-001-build-v2-inside-nock-brain.md` | Accepted: v2 lives in this repo, not a sibling |
| `audits/2026-06-11-owasp-audit.md` | 11 findings (F1–F11), **all remediated** (N8054 stages 1–4) |
| `specs/2026-07-10-semantic-recall-hybrid-design.md` | Implemented through Phase 4; default OFF per-brain; Phase 3 rerank explicitly not triggered |
| `specs/2026-07-26-mira-brain-enhancement-plan.md` (+ 07-27 REDLINE) | E1–E7 plan, approved. REDLINE's key finding: marked-superseded count understates true supersession ≥2×; dedup is load-bearing. Order: E1→E5a→E2(+E6)→E4→re-eval→E3→E7→E5b |
| `specs/2026-07-28-e2-sqlite-store-design.md` | E2 design. P1+P2 shipped (`_storeback`, migrator, parity harness); P3–P5 (fleet migrate, 7-green-nightly soak, cutover) NOT done. Key insight: zero re-signing needed |
| `rebuild-store.md` | The nightly orchestrator; motivated by the store silently rotting to a stale snapshot |
| `evals/README.md` + `recall-gold-v1.json` (CI) + `curated-recall-suite.json` (Phase-2/offline) | Gold is a reconstruction of a lost n=90 set; queries must stay hand-authored |
| `tracking/nockcc-nocks.md` | ⚠ Stale: stops at 2026-06-12 (N8054); covers nothing from #63–#83 |
| `memory-explorer.md` | Local read-only Explorer launch, isolation and verification limits, snapshot behavior, and BM25 preview semantics |
| `customer-setup.md` | Customer identity, selected sources, review queue, optional session hooks, correction/forgetting and recovery; pilot limits |
| `customer-agent.md` | Agent playbook for conversational approval and verified lookup through existing customer commands/API; no automatic approval enforcement |
| `customer-pilot.md` | Assisted conversational pilot, optional capture, human acceptance and privacy-conscious feedback; no claimed customer outcomes |
| `runbooks/purge-wrapper-facts.md` | Operator-only, writer-quiesced backup/dry-run/apply/resynthesis/verification for channel-wrapper facts, plus uninstalled daily curated-memory user-systemd units |

---

## 11. Known gaps & sharp edges

Found in the 2026-08-23 full sweep; triaged with the operator (bus msgs
66653/66657) and resolved in PRs #85/#86 where marked.

**Open by design / watch list:**

1. **`bin/_window.py` is RESERVED, deliberately unwired** (operator call:
   nightlies are fail-closed with backups; no double-run harm observed; wire
   only when a real double-run shows up — PR #86 docstring says so). The
   separate nightly problem — `--llm` jobs killed by the seat's 300s cap →
   silent heuristic fallback (N9709) — is an operate-side timeout/detach fix,
   not a `_window` use case.
2. **Two competing write paths into the store**: `extract-facts.py` writes
   live and additive (now with a stderr nudge, PR #86);
   `propose-facts`→`approve-proposals` is the gated route. Intended
   end-state: the gated route becomes the only writer — that hard-gate is a
   Kevin-approved change, not a cleanup.
3. **`review-promotions.py` has no applier** — deliberate; the queue's
   terminal step (editing CLAUDE.md/AGENTS.md) is manual.
4. **The hook has no timeout** despite its own <2s contract — accepted
   (wontfix): the operator has never observed this hook hang, and a bash
   watchdog adds more risk than it removes. Only SQLite's 2s busy_timeout is
   enforced.
5. **Dry-run polarity is inconsistent** across CLIs (`--apply` vs
   `--execute`+ack vs opt-in `--dry-run` vs no gate) — accepted (wontfix):
   documenting it here beats a breaking CLI change. Check each tool's gate
   before running it.
6. **Silent-degradation paths to watch**: degraded SQLite **or JSON** read →
   empty recall (visible via health / `recall-degradations.jsonl`); dense/graph
   unavailability or a `fuse` crash → flat BM25 (stderr only, discarded by
   hook); corrupt store → `[]` (never raises); health prints `FACTS UNREADABLE`
   rather than traceback; ambiguous projection receipts (health check
   default-off); unwritable verify-cache sidecar (parent dir exists) →
   in-memory only (one stderr line per process; health flags `VERIFICATION
   CACHE UNWRITABLE`); a missing parent is a cold start, not that flag;
   hook-errors.log rotates at 1 MiB.
7. **The two 2026-08-22 recall-pilot reports are external** — internal
   working documents kept outside this repo (refs reworded in PR #86);
   `docs/tracking/nockcc-nocks.md` is historical through 2026-06-12 by
   choice (banner, PR #85) — the live NockCC board is the source of truth
   for nock state.

**Resolved 2026-08-23:** dead `load_facts` import in budget-recall (#85) ·
duplicate `projection_lib` conftest fixture (#85) · `consolidate-may19.py`
deleted (#85) · CI now exercises the real 3.9 floor via the `floor` job and
`NOCKBRAIN_FLOOR_PYTHON` (#85) · dangling `reports/` paths reworded (#86).

---

## 12. Optional Memory Explorer

`bin/explore-memory.py` is an independently launched, read-only local service.
No mode prints usage before store access. `--demo` builds fictitious records in
owner-only scratch; `--store` chooses one directory explicitly. These modes
are mutually exclusive. `--verify-key` overrides the selected
`signing-key.pub`; `--open` opens the printed local URL. The viewer accepts
only the authoritative JSON backend and refuses a selected SQLite cutover.
It does not install a hook or alter the recall path in §6.

`bin/_explorer_store.py` owns `ExplorerStore` and `ExplorerError`: bounded,
drift-checked copies of the selected `facts.json`, `insights.json`,
`revocations.jsonl`, and public verification material; parsed records;
verification and lifecycle annotations; stable per-snapshot handles; and
scratch cleanup. It never reads the selected private signing key or follows
evidence references. `refresh()` changes snapshot generation only after a
successful capture. The summary distinguishes `missing`, `empty`,
`unreadable`, and `readable`, with explicit notices. A browser record handle is
only meaningful together with its `snapshot_id`.

`bin/_explorer_preview.py` owns `run_preview(snapshot_dir, query, budget=800)`.
It launches a bounded child over completed scratch inputs, clears inherited
Brain/Python configuration, and uses the production classifier,
`budget-recall.select_recall`, and production formatting. The displayed
settings are BM25-only (semantic and graph off), a date-diversity cap of four
per date (overflow deferred), default budget 800, no agent scope, and
non-strict verification. Classifier eligibility
is reported separately from matching results. The response is a selection
preview, not an injection receipt or history.

The service binds `127.0.0.1` on an allocated port. Fixed bundled assets live
under `web/explorer/`; it is not a general file server. Data routes require a
per-launch Bearer capability, and browser requests face exact Host/Origin
checks, no CORS, and no-store responses. The launch URL carries the capability
in a fragment; `app.js` removes it from the address bar and retains it only in
tab memory. Browser reloads require reopening the printed launch URL.

| Route | Contract |
|---|---|
| `GET /api/summary` | `{snapshot_id, captured_at, mode, store, state, counts, kinds, verification, files, notices}` |
| `GET /api/records` | Search/filter/page by `query`, `kind`, `lifecycle`, `collection`, `offset`, `limit`; returns `{snapshot_id, items, total, offset, limit}`. Default 50, maximum 100. |
| `GET /api/record?handle=…&snapshot_id=…` | Complete allowlisted detail, evidence as text, and available supersession links. A stale generation is HTTP 409. |
| `POST /api/refresh` | Empty JSON object; returns the new summary after recapture. |
| `POST /api/preview` | `{query, budget, snapshot_id}`; returns classifier, fixed settings, selected items, rendered text, approximate token cost, and notices. |

The list includes `facts` and `insights` records, each with a stable handle,
kind, lifecycle (`current`, `superseded`, `inactive`, `revoked`, `invalid`),
source date, stored confidence score, and verification label. Current is the
default filter; `all` explicitly includes historical records. Details retain
complete content while excluding arbitrary private fields and key material.
Source references are untrusted text, never browser links or fetched paths.
The service limits each input file to 32 MiB, the snapshot to 64 MiB, request
bodies to 16 KiB, preview queries to 2,000 characters, and previews to five
seconds; failures are visible rather than silently truncated.

---

## 13. Standalone customer pilot

`bin/consumer-brain.py` owns `init`, `status`, `propose`, `pending`, `review`,
`apply`, `discard`, `correct`, `forget`, `recover`, `setup-hooks`, and `open`.
Every operation requires `--store` with an explicit absolute destination;
`propose` requires `--format markdown|claude-jsonl` and repeated `--source`
paths; `review`, `apply`, and `discard` require a full SHA-256 `--proposal` digest. With
no subcommand, it prints help without store access. Output containing source
or path data is ASCII JSON; review prints the complete proposal. Errors are
concise and do not echo raw inputs. `correct --fact ID --replacement-proposal
DIGEST` requires one current import candidate; `forget --fact ID` proposes a
removal. Both produce a digest for the same review/apply flow. Review also shows
the selected target, stale/recovery state and forgetting scope. `open` launches
Explorer (§12), which remains read-only. No command discovers home or fleet
sources. `setup-hooks` writes only a selected store's opt-in settings file.

`bin/_consumer_store.py` owns `ConsumerError`, `init_store(Path)`, and the
locked `ConsumerStore(Path)` context manager. Its read-only `path`, `manifest`,
`facts`, and `generation` observations are defensive; `save_proposal`,
`read_proposal`, and `apply_proposal` implement digest-bound review and signed
additive publication. `read_regular` is the bounded no-follow reader and
`canonical_bytes` provides strict deterministic ASCII JSON. `init_store`
creates a fresh 0700 directory only, with a local identity and keypair, empty
`facts.json`, private proposals directory and ignore-all `.gitignore`. Existing destinations are never
adopted. Existing facts must be valid signed local v1 customer facts; foreign,
unsigned, malformed, or tampered records fail closed. Proposals bind store
UUID, key ID and combined facts/revocations generation. Apply rejects stale generations, signs new
facts through `_sign.sign_facts`, verifies before atomic replacement, and
skips existing or revoked IDs without editing their signatures or evidence. A customer
lock and pre-replacement byte check coordinate these writers; unrelated
legacy writers have no transaction guarantee and are unsupported. SQLite
cutover and the legacy purge ledger are refused. Signed customer revocations
are strictly parsed and verified with the captured customer key.

`propose_correction` and `propose_forget` save lifecycle proposals.
`pending_proposals` returns digest/action/count/stale summaries (100 proposal
files / 64 MiB queue limit);
`discard_proposal` removes one; `status` reports record counts and recovery state.
Correction preserves the old signed core and attestation while marking it
superseded, adds a newly signed replacement, and records its signed revocation.
Forgetting removes the selected record and clears all saved proposals while
retaining a content-free ID revocation against exact replay. Unsupported derived
outputs cause lifecycle changes to fail rather than claiming complete erasure.

Lifecycle apply signs a private `.customer-journal.json` containing the intended
post-operation facts, hashes of the previous inputs and the signed event. It
publishes the journal before the revocation, then facts, then proposal cleanup.
This is recoverable multi-file publication, not a single atomic transaction.
An interrupted operation blocks normal mutations; explicit `recover()` validates
and finishes that accepted transition. Opening/status does not silently finish
erasure. The journal never copies the forgotten record's content. External
sources, backups and already open Explorer snapshots are outside erasure scope.
Recognized owner-private transaction temporaries also require explicit recovery;
reads never clean them. Recovery either finishes a validated journal or discards
unpublished temporaries, and supports paired hardlink remnants from the older
proposal publisher. New publication uses atomic replacement under the customer
lock. Revocation and journal size limits are checked before publication.
Process-death boundaries are tested; power-loss durability is not claimed.

`bin/_consumer_import.py` owns `collect_candidates(sources, format, manifest)`.
It reads only selected single-link regular source files and returns candidates, source
receipts, and statistics without writing facts. Limits are 16 files, 8 MiB
each, 32 MiB total, at most 1,000 candidates and 1,500 characters per
candidate. It reuses the existing classifier, authority rules, scrubber and
JSONL privacy fences while keeping customer construction separate from fleet
machine minting. Markdown accepts curated `- ` bullets. JSONL preserves
user/assistant roles; assistant text cannot mint decision/directive/correction
authority. Tool payloads and mixed tool-result envelopes contribute no candidate
text. Lexical and resolved source paths must pass privacy checks, and the
selected file's identity must remain stable through resolution and capture.
IDs cover the full sanitized
content and kind plus customer UUID. The selected source hash, sanitized path
and event anchor are signed evidence. Overlong candidates are counted and
skipped; source or proposal limit violations fail visibly. Source files are
never rewritten. The fleet mint gate, default extraction paths, recall hook,
and Explorer's read-only service contract remain unchanged.

`bin/_consumer_hooks.py` owns `setup_hooks(store_path, roots) -> settings_path`,
explicit transcript containment, pending-only capture and public-key recall.
`bin/consumer-hook.py` is the bounded Claude adapter and subprocess worker.
`setup-hooks --transcript-root ABSOLUTE_DIRECTORY` (repeatable) generates
`claude-settings.json` with absolute interpreter/script paths and event timeouts.
The printed `claude --settings FILE` opts a session in; no global settings are
rewritten. Stop captures only its supplied, allowlisted transcript. Repeated
unchanged capture is idempotent; existing/revoked candidate IDs are excluded.
No hook applies a proposal or logs raw transcript text.

UserPromptSubmit returns `hookSpecificOutput.additionalContext` after the
production classifier and strict verified BM25 recall. Recall reads only the
selected manifest, facts, revocations and public verifier; it never reads the
private key. Private scratch contains caches and worker output, inherited fleet
configuration is cleared, semantic/graph are off and journal recovery states
refuse recall. Failures exit successfully with a fixed diagnostic and no context.
The customer hook closure is independently acknowledged by Python-floor tests.
See [customer setup](customer-setup.md) and the [conversational pilot](customer-pilot.md).
The [agent playbook](customer-agent.md) is loaded through Claude's native
prompt-file flag; it adds guidance, not runtime commands or approval enforcement.
Distribution packaging and real customer quality evaluation remain release work.
