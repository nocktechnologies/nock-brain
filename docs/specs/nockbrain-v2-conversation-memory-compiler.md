# Spec: NockBrain v2 Conversation Memory Compiler

## Objective
NockBrain v2 turns saved agent conversations into durable, auditable memory without replaying whole transcripts into context.

The first target is Claude Code JSONL history from `~/.claude/projects/**/*.jsonl`, using Mira and `claude-remote-manager` as the dogfood corpus. The product should extract evidence-backed facts, decisions, corrections, user directives, workflow lessons, and agent behavior patterns, then route them into the existing NockBrain memory spine: fact store, synthesized insights, supersession, budget-capped recall, and Claude Code prompt injection.

Success means a user or agent can ask, "What did we decide?", "What did this agent do?", or "What lesson keeps recurring?" and get compact current answers with source anchors, privacy guarantees, and clear review gates.

## Assumptions
- NockBrain v2 builds inside the existing `nocktechnologies/nock-brain` repository.
- The current v1 fact, synthesis, recall, supersession, and hook flow remains the memory spine.
- Claude Code raw JSONL is the first ingest adapter; Codex/Hermes stores are later adapters.
- `claude-remote-manager` is the first integration target and fixture corpus, not the product boundary.
- The MVP is local-first and stdlib-first unless a later approved phase explicitly adds dependencies.
- Promotion to `CLAUDE.md`, `AGENTS.md`, agent identity files, rules, or skills requires review.

## Tech Stack
- Language: Python 3.10+.
- Runtime: local CLI scripts and Claude Code hooks.
- Current dependency policy: stdlib-only for the core v1 path; pytest for tests.
- Storage today: JSON files under `~/.nock-brain/`.
- Storage target for v2: keep JSON exports stable; introduce SQLite only after the raw ingest schema is proven.
- Integrations: Claude Code JSONL, memsearch Markdown summaries, NockCC tracking, Obsidian-compatible Markdown, Graphify-compatible graph export.

## Existing v1 Inventory
NockBrain v1 already provides the critical recall spine:

- Keep: `bin/extract-facts.py` for Markdown summary facts.
- Keep: `bin/synthesize.py` for recurring-fact insights.
- Keep: `bin/budget-recall.py` for BM25 ranking, confidence filtering, supersession filtering, and token budgets.
- Keep: `bin/recall-classifier.py` for prompt-time recall gating.
- Keep: `bin/supersede-fact.py` for stale decision handling.
- Keep: `hooks/memory-inject.sh` as the default Claude Code injection path.
- Wrap: v1 fact schema with v2 evidence and source-anchor fields.
- Wrap: v1 extraction so raw JSONL refinement can produce compatible facts.
- Replace: Markdown-only transcript assumptions in the default ingest path.
- Add: raw JSONL session parser, evidence-event model, secret scrubber, ingest denylist, review queue, session-note writer, and optional graph/vault exports.

## Commands
Current commands must continue to work:

```bash
python3 bin/extract-facts.py
python3 bin/query-facts.py "what did we decide about pricing"
python3 bin/budget-recall.py --budget 800 "what happened with auth"
python3 bin/recall-classifier.py --test
python3 bin/supersede-fact.py --search "old approach" --mark-superseded --reason "direction changed"
pytest -q
```

Current v2 MVP commands:

```bash
python3 bin/ingest-jsonl.py --output ~/.nock-brain/events.jsonl ~/.claude/projects/.../session.jsonl
python3 bin/refine-sessions.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions
python3 bin/review-promotions.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/review
python3 bin/export-obsidian.py --facts ~/.nock-brain/facts.json --sessions ~/.nock-brain/sessions --review ~/.nock-brain/review --vault ~/.nock-brain/vault
python3 bin/export-graph.py --facts ~/.nock-brain/facts.json --output ~/.nock-brain/graph.json
python3 bin/nockbrain-health.py --events ~/.nock-brain/events.jsonl --facts ~/.nock-brain/facts.json --notes-dir ~/.nock-brain/sessions --env-file /path/to/.env --scan-root ~/.nock-brain
```

