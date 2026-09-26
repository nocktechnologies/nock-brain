# Consumer Memory Explorer Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver an optional local, read-only browser for an explicitly selected Brain store, with a synthetic demo and production BM25 preview.

**Architecture:** Snapshot a fixed source into owner-only temporary storage; browse the parsed snapshot and run production recall in a bounded child with an isolated environment. A stdlib loopback server exposes a capability-protected JSON API and fixed bundled assets. Nothing is imported by or changed in the fleet hook path.

**Tech Stack:** Python 3.10+ stdlib, existing Brain verification/recall helpers, plain HTML/CSS/JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-consumer-memory-explorer-design.md`

## Global Constraints

- No runtime dependency, model/network call, harness requirement, or settings installation.
- No implicit home store; mutually exclusive `--demo` and `--store`; no-mode help before store access. Optional `--verify-key` and `--open`.
- Never read/copy a selected private signing key; verification is selected public file or explicit verification file. Never serialize key documents into API responses.
- Fixed JSON backend only; reject `NOCKBRAIN_STORE=sqlite` or selected `store-v2` marker. No source writes, chmod, caches or logs.
- Scratch directories 0700, files 0600. Snapshot facts, insights, revocations and verification material only; no evidence-file reads.
- Per-file stat before/after plus generation-wide recheck; retry once on drift then report failure. No cross-file transaction claim.
- Limits: 50-row default/100 max; 2,000-character preview query; 16-KiB request; 32-MiB per file; 64-MiB total snapshot; five-second preview timeout.
- Loopback `127.0.0.1` only, allocated port, exact Host and applicable Origin, random launch capability, fixed routes, no CORS, no-store, no private content/query/token logs.
- Content and evidence are untrusted text; use DOM textContent, no external assets.
- Preview uses classifier and `select_recall`; semantic/graph off, max-per-date 4, no agent scope, default budget 800, strict verification false (display this), superseded excluded. Missing/corrupt key and allowed unsigned states visibly distinct.
- Existing defaults and hook contracts unchanged. Contract changes documented in REPO-MAP.

## Review Focus

1. Atomic replacements or symlinks during capture: reject special/symlink inputs and visibly fail repeated drift (Task 1).
2. Missing versus corrupt files, malformed records and duplicate IDs: no false healthy-empty state; stable collection/index handles, notices and unambiguous details (Task 1).
3. Malicious NOCKBRAIN/HOME/PYTHON environment: no source/private/home access and preview parity with fixed displayed settings (Task 2).
4. Foreign websites, content injection, oversized/slow requests: no authenticated data through cross-origin routes; bounded requests and worker timeout (Tasks 2–3).
5. Refresh racing browser requests or preview: one snapshot generation per operation, reject stale detail requests, show fresh identity/time and keep UI recoverable (Tasks 2–3).

## File structure and shared contract

- `bin/_explorer_store.py`: source capture, synthetic demo, verification, read model and cleanup.
- `bin/_explorer_preview.py`: child launcher and worker using production recall.
- `bin/explore-memory.py`: CLI and loopback API lifecycle, fixed assets.
- `web/explorer/index.html`, `style.css`, `app.js`: accessible offline workspace.
- `tests/test_explorer_store.py`, `test_explorer_preview.py`, `test_explorer_http.py`: behavior and isolation checks.
- `docs/memory-explorer.md`, `README.md`, `docs/REPO-MAP.md`: operating instructions and contracts.

`ExplorerStore(store: Path | None = None, *, demo: bool = False, verify_key: Path | None = None)` is a context manager; constructor rejects ambiguous modes. `refresh() -> dict`, `summary() -> dict`, `list_records(*, query='', kind='', lifecycle='current', collection='', offset=0, limit=50) -> dict`, `detail(handle: str) -> dict | None`, `close()`. `snapshot_dir: Path` and `snapshot_id: str` are service-private. The HTTP server serializes refresh, preview and browse through one lock, so the store need not support concurrent mutation. Initialization captures once. Errors use `ExplorerError(ValueError)` with safe fixed messages.

Summary schema: `{snapshot_id, captured_at, mode: 'demo'|'store', store: str, state: 'missing'|'empty'|'unreadable'|'readable', counts: {facts, insights}, kinds: [str], verification: {state: 'available'|'missing'|'invalid', counts: dict}, files: {filename: {state, count}}, notices: [str]}`. A failed refresh surfaces a failure and does not label a previous generation as fresh. Snapshot source corruption may produce an unreadable summary for inspection, but preview refuses unreadable inputs. Browser must show all notices.

Records: `{handle: 'facts:0', collection: 'facts'|'insights', id: str, kind: str, content: str, status: str, lifecycle: 'current'|'superseded'|'inactive'|'revoked'|'invalid', source_date: str, confidence: number|null, verification: str, details: dict}`. Detail includes complete content and allowlisted stored metadata: dates/validity, confidence, evidence, source, parents, superseded_by and v2 authority identifiers; never arbitrary top-level private fields. Do not mutate signed records. Missing verification is `unavailable`; malformed is `invalid`; valid verification states use engine strings. Detail additionally has `links: [{id, handle, relation}]` for available supersession targets. List omits `details` and returns `{snapshot_id, items, total, offset, limit}`. Lifecycle `all` explicitly includes superseded; current is default. Search case-insensitive over id/content, kind exact; collection empty means both. Pagination and parameter errors are explicit.

HTTP contract:

| Route | Request | Response |
|---|---|---|
| GET `/` `/app.js` `/style.css` | exact Host, safe origin | bundled fixed assets only; no store data |
| GET `/api/summary` | bearer capability | summary |
| GET `/api/records` | bearer; query/kind/lifecycle/collection/offset/limit | list |
| GET `/api/record?handle=…&snapshot_id=…` | bearer; current generation required | detail or 404; stale generation 409 |
| POST `/api/refresh` | bearer; JSON `{}` | new summary |
| POST `/api/preview` | bearer; JSON `{query, budget:800, snapshot_id}` | preview or visible error |

Use `Authorization: Bearer <random>`; launch URL carries capability in `#token=…`, JS removes fragment and holds token in memory. No cookies or persistent browser storage. Origin absent allowed for non-browser clients; present must equal server origin. Reject any `Sec-Fetch-Site: cross-site`. API errors JSON `{error: str}` with appropriate 400/403/404/409/413/503/504. `create_server(store) -> HTTPServer` binds loopback port0 and exposes `.capability` and `.origin`. `main(argv=None) -> int`; no mode returns help and zero. CLI exits cleanly on SIGINT/SIGTERM and owns only its temporary files.

