#!/usr/bin/env python3
"""Actor-tracked, safe in-place edits of a fact's content (S9).

Borrowed from Letta: to let an AGENT edit its own memory without silently
corrupting it, two guarantees ride every edit.

1. UNIQUE-MATCH replace (``unique_replace``): an edit names a target substring
   and its replacement, and is REFUSED unless the target occurs exactly once in
   the fact's content. A 0- or >1-match edit is ambiguous — it would either do
   nothing or rewrite several places at once — so instead of guessing we raise a
   clear, retryable error. Silent memory corruption becomes a caught mistake.

2. Actor-tracked, append-only history (``fact-edits.jsonl`` next to the store):
   every content change is snapshotted with WHO made it (``human`` | ``agent``)
   into a linear log, so a human can one-click ``--revert`` the last change.

Because the attestation signs the fact's CORE (id + kind + content), changing
content invalidates the signature — so every edit/revert RE-SIGNS the fact with
the store key (same env resolution as the recall path: NOCKBRAIN_SIGNING_KEY /
NOCKBRAIN_SIGNING_PUB). With no key available the edit still lands, the stale
attestation is dropped so the fact reads UNSIGNED (never TAMPERED), and a loud
warning is printed. id and kind are NEVER mutated.

Usage:
    python3 edit-fact.py <fact_id> --replace "<old>" --with "<new>" --actor agent
    python3 edit-fact.py --revert <fact_id> [--facts PATH]
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _revoke import resolve_signing_key
from _sign import sign_fact
from _store import FILE_MODE
from _storeback import resolve_store

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
EDITS_FILENAME = "fact-edits.jsonl"


def unique_replace(content: str, old: str, new: str) -> str:
    """Replace ``old`` with ``new`` — but only when ``old`` occurs EXACTLY once.

    The safe primitive: 0 or >1 matches raise a ValueError naming the count, so
    an ambiguous edit becomes a retryable error instead of a silent no-op or a
    multi-site rewrite. ``old``/``new`` are literal substrings, not patterns."""
    count = content.count(old)
    if count != 1:
        raise ValueError(
            f"refusing edit: target substring occurs {count} time(s), need exactly 1"
        )
    return content.replace(old, new)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_edit_row(fact_id: str, actor: str, old_content: str, new_content: str) -> "dict[str, Any]":
    """One append-only history row for a single content change.

    ``old_excerpt``/``new_excerpt`` hold the FULL pre/post content (not a
    truncated preview): they are the only record of the pre-edit text, so
    ``--revert`` restores from them, and the paired sha256 guards that restore
    against a corrupted history line. The linear chain also self-links — an
    edit's ``new_sha256`` equals the next row's ``old_sha256``."""
    return {
        "at": _now_iso(),
        "fact_id": fact_id,
        "actor": actor,
        "old_sha256": _sha256(old_content),
        "new_sha256": _sha256(new_content),
        "old_excerpt": old_content,
        "new_excerpt": new_content,
    }


def append_edit(path: Path, row: "dict[str, Any]") -> None:
    """Append one row to the history sidecar (0600); never rewrites prior lines."""
    path = Path(path)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    path.chmod(FILE_MODE)


def load_edits(path: Path) -> "list[dict[str, Any]]":
    """Load the append-only history, skipping any malformed line (lenient by
    design — one bad row must never hide the rest of a fact's history)."""
    path = Path(path)
    if not path.exists():
        return []
    rows: "list[dict[str, Any]]" = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _resign(facts: "list[dict]", fact: "dict[str, Any]", key) -> bool:
    """Re-sign the mutated fact, or drop its now-stale attestation when no key.

    Content changed, so any prior attestation no longer matches its core. With a
    key we re-sign in place (parent linkage is preserved by ``attest_fact``).
    With NO key we REMOVE the attestation so the fact reads UNSIGNED
    (backward-compat) rather than leaving a stale signature that would verify as
    TAMPERED. Returns True iff the fact was re-signed."""
    if key is None:
        fact.pop("attestation", None)
        return False
    facts_by_id = {f.get("id", ""): f for f in facts if isinstance(f, dict)}
    sign_fact(fact, key, facts_by_id=facts_by_id)
    return True


def _apply_edit(args, store, facts: "list[dict]", edits_path: Path, key) -> None:
    fact = next((f for f in facts if f.get("id") == args.fact_id), None)
    if not fact:
        print(f"Fact {args.fact_id} not found.", file=sys.stderr)
        sys.exit(1)

    old_content = str(fact.get("content", ""))
    try:
        new_content = unique_replace(old_content, args.replace, args.new)
    except ValueError as exc:
        print(f"edit-fact: {exc}", file=sys.stderr)
        sys.exit(1)

    fact["content"] = new_content  # id/kind are never touched
    signed = _resign(facts, fact, key)
    store.replace_all(facts)
    append_edit(edits_path, build_edit_row(args.fact_id, args.actor, old_content, new_content))

    if not signed:
        print("edit-fact: WARNING no signing key available — fact left UNSIGNED "
              "(re-sign with sign-facts.py once a key exists)", file=sys.stderr)
    print(f"Edited {args.fact_id} by {args.actor} "
          f"({'re-signed' if signed else 'unsigned'}).")


def _revert(args, store, facts: "list[dict]", edits_path: Path, key) -> None:
    fid = args.revert
    fact = next((f for f in facts if f.get("id") == fid), None)
    if not fact:
        print(f"Fact {fid} not found.", file=sys.stderr)
        sys.exit(1)

    history = [r for r in load_edits(edits_path) if r.get("fact_id") == fid]
    if not history:
        print(f"No edit history for {fid}; nothing to revert.", file=sys.stderr)
        sys.exit(1)

    last = history[-1]
    prior = str(last.get("old_excerpt", ""))
    # The history line is the only record of the pre-edit content; verify it
    # against the row's own hash before trusting it as the restore target.
    if _sha256(prior) != last.get("old_sha256"):
        print(f"edit-fact: history for {fid} is corrupt (hash mismatch); "
              "refusing revert", file=sys.stderr)
        sys.exit(1)

    current = str(fact.get("content", ""))
    fact["content"] = prior  # id/kind are never touched
    signed = _resign(facts, fact, key)
    store.replace_all(facts)
    # A revert is itself an actor=human change: current -> restored prior.
    append_edit(edits_path, build_edit_row(fid, "human", current, prior))

    if not signed:
        print("edit-fact: WARNING no signing key available — fact left UNSIGNED "
              "(re-sign with sign-facts.py once a key exists)", file=sys.stderr)
    print(f"Reverted {fid} to prior content "
          f"({'re-signed' if signed else 'unsigned'}).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fact_id", nargs="?", default="")
    parser.add_argument("--replace", default=None,
                        help="substring to replace (must occur exactly once)")
    parser.add_argument("--with", dest="new", default=None,
                        help="replacement text")
    parser.add_argument("--actor", choices=("human", "agent"), default="human",
                        help="who is making the edit")
    parser.add_argument("--revert", metavar="FACT_ID", default="",
                        help="restore a fact to its previous content (human undo)")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    args = parser.parse_args()

    store = resolve_store(args.facts)
    if not store.freshness_path.exists():
        print(f"No fact store found ({store.describe()}).", file=sys.stderr)
        sys.exit(1)

    facts = store.load_facts()
    edits_path = Path(store.freshness_path).parent / EDITS_FILENAME
    key = resolve_signing_key()

    if args.revert:
        _revert(args, store, facts, edits_path, key)
        return

    if not args.fact_id:
        parser.print_help()
        return
    if args.replace is None or args.new is None:
        parser.error("--replace and --with are required for an edit")

    _apply_edit(args, store, facts, edits_path, key)


if __name__ == "__main__":
    main()
