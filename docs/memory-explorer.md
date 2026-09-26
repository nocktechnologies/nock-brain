# Memory Explorer

Memory Explorer is a local, read-only view of one Nock Brain JSON store. It shows facts and insights, their lifecycle and verification state, stored evidence references, and a BM25 recall preview. It is an evaluation milestone for a consumer Brain, not a complete customer distribution or an edit tool.

## Start with fictitious memories

From the repository root:

```sh
python3 bin/explore-memory.py --demo
```

The demo uses a temporary, owner-only store and disposable verification identity. Its records are synthetic. It needs no existing Brain store, Claude Code harness, model, credentials, or network service. Copy the printed `http://127.0.0.1:…/#token=…` link into a browser, or pass `--open`. Keep the terminal running while you browse; use Ctrl-C to shut down and remove the demo's temporary files.

The link's fragment contains a random launch capability. The page removes that fragment from the address bar and keeps the token only in tab memory. After a reload or new tab, reopen the full link printed in the terminal. Do not publish the link.

## Inspect a chosen store

```sh
python3 bin/explore-memory.py --store /absolute/path/to/customer-brain
```

The path selects exactly one store directory when the process starts. There is no implicit home-store mode: running the command without `--demo` or `--store` prints usage and exits. The viewer reads the authoritative `facts.json` and optional `insights.json` and `revocations.jsonl`; it does not traverse evidence paths or ingest transcripts. SQLite (`brain.db` cutover) is not supported in this milestone. Select a JSON store explicitly. The service binds only to `127.0.0.1` on an available port.

The viewer looks for `signing-key.pub` in the selected store, or you can select a verification file explicitly:

```sh
python3 bin/explore-memory.py --store /absolute/path/to/customer-brain --verify-key /absolute/path/to/verification-file
```

It never reads the selected store's private `signing-key`. Legacy HMAC verification files contain symmetric material, so treat a copied or supplied HMAC verification file as secret. If a verification file is missing, invalid, or cannot be used because the required cryptography support is unavailable, the UI shows the limit; it does not silently label records verified. The UI reports the stored confidence number as a score, not as a calibrated probability.

## Reading the workspace

The source strip names the selected store, its health state, verification availability, and the time of the captured snapshot. `Refresh snapshot` takes a new bounded copy. The visible data does not update continuously, and a failed refresh does not advance the displayed capture time. Missing, empty, and unreadable stores have separate states. All snapshot and preview notices appear in the page.

Use search and the kind, lifecycle, and collection filters to browse pages of 50 records. `Current` is the default lifecycle filter. Choose `All states` or `Superseded` to inspect older decisions. Select a row to see full stored content, dates, evidence references, verification, and any available supersession links. Evidence paths and anchors are shown as text; the viewer never opens referenced files. Stored content is treated as untrusted text.

The **BM25 preview** tab runs the real recall classifier and production selection against the displayed snapshot, with semantic and graph tiers off, a default 800-token budget, and a date-diversity cap of four items per date (overflow is deferred). In the synthetic demo, try “What did we decide about Friday delivery?” It shows classifier eligibility separately from selected matches, plus the rendered text and approximate token cost. The preview represents one selection under its displayed settings; it is not a history of what an agent actually received. If source input is unreadable, preview is unavailable. No preview query is sent to a model or remote service.

The HTTP surface accepts only fixed routes and bundled offline assets. It requires the per-launch capability for data requests and uses exact local Host and Origin checks. The page does not store the capability in cookies or browser storage, load external assets, or send source paths in requests.

## Limits and scope

Each list page contains 50 records by default, with a service maximum of 100. Preview prompts are limited to 2,000 characters, request bodies to 16 KiB, and source snapshot inputs to 32 MiB per file and 64 MiB total. Preview times out after five seconds. Exceeding a limit produces a visible error instead of a partial result labeled complete. Snapshotting retries once if selected files change during capture; independent live writers are not covered by a shared transaction.

The viewer performs no memory edits, approvals, supersessions, purges, hook installation, scheduler changes, model downloads, or settings changes. A later consumer release must still address customer bootstrap, capture and freshness, correction and deletion policy, packaging, and standalone injection.
