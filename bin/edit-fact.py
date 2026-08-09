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
import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _revoke import resolve_signing_key
from _sign import (
    CLAIM_ATTESTATION_V2_SCHEMA,
    _CLAIM_V2_ONLY_AUTHORITY_FIELDS,
    sign_fact,
)
from _store import FILE_MODE
from _storeback import resolve_store

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
EDITS_FILENAME = "fact-edits.jsonl"
LOCK_FILENAME = ".edit-fact.lock"


class _StoreLock:
    """Advisory exclusive lock over one store, held for the whole
    load-modify-store cycle so two concurrent edit-fact processes cannot
    load the same content, append different history rows, and clobber each
    other's write. flock is advisory (only cooperating edit-fact processes
    honor it), which is exactly the scope here — this is the one writer."""

    def __init__(self, store_path: Path):
        self._path = Path(store_path).parent / LOCK_FILENAME
        self._fd = None

    def __enter__(self):
        self._fd = os.open(str(self._path),
                           os.O_WRONLY | os.O_CREAT, FILE_MODE)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        return False


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


def _raw_record_count(store_path: Path) -> "int | None":
    """Number of raw records in the store file, or None if it is not a JSON
    fact list (e.g. a SQLite backend, whose load is lossless). Used to refuse
    an edit that would silently drop malformed siblings on write-back."""
    try:
        data = json.loads(Path(store_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return len(data) if isinstance(data, list) else None


def _is_v2_claim(fact: "dict[str, Any]") -> bool:
    """True if a fact carries the v2 claim attestation schema or its authority
    fields — the legacy sign_fact cannot safely re-sign it (verify_fact would
    return TAMPERED), so edit-fact refuses rather than silently corrupt."""
    att = fact.get("attestation")
    if isinstance(att, dict) and att.get("schema") == CLAIM_ATTESTATION_V2_SCHEMA:
        return True
    return bool(_CLAIM_V2_ONLY_AUTHORITY_FIELDS.intersection(fact))


def _child_ids(facts: "list[dict]", parent_id: str) -> "list[str]":
    """Facts that name ``parent_id`` in their attestation's parent_fact_ids —
    their signatures commit to the parent's core hash, so editing the parent
    would make them parent-suspect (see verify_fact)."""
    out = []
    for f in facts:
        if not isinstance(f, dict) or f.get("id") == parent_id:
            continue
        att = f.get("attestation") or {}
        if isinstance(att, dict) and parent_id in (att.get("parent_fact_ids") or []):
            out.append(str(f.get("id", "")))
    return out


def append_edit(path: Path, row: "dict[str, Any]") -> None:
    """Append one row to the history sidecar; never rewrites prior lines.

    The file is created 0600 BEFORE any content is written — the row carries
    fact-content excerpts, so a umask-created file would briefly expose them
    world-readable (the store itself is 0600)."""
    path = Path(path)
    # O_CREAT with mode 0600 is applied at creation, closing the umask window
    # secure_write's create-then-chmod leaves open for a brand-new file.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    path.chmod(FILE_MODE)  # tighten an existing file that predated this guard


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

    if _is_v2_claim(fact):
        print(f"edit-fact: {args.fact_id} carries a v2 claim attestation; "
              "re-signing it with the legacy signer would make it verify as "
              "TAMPERED. Editing v2-claim facts is not supported — refusing.",
              file=sys.stderr)
        sys.exit(1)
    children = _child_ids(facts, args.fact_id)
    if children:
        print(f"edit-fact: {args.fact_id} is a parent of {len(children)} fact(s) "
              f"({', '.join(children[:5])}{'…' if len(children) > 5 else ''}); "
              "editing it would make those children parent-suspect. Re-signing "
              "the child subtree is not yet supported — refusing.", file=sys.stderr)
        sys.exit(1)
    old_content = str(fact.get("content", ""))
    try:
        new_content = unique_replace(old_content, args.replace, args.new)
    except ValueError as exc:
        print(f"edit-fact: {exc}", file=sys.stderr)
        sys.exit(1)

    fact["content"] = new_content  # id/kind are never touched
    signed = _resign(facts, fact, key)
    # History BEFORE the store write: a crash between the two must never leave
    # an edited store with no revert row (which would silently defeat --revert).
    # The reverse order is safe — a history row whose store write never landed
    # just describes a change that didn't happen, and --revert no-ops on it.
    append_edit(edits_path, build_edit_row(args.fact_id, args.actor, old_content, new_content))
    store.replace_all(facts)

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
    if last.get("op") == "revert":
        print(f"edit-fact: the last change to {fid} was already a revert; a "
              "second revert would re-apply the undone edit. Refusing — make a "
              "new edit if you want to change it again.", file=sys.stderr)
        sys.exit(1)
    prior = str(last.get("old_excerpt", ""))
    # The history line is the only record of the pre-edit content; verify it
    # against the row's own hash before trusting it as the restore target.
    if _sha256(prior) != last.get("old_sha256"):
        print(f"edit-fact: history for {fid} is corrupt (hash mismatch); "
              "refusing revert", file=sys.stderr)
        sys.exit(1)

    current = str(fact.get("content", ""))
    # The store may have been changed out-of-band since the last recorded edit
    # (a direct write, a distill rebuild). Reverting then would restore stale
    # content OVER an unexpected current — silent data loss. Only revert when
    # the live content is exactly what the last history row produced.
    if _sha256(current) != last.get("new_sha256"):
        print(f"edit-fact: {fid}'s current content does not match the last "
              "recorded edit (changed out-of-band?); refusing revert to avoid "
              "clobbering unexpected content.", file=sys.stderr)
        sys.exit(1)
    fact["content"] = prior  # id/kind are never touched
    signed = _resign(facts, fact, key)
    # History BEFORE the store write (see _edit): the undo trail must survive a
    # crash in the write window. A revert is itself an actor=human change.
    revert_row = build_edit_row(fid, "human", current, prior)
    revert_row["op"] = "revert"
    append_edit(edits_path, revert_row)
    store.replace_all(facts)

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

    if not args.revert and not args.fact_id:
        parser.print_help()
        return
    if not args.revert and (args.replace is None or args.new is None):
        parser.error("--replace and --with are required for an edit")

    edits_path = Path(store.freshness_path).parent / EDITS_FILENAME
    key = resolve_signing_key()

    # Hold the store lock across the ENTIRE load-modify-store cycle so a
    # concurrent edit-fact cannot interleave and clobber this write.
    with _StoreLock(store.freshness_path):
        facts = store.load_facts()
        raw = _raw_record_count(store.freshness_path)
        if raw is not None and raw != len(facts):
            print(f"edit-fact: store has {raw} records but only {len(facts)} "
                  "loaded as valid — writing back would silently DROP the "
                  f"{raw - len(facts)} malformed record(s). Refusing; clean the "
                  "store first (extract/refine) before an in-place edit.",
                  file=sys.stderr)
            sys.exit(1)
        if args.revert:
            _revert(args, store, facts, edits_path, key)
        else:
            _apply_edit(args, store, facts, edits_path, key)


if __name__ == "__main__":
    main()
