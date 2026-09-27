# Customer Brain pilot guide

This is a **customer pilot**, not a stable release. It creates
one fresh, private identity and imports only files you select. It does not
discover home data, change global Claude Code settings, call a model,
or connect to the fleet. Memory Explorer is the local, read-only view of your
store. Optional session hooks capture proposals and recall accepted memories
directly in Claude Code; Nock Harness is not required.
The setup command currently targets macOS and Linux.

From the repository root, choose a **new absolute destination** whose parent
already exists, outside your source-code checkout. The destination itself
must not exist. This example source is
fictitious; replace it with your own selected, ordinary single-link file.

```sh
STORE="$HOME/customer-brain"
SOURCE="$PWD/examples/customer-notes.md"
python3 bin/consumer-brain.py init --store "$STORE"
python3 bin/consumer-brain.py status --store "$STORE"
python3 bin/consumer-brain.py propose --store "$STORE" --format markdown --source "$SOURCE"
```

`init` prints the new store UUID and algorithm, never private key bytes.
`propose` prints a candidate count, full SHA-256 proposal digest, statistics,
and an exact review command as a JSON string. The simplest way to run review
is to use the command below with the printed digest. If using the command
value from JSON, decode it first; removing the outer quotes alone does not
decode escapes in unusual paths:

```sh
python3 bin/consumer-brain.py review --store "$STORE" --proposal FULL_DIGEST
```

Read the **entire** pretty-printed review JSON: each candidate's full content,
kind, confidence, subject and evidence; the selected source paths and hashes;
and the statistics. JSON escapes terminal controls. Check that the content is
accurate, current, appropriately attributed, and free of sensitive details.
Review only reads the proposal; `facts.json` remains empty until acceptance.
If it is wrong, edit your source and propose again. Only after accepting that
specific digest, run the printed `apply_command` value or:

```sh
python3 bin/consumer-brain.py apply --store "$STORE" --proposal FULL_DIGEST
python3 bin/consumer-brain.py open --store "$STORE"
```

`apply` prints added, skipped, and total counts. `open` starts Explorer and
opens its loopback URL to browse records and try BM25 recall preview. Keep
that terminal running while using the page; Ctrl-C stops the server. Explorer is
read-only and its preview is a selection under displayed settings, not proof
of what an agent received. It does not install automatic recall. You can still
use `python3 bin/explore-memory.py --store "$STORE"` to print the URL without
opening a browser.

## Review queue

```sh
python3 bin/consumer-brain.py pending --store "$STORE"
python3 bin/consumer-brain.py discard --store "$STORE" --proposal FULL_DIGEST
```

`pending` lists saved proposal digests and their status without dumping memory
content. Read a complete proposal with `review` before applying it. `discard`
removes the selected proposal, leaving accepted facts alone. The queue is
bounded to 100 files and 64 MiB; discard old or stale proposals when it fills.

## Optional capture and recall in Claude Code

Choose the specific existing directory containing transcripts you want to
allow. Claude supplies a transcript path to the hook; it must be under that
directory. This does not scan the directory or import old conversations.
Avoid broad roots containing unrelated projects.

```sh
python3 bin/consumer-brain.py setup-hooks --store "$STORE" \
  --transcript-root /absolute/path/to/selected/project-transcripts
claude --settings "$STORE/claude-settings.json"
```

Repeat `--transcript-root` to allow another selected directory. Setup writes
only the private customer settings file and prints a quoted launch command.
The generated hooks refer to this checkout and Python interpreter by absolute
path; regenerate the settings after moving either. Normal Claude settings
still apply. Existing hooks are not removed by this workflow: Claude merges
hook entries across settings levels. If your usual configuration has fleet
memory hooks, they can still inject fleet context alongside customer memory.
Use `/hooks` to inspect the active integrations and run an isolation pilot in
a Claude setup without fleet hooks. Managed and plugin hooks may also apply.
This creates an independent store, not a sandbox for the entire Claude session.
Stop launching with this settings file to stop opting in for new sessions.

On `UserPromptSubmit`, the hook checks the recall classifier, verifies the
selected store and returns BM25 results as reference context. On `Stop`, it
imports eligible transcript text into the **pending queue**. It never runs
`apply`. A fixed notification shows the proposal digest and count; use `pending`
and `review` to inspect it. If the transcript is still being written or exceeds
the import limits, capture fails safely instead of publishing partial facts.
Some Claude versions append the final assistant response after Stop; that text
may only be considered at a later Stop or manual import.

