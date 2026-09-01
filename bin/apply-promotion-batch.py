#!/usr/bin/env python3
"""Apply NockCC memory-promotion batches into the live NockBrain store.

The server-side Memory Curator (N8846) converts operator-accepted proposals
into signed, hash-chained ``memory-promotion-batch/v1`` artifacts — but by
design NEVER touches the live store. This is the external applier that
contract (nock-command-center brain/memory_promotion.py, simulate_apply)
specifies: ADDITIVE ONLY. An existing fact id is never overwritten, the store
never shrinks, rolled-back batches are no-ops, and the parent-digest chain
must be consistent in batch_seq order.

Until 2026-08-20 this applier did not exist — batches had no consumer, one of
the memory system's built-but-never-wired gaps. Facts applied here carry the
batch digest and are stamped with this machine's provenance tag, then the
store is re-signed and verified; the batch is recorded applied only after
verification passes (idempotent across reruns via applied-batches.json).

Usage (NOCKBRAIN_MACHINE must name the seat you are actually on — it is a
closed enum in extract-facts.KNOWN_MACHINES and mints the applied facts'
provenance; the retired fleet-02 seat no longer mints):
    NOCKCC_API_KEY=... NOCKBRAIN_MACHINE=kevins-linux \
        python3 apply-promotion-batch.py --agent mira-nockos [--dry-run]
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve).
from __future__ import annotations

import argparse
import importlib.util as _ilu
import json
import os
import subprocess  # nosec B404 - only invokes trusted sibling bin/ CLIs
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_write_json  # noqa: E402

_spec = _ilu.spec_from_file_location("extract_facts", BIN_DIR / "extract-facts.py")
_extract_facts = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_extract_facts)

DEFAULT_STORE_DIR = Path.home() / ".nock-brain"
DEFAULT_API_BASE = "https://cc.nocktechnologies.io"
STATE_NAME = "applied-batches.json"


class ApplyError(RuntimeError):
    """Abort with a clear operator-facing message; live store untouched."""


def fetch_batches(api_base: str, agent: str, api_key: str) -> list[dict]:
    req = urllib.request.Request(
        f"{api_base}/api/brain/memory-curator/batches/?agent={agent}&full=1",
        headers={"X-API-Key": api_key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - https, fixed host
        doc = json.load(resp)
    if not doc.get("success"):
        raise ApplyError(f"batch fetch failed: {doc.get('message')}")
    return sorted(doc["data"]["batches"], key=lambda b: b["batch_seq"])


def last_applied_digest(state: dict) -> str | None:
    """Highest batch_seq already recorded in applied-batches.json, or None."""
    if not state:
        return None

    def seq_key(item):
        seq, _digest = item
        try:
            return (0, int(seq))
        except (TypeError, ValueError):
            return (1, str(seq))

    _seq, digest = max(state.items(), key=seq_key)
    return digest or None


def check_chain(batches: list[dict], *, anchor_digest: str | None = None) -> None:
    """Parent-digest chain must be consistent in batch_seq order.

    ``anchor_digest`` is the last applied digest from applied-batches.json so
    a fetch that omits already-applied batches still has to chain onto live
    state. A missing ``batch_digest`` is a broken link, not a skip (N10026).
    """
    prev_digest = anchor_digest
    for batch in batches:
        seq = batch.get("batch_seq")
        digest = batch.get("batch_digest") or None
        if not digest:
            raise ApplyError(
                f"batch {seq} has no batch_digest; refusing to apply"
            )
        parent = batch.get("parent_batch_digest") or None
        if prev_digest is not None and parent != prev_digest:
            raise ApplyError(
                f"batch {seq} parent digest does not chain to "
                f"the previous digest; refusing to apply"
            )
        prev_digest = digest


def apply_batch(store: list[dict], batch: dict, machine: str) -> list[dict]:
    """Return store + this batch's facts. ADDITIVE ONLY (the simulate_apply
    contract): a payload fact whose id already exists in the store aborts the
    whole batch — collisions mean the artifact and the store disagree, and
    resolving that is an operator decision, not an overwrite."""
    existing = {f.get("id") for f in store if isinstance(f, dict)}
    incoming = batch["payload"]["facts"]
    collisions = [f["id"] for f in incoming if f.get("id") in existing]
    if collisions:
        raise ApplyError(
            f"batch {batch['batch_seq']} collides with {len(collisions)} existing "
            f"fact id(s) (e.g. {collisions[:3]}); additive-only contract forbids "
            "overwrite — refusing the batch"
        )
    applied_at = datetime.now(timezone.utc).isoformat()
    out = list(store)
    for fact in incoming:
        fact = dict(fact)
        fact["machine"] = machine
        fact["applied_at"] = applied_at
        if not fact.get("source_date"):
            source_time = fact.get("source_time")
            if isinstance(source_time, str) and len(source_time) >= 10:
                fact["source_date"] = source_time[:10]
        out.append(fact)
    return out


def run(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ApplyError as exc:
        print(f"apply-promotion-batch: {exc}", file=sys.stderr)
        return 1


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply memory-promotion batches")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--store-dir", type=Path, default=DEFAULT_STORE_DIR)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("NOCKCC_API_KEY", "")
    if not api_key:
        print("NOCKCC_API_KEY is not set", file=sys.stderr)
        return 1
    machine = _extract_facts.machine_tag()  # raises loudly off-registry

    facts_path = args.store_dir / "facts.json"
    state_path = args.store_dir / STATE_NAME
    store = json.loads(facts_path.read_text(encoding="utf-8"))
    if not isinstance(store, list):
        print("facts.json is not a bare list; refusing", file=sys.stderr)
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    batches = fetch_batches(args.api_base, args.agent, api_key)
    pending = [
        b for b in batches
        if b.get("status") == "built" and str(b["batch_seq"]) not in state
    ]
    check_chain(pending, anchor_digest=last_applied_digest(state))
    skipped_rolled_back = sum(1 for b in batches if b.get("status") == "rolled_back")
    if not pending:
        print(f"nothing to apply ({len(batches)} batch(es), "
              f"{skipped_rolled_back} rolled back, rest already applied)")
        return 0

    before = len(store)
    for batch in pending:
        store = apply_batch(store, batch, machine)
    print(f"applying {len(pending)} batch(es): {before} -> {len(store)} facts")
    if args.dry_run:
        print("DRY RUN — store untouched")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = facts_path.with_name(f"facts.json.bak-preapply-{stamp}")
    candidate = facts_path.with_name("facts.json.applying")
    # Stage → sign/verify the candidate → commit. Live facts.json is untouched
    # until verification passes (N10026).
    try:
        secure_write_json(candidate, store, ensure_ascii=False)
        for script, extra in (
            ("sign-facts.py", []),
            ("verify-facts.py", ["--strict"]),
        ):
            proc = subprocess.run(  # nosec B603 - fixed sibling scripts
                [sys.executable, str(BIN_DIR / script),
                 "--facts", str(candidate), *extra],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode != 0:
                print(f"{script} FAILED on staged apply — live store untouched:\n"
                      f"{(proc.stderr or proc.stdout)[-400:]}",
                      file=sys.stderr)
                return 1
        backup.write_bytes(facts_path.read_bytes())
        os.replace(candidate, facts_path)
        candidate = None
    finally:
        if candidate is not None:
            try:
                candidate.unlink()
            except OSError:
                pass

    for batch in pending:
        digest = batch.get("batch_digest") or ""
        state[str(batch["batch_seq"])] = digest
    secure_write_json(state_path, state, indent=1)
    print(f"applied {len(pending)} batch(es); signed + verified; "
          f"backup {backup.name}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
