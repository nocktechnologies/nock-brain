# nock-brain — session bootstrap

**Read `docs/REPO-MAP.md` before exploring code.** It maps every module, the
end-to-end pipeline, the recall ranking order, the v1/v2 attestation model,
and the known sharp edges — reading it replaces reading most of the tree.

Non-negotiables (details + rationale in the map):

- Never sign facts except through `_sign.sign_facts` — bulk legacy signing
  silently drops v2 claim-authority facts from recall (the N9851 trap, §7).
- Never add an import into the hook-reachable path without updating
  `tests/test_python_floor.py` and confirming Python 3.9 compatibility (§8).
- BM25 is the recall floor: optional tiers (dense, graph) must degrade to the
  unchanged seed list, and their off-paths must be pure pass-throughs.
- Consolidation is mark-only: never rewrite a signed core
  (id+kind+content+evidence); supersessions mint signed revocation events.
- `facts.json` is authoritative; sidecars and exports are derived. The E2
  SQLite store exists but is NOT cut over — `NOCKBRAIN_STORE=json` is the
  kill switch.

When a PR changes a module's *contract*, update `docs/REPO-MAP.md` in the
same PR.
