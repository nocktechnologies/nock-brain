# nock-brain

Memory layer for Claude Code agents. Extracts durable facts from session transcripts, classifies when recall is needed, and auto-injects relevant context — all within a token budget.

Built by [Nock Technologies](https://nocktechnologies.com) from patterns running a 14-agent autonomous fleet 24/7.

## What it does

Most Claude Code sessions start from zero. nock-brain fixes that.

1. **Ingest** — Converts raw Claude Code JSONL into sanitized evidence events, including `tool_use.input` payloads, with source anchors.
2. **Extract** — Parses markdown transcripts or sanitized events into structured facts: decisions, directives, corrections, architecture changes, merges, bug fixes.
3. **Synthesize** — Periodically reviews the fact store, clusters recurring same-kind facts, and writes consolidated *insights* ("you've corrected this 3 times") to a higher tier. This is the consolidation layer that keeps the store from becoming a giant unreadable log. Heuristic and dependency-free by default; structured so an LLM-backed synthesizer can drop in.
4. **Review** — Suggests promotion candidates for durable rules or skills, but never rewrites agent behavior without human approval.
5. **Export** — Writes derived Obsidian vault and Graphify-compatible graph views for audit and exploration.
6. **Classify** — Determines if a prompt needs past-session context. "What did we decide about X?" triggers recall. "merge PR 223" doesn't.
7. **Recall** — Ranks with **BM25** (IDF-weighted token matching with length normalization) and retrieves the most relevant items within a configurable token budget — **synthesized insights first**, then raw facts — so memory enhances without overwhelming the context window.
8. **Inject** — A Claude Code hook that chains the steps transparently. Relevant context appears as system messages when needed.

## Install

```bash
git clone https://github.com/nocktechnologies/nock-brain.git
cd nock-brain && bash install.sh
```

The installer:
- Creates `~/.nock-brain/` for fact storage
- Finds transcript sources (memsearch plugin or local files)
- Extracts facts from existing transcripts
- Wires the auto-injection hook into Claude Code settings

Restart Claude Code after install.

## Requirements

- Python 3.10+
- Claude Code (for the auto-injection hook)
- Session transcripts in markdown format (from [memsearch](https://github.com/zilliztech/memsearch) or your own)
- Optional raw Claude Code JSONL transcripts from `~/.claude/projects/**/*.jsonl`

## Usage

### Auto-injection (recommended)

After install, nock-brain works transparently. When you ask questions like:
- "What did we decide about the pricing model?"
- "Have we seen this bug before?"
- "What happened with the auth migration?"

...relevant facts from past sessions appear automatically in context.

Operational prompts ("merge PR 223", "yes", "dispatch the agent") are filtered out — no noise.

### Manual tools

```bash
# Ingest raw Claude Code JSONL into sanitized evidence events
python3 bin/ingest-jsonl.py --output ~/.nock-brain/events.jsonl ~/.claude/projects/.../session.jsonl

# Refine sanitized events into facts and auditable session notes
python3 bin/refine-sessions.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions

# Generate human-gated promotion candidates
python3 bin/review-promotions.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/review

# Export audit views
python3 bin/export-obsidian.py --facts ~/.nock-brain/facts.json --sessions ~/.nock-brain/sessions --review ~/.nock-brain/review --vault ~/.nock-brain/vault
python3 bin/export-graph.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/graph.json

# Health report
python3 bin/nockbrain-health.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions --env-file /path/to/.env --scan-root ~/.nock-brain

# Extract facts from transcripts
python3 bin/extract-facts.py
python3 bin/extract-facts.py --dir ./my-transcripts --since 2026-05-18

# Query facts
python3 bin/query-facts.py "content strategy"
python3 bin/query-facts.py --kind decision --limit 10
python3 bin/query-facts.py --kind correction --since 2026-05-01

# Budget-capped recall
python3 bin/budget-recall.py --budget 800 "what was decided about pricing"

# Test the classifier
python3 bin/recall-classifier.py --test

# Mark outdated facts as superseded
python3 bin/supersede-fact.py <fact_id> --reason "direction changed"
python3 bin/supersede-fact.py --search "old pricing" --mark-superseded
```

## How it works

### Raw JSONL ingest

`ingest-jsonl.py` reads Claude Code JSONL sessions and normalizes messages, tool calls, tool results, compaction metadata, and PR provenance into evidence events. It preserves source file, line, session id, timestamp, surface, kind, actor, and sanitized content.

The ingest path has three privacy fences:

1. Denied source paths never persist content.
2. Private tool or endpoint payloads, such as diary/private NockCC calls, are dropped before event persistence.
3. Secret-looking strings in surviving content are replaced with `[REDACTED_SECRET]`. This includes value-shape matches and `KEY=value` env dumps where the key ends in `_API_KEY`, `_TOKEN`, `_SECRET`, or `_PASSWORD`.

### Session refinement

`refine-sessions.py` consumes sanitized event JSONL, reuses the same classification rules as markdown extraction, writes v1-compatible `facts.json`, and emits markdown session notes with evidence anchors. Oversized fact content is capped at 1,500 characters with a `session_anchor` drill-back pointer so raw tool output cannot be amplified into review or vault artifacts. The output can be used immediately by `budget-recall.py`.

### Review and exports

`review-promotions.py` writes a human-gated review queue. Entries include proposed target, proposed text, confidence, risk, actions, and evidence. The command never modifies project rules, agent identity, hooks, or skills.

`export-obsidian.py` creates a derived markdown vault with index, facts, sessions, and review notes. `export-graph.py` creates a Graphify-compatible JSON graph with fact, session, source, and concept nodes.

`nockbrain-health.py` summarizes event/fact/note counts, malformed records, privacy redactions, denied payload counts when stats are provided, optional live-value scan findings against local `.env` files, and recall readiness.

### Fact extraction

`extract-facts.py` reads markdown transcript files and identifies facts using two methods:

1. **Tagged facts** — Lines with explicit tags like `[DECISION]`, `[DIRECTIVE]`, `[CORRECTION]` get high confidence (0.9).
2. **Inferred facts** — Pattern matching for decision language ("user decided", "approved", "corrected") at lower confidence (0.7-0.85).

Operational noise (heartbeats, checkpoints, status confirmations) is filtered out. Facts are deduplicated across files.

### Recall classification

`recall-classifier.py` runs in <50ms and checks prompts against five trigger categories:

| Category | Example triggers |
|----------|-----------------|
| Past reference | "last time", "previously", "what did we" |
| Decision recall | "what was decided", "why did we", "is X still current" |
| Entity lookup | PR numbers, agent names + "status/did/built" |
| User context | "user said", "user wants", "their direction" |
| Thread followup | "what happened with", "status of", "where are we on" |

### Budget-capped retrieval

`budget-recall.py` retrieves relevant facts within a token budget (default: 1,000 tokens, max: 1,500). Facts below 0.7 confidence are excluded. Superseded facts are excluded by default. Results are ranked by relevance score then confidence.

### Supersession tracking

When decisions change, mark the old fact as superseded:

```bash
python3 bin/supersede-fact.py --search "old approach" --mark-superseded --reason "direction changed"
```

Superseded facts are excluded from recall by default but can be included with `--include-superseded` for audit trails.

## Transcript format

nock-brain reads markdown files with bullet-point summaries. Compatible with:

- **memsearch plugin** transcripts (`~/.memsearch/memory/*.md`)
- **Claude Code session summaries** (any markdown with `- ` bullet points)
- **Custom transcripts** — any markdown where each `- ` line is a session event
- **Claude Code JSONL** via `ingest-jsonl.py` followed by `refine-sessions.py`

Example:
```markdown
## Session 14:30
- User decided to use PostgreSQL instead of SQLite for the auth service
- [DECISION] Pricing model locked at $29/mo for the pro tier
- Merged PR #45 — auth middleware refactor
- [BUG] Found race condition in the session handler, fixed in commit abc123
```

## File structure

```
nock-brain/
  bin/
    ingest-jsonl.py       # Normalize raw Claude Code JSONL into sanitized evidence events
    refine-sessions.py    # Convert sanitized events into facts and session notes
    review-promotions.py  # Generate human-gated promotion candidates
    export-obsidian.py    # Write a derived markdown vault
    export-graph.py       # Write a Graphify-compatible memory graph
    nockbrain-health.py   # Report local store health
    extract-facts.py      # Parse transcripts into structured facts
    synthesize.py          # Consolidate recurring facts into insights
    query-facts.py         # Search and filter facts
    budget-recall.py       # Token-budgeted retrieval (insights first)
    recall-classifier.py   # Classify prompts for recall need
    supersede-fact.py      # Mark outdated facts
  hooks/
    memory-inject.sh       # Claude Code auto-injection hook
  tests/                   # pytest suite for the extraction + recall pipeline
  install.sh               # One-command setup
  SKILL.md                 # Claude Code skill reference
  README.md
  LICENSE
```

## Development

The bin/ scripts are dependency-free (Python 3.11+ stdlib only). Run the tests:

```bash
pip install pytest
pytest -q
python3 bin/recall-classifier.py --test   # classifier smoke test
```

CI runs the suite on every push and pull request (`.github/workflows/ci.yml`).

## Configuration

Facts are stored at `~/.nock-brain/facts.json`. Override with `--facts` or `--output` flags.

Transcript sources are auto-detected:
1. `~/.memsearch/memory/` (memsearch plugin)
2. `~/.nock-brain/transcripts/` (manual placement)
3. Custom path via `--dir`

Raw Claude Code JSONL is intentionally explicit for now:

```bash
python3 bin/ingest-jsonl.py --output ~/.nock-brain/events.jsonl ~/.claude/projects/.../session.jsonl
python3 bin/refine-sessions.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions
```

## Background

nock-brain was extracted from Nock Technologies' internal fleet infrastructure. We run 14 Claude Code agents autonomously — sessions reset, context compacts, agents restart. Without memory persistence, every session starts from zero and re-derives context that was already established.

The recall classifier exists because naive "always retrieve" approaches waste context window on prompts that don't need it. The budget cap exists because unbounded retrieval can push useful context out of the window. The supersession tracker exists because stale decisions are worse than no memory at all.

These aren't theoretical problems. They're bugs we shipped fixes for.

## License

MIT

---

Built by [Nock Technologies](https://nocktechnologies.com) · Part of the [nock-skills](https://github.com/kkwills13/nock-skills) family
