# Skeptical review of the #47–#93 fix train

Scope: the verification-cache / sidecar / signing / recall / consolidation work
landed between PR #58 and PR #93, with emphasis on the #87–#93 fix commits
(issues #47–#54) and their interaction with the layers they touch. Every claim
below was verified against the code at commit `4be09b9`; findings A, B (the
sqlite leg), E, and F were additionally reproduced dynamically with throwaway
scripts against the real modules (method noted per finding).

---

## Finding A — The nightly rebuild's merge resurrects superseded facts, reverts edits, and un-deletes purged facts

**Severity: high**

**Where:**
- `bin/rebuild-store.py:156-172` — `merge_facts(live, recent)`: "recent wins on
  collision", keyed by fact id (`_fact_key`, lines 147-153).
- `bin/rebuild-store.py:234-249` — the live store is merged into freshly
  re-extracted staging facts on every non-`--replace` rebuild.
- `bin/extract-facts.py:108-109` — `make_id` is deterministic:
  `sha256(f"{date}:{content[:200]}")[:12]`.
- `bin/refine-sessions.py:120-123` — a re-extracted fact gets that same
  deterministic id and a hardcoded `"status": "current"`.
- `bin/rebuild-store.py:87-116` (esp. 107) — the ingest window selects
  transcript **files by mtime**; an active project's `.jsonl` keeps a fresh
  mtime, so its *entire history* is re-ingested every night (`--since 3` per
  the live crontab, `bin/rebuild-store.py:515,520`).

**Failure scenarios (three, same root):**

1. *Supersession erased.* `supersede-fact.py:101-109` and
   `dedup-facts.py:155-160` mutate only lifecycle fields (`status`,
   `superseded_by`, `invalid_at`) in place, keeping id and content — the
   mark-only invariant. The nightly re-extracts the same content from a
   still-in-window transcript, mints the same id with `status: "current"`, and
   `merge_facts` lets the recent copy replace the live one. All supersession
   marks vanish; the fact returns to default recall as current.
2. *Edits reverted.* `edit-fact.py:281` changes content but keeps the id. The
   nightly re-extracts the **original** content → same id → recent wins → the
   actor-tracked edit is silently undone.
3. *Purges undone.* `purge-fact.py` rewrites `facts.json`/`events.jsonl`/notes/
   vault — but never the raw transcripts under `~/.claude/projects` that the
   rebuild ingests from. A hard-deleted (GDPR-style) fact whose source
   transcript file is still active is simply re-minted the next night, then
   re-signed by `sign_and_export` (`rebuild-store.py:326-334`), fully
   legitimizing it.

Nothing in the pipeline catches this on the recall path: `budget-recall.py`
never consults revocations (no `_revoke` import), and `rebuild-store.py` runs
`sign-facts.py` but never `verify-facts.py`. The signed revocation events
minted by supersede/dedup (`supersede-fact.py:85,116`, `dedup-facts.py:255`)
would flag case 1 as RESURRECTED — but only when `verify-facts.py` is run
(exit 4 via `blocking_findings`, `verify-facts.py:153-158`), i.e. at the next
manual audit or the next promotion-batch apply
(`apply-promotion-batch.py:152`), where it surfaces as a confusing distant
failure. Cases 2 and 3 mint no revocation events and are never detected.

**How verified:** ran `merge_facts` directly: a live fact
`{status: superseded, superseded_by, invalid_at}` merged against a re-extracted
`{status: current}` copy with the same id yields the current copy with every
supersession field gone. Determinism of `make_id`, the `"status": "current"`
literal in `refine-sessions.py`, the mtime-window file selection, and the
absence of `_revoke` from the recall/rebuild paths were each confirmed by
reading the cited lines. Note the fix train made this *worse in consequence*,
not better: #58/#64/#65/#82 made mark-only supersession and signed revocation
the load-bearing integrity mechanism, and #91 made purge-fact more thorough —
while the nightly merge (older, N8142) silently undoes all three.

---

## Finding B — purge-fact leaves purged content in insights.json, graph.json, and every timestamped backup

**Severity: high**

**Where:**
- `bin/purge-fact.py:140-145` — the purge surface is exactly facts, events,
  notes-dir, vault, embeddings sidecar (plus the verified-cache sidecar at
  line 179). No `insights.json`, no `graph.json`, no `*.bak-*`.
- `bin/synthesize.py:229,260` — an insight's `content` embeds the most recent
  member fact's text (`latest[:160]`, or an LLM enrichment of the cluster).
- `bin/export-graph.py:64` — graph nodes embed `fact.get("content","")[:120]`.
- `bin/rebuild-store.py:390-398` and `bin/apply-promotion-batch.py:147` —
  timestamped full-store backups (`facts.json.bak-<stamp>`,
  `facts.json.bak-preapply-<stamp>`) accumulate and are never purged.

**Failure scenario:** operator hard-deletes sensitive content with
`purge-fact --apply`. The text survives verbatim in `insights.json` — which
recall injects **first** (`select_recall` prepends `insight_results`,
`budget-recall.py:884`) — so the purged content keeps being injected into live
sessions until the next nightly `refresh_insights` (`rebuild-store.py:411-430`)
regenerates the file, up to ~24h later. It survives in `graph.json` until the
next export, and in the `.bak-*` files indefinitely. REPO-MAP §5 describes
purge-fact as a GDPR-style hard delete across derived artifacts; the two
derived artifacts that actually re-enter recall or persist forever are not
covered. (Finding A's third leg then un-deletes the fact itself.)

**How verified:** read `purge-fact.py`'s complete argument/rewrite surface;
confirmed insight and graph content embedding at the cited lines; confirmed
insights lead recall at `budget-recall.py:881-884`; confirmed no code path
anywhere deletes `*.bak-*` files (grep for `bak` across `bin/`).

---

## Finding C — `resolve_store` routes *insights.json* to the facts SQLite DB once SQLite is selected

**Severity: medium** (latent — fires on `NOCKBRAIN_STORE=sqlite` today, and on the planned P5 cutover)

**Where:**
- `bin/_storeback.py:302-320` — `resolve_store(facts_path)` ignores which file
  it was asked for whenever the env says `sqlite` (line 316-317) or the
  `store-v2` marker + `brain.db` exist (line 318-319): it always returns
  `SqliteStore(store_dir / "brain.db")`.
- `bin/budget-recall.py:673-674` — `_load` resolves **every** store through
  `resolve_store`, and `select_recall` loads the insights store through the
  same `_load` (`budget-recall.py:862-872`).

**Failure scenario:** with SQLite selected, the insights tier of every recall
loads the *entire facts store* as "insights". Facts get ranked as insights,
prepended ahead of everything (`budget-recall.py:884`), and the
covered-source dedup (`budget-recall.py:881-882`) misfires on fields facts
don't have. Recall output is silently wrong on every query — no error
anywhere. The E2 cutover bar won't catch it: `eval-store-parity.py` contains
no insights coverage at all (grep for "insights": zero matches).

**How verified:** dynamically — created a temp dir, built a real `brain.db`
via `SqliteStore.create()/replace_all()` containing a fact, then called
`resolve_store(tmpdir/"insights.json", env={"NOCKBRAIN_STORE": "sqlite"})`:
it returned `sqlite:<tmpdir>/brain.db` and `load_facts()` returned the fact.
Repeated with the `store-v2` marker route (empty env): same result.

---

## Finding D — The authoritative store is written non-atomically by every mutating CLI; the "atomic promote" is not atomic

**Severity: medium**

**Where:**
- `bin/_store.py:24-28` — `secure_write_text` (and thus `secure_write_json`)
  is documented "Not atomic": truncate-and-write in place.
- Live-store writers using it: `bin/purge-fact.py:174`,
  `bin/approve-proposals.py:115`, `bin/consolidate-facts.py:377`,
  `bin/extract-facts.py:279`, `bin/apply-promotion-batch.py:148`
  (`facts_path.write_text`), and `JsonStore.replace_all`
  (`bin/_storeback.py:168-169`) used by `supersede-fact.py:84,115`,
  `edit-fact.py:288,352`, `dedup-facts.py:253`, `migrate-store.py:137`.
- `bin/rebuild-store.py:358-367` — `_move_into_place` does
  `unlink()` **then** `shutil.move`; staging is a `tempfile.mkdtemp` dir
  (line 564), which is routinely on another filesystem, making the move a
  progressive copy. During that window `facts.json` is absent or partial,
  despite the module docstring's "atomically promote".

**Failure scenario:** the UserPromptSubmit hook fires during any of these
writes. `load_facts` hits partial JSON → prints one stderr line (swallowed
into `hook-errors.log`) and returns `[]` (`bin/_facts.py:81-86`) → the hook
emits `{}`. Recall silently returns nothing, exactly the failure class this
train spent five PRs hardening the *cache* against. A crash mid-write leaves
the authoritative store truncated; of the writers above, only
`consolidate-facts.py:371-373` and `apply-promotion-batch.py:147` back up
first — purge, approve, supersede, edit, and dedup leave nothing to restore
except an up-to-24h-old rebuild `.bak`.
The irony: PR #91's headline was "share one atomic writer", and the derived
sidecars got it (`_verify_cache.save`, `_embed.save_sidecar`) while the
authoritative store did not.

**How verified:** read `secure_write_text`/`secure_write_json` and every
call site listed (grep for `secure_write_json(` and `replace_all` across
`bin/`); read `_move_into_place` and confirmed no `os.replace` on the
promote path; confirmed `load_facts`' fail-open-to-empty contract.

---

## Finding E — nockbrain-health crashes on the corrupt store it exists to surface

**Severity: medium**

**Where:**
- `bin/nockbrain-health.py:26-29` — `load_json` calls `json.loads` unguarded;
  used for the facts store at line 229.
- `bin/nockbrain-health.py:142-148` — `malformed_facts` calls `fact.get(...)`
  on every entry; a non-dict entry (e.g. a stray string in the list) raises
  `AttributeError`.

**Failure scenario:** the exact input health is supposed to diagnose —
"corrupt store → `[]` → empty recall", listed in REPO-MAP §11.6 as the
loudest silent-degradation path — makes `nockbrain-health.py` die with a
traceback instead of reporting `recall_ready: false`. Inside the rebuild
gate this aborts (fail-closed, acceptable); run standalone by an operator
chasing an empty-recall incident, the diagnostic tool crashes and the crash
is indistinguishable from tooling breakage. Note the tension with Finding D:
the non-atomic writers are what produce exactly this corrupt state.

**How verified:** dynamically — wrote `[{"truncated": ` to a temp
`facts.json` and ran `nockbrain-health.py --facts <it>`: exit 1 with
`json.decoder.JSONDecodeError` traceback, no report.

---

## Finding F — `purge-fact --apply` rewrites the store even on zero matches, silently deleting loader-malformed records; its pattern match includes signature hex

**Severity: medium**

**Where:**
- `bin/purge-fact.py:169-174` — under `--apply`, `secure_write_json` runs
  unconditionally, even when `removed_facts == 0`.
- `bin/purge-fact.py:50-54` — `purge_facts` round-trips through
  `load_facts(path)` with the default `REQUIRED_FACT_FIELDS`
  (`bin/_facts.py:36-47,62-69`), which **drops** any record missing
  id/kind/status/confidence/content/source_date/evidence before the rewrite.
- `bin/purge-fact.py:34-37` — `fact_matches` substring-matches patterns
  against `json.dumps(fact)`, i.e. including attestation fields: a short
  pattern like `beef` or `c0de` can match inside a signature or hash hex
  string and hard-delete an unrelated fact.

**Failure scenario:** any `--apply` purge — including one that matches
nothing — permanently deletes every record the defensive loader skips. This
class of record is real in this store's history: PR #43 existed precisely
because extractor facts once lacked `evidence` anchors. A malformed record
was previously recoverable (invisible to recall but repairable in place);
after any purge it is gone, with the summary reporting "removed 0 fact(s)".
The hex-substring match additionally makes short patterns a wider blast
radius than the operator asked for (dry-run default is the only guard).

**How verified:** dynamically — ran the real CLI with
`--pattern zzz-no-such-content-zzz --apply` against a temp store containing
one valid and one malformed record: output said "removed 0 fact(s)"; the
store was rewritten and the malformed record was gone.

---

## Finding G — Empty-recall degradation logging covers only the SQLite backend, so health's RECALL DEGRADED flag can't fire for the backend actually in production

**Severity: medium**

**Where:**
- `bin/_storeback.py:49-71` — `_record_degradation` writes
  `recall-degradations.jsonl`; its only callers are inside `SqliteStore`
  (lines 245, 258).
- `bin/_facts.py:81-86` — the default JSON backend's corrupt-store path
  prints to stderr and returns `[]`; nothing is recorded.
- `bin/nockbrain-health.py:269-273` — the RECALL DEGRADED flag aggregates
  only that JSONL.

**Failure scenario:** the store everyone actually runs (JSON — SQLite is
explicitly not cut over) degrades to empty recall with its only trace a
stderr line the hook redirects into `hook-errors.log` (rotated away at
1 MiB, `hooks/memory-inject.sh:65-74`). PR #61 built the aggregation
("stderr nobody watches is still a silent outage") but wired it to the
backend nobody uses; the identical failure on the default backend stays
invisible to health.

**How verified:** grep for `_record_degradation` callers (SqliteStore only);
read the JSON path in `load_facts` and the health aggregation.

---

## Finding H — `sidecar_status` freshness ignores key_id/alg: health reports "fresh" for a sidecar the loader rejects wholesale

**Severity: low**

**Where:** `bin/_verify_cache.py:558-572` checks only `version` and the
store stamp; the loader's acceptance test `_sidecar_header_ok`
(`bin/_verify_cache.py:401-409`) additionally requires matching `key_id`,
`alg`, and a well-typed digest list. Also, a well-formed sidecar with an old
`version` reports `reason: "unreadable"` (the initial value at line 543 is
never refined for a version mismatch), pointing operators at corruption
instead of a version bump.

**Failure scenario:** after a signing-key rotation (or a foreign/stale-version
sidecar), health prints "Verification cache: present, fresh, writable"
(`nockbrain-health.py:361-362`) while every recall pays full cold
verification. Self-heals on the first successful dirty save, but during an
unwritable-directory outage — the very state this health line was built for
in #90/#92 — the "fresh" report is persistently wrong.

**How verified:** compared the two predicates line by line; traced the
`reason` initialization at line 543 through the version-check branch at
lines 563-572.

---

## Finding I — Same-stamp concurrent cache saves can still drop one writer's digests; "converge" (#90) overstates it

**Severity: low**

**Where:** `bin/_verify_cache.py:271-273` — `save()` reads `_peer_digests`
and unions **before** writing the tmp file; the `before_replace` guard
(lines 288-294) re-checks only the *store* stamp, not whether the sidecar
changed since the peer read.

**Failure scenario:** two same-stamp writers interleave (A reads peers, B
reads peers, A replaces, B replaces): B's replace discards A's newly added
digests. Consequence is only a redundant re-verification on the next recall
— the design's fail-safe direction — but the #90 commit message's claim that
concurrent saves "converge" holds only for adds already persisted before the
peer read, not for concurrent adds.

**How verified:** traced the read-union-write sequence in `save()` and the
`before_replace` closure; confirmed nothing re-reads the sidecar between the
union and `os.replace`.

---

## Finding J — recall-eval's "neutralize ambient knobs" list misses score-affecting env vars

**Severity: low**

**Where:** `bin/recall-eval.py:224-231` pops five knobs, but
`NOCKBRAIN_BULK_DATE_THRESHOLD` / `NOCKBRAIN_BULK_DATE_MIN_FACTOR` (read at
scoring time, `budget-recall.py:118-136`, applied at 466-468 and 503),
`NOCKBRAIN_UNTRUSTED_FACTOR` (`budget-recall.py:361-369,382-388`), and
`NOCKBRAIN_STORE` (backend selection in `_load`) still flow into the
"hermetic" measurement. CI's clean env masks it; a developer with any of
these exported gets silently skewed gate numbers (e.g.
`NOCKBRAIN_STORE=sqlite` makes the fixture load return `[]` and the gate
fail mysteriously). `NOCK_BRAIN_NOW` is safe — `now=EVAL_NOW` is passed
explicitly (recall-eval.py:70,94-103).

**How verified:** read the pop list; traced each remaining env read to its
scoring/loading call site.

---

## Checked and held up

Skepticism applied to the headline fixes themselves found them sound:

- **#89 per-entry retention + live-set prune** is safe *for budget-recall*
  because `_verify_filter` runs `verify_facts` over every loaded fact on
  every recall (`budget-recall.py:635`), so nothing live is pruned; and
  because Ed25519/HMAC signatures are deterministic, the nightly re-sign
  reproduces byte-identical signatures over unchanged payloads, keeping
  digests warm across rebuilds.
- **#88 status-bound digests**: `cache_digest` (`_sign.py:847-858`) binds
  the fail status into an HMAC keyed under the verifying key material — a
  sidecar writer cannot upgrade a cached failure to VALID; the committed
  content-hash comparison (`_sign.py:972-977`) runs on every recall, warm or
  cold; and repairing a parent changes `payload_now` → miss → real
  re-verify. All confirmed by tracing `_cached_signature_status` and
  `verify_fact`.
- **#91 lifecycle ownership**: `for_store` (`_verify_cache.py:138-157`)
  stamps before the caller's load and saves in `finally`;
  `purge-fact --apply` rewrites the store before unlinking the sidecar
  (`purge-fact.py:169-184`), and the combination of the load-time-stamp skip
  (`_verify_cache.py:269`) and `before_replace` re-stat (288-294) makes a
  stale writer unable to recreate purged digests.
- **#87 warmup**: the discarded dry run (`eval-graph-recall.py:100-111`)
  plus the hermetic `--self-test` (114-184) genuinely remove the A/B skew.
- **#82/#93**: `sign_facts` routing goes through the single shared predicate
  `is_v2_claim_fact` (`_sign.py:695-703`), and `verify-facts.py:90` passes
  no cache (full audit). `kevins-linux` is in the closed enum
  (`extract-facts.py:117`) and `machine_tag` fails loudly as designed.

---

## Verdict

The fix train's micro-layer work is genuinely good: the verification-cache
lifecycle, the status-bound digest scheme, the purge/save ordering, and the
eval warmup fix are carefully reasoned, correctly implemented, and honestly
tested — I tried to break each of them and could not. But the train's
hardening effort is concentrated on a derived cache worth ~0.5s per recall
while the authoritative data lifecycle around it carries live correctness
and privacy defects that the same PRs either left behind or made more
consequential: the nightly merge silently undoes the mark-only supersession,
edit, and purge machinery the train made load-bearing (Finding A); purged
content keeps flowing into sessions through insights, exports, and backups
(Finding B); the authoritative store is still torn-writable by every
mutating CLI under a hook that fails open to empty (Findings D, G); and the
observability layer built to catch exactly these silences crashes or reports
"fresh" at the wrong moments (Findings E, H). The train is trustworthy where
it looked; the suspect part is what it never looked at — prioritize Findings
A and B before the next consolidation or purge is trusted, and gate the E2
cutover on Finding C.
