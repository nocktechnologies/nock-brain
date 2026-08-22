#!/usr/bin/env python3
"""Build the committed, signed recall-eval fixture store from a source store.

DEV / MAINTAINER TOOL, not run in CI. It reads a source fact store READ-ONLY
(default: the live ~/.nock-brain/facts.json) and writes a small, self-contained
slice to tests/fixtures/recall-eval-store.json, re-signed with the disposable
fixture key so `recall-eval.py` runs hermetically with attestation verification
ON. This is how the committed fixture is regenerated deliberately — the fixture
is DERIVED data (ONE SOURCE OF TRUTH: the live store), never a second vault.

The slice is chosen to preserve the two structures the eval measures:
  * the date-diversity CAP LEVER  — all gold facts + up to N same-date
    distractors per gold date, so `max_per_date` binds at the prod budget;
  * COMPANIONSHIP                 — every session-sibling of every gold fact, so
    a hit can (or fail to) arrive with its surrounding session context.

Large / transcript-linked fields (evidence, source_file, subject, created_at,
last_seen_at) are dropped so no live source anchors or secrets land in the repo;
`content` is additionally run through the store's own secret scrubber. The old
attestation is stripped and every fact re-signed via `_sign.sign_facts`, which
routes v2-authority facts to `sign_claim_fact_v2` (the routing whose violation
caused the Aug-2026 40%-exclusion incident).

Usage:
    python3 bin/build-recall-fixture.py                      # from live store
    python3 bin/build-recall-fixture.py --source path/to/facts.json
    python3 bin/build-recall-fixture.py --distractors 10     # per gold date
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
REPO = BIN_DIR.parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

DEFAULT_SOURCE = Path.home() / ".nock-brain" / "facts.json"
GOLD = REPO / "docs" / "evals" / "recall-gold-v1.json"
OUT = REPO / "tests" / "fixtures" / "recall-eval-store.json"
KEY = REPO / "tests" / "fixtures" / "recall-eval-key.json"
PUB = REPO / "tests" / "fixtures" / "recall-eval-key.pub"

# Dropped from NON-v2 facts so no transcript anchors / large blobs / secrets
# reach the repo. None is a v2-authority field; v2 facts keep all fields (their
# evidence list is hash-only and is bound into the signed v2 payload).
DROP_FIELDS = {"evidence", "source_file", "subject", "created_at", "last_seen_at"}

# v2 claim-authority routing predicate (mirrors _sign._CLAIM_V2_ONLY_AUTHORITY_FIELDS).
_CLAIM_V2_FIELDS = frozenset({
    "memory_id", "revision_id", "valid_from", "valid_to", "verify_before_act",
    "promotion_batch_digest", "parent_revision_ids", "revokes_revision_ids",
})


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), BIN_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _non_negative_int(value: str) -> int:
    """argparse type that rejects negatives. A negative --distractors would make
    `extra[:n]` slice from the tail (e.g. -1 keeps nearly all same-date facts),
    silently bloating the fixture with the very date-flood the slice bounds."""
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def session_key(f: dict) -> str:
    return str(f.get("session") or f.get("session_anchor") or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="source store, read-only (default: live store)")
    ap.add_argument("--distractors", type=_non_negative_int, default=10,
                    help="max same-date distractor facts per gold date (>= 0)")
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    if not args.source.exists():
        print(f"source store not found: {args.source}", file=sys.stderr)
        return 2

    _sign = _load("_sign")
    _scrub = _load("_scrub")

    facts = json.loads(args.source.read_text())
    by_id = {f["id"]: f for f in facts if isinstance(f, dict) and "id" in f}

    gold_doc = json.loads(args.gold.read_text())
    gold_ids = list((gold_doc.get("queries") or {}).keys())
    missing = [g for g in gold_ids if g not in by_id]
    if missing:
        print(f"gold ids absent from source: {missing}", file=sys.stderr)
        return 3

    current = [f for f in facts if f.get("status", "current") == "current"]
    by_session: dict[str, list[dict]] = collections.defaultdict(list)
    for f in current:
        s = session_key(f)
        if s:
            by_session[s].append(f)
    by_date: dict[str, list[dict]] = collections.defaultdict(list)
    for f in current:
        by_date[str(f.get("source_date"))].append(f)

    keep: set[str] = set(gold_ids)
    # every session-sibling of every gold fact (companionship structure)
    for g in gold_ids:
        s = session_key(by_id[g])
        if s:
            keep.update(f["id"] for f in by_session[s])
    # up to N same-date distractors per gold date (cap-lever structure)
    gold_dates = {str(by_id[g].get("source_date")) for g in gold_ids}
    for dt in sorted(gold_dates):
        extra = [f["id"] for f in by_date[dt] if f["id"] not in keep]
        keep.update(extra[: args.distractors])

    # deterministic order (stable diffs); gold first then the rest by id
    ordered = [g for g in gold_ids] + sorted(keep - set(gold_ids))

    scrubbed = 0
    slice_facts: list[dict] = []
    for fid in ordered:
        src = by_id[fid]
        # v2-authority facts bind their (hash-only, secret-free) `evidence` list
        # and authority fields into the signed payload, so for them strip ONLY
        # the stale attestation and keep everything else — otherwise
        # sign_claim_fact_v2 fails closed. Non-v2 facts keep only content in the
        # signed core, so their transcript `evidence` anchor and other blobs are
        # dropped to keep the fixture small and free of source anchors.
        if _CLAIM_V2_FIELDS.intersection(src):
            f = {k: v for k, v in src.items() if k != "attestation"}
        else:
            f = {k: v for k, v in src.items()
                 if k not in DROP_FIELDS and k != "attestation"}
        content, n = _scrub.scrub_secrets(str(f.get("content", "")))
        if n:
            scrubbed += n
            f["content"] = content
        slice_facts.append(f)

    # Disposable fixture-only key. HMAC-SHA256 is portable (no `cryptography`
    # dependency in CI) and its verification is symmetric, so the ".pub" is the
    # same secret — it has NO relationship to the production signing key and is
    # safe to commit under tests/ (gitleaks-allowlisted, bandit scans bin/ only).
    key = _sign.load_or_create_key(KEY, PUB, alg="hmac-sha256", create=True)
    signed = _sign.sign_facts(slice_facts, key)

    # Prove the fixture verifies before writing it (catches a signing-routing bug
    # like the 40%-exclusion incident at build time, not in CI).
    vkey = _sign.load_public_key(PUB)
    by_sid = {f["id"]: f for f in signed}
    bad = []
    for f in signed:
        status = _sign.verify_fact(f, vkey, facts_by_id=by_sid)
        if status != _sign.VALID:
            bad.append((f["id"], status))
    if bad:
        print(f"REFUSING to write: {len(bad)} fact(s) do not verify: {bad[:5]}",
              file=sys.stderr)
        return 4

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(signed, indent=1, sort_keys=True) + "\n")

    v2 = sum(1 for f in signed if _sign.is_v2_claim_fact(f))
    sess = len({session_key(f) for f in signed if session_key(f)})
    print(f"wrote {args.out.relative_to(REPO)}: {len(signed)} facts "
          f"({len(gold_ids)} gold, {v2} v2-authority, {sess} sessions), "
          f"{scrubbed} secret span(s) scrubbed, all VALID")
    print(f"key: {KEY.relative_to(REPO)} / {PUB.relative_to(REPO)} "
          f"(disposable fixture key)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
