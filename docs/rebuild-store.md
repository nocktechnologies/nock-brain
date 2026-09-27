# rebuild-store — one-command promote-and-migrate (N8070)

`bin/rebuild-store.py` runs the entire proven v2 chain into a **staging**
directory, applies a hard health gate, signs, exports, and only then
**atomically promotes** the result into the live store at `~/.nock-brain`.

## Why this exists

The v2 pipeline is a set of separate, hand-run CLIs
(`ingest-jsonl` → `refine-sessions` → `review-promotions` → `nockbrain-health`
→ `sign-facts` → `export-obsidian` / `export-graph`). Running them by hand, in
order, every week is how the live store silently rotted to a stale May-19 v1
snapshot — recall was dead until a manual rebuild. This command collapses the
chain into one safe, schedulable step so the store can never silently rot again.

## What it does

1. **Ingest** transcripts from `~/.claude/projects` (or any `--source` roots)
   modified within a `--since DAYS` window (default 7) into a **staging** dir —
   never directly into the live store.
2. **Refine** → staging `facts.json` + session notes.
3. **Review** → staging promotion queue.
4. **Health gate (HARD)** on staging. The build **aborts with a non-zero exit
   and leaves the live store completely untouched** if either:
   - `live_secret_findings > 0` (secrets would be promoted), or
   - `recall_ready` is not `true` (no usable facts / malformed store).
   This is the most important safety property: a store with secrets or that is
   not recall-ready is NEVER promoted.
5. **Sign** staging `facts.json` (Ed25519, the live signing key).
6. **Export** the Obsidian vault + graph from the signed staging facts.
7. **Atomic promote**: back up the current live `facts.json`, `sessions`,
   `review`, `vault`, and `graph.json` to timestamped `.bak-<UTC>` paths, then
   move the staging artifacts into place (secure perms via `_store.py`).

## Usage

```bash
# Build + promote with the default 7-day window:
python3 bin/rebuild-store.py

# Build + health-gate + sign + export into staging, but DO NOT swap:
python3 bin/rebuild-store.py --dry-run

# Wider window / extra source roots:
python3 bin/rebuild-store.py --since 14 --source ~/.claude/projects --source /other/root

# Emit an example weekly launchd plist + cron line (installs nothing):
python3 bin/rebuild-store.py --print-schedule
```

## Scheduling (not auto-installed)

`--print-schedule` prints a ready-to-save launchd plist and an equivalent cron
line for a weekly run. Installation is the operator's call:

```bash
python3 bin/rebuild-store.py --print-schedule > /tmp/nockbrain-rebuild.plist
# inspect, then if desired:
cp /tmp/nockbrain-rebuild.plist ~/Library/LaunchAgents/io.nocktechnologies.nockbrain-rebuild.plist
launchctl load ~/Library/LaunchAgents/io.nocktechnologies.nockbrain-rebuild.plist
```

A weekly run keeps the live store fresh and signed without any manual step.

## Channel-wrapper remediation (operator-run)

After the ingest fix is deployed to the checkout that owns the live store,
first preview the channel-wrapper purge, then apply it only after reviewing the
match count. Rebuild synthesized insights after the applied purge:

```bash
python3 bin/purge-fact.py --pattern '<channel source='
python3 bin/purge-fact.py --pattern '<channel source=' --apply
run-brain-synthesize.sh
```

This repository does not run those commands against a live store. In
particular, updating a source checkout does not update any separately pinned
runtime checkout.

## Daily curated-memory timer (operator-installed)

Curated Markdown memory can be re-ingested daily with user-level systemd units.
Save the following templates with real absolute paths substituted; review and
install or enable them as an operator decision, not as part of this repository.

`~/.config/systemd/user/nockbrain-curated-memory.service`:

```ini
[Unit]
Description=Ingest curated NockBrain memory

[Service]
Type=oneshot
WorkingDirectory=/absolute/path/to/nock-brain
Environment=NOCKBRAIN_CURATED_DIR=/absolute/path/to/curated-memory
ExecStart=/usr/bin/python3 /absolute/path/to/nock-brain/bin/ingest-curated-memory.py
```

`~/.config/systemd/user/nockbrain-curated-memory.timer`:

```ini
[Unit]
Description=Run curated NockBrain memory ingest daily

[Timer]
OnCalendar=daily
Persistent=true
Unit=nockbrain-curated-memory.service

[Install]
WantedBy=timers.target
```
