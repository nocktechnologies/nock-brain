#!/usr/bin/env python3
"""Mint signed revocation events for legacy supersession marks (S1 backfill).

Facts superseded before S1 carry marks but no signed revocation event, so
strict verification reports them as *unattested*. This one-shot attests them
under today's key. Propose-by-default: lists what would be minted; --apply
appends the events. Never touches the facts store itself — it only writes
revocations.jsonl, so the fail-closed distill contract is untouched.

Usage:
    python3 bin/backfill-revocations.py                 # propose (read-only)
    python3 bin/backfill-revocations.py --apply         # mint + append
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _revoke import (  # noqa: E402
    REVOCATIONS_FILENAME,
    append_revocation,
    audit,
    load_revocations,
    resolve_signing_key,
    sign_revocation,
)
from _storeback import resolve_store  # noqa: E402

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--revocations", type=Path, default=None,
                        help="revocations.jsonl (default: next to the store)")
    parser.add_argument("--apply", action="store_true",
                        help="mint and append events (default: propose only)")
    args = parser.parse_args()

    store = resolve_store(args.facts)
    if not store.freshness_path.exists():
        print(f"No fact store found ({store.describe()}).", file=sys.stderr)
        sys.exit(1)
    key = resolve_signing_key()
    if key is None:
        print("No signing key available; cannot mint revocation events.",
              file=sys.stderr)
        sys.exit(1)

    facts = store.load_facts()
    sidecar = args.revocations or store.freshness_path.parent / REVOCATIONS_FILENAME
    report = audit(facts, load_revocations(sidecar), key)
    unattested = report["unattested_superseded"]

    if not unattested:
        print("Nothing to backfill: every superseded fact is attested.")
        return

    facts_by_id = {str(f.get("id", "")): f for f in facts}
    if not args.apply:
        print(f"Would mint {len(unattested)} revocation event(s):")
        for fid in unattested:
            fact = facts_by_id[fid]
            print(f"  {fid} <- superseded_by {fact.get('superseded_by', '') or '?'}"
                  f" [{str(fact.get('supersession_reason', ''))[:60]}]")
        print("Sidecar untouched. Apply with: backfill-revocations.py --apply")
        return

    minted = 0
    for fid in unattested:
        fact = facts_by_id[fid]
        event = sign_revocation(
            key,
            superseded_id=fid,
            superseding_id=str(fact.get("superseded_by", "") or ""),
            reason=str(fact.get("supersession_reason", "") or "backfill: legacy mark"),
            superseded_at=str(fact.get("superseded_at", "") or "") or None,
        )
        append_revocation(sidecar, event)
        minted += 1

    after = audit(facts, load_revocations(sidecar), key)
    print(f"Minted {minted} signed revocation event(s) -> {sidecar}")
    print(f"After: {after['attested']} attested, "
          f"{after['invalid_events']} invalid, "
          f"{len(after['unattested_superseded'])} unattested, "
          f"{len(after['resurrected'])} resurrected.")
    # invalid_events is the S1 tampering/exit-4 class: a corrupt or tampered
    # sidecar must fail the backfill loudly, not slip through as success.
    if (after["resurrected"] or after["unattested_superseded"]
            or after["invalid_events"]):
        print("WARNING: post-backfill audit is not clean — investigate before "
              "trusting strict verification.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
