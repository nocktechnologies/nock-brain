# Recall-eval fixtures

- `recall-eval-store.json` — small signed slice of the fact store the recall eval
  runs against (never the live `~/.nock-brain` store). Rebuild with
  `python3 bin/build-recall-fixture.py`.
- `recall-eval-key.json` / `recall-eval-key.pub` — a **disposable, fixture-only**
  HMAC-SHA256 signing key. It exists only so the fixture verifies hermetically in
  CI without a `cryptography` dependency. HMAC verification is symmetric, so the
  `.pub` holds the same secret — this is intentional and safe: the key has **no
  relationship to the production signing key** and signs nothing but this test
  fixture. (`tests/` is gitleaks-allowlisted; bandit scans `bin/` only.)

See `docs/evals/README.md` for how the eval and its CI gate work.
