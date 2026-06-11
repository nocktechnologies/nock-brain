# ADR-001: Build NockBrain v2 Inside the Existing nock-brain Repository

## Status
Accepted

## Date
2026-06-11

## Context
NockBrain v1 already exists as `nocktechnologies/nock-brain` and provides a working memory layer for Claude Code agents:

- Markdown transcript fact extraction.
- Synthesized recurring insights.
- BM25 budget-capped recall.
- Recall classification.
- Supersession handling.
- Claude Code prompt-time memory injection.
- Test coverage and CI.

The proposed Conversation Memory Compiler extends this into raw saved-conversation ingest, starting with Claude Code JSONL under `~/.claude/projects/**/*.jsonl`. The first dogfood corpus is Mira and `claude-remote-manager`, where raw transcripts recovered information that lossy memory had reconstructed incorrectly.

The key architectural choice is whether to create a new repository for the compiler or build it as NockBrain v2 inside the existing product repository.

## Decision
Build the Conversation Memory Compiler inside `nocktechnologies/nock-brain` as the NockBrain v2 engine.

`claude-remote-manager` remains the first dogfood adapter, fixture source, and deployment target, but not the product boundary.

## Rationale
- One memory product should have one canonical repo, brand, README, installer, test suite, and issue trail.
- The existing repo already contains the recall spine v2 needs to preserve: facts, insights, supersession, budget-capped recall, and hook injection.
- A sibling repo would fragment NockBrain into "memory product" and "memory compiler" before there is evidence that the boundary needs to exist.
- The engine-first design still fits inside the repo through internal package structure and adapters.
- If a future non-Nock package boundary becomes necessary, it can be extracted after the engine proves stable.

## Alternatives Considered

### Create a new `nockbrain-memory-compiler` repository
Pros:
- Clean greenfield boundary.
- Easier to prototype without touching v1.
- Could be framed as a standalone library from day one.

Cons:
- Splits one memory product across two repos.
- Duplicates packaging, docs, CI, and install paths.
- Makes the current v1 recall spine an integration dependency instead of local code.
- Encourages speculative abstraction before the JSONL parser, scrubber, and review model are proven.

Rejected because the compiler is a v2 expansion of the existing product, not a separate product yet.

### Build inside `claude-remote-manager`
Pros:
- Closest to the first dogfood corpus.
- Easy access to Mira, memsearch, and fleet-specific hooks.

Cons:
- Ties the product to one internal fleet repo.
- Makes public NockBrain users second-class.
- Blurs product logic with CRM operational infrastructure.
- Makes Codex/Hermes/non-Nock adapters harder to reason about.

Rejected because CRM should be a consumer and fixture source, not the product boundary.

### Keep v1 unchanged and only document a manual workflow
Pros:
- Zero implementation risk.
- Uses current facts and recall path.

Cons:
- Does not solve raw JSONL evidence loss.
- Leaves tool-use inputs, secret scrub, privacy denylist, and review queue unimplemented.
- Repeats the failure mode where lossy memory can reconstruct a wrong answer.

Rejected because the raw transcript evidence layer is the point of v2.

## Consequences
- V2 work must preserve current v1 behavior and tests.
- New raw-ingest behavior should be added behind explicit commands and adapters.
- Docs must describe what v2 keeps, wraps, replaces, and adds.
- NockCC tracking should point to the `nock-brain` repo.
- Public README and install flow should not promise v2 behavior until the MVP is implemented.
- The first implementation phase should prove the three blockers before broader exports or product polish.

## Follow-Ups
- File NockCC tracking Nocks for the approved spec and blocker work.
- Review sanitized CRM transcript fixtures before committing them.
- Decide whether the first implementation lands as scripts or as an importable `nockbrain/` package.
