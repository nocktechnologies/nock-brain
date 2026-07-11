#!/usr/bin/env python3
"""Hard-delete fact material from local NockBrain stores.

Dry-run by default. Use --apply to rewrite files.
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _embed import DEFAULT_SIDECAR, EmbedUnavailable, load_sidecar, save_sidecar
from _facts import load_facts
from _store import secure_write_json, secure_write_text

DEFAULT_ROOT = Path.home() / ".nock-brain"


def matches_text(text: str, patterns: list[str]) -> bool:
    haystack = text.lower()
    return any(pattern.lower() in haystack for pattern in patterns if pattern)


def fact_matches(fact: dict[str, Any], fact_id: str, patterns: list[str]) -> bool:
    if fact_id and fact.get("id") == fact_id:
        return True
    return matches_text(json.dumps(fact, ensure_ascii=False), patterns)


def fact_event_ids(facts: list[dict[str, Any]]) -> set[str]:
    event_ids: set[str] = set()
    for fact in facts:
        for evidence in fact.get("evidence", []):
            event_id = evidence.get("event_id") if isinstance(evidence, dict) else ""
            if event_id:
                event_ids.add(str(event_id))
    return event_ids


def purge_facts(path: Path, fact_id: str, patterns: list[str]) -> tuple[list[dict[str, Any]], int]:
    facts = load_facts(path)
    removed = [fact for fact in facts if fact_matches(fact, fact_id, patterns)]
    kept = [fact for fact in facts if fact not in removed]
    return kept, len(removed)


def purge_events(path: Path, event_ids: set[str], patterns: list[str]) -> tuple[str, int]:
    if not path.exists():
        return "", 0
    kept: list[str] = []
    removed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            drop = False
            try:
                event = json.loads(line)
                drop = str(event.get("id", "")) in event_ids
            except json.JSONDecodeError:
                drop = False
            if not drop:
                drop = matches_text(line, patterns)
            if drop:
                removed += 1
            else:
                kept.append(line)
    return "".join(kept), removed


def purge_text_tree(root: Path, patterns: list[str]) -> tuple[dict[Path, str], int]:
    if not root.exists():
        return {}, 0
    rewrites: dict[Path, str] = {}
    removed = 0
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        except OSError:
            continue
        kept = [line for line in lines if not matches_text(line, patterns)]
        removed += len(lines) - len(kept)
        if len(kept) != len(lines):
            rewrites[path] = "".join(kept)
    return rewrites, removed


def purge_sidecar(path: Path, removed_ids: set[str], apply: bool) -> tuple[str, int]:
    """Vector purge parity: embeddings are content-derived, so a purged fact
    may not leave its vector behind. Surgical row removal when numpy is
    available; when it is not, fail SAFE by deleting the whole sidecar (it is
    derived data — re-embedding takes seconds) rather than skipping."""
    if not path.exists() or not removed_ids:
        return "", 0
    try:
        sidecar = load_sidecar(path)
    except EmbedUnavailable:
        if apply:
            path.unlink()
        return (
            f"numpy unavailable for surgical vector purge; "
            f"{'deleted' if apply else 'would delete'} entire sidecar {path} "
            f"(derived data; rerun embed-facts.py to rebuild)"
        ), -1
    if sidecar is None:
        # Unreadable/corrupt sidecar: treat like the no-numpy case.
        if apply:
            path.unlink()
        return (
            f"unreadable sidecar; "
            f"{'deleted' if apply else 'would delete'} {path}"
        ), -1
    keep = [i for i, fact_id in enumerate(sidecar["ids"])
            if fact_id not in removed_ids]
    removed = len(sidecar["ids"]) - len(keep)
    if removed and apply:
        save_sidecar(
            path,
            [sidecar["ids"][i] for i in keep],
            [sidecar["hashes"][i] for i in keep],
            sidecar["model"],
            sidecar["mat"][keep],
        )
    return "", removed


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge fact material from local NockBrain stores")
    parser.add_argument("fact_id", nargs="?", default="")
    parser.add_argument("--pattern", action="append", default=[])
    parser.add_argument("--facts", type=Path, default=DEFAULT_ROOT / "facts.json")
    parser.add_argument("--events", type=Path, default=DEFAULT_ROOT / "events.jsonl")
    parser.add_argument("--notes-dir", type=Path, default=DEFAULT_ROOT / "sessions")
    parser.add_argument("--vault", type=Path, default=DEFAULT_ROOT / "vault")
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--apply", action="store_true", help="Rewrite files; otherwise dry-run only")
    args = parser.parse_args(argv)

    if not args.fact_id and not args.pattern:
        parser.error("provide a fact_id or --pattern")

    kept_facts, removed_facts = purge_facts(args.facts, args.fact_id, args.pattern)
    removed_fact_records = [
        fact for fact in load_facts(args.facts)
        if fact_matches(fact, args.fact_id, args.pattern)
    ]
    patterns = list(args.pattern)
    patterns.extend(str(fact.get("content", "")) for fact in removed_fact_records)
    event_ids = fact_event_ids(removed_fact_records)
    kept_events, removed_events = purge_events(args.events, event_ids, patterns)
    note_rewrites, removed_note_lines = purge_text_tree(args.notes_dir, patterns)
    vault_rewrites, removed_vault_lines = purge_text_tree(args.vault, patterns)
    removed_ids = {str(fact.get("id")) for fact in removed_fact_records
                   if fact.get("id")}
    sidecar_note, removed_vectors = purge_sidecar(
        args.sidecar, removed_ids, args.apply)

    print(
        f"{'would remove' if not args.apply else 'removed'} "
        f"{removed_facts} fact(s), {removed_events} event(s), "
        f"{removed_note_lines} note line(s), {removed_vault_lines} vault line(s), "
        f"{'all' if removed_vectors < 0 else removed_vectors} vector(s)"
    )
    if sidecar_note:
        print(sidecar_note, file=sys.stderr)

    if not args.apply:
        return 0

    secure_write_json(args.facts, kept_facts, indent=2, default=str)
    if args.events.exists():
        secure_write_text(args.events, kept_events, encoding="utf-8")
    for path, text in {**note_rewrites, **vault_rewrites}.items():
        secure_write_text(path, text, encoding="utf-8")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
