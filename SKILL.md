---
name: "nock-brain"
description: "Memory persistence for Claude Code agents — auto-injects relevant past-session facts when prompts suggest recall is needed. Use when starting sessions, making decisions that should persist, or querying what was decided previously."
---

# nock-brain

Memory layer for Claude Code. Extracts facts from session transcripts, classifies when recall is needed, and auto-injects relevant context within a token budget.

## Quick reference

| Tool | Purpose | When to use |
|------|---------|-------------|
| `ingest-jsonl.py` | Normalize raw Claude Code JSONL into sanitized evidence events | When importing saved Claude conversations |
| `refine-sessions.py` | Convert sanitized events into v1-compatible facts and session notes | After JSONL ingest |
| `review-promotions.py` | Generate human-gated promotion candidates | Before turning memories into rules, skills, hooks, or identity changes |
| `export-obsidian.py` | Export a derived markdown vault | When auditing or browsing memory manually |
| `export-graph.py` | Export a Graphify-compatible memory graph | When exploring fact/session/source/concept relationships |
| `nockbrain-health.py` | Report event, fact, privacy, note, and recall readiness | Before relying on a memory store |
| `extract-facts.py` | Parse transcripts into facts | After sessions, on a schedule |
| `recall-classifier.py` | Check if a prompt needs memory | Automatically via hook |
| `budget-recall.py` | Retrieve facts within token budget | Automatically via hook |
| `query-facts.py` | Search facts manually | When exploring what's stored |
| `supersede-fact.py` | Mark outdated decisions | When direction changes |

## Auto-injection

If the `memory-inject.sh` hook is installed, recall happens transparently on prompts that match trigger patterns (past references, decision recall, entity lookups, thread followups). Operational prompts are filtered out.

## Manual recall

```bash
python3 bin/ingest-jsonl.py --output ~/.nock-brain/events.jsonl ~/.claude/projects/.../session.jsonl
python3 bin/refine-sessions.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions
python3 bin/review-promotions.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/review
python3 bin/export-obsidian.py --facts ~/.nock-brain/facts.json --sessions ~/.nock-brain/sessions --review ~/.nock-brain/review --vault ~/.nock-brain/vault
python3 bin/export-graph.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/graph.json
python3 bin/nockbrain-health.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions
python3 bin/query-facts.py "what was decided about pricing"
python3 bin/query-facts.py --kind decision --since 2026-05-18
python3 bin/budget-recall.py --budget 800 "auth migration status"
```

## Fact kinds

| Kind | What it captures |
|------|-----------------|
| `decision` | Choices made, approaches selected |
| `directive` | Instructions from the user |
| `correction` | Mistakes caught and fixed |
| `merge` | PRs merged |
| `dispatch` | Work assigned to agents |
| `architecture` | Schema/design changes |
| `bug` | Bugs found and fixed |
| `config` | Configuration changes |
| `content` | Content decisions |

## When to extract

Run `extract-facts.py` after substantive markdown-summary sessions or on a schedule. For saved Claude Code JSONL, run `ingest-jsonl.py` first and then `refine-sessions.py`. Facts accumulate in `~/.nock-brain/facts.json` and are deduplicated automatically.

## When to supersede

When a decision is reversed or replaced, mark the old fact:

```bash
python3 bin/supersede-fact.py --search "old approach" --mark-superseded --reason "new direction"
```

This prevents stale memories from overriding current decisions during recall.

## Promotion safety

Use `review-promotions.py` to propose durable rule or skill changes. Treat the output as a review queue only; do not auto-apply candidates without explicit approval.