Future proposed v2 commands:

```bash
python3 bin/import-codex-history.py --output ~/.nock-brain/events.jsonl
python3 bin/import-hermes-history.py --output ~/.nock-brain/events.jsonl
```

Command names are provisional until planning. The contract matters more than the names: ingest raw conversations, refine sessions, extract facts, review promotions, export optional human/graph views, and verify health.

## Project Structure
Current structure:

```text
bin/                  CLI scripts
hooks/                Claude Code hook scripts
tests/                pytest suite
README.md             public product entry point
SKILL.md              Claude Code skill reference
install.sh            local installer
```

Target structure:

```text
bin/                  Thin executable wrappers
nockbrain/            Importable core package once v2 grows past scripts
  ingest/             Transcript adapters and event normalization
  extract/            Fact extraction and schema helpers
  privacy/            Denylist and secret scrub policies
  recall/             Ranking, budget, and prompt injection support
  review/             Human review queue and promotion candidates
  export/             Obsidian and graph exporters
hooks/                Claude Code hook scripts
docs/specs/           Product and implementation specs
docs/decisions/       ADRs
docs/tracking/        NockCC tracking snapshots
tests/                Unit and fixture tests
fixtures/             Sanitized transcript fixtures
```

The package split is a target architecture. The first implementation may keep small stdlib modules under `bin/` while tests force stable behavior.

Implemented MVP scripts:

- `bin/ingest-jsonl.py`: raw Claude Code JSONL to sanitized evidence-event JSONL.
- `bin/refine-sessions.py`: sanitized events to v1-compatible facts and markdown session notes.
- `bin/review-promotions.py`: facts to human-gated promotion candidates.
- `bin/export-obsidian.py`: facts, session notes, and review notes to a derived markdown vault.
- `bin/export-graph.py`: facts to a Graphify-compatible conversation-memory graph.
- `bin/nockbrain-health.py`: local store health, privacy, and recall-readiness report.

## Core Model
Raw conversations are evidence, not memory. V2 introduces an evidence-event layer before fact extraction.

```json
{
  "id": "event_sha",
  "source": {
    "adapter": "claude-jsonl",
    "path": "~/.claude/projects/.../session.jsonl",
    "line": 1234,
    "session_id": "uuid",
    "timestamp": "2026-06-11T04:39:50Z"
  },
  "actor": "assistant|user|tool",
  "surface": "text|tool_use.input|tool_result.content|attachment|system",
  "kind": "message|tool_call|tool_result|external_directive|file_write|api_call|voice_transcript|compaction",
  "content": "sanitized content",
  "metadata": {
    "tool_name": "Bash",
    "tool_use_id": "...",
    "is_sidechain": false,
    "compact_boundary": false
  },
  "privacy": {
    "scrubbed": true,
    "excluded": false,
    "policy_version": "v1"
  }
}
```

Facts remain compact, but they must point back to evidence:

```json
{
  "id": "fact_sha",
  "kind": "decision|directive|correction|bug|architecture|workflow|agent_behavior|identity|task_state|insight",
  "scope": "global|project|agent|repo|file",
  "subject": "mira-nockos",
  "content": "Memory recall should stay budget-capped and use existing hook spine.",
  "status": "current|superseded|stale|disputed",
  "confidence": 0.9,
  "evidence": [
    {
      "event_id": "event_sha",
      "path": "~/.claude/projects/.../session.jsonl",
      "line": 1234
    }
  ],
  "created_at": "2026-06-11T00:00:00Z",
  "last_seen_at": "2026-06-11T00:00:00Z"
}
```

## Claude JSONL Ingest Requirements
The raw JSONL adapter must handle the fields observed in Mira transcripts:

- `type=user|assistant|system|attachment|ai-title|pr-link|last-prompt|mode|permission-mode|queue-operation|file-history-snapshot`.
- Nested `.message.content`, where content may be a string or a list of parts.
- Text parts for prose.
- `tool_use` parts, especially `input` payloads.
- `tool_result` parts, paired by `tool_use_id`.
- `isSidechain` scoping or exclusion.
- `compactMetadata` as a compaction boundary signal.
- `pr-link` as provenance metadata.
- External-message envelopes from Telegram and NockCC.
- Voice-message envelopes whose literal words appear later in transcription tool results.