`run_preview(snapshot_dir: Path, query: str, budget: int=800, *, timeout: float=5.0) -> dict` consumes only the completed scratch snapshot. Use a minimal subprocess environment with HOME=private child scratch, no inherited NOCKBRAIN/PYTHONPATH, explicit public-key path (even when missing), JSON forced, all optional tiers disabled. Child executes `_explorer_preview.py` by absolute path, request on stdin, safe error response only, stderr captured but not echoed. Reuse `select_recall` and `format_fact`; do not rank independently. Return `{classifier:{eligible,reason,categories}, settings:{budget,semantic:false,graph:false,max_per_date:4,strict_verify:false,agent_scope:null}, items:[{id,kind,content,source_date}], rendered:str, tokens_used:int, matches:int, truncated:bool, notices:[str]}`. Header/footer formatting agrees with `budget_recall`; derive warning messages from counts/key state without returning raw worker stderr.

### Task 1: Safe store snapshots and synthetic demo

**Files:** Create `_explorer_store.py` and `tests/test_explorer_store.py`.

**Interfaces:** Produce `ExplorerStore` and `ExplorerError` exactly as above; consume `_sign.sign_facts`, `load_public_key`, `verify_facts`, `_revoke.audit`, `_facts.fact_currently_valid` and secure write helpers, always with explicit scratch paths. Demo key can be a disposable HMAC `SigningKey` created in memory; publish only its verification material in scratch.

- [ ] Write a failing hermetic demo test and run it:

```python
def test_demo_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'unrelated-home'))
    with ExplorerStore(demo=True) as store:
        assert store.summary()['mode'] == 'demo'
        assert store.summary()['state'] == 'readable'
        assert store.summary()['verification']['state'] == 'available'
        assert store.list_records()['items']
        scratch = store.snapshot_dir
    assert not scratch.exists()
    assert not (tmp_path / 'unrelated-home').exists()
```

- [ ] Implement the context-manager skeleton and bounded snapshot helpers. Use `os.open` with `O_NOFOLLOW`/`O_NONBLOCK`, `fstat` regular-file checks, bounded reads, path identity checks and retry. All files must be checked before and after the set is copied. Explicit permission and parsing errors become notices, never exception text that contains contents. Reject SQLite selection before capture. Missing optional inputs are represented accurately. Demo: at least eight fictitious Willow Workshop records including a decision, correction, superseded decision, preference, unsigned note and insight; sign through `sign_facts`; include trusted synthetic supersession event.
- [ ] Parse valid list roots, expose malformed-record notices without crashing; validate identifier types before verification. Verify per collection with engine helpers. Audit revocations, mark trusted revoked facts separately from signature status. Never follow evidence references. Preserve full stored content in detail. Add tests for search/kind/lifecycle, duplicate handles, missing/empty/corrupt stores, wrong roots, invalid key, missing key, unsigned/tampered/revoked, no read of private key (a sentinel or symlink), symlink/FIFO refusal, bounds and one-retry drift. Capture `{relative path: (bytes, mtime_ns, mode)}` before/after browse/refresh and compare.
- [ ] Run `python3 -m pytest tests/test_explorer_store.py -q`, self-review source and tests, commit only these files; report exact commands/results and any concern.

### Task 2: Isolated production preview and authenticated local service

