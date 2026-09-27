#!/usr/bin/env python3
"""Hard-delete fact material from local NockBrain stores.

Dry-run by default. Use --apply to rewrite files.
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _embed import DEFAULT_SIDECAR, EmbedUnavailable, load_sidecar, save_sidecar
from _facts import TOMBSTONES_FILENAME
from _store import FILE_MODE, secure_write_json, secure_write_text
from _storeback import resolve_store
from _verify_cache import cache_path_for, unlink_for_store

DEFAULT_ROOT = Path.home() / ".nock-brain"


def matches_text(text: str, patterns: list[str]) -> bool:
    haystack = text.lower()
    return any(pattern.lower() in haystack for pattern in patterns if pattern)


def fact_matches(fact: dict[str, Any], fact_id: str, patterns: list[str]) -> bool:
    """Match id exactly, or a pattern against id/content only.

    Patterns must not search the whole JSON dump: attestation signatures are
    hex and a substring match would purge unrelated facts (N10028).
    """
    if fact_id and fact.get("id") == fact_id:
        return True
    haystack = f"{fact.get('id', '')}\n{fact.get('content', '')}"
    return matches_text(haystack, patterns)


def fact_event_ids(facts: list[dict[str, Any]]) -> set[str]:
    event_ids: set[str] = set()
    for fact in facts:
        for evidence in fact.get("evidence", []):
            event_id = evidence.get("event_id") if isinstance(evidence, dict) else ""
            if event_id:
                event_ids.add(str(event_id))
    return event_ids


def purge_facts(path: Path, fact_id: str, patterns: list[str]) -> tuple[list[dict[str, Any]], int]:
    store = resolve_store(path)
    facts = store.load_facts()
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


def purge_insights(
    path: Path, removed_ids: set[str], patterns: list[str],
) -> tuple[list[dict[str, Any]] | None, int]:
    """Drop insights that cite a purged fact or still carry its content.

    The content match covers the N10052 contaminated-cluster shape: the
    heuristic quotes the latest member in "Most recent: ...", so an insight
    whose cluster included a leaked judge-prompt fact carries the template
    verbatim in ``content`` even when most members are genuine. Dropping it
    is safe — insights are derived and the next synthesize regenerates the
    cluster cleanly from the surviving members. (``theme`` is deliberately
    NOT matched: cluster_theme is a top-5 keyword join and can never carry a
    full pattern sentence, so a theme match could only fire on keyword
    coincidence.)"""
    if not path.exists():
        return None, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0
    if not isinstance(data, list):
        return None, 0
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in data:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        source_ids = item.get("source_ids") or []
        drop = (
            (item.get("id") and str(item.get("id")) in removed_ids)
            or any(str(sid) in removed_ids for sid in source_ids)
            or matches_text(str(item.get("content", "")), patterns)
        )
        if drop:
            removed += 1
        else:
            kept.append(item)
    return kept, removed


def purge_graph(path: Path, removed_ids: set[str]) -> tuple[dict[str, Any] | None, int]:
    """Drop fact nodes (and incident edges) for purged ids from graph.json."""
    if not path.exists():
        return None, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0
    if not isinstance(data, dict):
        return None, 0
    drop_nodes = {f"fact:{fid}" for fid in removed_ids}
    nodes = [
        node for node in data.get("nodes") or []
        if not (isinstance(node, dict) and node.get("id") in drop_nodes)
    ]
    edges = [
        edge for edge in data.get("edges") or []
        if not (
            isinstance(edge, dict)
            and (edge.get("source") in drop_nodes or edge.get("target") in drop_nodes)
        )
    ]
    removed = (
        len(data.get("nodes") or []) - len(nodes)
        + len(data.get("edges") or []) - len(edges)
    )
    if removed == 0:
        return None, 0
    data = dict(data)
    data["nodes"] = nodes
    data["edges"] = edges
    return data, removed


def append_tombstones(path: Path, ids: set[str]) -> None:
    """Append purged ids so a later rebuild cannot re-extract them (N10014)."""
    if not ids:
        return
    stamp = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as stream:
        for fact_id in sorted(ids):
            stream.write(
                json.dumps({"id": fact_id, "purged_at": stamp}, ensure_ascii=False)
                + "\n"
            )
    path.chmod(FILE_MODE)


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

    store = resolve_store(args.facts)
    kept_facts, removed_facts = purge_facts(args.facts, args.fact_id, args.pattern)
    removed_fact_records = [
        fact for fact in store.load_facts()
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
    insights_path = args.facts.parent / "insights.json"
    graph_path = args.facts.parent / "graph.json"
    kept_insights, removed_insights = purge_insights(
        insights_path, removed_ids, patterns)
    kept_graph, removed_graph = purge_graph(graph_path, removed_ids)

    cache_path = cache_path_for(store.freshness_path)
    cache_note = ""
    if args.apply:
        # Rewrite the store first so a concurrent recall that loaded the old
        # facts.json cannot save() the sidecar back: save() re-stats and skips
        # when the stamp moved. Then drop the sidecar (opaque digests; next
        # recall cold-starts). Dry-run never reaches here.
        # Zero-match must not rewrite: load_facts drops malformed records, and
        # a no-op apply would destroy them (N10028).
        if removed_facts:
            store.replace_all(kept_facts)
            append_tombstones(
                args.facts.parent / TOMBSTONES_FILENAME, removed_ids)
            # Always unlink so leftover `{sidecar}.*.tmp` files are swept even
            # when an interrupted cache write never produced the sidecar.
            had_cache = cache_path.exists()
            unlinked = unlink_for_store(store.freshness_path)
            if had_cache:
                cache_note = (
                    f"deleted verification cache {cache_path}" if unlinked
                    else f"could not delete verification cache {cache_path}"
                )
        if args.events.exists() and removed_events:
            secure_write_text(args.events, kept_events, encoding="utf-8")
        for path, text in {**note_rewrites, **vault_rewrites}.items():
            secure_write_text(path, text, encoding="utf-8")
        if kept_insights is not None and removed_insights:
            secure_write_json(insights_path, kept_insights, indent=2, default=str)
        if kept_graph is not None and removed_graph:
            secure_write_json(graph_path, kept_graph, indent=2, default=str)
    elif removed_facts and cache_path.exists():
        cache_note = f"would delete verification cache {cache_path}"

    print(
        f"{'would remove' if not args.apply else 'removed'} "
        f"{removed_facts} fact(s), {removed_events} event(s), "
        f"{removed_note_lines} note line(s), {removed_vault_lines} vault line(s), "
        f"{'all' if removed_vectors < 0 else removed_vectors} vector(s), "
        f"{removed_insights} insight(s), {removed_graph} graph item(s)"
    )
    if sidecar_note:
        print(sidecar_note, file=sys.stderr)
    if cache_note:
        print(cache_note, file=sys.stderr)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