The first blocker is non-negotiable: tool-call inputs are first-class evidence. The parser must extract Bash heredocs, `Write`/`Edit` contents, API payloads, and outbound message bodies from `tool_use.input`.

## Privacy And Safety
Secret scrubbing happens before derived artifacts are written.

NockBrain v2 has three separate privacy fences:

- Ingest path denylist: excludes content by source file path before event persistence.
- Ingest tool/endpoint denylist: excludes private payloads carried through tool calls before event persistence.
- Extraction scrubber: redacts secret-looking content anywhere that survives ingest, including user chat messages.

These fences solve different problems and all three are required. Path denial protects private files. Tool/endpoint denial protects private API and MCP payloads. Content scrubbing protects secrets pasted into ordinary conversation text where no path or tool pattern applies.

The secret scrubber is shared by both extraction paths. JSONL ingest and the v1 markdown extractor must import the same scrub implementation so the default installer path cannot drift behind the v2 path.

The extraction path must reject or redact:

- API keys, bot tokens, bearer tokens, session cookies, private keys, and webhook secrets.
- Values in `KEY=value` env dumps when the key ends in `_API_KEY`, `_TOKEN`, `_SECRET`, or `_PASSWORD`, regardless of token shape.
- Telegram bot tokens and chat credentials.
- `.env` contents unless explicitly transformed into non-secret configuration facts.
- File contents from denied paths.

Ingest denylist runs before event persistence. Some material should never enter `events`, `memory.db`, `facts.json`, vault notes, graph exports, or review queues.

Default denied patterns for the dogfood corpus:

```text
agents/*/private/**
**/diary-register*
.env
.env.*
**/.env
**/.env.*
*token*
**/*token*
*secret*
**/*secret*
credentials*
**/credentials*
id_rsa*
**/id_rsa*
*.pem
**/*.pem
```

Path denylist matching normalizes obvious shell/path variants before comparing: stripped quotes, leading slash removal, leading `./` removal, basename-only comparison, and case-insensitive glob matching. This catches cases such as `cat .env`, `logs/MYTOKEN.txt`, and `client.PEM`.

Default denied tool and endpoint patterns for the dogfood corpus:

```text
nockcc_diary_*
nockcc_private_*
*/api/brain/diary/*
*/api/brain/private/*
```

Tool/endpoint denial applies to `tool_use.input`, API payloads, MCP arguments, curl bodies, and other structured action payloads. Denied payload content must be dropped or reduced to aggregate counts at ingest. This matters because the v2 parser intentionally treats tool inputs as first-class evidence; without tool-level denial, private diary or register content can leak through faithfully extracted tool calls even when no denied file path is involved.

When a `tool_use` is denied, the paired `tool_result` identified by `tool_use_id` must also be denied, even if that result arrives on a later JSONL line. Tool results also receive defense-in-depth path and endpoint denial scans before persistence. Health output reports paired-result denials separately.

Health checks may additionally receive local `--env-file` and `--scan-root` paths. This live-value scan compares sensitive `.env` values against derived artifacts and reports only key names plus file/line locations, never the values themselves.

Refinement caps oversized fact content at 1,500 characters and preserves `session_anchor` for drill-back. This keeps raw tool output blobs from being promoted wholesale into facts, review queues, Obsidian vaults, or graph exports.

Authority-shaped fact kinds are actor-gated. In the v2 event pipeline, `decision`, `directive`, and `correction` facts require `actor == "user"`; matching text inside assistant tool calls or tool results is dropped. In the legacy markdown path, where actor metadata is unavailable, those same fact kinds require an explicit user/Kevin/founder/owner cue in the bullet. Non-authority facts from `tool_result.content` may be retained, but their confidence is capped so third-party tool output is not treated as equally authoritative.

