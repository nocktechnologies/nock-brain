#!/usr/bin/env python3
"""Verify the tamper-evident attestations on a NockBrain facts.json (N8068).

Reports counts: valid / TAMPERED / unsigned / parent-suspect. Exits non-zero if
ANY fact is tampered (the security gate). Unsigned facts are reported but do not
by themselves fail the run (backward-compat with stores not yet signed); pass
``--strict`` to also fail when any fact is unsigned.

Usage:
    python3 bin/verify-facts.py --facts ~/.nock-brain/facts.json
    python3 bin/verify-facts.py --facts facts.json --json
    python3 bin/verify-facts.py --facts facts.json --strict
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _revoke import (  # noqa: E402
    REVOCATIONS_FILENAME,
    audit as revocation_audit,
    blocking_findings,
    load_revocations,
)
from _sign import (  # noqa: E402
    PARENT_SUSPECT,
    TAMPERED,
    UNSIGNED,
    VALID,
    load_public_key,
    resolve_verify_key,
    verify_facts,
)

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify facts.json attestations")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS,
                        help="facts.json to verify (default ~/.nock-brain/facts.json)")
    parser.add_argument("--pub", type=Path, default=None,
                        help="public (verifying) key (default: NOCKBRAIN_SIGNING_PUB "
                             "or ~/.nock-brain/signing-key.pub)")
    parser.add_argument("--key", type=Path, default=None,
                        help="private key fallback (default: NOCKBRAIN_SIGNING_KEY "
                             "or ~/.nock-brain/signing-key)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--strict", action="store_true",
                        help="also exit non-zero if any fact is unsigned")
    parser.add_argument("--revocations", type=Path, default=None,
                        help="revocations.jsonl (default: next to --facts)")
    parser.add_argument("--retired-pub", action="append", type=Path, default=[],
                        help="retired public key(s) so pre-rotation revocation "
                             "events still verify (repeatable)")
    parser.add_argument("--strict-revocations", action="store_true",
                        help="also exit non-zero when a superseded fact has "
                             "no signed revocation event (legacy marks)")
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

    key, key_error = resolve_verify_key(key_path=args.key, pub_path=args.pub)
    if key is None:
        if key_error:
            print(f"could not load verifying key: {key_error}", file=sys.stderr)
        else:
            print("no verifying key found; signed facts cannot be verified",
                  file=sys.stderr)

    report = verify_facts(data, key)

    # S1: cross-check supersession marks against signed revocation events.
    # Resurrection — a live fact a valid event says is dead — is tampered-class
    # and fails the run unconditionally; unattested legacy marks warn unless
    # --strict-revocations.
    revocations_path = args.revocations or args.facts.parent / REVOCATIONS_FILENAME
    revocation_report = None
    if key is not None:
        retired_keys = []
        for retired_path in args.retired_pub:
            try:
                retired_keys.append(load_public_key(retired_path))
            except Exception as exc:  # noqa: BLE001 - report, don't crash
                print(f"could not load retired key {retired_path}: {exc}",
                      file=sys.stderr)
        revocation_report = revocation_audit(
            data, load_revocations(revocations_path), key,
            retired_keys=tuple(retired_keys),
        )
        revocation_report["path"] = str(revocations_path)
        report["revocations"] = revocation_report
    elif args.strict_revocations:
        # A strict run that cannot load the key must fail loudly — silently
        # returning 0 would let a nightly job believe it is enforcing
        # revocations while checking nothing.
        print("--strict-revocations requested but no verifying key loaded; "
              "revocation enforcement CANNOT run", file=sys.stderr)
        return 5

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Verified {report['total']} fact(s) against "
              f"{key.alg + ' key ' + key.key_id if key else 'NO key'}:")
        print(f"  {VALID:<14} {report['valid']}")
        print(f"  {TAMPERED.upper():<14} {report['tampered']}")
        print(f"  {UNSIGNED:<14} {report['unsigned']}")
        print(f"  {PARENT_SUSPECT:<14} {report['parent_suspect']}")
        if report["tampered"]:
            print("\nTAMPERED fact ids:", file=sys.stderr)
            for s in report["statuses"]:
                if s["status"] == TAMPERED:
                    print(f"  {s['id']}", file=sys.stderr)
        if report["parent_suspect"]:
            print("\nparent-suspect fact ids:", file=sys.stderr)
            for s in report["statuses"]:
                if s["status"] == PARENT_SUSPECT:
                    print(f"  {s['id']}", file=sys.stderr)
        if revocation_report is not None:
            print(f"  revocations: {revocation_report['attested']} attested, "
                  f"{revocation_report['invalid_events']} invalid, "
                  f"{revocation_report['foreign_key_events']} foreign-key, "
                  f"{len(revocation_report['resurrected'])} RESURRECTED, "
                  f"{len(revocation_report['unattested_superseded'])} unattested")
            if revocation_report["resurrected"]:
                print("\nRESURRECTED fact ids (live despite a valid signed "
                      "revocation):", file=sys.stderr)
                for fid in revocation_report["resurrected"]:
                    print(f"  {fid}", file=sys.stderr)

    if report["tampered"]:
        return 2
    # Revocation exit invariant via the single predicate (_revoke.blocking_
    # findings): resurrected/invalid are always blocking (exit 4); unattested
    # is blocking only under --strict-revocations (exit 5). One source of
    # truth so a new exit path can't silently skip a condition.
    if revocation_report is not None:
        if blocking_findings(revocation_report):  # resurrected or invalid
            return 4
        if args.strict and report["unsigned"]:
            return 3
        if blocking_findings(revocation_report, strict_unattested=True) \
                and args.strict_revocations:
            return 5
        return 0
    if args.strict and report["unsigned"]:
        return 3
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
