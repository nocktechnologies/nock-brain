# Purge historical channel-wrapper facts

Run this only after the Nock #10817 fix is present in the pinned runtime
checkout. This repository does not operate on a live store or alter that pin.

`purge-fact.py --apply` loads the fact store and replaces it without a shared
writer lock. **It is not safe to run this purge concurrently with any
fact-store writer.** Complete the following procedure in order, with the
writers kept quiesced until the purge, re-synthesis, and verification finish.

## 1. Quiesce every writer

Identify every systemd timer or cron entry that runs `rebuild-store.py`, an
`ingest-*.py` script, `refine-sessions.py`, `synthesize`, or
`ingest-curated-memory.py`. Also quiesce any entry that runs
`export-obsidian.py`, `export-graph.py`, or `embed-facts.py`, plus any manually
started fact-store or derived-artifact writer. Do not run a rebuild during the
purge.

```bash
systemctl list-timers --all
systemctl --user list-timers --all
crontab -l
```

Stop every matching installed system and user timer before proceeding. Comment
out every matching cron entry with `crontab -e`; retain the exact entries so
they can be restored in step 5.

```bash
systemctl stop <ingest-or-distill.timer> <synthesis.timer> <curated-memory.timer>
systemctl --user stop <ingest-or-distill.timer> <synthesis.timer> <curated-memory.timer>
crontab -e
```

Confirm that no already-started writer remains. A match means stop that process
cleanly and run this check again; do not start the purge until it returns no
PIDs.

```bash
if pgrep -f '[r]ebuild-store\.py|[i]ngest-.*\.py|[r]efine-sessions\.py|[s]ynthesize|[i]ngest-curated-memory\.py|[e]xtract-facts\.py|[a]pprove-proposals\.py|[e]dit-fact\.py|[d]edup-facts\.py|[c]onsolidate-facts\.py|[s]upersede-fact\.py|[a]pply-promotion-batch\.py|[s]ign-facts\.py|[r]esign-v2-authority-facts\.py|[b]ackfill-source\.py|[e]xport-obsidian\.py|[e]xport-graph\.py|[e]mbed-facts\.py'; then
  echo 'A fact-store or derived-artifact writer is still running; do not purge.' >&2
  exit 1
fi
```

## 2. Create a restorable backup

Back up the authoritative files, all files this purge can rewrite, and the
store's signing and sidecar material before the dry run. Keep the shell
variables below for the restore command.

```bash
# This runbook operates on the authoritative JSON store, which is also what it backs up.
export NOCKBRAIN_STORE=json
store_dir="$HOME/.nock-brain"  # replace only for an explicitly selected store
backup_dir="$store_dir/purge-wrapper-facts-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 -p -- "$backup_dir"
chmod 700 -- "$backup_dir"

backup_files=(
  facts.json insights.json events.jsonl graph.json embeddings.npz \
  sessions vault signing-key signing-key.pub revocations.jsonl \
  purged-ids.jsonl facts.json.verified-cache.json \
  .insights.json.synthesis.lock
)
printf '%s\n' "${backup_files[@]}" > "$backup_dir/managed-files.txt"
: > "$backup_dir/present-files.txt"
for name in "${backup_files[@]}"; do
  if [ -e "$store_dir/$name" ]; then
    printf '%s\n' "$name" >> "$backup_dir/present-files.txt"
    cp -a -- "$store_dir/$name" "$backup_dir/"
  fi
done
```

If any later step fails or the results are unexpected, restore while the
writers are still quiesced. The manifest also removes mutable files that did
not exist at backup time, so a newly created tombstone or insight file cannot
survive the rollback.

```bash
while IFS= read -r name; do
  [[ -n "$name" && "$name" != */* ]] || continue
  rm -rf -- "$store_dir/$name"
  cp -a -- "$backup_dir/$name" "$store_dir/"
done < "$backup_dir/present-files.txt"

while IFS= read -r name; do
  grep -Fxq -- "$name" "$backup_dir/present-files.txt" || rm -f -- "$store_dir/$name"
done < "$backup_dir/managed-files.txt"
```

## 3. Dry-run and confirm the expected count

`purge-fact.py` is dry-run by default. Record its output in the backup
directory and apply only when the reported fact count is the expected 70.

```bash
(
  set -e
  set -o pipefail
  NOCKBRAIN_STORE=json python3 bin/purge-fact.py --content-prefix '<channel source=' \
    --facts "$store_dir/facts.json" \
    --events "$store_dir/events.jsonl" \
    --notes-dir "$store_dir/sessions" \
    --vault "$store_dir/vault" \
    --sidecar "$store_dir/embeddings.npz" 2>&1 | tee "$backup_dir/dry-run.txt"
  expected_fact_count=70
  observed_fact_count="$(sed -n 's/^would remove \([0-9][0-9]*\) fact(s),.*/\1/p' "$backup_dir/dry-run.txt")"
  test "$observed_fact_count" = "$expected_fact_count" || {
    echo "Expected $expected_fact_count wrapper facts; observed ${observed_fact_count:-none}. Not applying." >&2
    exit 1
  }
)
```

## 4. Apply, regenerate derived views, synthesize, and verify

The applied purge removes matching facts and their derived references,
including insights. Re-synthesize before re-enabling writers, then fail if a
wrapper-derived insight remains. These checks do not print live fact content.

```bash
(
  set -e
  NOCKBRAIN_STORE=json python3 bin/purge-fact.py --content-prefix '<channel source=' --apply \
    --facts "$store_dir/facts.json" \
    --events "$store_dir/events.jsonl" \
    --notes-dir "$store_dir/sessions" \
    --vault "$store_dir/vault" \
    --sidecar "$store_dir/embeddings.npz"
  # review/ and vault/ are derived views. Regenerate them from the cleaned
  # fact store and session-note sources; do not hand-edit their copies.
  python3 bin/review-promotions.py --facts "$store_dir/facts.json" --output "$store_dir/review"
  python3 bin/detect-contradictions.py --facts "$store_dir/facts.json" --queue-dir "$store_dir/review"
  python3 bin/export-obsidian.py --facts "$store_dir/facts.json" \
    --sessions "$store_dir/sessions" --review "$store_dir/review" --vault "$store_dir/vault"
  python3 bin/synthesize.py --facts "$store_dir/facts.json" --output "$store_dir/insights.json" --sign
  python3 - "$store_dir/insights.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    insights = json.load(stream)

if any(
    isinstance(insight, dict)
    and "<channel source=" in str(insight.get("content", ""))
    for insight in insights
):
    raise SystemExit("wrapper-derived insight remains; keep writers stopped and restore or investigate")
PY
)
```

## 5. Re-enable the writers

Only after the verification succeeds, restore the cron entries commented out in
step 1 and restart every timer that was stopped:

```bash
crontab -e
systemctl start <ingest-or-distill.timer> <synthesis.timer> <curated-memory.timer>
systemctl --user start <ingest-or-distill.timer> <synthesis.timer> <curated-memory.timer>
```

## Daily curated-memory timer (operator-installed)

The committed user-systemd service and timer invoke
`ingest-curated-memory.py` from the pinned runtime checkout. The timer runs
daily and is deliberately not installed or enabled by this repository. An
operator may review the unit files and install them into the user systemd
directory when the runtime is ready.
