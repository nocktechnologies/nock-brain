#!/usr/bin/env python3
"""Build the SQLite store from facts.json — fail-closed, zero re-signing (E2 P2).

Attestations sign fact VALUES (core + evidence anchor + parent hashes), not
container bytes, so this migration copies facts into ``brain.db`` with their
original attestations preserved verbatim. Fidelity is proven, not assumed:

    1. gate: no malformed facts, no duplicate ids in the source
    2. gate: strict whole-store signature verify of facts.json (when a
       verifying key is available; an unsigned store skips with a receipt note)
    3. build brain.db.staging and reload it through SqliteStore
    4. gate: fact count equal · per-fact canonical hash set equal ·
       value-identical dicts · strict verify of the RELOADED facts
    5. atomic rename staging -> brain.db; emit a machine-readable receipt

``facts.json`` is never modified, demoted, or deleted — it stays authoritative
until the deliberate cutover (NOCKBRAIN_STORE=sqlite or the store-v2 marker)
after the parity bar clears (design §8-§9). Re-running is idempotent: the DB is
rebuilt from the current JSON each time. Embeddings/insights stay file-based
derived artifacts until P5.

Usage:
    python3 migrate-store.py                 # propose: report, write nothing
    python3 migrate-store.py --apply         # build brain.db next to facts.json
    python3 migrate-store.py --facts /path/facts.json --apply
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _sign
from _facts import malformed_fact_reason
from _storeback import DB_FILENAME, SqliteStore

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


class MigrationError(RuntimeError):
    """A fail-closed gate refused the migration."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_raw_facts(path: Path) -> "list":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"facts store unreadable: {exc}")
    if not isinstance(data, list):
        raise MigrationError("facts store is not a list")
    return data


def _resolve_verify_key(store_dir: Path):
    """The verifying key, or None for an unsigned store (skip with a note)."""
    pub = Path(os.environ.get("NOCKBRAIN_SIGNING_PUB") or store_dir / "signing-key.pub")
    if not pub.exists():
        return None
    return _sign.load_public_key(pub)


def _strict_verify(facts: "list[dict]", key, label: str) -> None:
    result = _sign.verify_facts(facts, key)
    bad = result["tampered"] + result["parent_suspect"]
    if bad:
        raise MigrationError(
            f"{label}: strict verify failed ({result['tampered']} tampered, "
            f"{result['parent_suspect']} parent-suspect)"
        )


def _hash_set(facts: "list[dict]") -> "set[str]":
    return {_sign.canonical_fact_hash(f) for f in facts if isinstance(f, dict)}


def run_migration(facts_path: Path, *, apply: bool) -> "dict":
    facts_path = Path(facts_path)
    if not facts_path.exists():
        raise MigrationError(f"no facts store at {facts_path}")
    store_dir = facts_path.parent
    db_path = store_dir / DB_FILENAME

    raw = _load_raw_facts(facts_path)

    malformed = [
        (i, malformed_fact_reason(f)) for i, f in enumerate(raw) if malformed_fact_reason(f)
    ]
    if malformed:
        first = ", ".join(f"#{i}: {reason}" for i, reason in malformed[:3])
        raise MigrationError(
            f"{len(malformed)} malformed fact(s) — a migration must not silently "
            f"drop records ({first})"
        )

    ids = [f.get("id") for f in raw]
    duplicates = len(ids) - len(set(ids))
    if duplicates:
        raise MigrationError(f"{duplicates} duplicate fact id(s) — ids must be unique")

    key = _resolve_verify_key(store_dir)
    if key is not None:
        _strict_verify(raw, key, "facts.json")

    receipt = {
        "schema": "nockbrain-store-migration/v1",
        "mode": "apply" if apply else "propose",
        "facts": len(raw),
        "facts_json_sha256": _sha256_file(facts_path),
        "signature_verify": "valid" if key is not None else "skipped-unsigned",
        "db_path": str(db_path),
        "embeddings": "sidecar remains the derived dense-recall source until P5",
    }
    if not apply:
        return receipt

    staging = Path(str(db_path) + ".staging")
    for stale in (staging, Path(str(staging) + "-wal"), Path(str(staging) + "-shm")):
        stale.unlink(missing_ok=True)
    store = SqliteStore(staging)
    store.create(store_uuid=str(uuid.uuid4()))
    store.replace_all(raw)
    store.set_meta("migrated_from_sha256", receipt["facts_json_sha256"])
    store.set_meta("migrated_at", datetime.now(timezone.utc).isoformat())

    reloaded = SqliteStore(staging).load_facts()
    try:
        if len(reloaded) != len(raw):
            raise MigrationError(
                f"fidelity: fact count changed in transit ({len(raw)} -> {len(reloaded)})"
            )
        if _hash_set(reloaded) != _hash_set(raw):
            raise MigrationError("fidelity: canonical fact-hash sets differ")
        by_id = {f["id"]: f for f in reloaded}
        for fact in raw:
            if by_id.get(fact["id"]) != fact:
                raise MigrationError(f"fidelity: fact {fact['id']!r} not value-identical")
        if key is not None:
            _strict_verify(reloaded, key, "staging db")
    except MigrationError:
        staging.unlink(missing_ok=True)
        raise

    os.replace(staging, db_path)
    for suffix in ("-wal", "-shm"):
        Path(str(staging) + suffix).unlink(missing_ok=True)
    db_path.chmod(0o600)

    receipt["db_sha256"] = _sha256_file(db_path)
    receipt["gates"] = {
        "malformed": 0,
        "duplicate_ids": 0,
        "count_equal": True,
        "hash_set_equal": True,
        "value_identical": True,
        "verify_on_db": receipt["signature_verify"],
    }
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--apply", action="store_true",
                        help="build brain.db (default: propose only, write nothing)")
    args = parser.parse_args()

    try:
        receipt = run_migration(args.facts, apply=args.apply)
    except MigrationError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(receipt, indent=2, sort_keys=True))
    if not args.apply:
        print("Propose only — nothing written. Build with: migrate-store.py --apply",
              file=sys.stderr)
    else:
        print("facts.json untouched and still authoritative; cutover is a separate "
              "flag-gated step after the parity bar (see the E2 design spec).",
              file=sys.stderr)


if __name__ == "__main__":
    main()
