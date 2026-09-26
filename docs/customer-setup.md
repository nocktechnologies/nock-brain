# Customer Brain setup and reviewed import

This is an **evaluation milestone**, not a ready customer release. It creates
one fresh, private identity and imports only files you select. It does not
discover home data, install a hook, change Claude Code settings, call a model,
or connect to the fleet. Memory Explorer is the available recall interface.
The setup command currently targets macOS and Linux.

From the repository root, choose a **new absolute destination** whose parent
already exists, outside your source-code checkout. The destination itself
must not exist. This example source is
fictitious; replace it with your own selected, ordinary single-link file.

```sh
STORE="$HOME/customer-brain"
SOURCE="$PWD/examples/customer-notes.md"
python3 bin/consumer-brain.py init --store "$STORE"
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
python3 bin/explore-memory.py --store "$STORE"
```

`apply` prints added, skipped, and total counts. Open the Explorer's printed
loopback URL to browse records and try BM25 recall preview. Explorer is
read-only and its preview is a selection under displayed settings, not proof
of what an agent received. It does not install automatic recall.

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

Proposals bind the store UUID, signing key, and `facts.json` generation. If a
proposal is stale after an import, run `propose` again and review its new
digest. The old digest cannot be applied. Back up the whole private store,
including **both** `signing-key` and `signing-key.pub`, with owner-only access.
New stores use Ed25519 when `cryptography` is already installed, otherwise
the engine's dependency-free HMAC-SHA256 fallback.
The HMAC fallback contains shared secret material in the `.pub` file too.
Losing or mixing either key can make the store unusable; the CLI will not
replace it silently. Customer writers share a lock, but mixing legacy writers
into this store is unsupported. Corrections, forgetting/deletion, standalone
hook setup, packaging, and real customer extraction/recall evaluation remain
release work. Treat changed decisions as a reason to stop and reconcile the
store, not merely append contradictory facts.

The older [`install.sh`](../install.sh) path has different behavior: it
automatically discovers sources and changes Claude Code settings to install a
hook. Do not use it for this explicit customer workflow.