**Files:** Create `_explorer_preview.py`, `explore-memory.py`, `tests/test_explorer_preview.py`, `tests/test_explorer_http.py`.

**Interfaces:** Consume Task 1 contract; produce `run_preview`, `create_server`, HTTP contract and CLI above. Fixed web asset paths may return 404 until Task 3 adds them. No changes to production ranking or renderer modules.

- [ ] Write a failing production parity/isolation test:

```python
def test_preview_uses_selected_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('NOCKBRAIN_AGENT_SCOPE', 'unrelated')
    monkeypatch.setenv('NOCKBRAIN_STORE', 'json')
    with ExplorerStore(demo=True) as store:
        result = run_preview(store.snapshot_dir, 'what did we decide about delivery', 800)
        assert result['classifier']['eligible'] is True
        assert result['settings']['semantic'] is False
        assert result['settings']['graph'] is False
        assert result['items']
```

- [ ] Implement the child runner with `subprocess.run(..., timeout=timeout, capture_output=True, env=env)` and a private per-preview work directory; copy only selected snapshot inputs, bound output processing and clean on timeout. Worker uses real classify/select and formatting. Compare selected IDs, rendered output and tokens with production under identical scratch and fixed settings; classifier skip still yields separately labeled preview matches. Test corrupt/missing verification, revoked/tampered exclusions, child timeout, query/budget validation and hostile env isolation. Never return raw exceptions/stderr.
- [ ] Write HTTP tests using a daemon server thread, `http.client` and synthetic stores only. Test missing/wrong capability, bad Host/Origin, no CORS, no-store/CSP, body/query bounds, negative offsets, unknown routes, traversal, stale generation, refresh and preview. Assert no content/token in captured request logs. Slow/read bounds must not let one socket block all server progress indefinitely. Use bounded handler sockets and a threaded server, serialize actual store operations.
- [ ] Implement CLI argument parsing, explicit modes, auto port, optional browser open and signal cleanup. On no mode, do not instantiate ExplorerStore; cover with monkeypatch/sentinel. Test SQLite refusal, no-mode safe output and process termination removes only owned scratch. No live-store invocation in tests.
- [ ] Run `python3 -m pytest tests/test_explorer_store.py tests/test_explorer_preview.py tests/test_explorer_http.py -q`, self-review and commit only task files; report test evidence.

### Task 3: Quiet offline workspace and complete verification

**Files:** Create `web/explorer/index.html`, `style.css`, `app.js`, `docs/memory-explorer.md`; modify `README.md` and `docs/REPO-MAP.md`.

**Interfaces:** Consume exact API above. Use `.impeccable.md` design context. No new API or request file paths; no dependencies/CDNs. The service handles source reads; UI never follows evidence links.

- [ ] Build a semantic HTML shell with store identity and snapshot strip, health/notices region, Memories/Recall preview controls, search/kind/lifecycle/collection filters, paginated selectable rows and detail pane. Use `<button>` and form controls with labels, skip link, visible focus, status live region. Only source text via `textContent`; counts and empty/error/loading states stay visible. Responsive below 760px: detail becomes stacked, headings wrap, no horizontal body overflow.
- [ ] Implement fetch helper with in-memory token, safe JSON errors, loading state and stale-response protection. Clear old selection on refresh. Detail fetch includes current snapshot id. Preview form includes prompt and budget, displays classifier eligibility, settings, selected items, rendered text and approximate tokens. Show 'BM25 preview', optional tiers off and 'Preview, not injection history'. Explain confidence as a stored score. Include synthetic example prompt. Render notices and missing/unreadable states without a fake empty success.

```javascript
function textNode(tag, value, className = '') {
  const node = document.createElement(tag);
  node.textContent = value == null ? '' : String(value);
  node.className = className;
  return node;
}
```

- [ ] Add local-user documentation with both launch commands, explicit JSON selection, read-only/snapshot limits, pub-key/HMAC caveat, unavailable cryptography notice, refresh behavior, shutdown and demo isolation. Explain this is an evaluation milestone, not a complete consumer distribution. Document all new module/API contracts in REPO-MAP; preserve existing hook documentation.
- [ ] Run syntax and focused tests; launch demo and verify real browser list/detail, keyboard focus, filters, pagination, preview, refresh, narrow screen and literal malicious HTML stored in a synthetic fixture. Capture only synthetic screenshots.
- [ ] Run full `python3 -m pytest -q`, classifier `--test`, recall evaluation `--gate`, Python-floor tests and secret/static checks appropriate to new public code. If Python3.9 unavailable, report the limit instead of claiming the runtime was tested. Inspect git diff; commit task files and validation results.

## Plan self-review

All first-milestone spec paragraphs map to Tasks 1–3; follow-on consumer/bootstrap actions remain excluded. The API and Python names agree across producers and consumers. The five review focus classes have concrete checks above. No fleet data or deployment is needed. User approved continuing with cheaper subagents; use a mid-tier model for multi-file implementation and scoped review, reserving the strongest model for the final branch review.