Hooks have time limits and never block a conversation on failure. No recall
context can mean the classifier did not request recall, there was no match, or
verification failed; check the fixed hook diagnostic and store status. The hook
does not keep a raw transcript log. Exact input/output contracts follow the
[Claude hooks reference](https://code.claude.com/docs/en/hooks); session setup
uses its [documented settings flag](https://code.claude.com/docs/en/cli-reference).

## Correct or forget a memory

Copy the complete fact ID from Explorer. To correct it, first create a notes
file containing exactly one replacement fact, then propose that file. Creating
the replacement proposal does not accept it. Use its digest here:

```sh
python3 bin/consumer-brain.py correct --store "$STORE" --fact FACT_ID \
  --replacement-proposal REPLACEMENT_DIGEST
python3 bin/consumer-brain.py review --store "$STORE" --proposal CORRECTION_DIGEST
python3 bin/consumer-brain.py apply --store "$STORE" --proposal CORRECTION_DIGEST
```

Correction signs the new record and retires the old one without rewriting its
signed content or evidence. Explorer can show the old record in history.

To erase one record, create and review a forgetting proposal:

```sh
python3 bin/consumer-brain.py forget --store "$STORE" --fact FACT_ID
python3 bin/consumer-brain.py review --store "$STORE" --proposal FORGET_DIGEST
python3 bin/consumer-brain.py apply --store "$STORE" --proposal FORGET_DIGEST
```

Applying it removes that record and **clears all saved proposals**, since
proposals can contain copies of the forgotten text. A signed revocation keeps
the identifier so importing the same content/kind cannot restore it. This is
exact-record forgetting: paraphrases, other records, original source files,
backups and shell output are outside its scope. Refresh or close any open
Explorer after forgetting; already captured browser snapshots are separate
copies. Exported/derived stores are unsupported by this customer workflow.

If a memory operation is interrupted, `status` reports recovery is required.
The store refuses new mutations until the accepted operation is completed:

```sh
python3 bin/consumer-brain.py recover --store "$STORE"
```

This resumes the previously accepted action; it does not accept pending
proposals. If interruption left only an unpublished temporary file, recovery
cleans it without publishing a memory change. Recovery validates the private
journal and store before continuing. Known customer transaction temporaries are
included in cleanup; unknown files and user-created copies are outside its scope.
Process interruption is tested; this pilot makes no power-loss durability claim.
Do not edit files by hand to get past a recovery error.

## Import and storage boundaries

For several sources of the **same format**, repeat `--source /absolute/file`.
Markdown accepts curated `- ` bullet lines. Use `[DECISION]`, `[DIRECTIVE]`,
or `[CORRECTION]` tags for first-person customer notes: the existing heuristic
classifier does not infer an untagged “I decided…” as a decision. The
`claude-jsonl` format accepts selected Claude Code JSONL files, retains
conversation roles, and applies the existing privacy fences. Messages that
mix text with tool results are skipped because their human authorship is
ambiguous. Assistant text
cannot mint decision, directive, or correction authority. Tool payloads are
not candidate facts. The extraction is heuristic and can miss useful facts;
overlong candidate messages are skipped and counted rather than truncated.

Inputs are bounded to 16 selected files, 8 MiB per file and 32 MiB combined;
one proposal has at most 1,000 candidates of at most 1,500 characters each.
Rejected malformed, unsafe, or changed sources fail the proposal. Pattern
based secret scrubbing is imperfect, which is why complete review matters.
Duplicate content and kind within one proposal can combine evidence; applying
the same fact again skips its existing signed record without changing that
record's evidence or signature. Source receipts include a sanitized path and
the source's raw SHA-256 hash; selected source files are never rewritten.

Proposals bind the store UUID, signing key, facts and signed revocation
generation. If a proposal is stale after a memory change, run `propose` again and review its new
digest. The old digest cannot be applied. Back up the whole private store,
including **both** `signing-key` and `signing-key.pub`, with owner-only access.
New stores use Ed25519 when `cryptography` is already installed, otherwise
the engine's dependency-free HMAC-SHA256 fallback.
Opening an existing Ed25519 store still requires `cryptography`; the CLI
does not convert its keys or silently replace them with HMAC keys.
The HMAC fallback contains shared secret material in the `.pub` file too.
Losing or mixing either key can make the store unusable; the CLI will not
replace it silently. Customer writers share a lock, but mixing legacy writers
into this store is unsupported. New stores contain an ignore-all `.gitignore`
as an additional safeguard; keep the store outside repositories anyway.
Installer packaging and real customer extraction/recall evaluation remain
release work. Follow the [pilot checklist](customer-pilot.md) before making
stable-release claims.

The older [`install.sh`](../install.sh) path has different behavior: it
automatically discovers sources and changes Claude Code settings to install a
hook. Do not use it for this explicit customer workflow.
