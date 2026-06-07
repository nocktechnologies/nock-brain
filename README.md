# nock-brain

Memory layer for Claude Code agents. Extracts durable facts from session transcripts, classifies when recall is needed, and auto-injects relevant context — all within a token budget.

Built by [Nock Technologies](https://nocktechnologies.com) from patterns running a 14-agent autonomous fleet 24/7.

## What it does

Most Claude Code sessions start from zero. nock-brain fixes that.

1. **Extract** — Parses session transcripts into structured facts: decisions, directives, corrections, architecture changes, merges, bug fixes.
2. **Synthesize** — Periodically reviews the fact store, clusters recurring same-kind facts, and writes consolidated *insights* ("you've corrected this 3 times") to a higher tier. This is the consolidation layer that keeps the store from becoming a giant unreadable log. Heuristic and dependency-free by default; structured so an LLM-backed synthesizer can drop in.
3. **Classify** — Determines if a prompt needs past-session context. "What did we decide about X?" triggers recall. "merge PR 223" doesn't.
4. **Recall** — Ranks with **BM25** (IDF-weighted token matching with length normalization) and retrieves the most relevant items within a configurable token budget — **synthesized insights first**, then raw facts — so memory enhances without overwhelming the context window.
5. **Inject** — A Claude Code hook that chains the steps transparently. Relevant context appears as system messages when needed.

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

## Background

nock-brain was extracted from Nock Technologies' internal fleet infrastructure. We run 14 Claude Code agents autonomously — sessions reset, context compacts, agents restart. Without memory persistence, every session starts from zero and re-derives context that was already established.

The recall classifier exists because naive "always retrieve" approaches waste context window on prompts that don't need it. The budget cap exists because unbounded retrieval can push useful context out of the window. The supersession tracker exists because stale decisions are worse than no memory at all.

These aren't theoretical problems. They're bugs we shipped fixes for.

## License

MIT

---

Built by [Nock Technologies](https://nocktechnologies.com) · Part of the [nock-skills](https://github.com/kkwills13/nock-skills) family
