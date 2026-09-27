# Purge historical channel-wrapper facts

Run this only after the Nock #10817 code is pinned into `%h/Dev/mira-brain`.
This procedure intentionally operates on the live store only after a dry run on
a private copy reports the audited count (about 70 facts). It does not install
the timer automatically.

```bash
set -eu
BRAIN="$HOME/Dev/mira-brain"
STORE="$HOME/.nock-brain"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

# Back up the two authoritative inputs before any live mutation.
cp -p "$STORE/facts.json" "$STORE/facts.json.bak-wrapper-$stamp"
cp -p "$STORE/insights.json" "$STORE/insights.json.bak-wrapper-$stamp"
chmod 600 "$STORE/facts.json.bak-wrapper-$stamp" "$STORE/insights.json.bak-wrapper-$stamp"

# Copy every input purge-fact.py can inspect. The dry run below never reads or
# writes the live store after this point.
scratch="$(mktemp -d /tmp/nockbrain-wrapper-purge.XXXXXX)"
trap 'rm -rf "$scratch"' EXIT
for file in facts.json insights.json events.jsonl graph.json embeddings.npz; do
  [ -f "$STORE/$file" ] && cp -p "$STORE/$file" "$scratch/$file"
done
for directory in sessions vault; do
  [ -d "$STORE/$directory" ] && cp -a "$STORE/$directory" "$scratch/$directory"
done

# Dry run: expect "would remove about 70 fact(s)". Stop and investigate any
# materially different count; do not pass --apply until it is understood.
python3 "$BRAIN/bin/purge-fact.py" \
  --facts "$scratch/facts.json" \
  --events "$scratch/events.jsonl" \
  --notes-dir "$scratch/sessions" \
  --vault "$scratch/vault" \
  --sidecar "$scratch/embeddings.npz" \
  --content-prefix '<channel ' \
  --content-prefix '[BEGIN UNTRUSTED'
```

Once the copy-only count is accepted, use the same prefix-only selectors on the
live store. This hard-deletes matching facts, their derived references and
vector/cache rows, and records tombstones so a later rebuild cannot re-mint
them.

```bash
python3 "$BRAIN/bin/purge-fact.py" \
  --facts "$STORE/facts.json" \
  --events "$STORE/events.jsonl" \
  --notes-dir "$STORE/sessions" \
  --vault "$STORE/vault" \
  --sidecar "$STORE/embeddings.npz" \
  --content-prefix '<channel ' \
  --content-prefix '[BEGIN UNTRUSTED' \
  --apply

python3 "$BRAIN/bin/synthesize.py" \
  --facts "$STORE/facts.json" \
  --output "$STORE/insights.json" \
  --sign
python3 "$BRAIN/bin/verify-facts.py" --facts "$STORE/facts.json" --strict
```

Confirm no wrapper fact remains and that the recurring attestation/channel/
digest/envelope/event clusters are gone. The check prints only counts and ids,
never fact text.

```bash
python3 - "$STORE/facts.json" "$STORE/insights.json" <<'PY'
import json
import re
import sys

facts = json.load(open(sys.argv[1], encoding="utf-8"))
insights = json.load(open(sys.argv[2], encoding="utf-8"))
prefixes = ("<channel ", "[BEGIN UNTRUSTED")
wrapper_ids = [fact.get("id") for fact in facts
               if isinstance(fact, dict) and str(fact.get("content", "")).startswith(prefixes)]
theme = re.compile(r"\b(attestation|channel|digest|envelope|event)\b", re.I)
insight_ids = [item.get("id") for item in insights
               if isinstance(item, dict)
               and str(item.get("content", "")).startswith("Recurring")
               and theme.search(str(item.get("content", "")))]
print(f"wrapper facts remaining: {len(wrapper_ids)}")
print(f"wrapper-themed recurring insights remaining: {len(insight_ids)}")
if wrapper_ids or insight_ids:
    print("remaining ids:", ", ".join(filter(None, wrapper_ids + insight_ids)))
    raise SystemExit(1)
PY
```

## Daily curated-memory timer (operator installation)

After reviewing the committed unit files, install them into Mira's user
systemd directory. The service invokes the pinned checkout and lets
`ingest-curated-memory.py` resolve its configured curated-memory directory.

```bash
install -D -m 644 "$BRAIN/docs/runbooks/nockbrain-curated-memory.service" \
  "$HOME/.config/systemd/user/nockbrain-curated-memory.service"
install -D -m 644 "$BRAIN/docs/runbooks/nockbrain-curated-memory.timer" \
  "$HOME/.config/systemd/user/nockbrain-curated-memory.timer"
systemctl --user daemon-reload
systemctl --user enable --now nockbrain-curated-memory.timer
systemctl --user start nockbrain-curated-memory.service
systemctl --user list-timers nockbrain-curated-memory.timer
```

The timer runs daily with a bounded randomized delay and catches up once after
downtime. The insight refresh remains a derived, non-gating step of the next
distill; it retries transient synthesis-publication failures before reporting a
loud non-fatal status.