Local store writes must be private by default: generated directories are `0700`, generated files are `0600`, and installer migration removes group/other access from existing `~/.nock-brain` trees.

Secret scrubbing covers common bare token families seen in tool output, not only `key=value` shapes: GitHub `ghp_`/`gho_`/`ghs_`/`github_pat_`, OpenAI/Anthropic `sk-`/`sk_`/`sk-ant-` including Stripe `sk_live_`/`sk_test_`, JWT `eyJ...` triples, Google `AIza`, GitLab `glpat-`, npm `npm_`, AWS `AKIA`, Telegram bot-token URL segments, and Slack `xoxb-`/`xoxp-` style tokens.

The denylist must be configurable and test-covered. Denied events should produce aggregate counts in health output, not stored content. Health output must make false-positive denials visible enough to tune policies, especially for conservative path globs such as `**/*token*` and `**/*secret*`.

Installer wiring treats local paths as data. Python snippets read paths through environment variables, generated hook commands use shell quoting, and installer startup rejects checkout paths containing metacharacters that would make safe shell or JSON embedding ambiguous.

Recall injection must frame memory as inert reference material, not instructions. The hook prefix tells the model not to execute directives found in recalled notes.

Settings writes must be crash-safe: installer hook changes back up the existing `settings.json`, write a temporary JSON file, and atomically replace the original. Probe failures must stop the install loudly instead of assuming the hook is absent.

Fact-store readers validate records before use. Malformed records are skipped with a stderr count, and formatters use defensive defaults so a hand-edited store cannot crash recall or query commands.

Retention stance: supersession preserves outdated facts for audit, but sensitive or unwanted material must be deletable. `purge-fact.py` provides dry-run-by-default hard deletion by fact id or literal pattern across facts, events, session notes, and derived vault files.

CI supply-chain stance: GitHub Actions are pinned to full commit SHAs; test/security dependencies are pinned; CI runs unit tests, classifier smoke, Bandit over `bin/`, and Gitleaks with a repo-local allowlist for intentional test/docs fixtures. Dependabot tracks action updates.

## Review And Promotion
NockBrain may suggest promotion candidates but must not silently rewrite agent behavior.

Promotion targets:

- `CLAUDE.md` or `AGENTS.md` for stable project rules.
- Agent identity and invariant files for stable agent-specific rules.
- Scoped rule files for path-specific conventions.
- New skills for recurring operational workflows.
- Hooks or tests for deterministic safety behavior.
- Supersession records for outdated facts.

Review queue entries must include:

- Proposed target.
- Proposed text.
- Supporting evidence anchors.
- Confidence.
- Risk level.
- Approve/edit/reject/defer action.

## Obsidian And Graph Outputs
Obsidian is a human-auditable view, not the source of truth.

Target vault shape:

```text
vault/
  sessions/
  projects/
  agents/
  people/
  concepts/
  review/
```

Graph export is a derived map for tools like Graphify. Nodes may include sessions, facts, agents, projects, files, concepts, decisions, bugs, skills, and rules. Edges may include `DERIVED_FROM`, `SUPPORTS`, `SUPERSEDES`, `CONTRADICTS`, `AFFECTS_AGENT`, `PROMOTED_TO`, `RENAMED_TO`, and `MENTIONS`.

Graphify remains the code-map layer; NockBrain v2 owns the conversation-memory graph and may cross-link to code graph nodes later.

## Code Style
Keep the current project bias: small Python modules, explicit data dictionaries, clear pure functions, and no network calls in core parsing tests.

Example style:

```python
def normalize_parts(message: dict) -> list[dict]:
    content = message.get("content", "")
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content}]


def event_source(path: Path, line_number: int, raw: dict) -> dict:
    return {
        "adapter": "claude-jsonl",
        "path": str(path),
        "line": line_number,
        "session_id": raw.get("sessionId", ""),
        "timestamp": raw.get("timestamp", ""),
    }
```

Guidelines:

- Prefer pure parsing functions with fixture inputs and deterministic outputs.
- Keep CLI wrappers thin.
- Keep source anchors on every derived object.
- Avoid broad dependencies until the parser, scrubber, and fact schema are stable.
- Do not add LLM calls to MVP extraction.

