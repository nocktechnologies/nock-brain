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

from _sign import (  # noqa: E402
    DEFAULT_PUB_PATH,
    PARENT_SUSPECT,
    TAMPERED,
    UNSIGNED,
    VALID,
    load_public_key,
    verify_facts,
)

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify facts.json attestations")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS,
                        help="facts.json to verify (default ~/.nock-brain/facts.json)")
    parser.add_argument("--pub", type=Path, default=DEFAULT_PUB_PATH,
                        help="public (verifying) key path")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--strict", action="store_true",
                        help="also exit non-zero if any fact is unsigned")
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

    key = None
    if args.pub.exists():
        try:
            key = load_public_key(args.pub)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"could not load public key {args.pub}: {exc}", file=sys.stderr)
            key = None
    else:
        print(f"no public key at {args.pub}; signed facts cannot be verified",
              file=sys.stderr)

    report = verify_facts(data, key)

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

    if report["tampered"]:
        return 2
    if args.strict and report["unsigned"]:
        return 3
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
