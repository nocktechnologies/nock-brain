#!/usr/bin/env python3
"""Corrective re-sign of v2 claim-authority facts that were legacy-signed.

Root cause (N9851): ``_sign.sign_facts`` historically signed EVERY fact with the
legacy ``sign_fact`` scheme. A fact carrying v2 claim authority (any of
``_CLAIM_V2_ONLY_AUTHORITY_FIELDS``, or an attestation with the v2 schema) must
be signed with ``sign_claim_fact_v2`` instead: ``verify_fact``'s legacy branch
rejects a legacy-signed v2-authority fact as ``TAMPERED``, so such facts silently
drop out of recall. A bulk ``sign-facts.py`` run legacy-signed 105 real facts
this way. ``_sign.sign_facts`` is now routed per fact (the pipeline fix); this
script is the one-shot that repairs a store already written with the bad
signatures.

For every fact that is a v2 claim AND whose attestation is not already the v2
schema (i.e. was wrongly legacy-signed, or is unsigned), re-sign with
``sign_claim_fact_v2``. Correctly-signed v2 facts and plain legacy facts are left
untouched, so the run is idempotent — a second run reports zero changes.

DEFAULT is a dry run: it reports how many facts WOULD change (and any that cannot
be re-signed, with the reason) but writes nothing. Pass ``--apply`` to write the
repaired store back via the secure-perm writer.

    python3 bin/resign-v2-authority-facts.py                 # dry run (default)
    python3 bin/resign-v2-authority-facts.py --apply         # write back in place
    python3 bin/resign-v2-authority-facts.py --facts /path/facts.json --apply
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions in
# signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _sign import (  # noqa: E402
    CLAIM_ATTESTATION_V2_SCHEMA,
    ClaimAttestationError,
    DEFAULT_KEY_PATH,
    DEFAULT_PUB_PATH,
    is_v2_claim_fact,
    load_or_create_key,
    sign_claim_fact_v2,
    verify_fact,
)
from _store import secure_write_json  # noqa: E402

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"

# Per-fact action buckets.
ACTION_RESIGNED = "resigned"
ACTION_ALREADY_V2 = "already-v2"
ACTION_CANNOT_RESIGN = "cannot-resign"
ACTION_LEGACY_UNTOUCHED = "legacy-untouched"


def _has_v2_attestation(fact: "dict[str, Any]") -> bool:
    att = fact.get("attestation")
    return isinstance(att, dict) and att.get("schema") == CLAIM_ATTESTATION_V2_SCHEMA


def resign_wrongly_signed_facts(
    facts: "list[dict[str, Any]]", key: "Any"
) -> "dict[str, Any]":
    """Re-sign, IN PLACE, every v2-claim fact not already v2-signed.

    A v2-claim fact (``is_v2_claim_fact``) whose attestation is not the v2 schema
    was wrongly legacy-signed (or is unsigned) and verifies ``TAMPERED``; re-sign
    it with ``sign_claim_fact_v2``. Correctly v2-signed facts and plain legacy
    facts are untouched, so calling this twice is a no-op the second time.

    A fact whose v2 authority contract is malformed makes ``sign_claim_fact_v2``
    raise ``ClaimAttestationError``; that fact is recorded in the
    ``cannot-resign`` bucket (with the reason) and left exactly as-is rather than
    aborting the whole run — the dry run must be able to report every such fact.

    Returns a summary with bucket counts and a per-fact record carrying the
    before/after ``verify_fact`` status.
    """
    facts_by_id = {
        f.get("id", ""): f for f in facts if isinstance(f, dict)
    }
    records: "list[dict[str, Any]]" = []
    counts = {
        ACTION_RESIGNED: 0,
        ACTION_ALREADY_V2: 0,
        ACTION_CANNOT_RESIGN: 0,
        ACTION_LEGACY_UNTOUCHED: 0,
    }
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id = fact.get("id", "")
        if not is_v2_claim_fact(fact):
            counts[ACTION_LEGACY_UNTOUCHED] += 1
            continue

        before = verify_fact(fact, key, facts_by_id=facts_by_id)
        if _has_v2_attestation(fact):
            counts[ACTION_ALREADY_V2] += 1
            records.append(
                {
                    "id": fact_id,
                    "action": ACTION_ALREADY_V2,
                    "before": before,
                    "after": before,
                }
            )
            continue

        try:
            sign_claim_fact_v2(fact, key)
        except ClaimAttestationError as exc:
            counts[ACTION_CANNOT_RESIGN] += 1
            records.append(
                {
                    "id": fact_id,
                    "action": ACTION_CANNOT_RESIGN,
                    "before": before,
                    "after": before,
                    "reason": str(exc),
                }
            )
            continue

        after = verify_fact(fact, key, facts_by_id=facts_by_id)
        counts[ACTION_RESIGNED] += 1
        records.append(
            {
                "id": fact_id,
                "action": ACTION_RESIGNED,
                "before": before,
                "after": after,
            }
        )

    v2_total = (
        counts[ACTION_RESIGNED]
        + counts[ACTION_ALREADY_V2]
        + counts[ACTION_CANNOT_RESIGN]
    )
    return {
        "total_facts": sum(1 for f in facts if isinstance(f, dict)),
        "v2_claim_facts": v2_total,
        "resigned": counts[ACTION_RESIGNED],
        "already_v2": counts[ACTION_ALREADY_V2],
        "cannot_resign": counts[ACTION_CANNOT_RESIGN],
        "legacy_untouched": counts[ACTION_LEGACY_UNTOUCHED],
        "records": records,
    }


def _print_summary(summary: "dict[str, Any]", *, applied: bool) -> None:
    mode = "APPLIED" if applied else "DRY RUN (no write)"
    print(f"resign-v2-authority-facts — {mode}")
    print(f"  total facts:        {summary['total_facts']}")
    print(f"  v2 claim facts:     {summary['v2_claim_facts']}")
    print(f"  would re-sign:      {summary['resigned']}")
    print(f"  already v2-correct: {summary['already_v2']}")
    print(f"  cannot re-sign:     {summary['cannot_resign']}")
    print(f"  legacy untouched:   {summary['legacy_untouched']}")

    changed = [r for r in summary["records"] if r["action"] == ACTION_RESIGNED]
    if changed:
        print("\n  re-signed facts (before -> after):")
        for rec in changed:
            print(f"    {rec['id']}: {rec['before']} -> {rec['after']}")

    blocked = [
        r for r in summary["records"] if r["action"] == ACTION_CANNOT_RESIGN
    ]
    if blocked:
        print("\n  CANNOT re-sign (left as-is — needs a fixed v2 contract):")
        for rec in blocked:
            print(f"    {rec['id']}: {rec['before']} — {rec.get('reason', '')}")


def run(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-sign v2 claim-authority facts that were legacy-signed"
    )
    parser.add_argument(
        "--facts",
        type=Path,
        default=DEFAULT_FACTS,
        help="facts.json to repair (default ~/.nock-brain/facts.json)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the repaired store back (default: dry run, no write)",
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=DEFAULT_KEY_PATH,
        help="signing private key path",
    )
    parser.add_argument(
        "--pub",
        type=Path,
        default=DEFAULT_PUB_PATH,
        help="signing public key path",
    )
    args = parser.parse_args(argv)

    if not args.facts.exists():
        print(f"No facts store found at {args.facts}", file=sys.stderr)
        return 1
    try:
        data = json.loads(args.facts.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{args.facts}: malformed JSON ({exc})", file=sys.stderr)
        return 1
    if not isinstance(data, list):
        print(f"{args.facts}: expected a JSON list of facts", file=sys.stderr)
        return 1

    try:
        key = load_or_create_key(args.key, args.pub, create=False)
    except FileNotFoundError as exc:
        print(
            f"Signing key not found ({exc}); refusing to mint a new key for a "
            "corrective re-sign. Point --key/--pub at the existing signing key.",
            file=sys.stderr,
        )
        return 1
    summary = resign_wrongly_signed_facts(data, key)
    _print_summary(summary, applied=args.apply)

    if args.apply:
        if summary["resigned"]:
            secure_write_json(
                args.facts, data, indent=2, ensure_ascii=False
            )
            print(f"\nWrote {args.facts} ({summary['resigned']} fact(s) re-signed)")
        else:
            print("\nNothing to re-sign; store left unchanged.")
    else:
        print("\nDry run only — re-run with --apply to write these changes.")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