## Testing Strategy
Use pytest. The v2 test suite must include sanitized fixtures for each observed transcript shape.

Required test categories:

- JSONL line parsing: strings, content arrays, text parts, tool calls, tool results.
- Tool input extraction: Bash heredocs, `Write` contents, API payloads.
- Sidechain scoping: sidechain events excluded or labeled according to config.
- Compaction metadata: boundaries captured as session signals.
- Voice pairing: `.ogg` envelope joined to transcription result.
- Secret scrub: known token formats redacted before any derived write.
- Secret scrub fixture: a live-credential-shaped token pasted by the user into ordinary chat text is redacted even though no path or tool deny pattern matches it.
- Ingest denylist: denied paths never persist content.
- Tool/endpoint denylist: private content in `tool_use.input`, MCP arguments, or API payloads never persists.
- Fact compatibility: v2 facts remain consumable by `budget-recall.py`.
- Review queue: candidates include evidence and never auto-promote.
- Backward compatibility: current v1 tests continue to pass.

Verification command:

```bash
pytest -q
```

## Boundaries
Always:

- Preserve source anchors.
- Run extraction-time secret scrub before writing derived artifacts.
- Apply ingest-time denylist before persistence.
- Apply tool/endpoint denial to private tool payloads before persistence.
- Keep recall budget-capped.
- Mark uncertainty and confidence.
- Require human approval before promotion changes rules, skills, identity, or hooks.
- Keep the existing v1 recall spine working while v2 ingest is added.

Ask first:

- Adding non-stdlib runtime dependencies.
- Introducing SQLite as the default store.
- Enabling cloud model calls.
- Writing to global Claude/Codex memory.
- Editing agent identity/invariant files.
- Installing or changing hooks.
- Exporting private vaults or graphs outside the local machine.

Never:

- Commit raw transcripts.
- Persist denied private paths.
- Persist denied private tool or endpoint payloads.
- Persist unsanitized secrets into facts, sessions, vault notes, graph exports, or review queues.
- Treat a single unreviewed mention as permanent truth.
- Inject large transcript chunks into context.
- Let extraction silently rewrite agent behavior.

## Success Criteria
- Current v1 commands and tests still pass.
- Raw Claude Code JSONL sessions can be ingested read-only.
- Tool-use inputs are extracted as first-class evidence.
- Sidechain turns are excluded or scoped by policy.
- Compaction boundaries are available as refinement signals.
- Voice directives can be paired with transcription results.
- Secret scrubbing occurs before any derived artifact write.
- Denied private paths never enter persisted event/fact/session stores.
- Denied private tool and endpoint payloads never enter persisted event/fact/session stores.
- Denial health output reports aggregate counts and enough policy labels to tune false positives without exposing denied content.
- Facts include evidence anchors and remain compatible with budget recall.
- Review queue produces actionable promotion candidates without auto-applying them.
- Obsidian and graph outputs are derived, regenerable views.
- `claude-remote-manager` can dogfood the flow without becoming the product boundary.

## Non-MVP
- Fleet-shared memory backend.
- Autonomous promotion.
- Full Obsidian application layer.
- Full Graphify merge.
- Cloud-hosted service.
- LLM-backed synthesis.
- Codex/Hermes ingest adapters.
- Public package extraction from the repo.

## Open Questions
- Should v2 introduce an importable package immediately, or first land as tested scripts and extract package modules once behavior stabilizes?
- Should SQLite be part of the first implementation phase or wait until JSON event/fact schemas stabilize?
- Which exact NockCC project/labels should own v2 tracking?
- What name should the user-facing CLI expose: `nock-brain`, `nockbrain`, or script-level commands?
- Which CRM transcript fixtures can be safely sanitized and committed?

## Phase Planning
Implementation phases are intentionally deferred until this spec is approved. The first phase must be small enough to prove the three blockers: tool-use input parsing, extraction-time secret scrub, and ingest-time privacy denylist.
