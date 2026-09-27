# Purge historical channel-wrapper facts

Run this only after the Nock #10817 fix is present in the pinned runtime
checkout. This repository does not operate on a live store or alter that pin.

Preview the exact wrapper pattern first. `purge-fact.py` is dry-run by default;
review its count before applying the deletion:

```bash
python3 bin/purge-fact.py --pattern '<channel source='
python3 bin/purge-fact.py --pattern '<channel source=' --apply
run-brain-synthesize.sh
```

The applied purge removes matching facts and their derived references, including
insights. Re-synthesis is required after an applied purge.

## Daily curated-memory timer (operator-installed)

The committed user-systemd service and timer invoke
`ingest-curated-memory.py` from the pinned runtime checkout. The timer runs
daily and is deliberately not installed or enabled by this repository. An
operator may review the unit files and install them into the user systemd
directory when the runtime is ready.
