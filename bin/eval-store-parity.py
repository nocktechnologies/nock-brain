#!/usr/bin/env python3
"""Prove the JSON and SQLite backends are interchangeable — the E2 cutover bar.

Loads the same store through both backends and checks, in order:

    1. fact counts equal
    2. facts value-identical (dict equality, matched by id)
    3. per-fact canonical hash sets equal
    4. strict signature verification parity (when a verifying key exists)
    5. recall parity: identical ranked id lists from budget-recall's search
       for every suite query plus a deterministic fuzz set sampled from the
       store's own contents

The verdict is machine-readable JSON on stdout; exit 0 only when every check
passes. "Close" is not a pass — the backends share all scoring code and differ
only in IO, so anything but identical output is a fidelity bug.

Usage:
    python3 eval-store-parity.py                      # ~/.nock-brain defaults
    python3 eval-store-parity.py --facts f.json --db brain.db
    python3 eval-store-parity.py --suite mira-recall-suite.json --fuzz 200
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _sign
from _facts import content_tokens
from _storeback import DB_FILENAME, JsonStore, SqliteStore

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def _load_budget_recall():
    """budget-recall.py has a hyphenated name; load it by path like conftest."""
    path = BIN_DIR / "budget-recall.py"
    spec = importlib.util.spec_from_file_location("budget_recall", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fuzz_queries(facts: "list[dict]", count: int) -> "list[str]":
    """Deterministic queries sampled from the store's own live contents:
    evenly-spaced facts, middle slice of their sorted token sets. No RNG —
    the same store always yields the same fuzz set."""
    live = [f for f in facts if isinstance(f, dict) and f.get("status") != "superseded"]
    if not live or count <= 0:
        return []
    queries = []
    step = max(1, len(live) // count)
    for fact in live[::step][:count]:
        tokens = sorted(content_tokens(fact.get("content")))
        if len(tokens) < 2:
            continue
        mid = len(tokens) // 2
        queries.append(" ".join(tokens[mid:mid + 4]))
    return queries


def run_parity(facts_path: Path, db_path: Path, suite_path: "Path | None",
               fuzz: int) -> "dict":
    json_facts = JsonStore(facts_path).load_facts()
    db_facts = SqliteStore(db_path).load_facts()

    checks: "dict[str, bool]" = {}
    checks["count_equal"] = len(json_facts) == len(db_facts)

    by_id_json = {f.get("id"): f for f in json_facts}
    by_id_db = {f.get("id"): f for f in db_facts}
    checks["value_identical"] = by_id_json == by_id_db

    checks["hash_set_equal"] = (
        {_sign.canonical_fact_hash(f) for f in json_facts}
        == {_sign.canonical_fact_hash(f) for f in db_facts}
    )

    key = None
    pub = Path(os.environ.get("NOCKBRAIN_SIGNING_PUB")
               or facts_path.parent / "signing-key.pub")
    if pub.exists():
        key = _sign.load_public_key(pub)
    if key is not None:
        verify_json = _sign.verify_facts(json_facts, key)
        verify_db = _sign.verify_facts(db_facts, key)
        checks["verify_parity"] = all(
            verify_json[k] == verify_db[k]
            for k in ("valid", "tampered", "unsigned", "parent_suspect")
        )
        verify_note = f"valid={verify_db['valid']}/{verify_db['total']}"
    else:
        verify_note = "skipped-unsigned"

    queries: "list[str]" = []
    if suite_path is not None:
        suite = json.loads(Path(suite_path).read_text(encoding="utf-8"))
        queries.extend(str(entry[1]) for entry in suite if len(entry) >= 2)
    queries.extend(fuzz_queries(json_facts, fuzz))

    budget_recall = _load_budget_recall()
    mismatches = []
    for query in queries:
        ids_json = [f.get("id") for f in budget_recall.search(json_facts, query)]
        ids_db = [f.get("id") for f in budget_recall.search(db_facts, query)]
        if ids_json != ids_db:
            mismatches.append({"query": query, "json": ids_json, "sqlite": ids_db})
    checks["recall_parity"] = not mismatches

    return {
        "schema": "nockbrain-store-parity/v1",
        "facts": len(json_facts),
        "queries_run": len(queries),
        "signature_verify": verify_note,
        "checks": checks,
        "recall_mismatches": mismatches[:10],
        "identical": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--db", type=Path, default=None,
                        help="brain.db path (default: next to --facts)")
    parser.add_argument("--suite", type=Path, default=None,
                        help="ground-truth suite JSON ([[label, query, ...], ...])")
    parser.add_argument("--fuzz", type=int, default=50,
                        help="deterministic content-derived queries to add")
    args = parser.parse_args()

    db_path = args.db or args.facts.parent / DB_FILENAME
    for path, label in ((args.facts, "facts"), (db_path, "db")):
        if not Path(path).exists():
            print(f"no {label} store at {path}", file=sys.stderr)
            sys.exit(1)

    result = run_parity(args.facts, db_path, args.suite, args.fuzz)
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result["identical"] else 1)


if __name__ == "__main__":
    main()
