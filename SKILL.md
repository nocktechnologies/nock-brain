---
name: "nock-brain"
description: "Memory persistence for Claude Code agents — auto-injects relevant past-session facts when prompts suggest recall is needed. Use when starting sessions, making decisions that should persist, or querying what was decided previously."
---

# nock-brain

Memory layer for Claude Code. Extracts facts from session transcripts, classifies when recall is needed, and auto-injects relevant context within a token budget.

## Quick reference

| Tool | Purpose | When to use |
|------|---------|-------------|
| `extract-facts.py` | Parse transcripts into facts | After sessions, on a schedule |
| `recall-classifier.py` | Check if a prompt needs memory | Automatically via hook |
| `budget-recall.py` | Retrieve facts within token budget | Automatically via hook |
| `query-facts.py` | Search facts manually | When exploring what's stored |
| `supersede-fact.py` | Mark outdated decisions | When direction changes |

## Auto-injection

If the `memory-inject.sh` hook is installed, recall happens transparently on prompts that match trigger patterns (past references, decision recall, entity lookups, thread followups). Operational prompts are filtered out.

## Manual recall

```bash
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

Run `extract-facts.py` after substantive sessions or on a schedule. Facts accumulate in `~/.nock-brain/facts.json` and are deduplicated automatically.

## When to supersede

When a decision is reversed or replaced, mark the old fact:

```bash
python3 bin/supersede-fact.py --search "old approach" --mark-superseded --reason "new direction"
```

This prevents stale memories from overriding current decisions during recall.
