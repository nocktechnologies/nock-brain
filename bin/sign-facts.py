#!/usr/bin/env python3
"""Sign a NockBrain facts.json store in place (N8068).

Attaches a tamper-evident ``attestation`` envelope to every fact, using the
local signing key (auto-generated on first use). Existing attestations are
re-signed so the store reflects the current content.

Usage:
    python3 bin/sign-facts.py                       # sign ~/.nock-brain/facts.json
    python3 bin/sign-facts.py --facts /path/facts.json
    python3 bin/sign-facts.py --facts in.json --out signed.json   # don't mutate input
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
    load_or_create_key,
    resolve_key_paths,
    sign_facts,
)
from _store import secure_write_text  # noqa: E402
from _storeback import resolve_store  # noqa: E402

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign a facts.json store in place")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS,
                        help="facts.json to sign (default ~/.nock-brain/facts.json)")
    parser.add_argument("--out", type=Path, default=None,
                        help="write signed store here instead of in place")
    parser.add_argument("--key", type=Path, default=None,
                        help="signing private key (default: NOCKBRAIN_SIGNING_KEY "
                             "or ~/.nock-brain/signing-key)")
    parser.add_argument("--pub", type=Path, default=None,
                        help="signing public key (default: NOCKBRAIN_SIGNING_PUB "
                             "or ~/.nock-brain/signing-key.pub)")
    args = parser.parse_args(argv)

    store = resolve_store(args.facts)
    if store.kind == "sqlite" and args.out is None:
        if not store.freshness_path.exists():
            print(f"No facts store found at {store.describe()}", file=sys.stderr)
            return 1
        data = store.load_facts()
    else:
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

    key_path, pub_path = resolve_key_paths(args.key, args.pub)
    key = load_or_create_key(key_path, pub_path)
    signed = sign_facts(data, key)

    if store.kind == "sqlite" and args.out is None:
        store.replace_all(signed)
        out_path = args.facts
    else:
        out_path = args.out or args.facts
        secure_write_text(out_path, json.dumps(signed, indent=2, ensure_ascii=False))

    print(f"Signed {len(signed)} fact(s) with {key.alg} (key {key.key_id})")
    print(f"Wrote {out_path}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
