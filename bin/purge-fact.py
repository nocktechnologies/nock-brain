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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _embed import DEFAULT_SIDECAR, EmbedUnavailable, load_sidecar, save_sidecar
from _channel_frame import CHANNEL_FRAME_RE
from _facts import TOMBSTONES_FILENAME
from _store import FILE_MODE, secure_write_json, secure_write_text
from _storeback import resolve_store
from _verify_cache import cache_path_for, unlink_for_store

DEFAULT_ROOT = Path.home() / ".nock-brain"
ENTITY_BACKLINK_DIRS = {"agents", "projects", "people", "concepts"}


def matches_text(text: str, patterns: list[str]) -> bool:
    haystack = text.lower()
    return any(pattern.lower() in haystack for pattern in patterns if pattern)


def starts_with_prefix(text: str, prefixes: list[str] | None) -> bool:
    return any(text.lstrip().startswith(prefix) for prefix in prefixes or [] if prefix)


def fact_matches(fact: dict[str, Any], fact_id: str, patterns: list[str],
                 content_prefixes: list[str] | None = None) -> bool:
    """Match id exactly, a literal pattern, or a content prefix.

    Patterns must not search the whole JSON dump: attestation signatures are
    hex and a substring match would purge unrelated facts (N10028). Prefixes
    are content-only, so a runbook can remove structural wrappers without
    matching an ordinary fact that merely mentions the marker.
    """
    if fact_id and fact.get("id") == fact_id:
        return True
    content = str(fact.get("content", ""))
    if starts_with_prefix(content, content_prefixes):
        return True
    haystack = f"{fact.get('id', '')}\n{content}"
    return matches_text(haystack, patterns)


def fact_event_ids(facts: list[dict[str, Any]]) -> set[str]:
    event_ids: set[str] = set()
    for fact in facts:
        for evidence in fact.get("evidence", []):
            event_id = evidence.get("event_id") if isinstance(evidence, dict) else ""
            if event_id:
                event_ids.add(str(event_id))
    return event_ids


def purge_facts(path: Path, fact_id: str, patterns: list[str],
                content_prefixes: list[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    store = resolve_store(path)
    facts = store.load_facts()
    removed = [fact for fact in facts if fact_matches(
        fact, fact_id, patterns, content_prefixes)]
    kept = [fact for fact in facts if fact not in removed]
    return kept, len(removed)


def purge_events(path: Path, event_ids: set[str], patterns: list[str],
                 content_prefixes: list[str] | None = None,
                 remove_channel_frames: bool = False) -> tuple[str, int]:
    if not path.exists():
        return "", 0
    kept: list[str] = []
    removed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            drop = False
            try:
                event = json.loads(line)
                content = str(event.get("content", ""))
                drop = (
                    str(event.get("id", "")) in event_ids
                    or (
                        remove_channel_frames
                        and next(CHANNEL_FRAME_RE.finditer(content), None) is not None
                    )
                    or starts_with_prefix(content, content_prefixes)
                )
            except json.JSONDecodeError:
                drop = False
            if not drop:
                drop = matches_text(line, patterns)
            if drop:
                removed += 1
            else:
                kept.append(line)
    return "".join(kept), removed


def purge_text_tree(
    root: Path,
    patterns: list[str],
    content_prefixes: list[str] | None = None,
    remove_channel_frames: bool = False,
    skip_paths: set[Path] | None = None,
) -> tuple[dict[Path, str], int, int, dict[Path, int]]:
    if not root.exists():
        return {}, 0, 0, {}
    rewrites: dict[Path, str] = {}
    removed_blocks = 0
    removed_lines = 0
    unmatched_openers: dict[Path, int] = {}
    skipped = skip_paths or set()
    paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        if path in skipped:
            continue
        try:
            original_text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = original_text
        frame_matches = list(CHANNEL_FRAME_RE.finditer(original_text)) if remove_channel_frames else []
        if remove_channel_frames:
            matched_spans = [(match.start(), match.end()) for match in frame_matches]
            unmatched = sum(
                not any(start <= opener.start() < end for start, end in matched_spans)
                for opener in re.finditer(r"(?m)^[ \t]*<channel\s+source=", original_text)
            )
            if unmatched:
                unmatched_openers[path] = unmatched
        if frame_matches:
            kept: list[str] = []
            cursor = 0
            for match in frame_matches:
                kept.append(original_text[cursor:match.start()])
                cursor = match.end()
            kept.append(original_text[cursor:])
            text = "".join(kept)
            removed_blocks += len(frame_matches)
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        for line in lines:
            if starts_with_prefix(line, content_prefixes) or matches_text(line, patterns):
                removed_lines += 1
            else:
                kept.append(line)
        rewritten = "".join(kept)
        if rewritten != original_text:
            rewrites[path] = rewritten
    return rewrites, removed_blocks, removed_lines, unmatched_openers


def vault_frontmatter_id(path: Path) -> str:
    """Read a per-fact vault mirror's id from its leading frontmatter."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    frontmatter = re.match(
        r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not frontmatter:
        return ""
    match = re.search(r"(?m)^id:[ \t]*(.*?)[ \t]*$", frontmatter.group(1))
    if not match:
        return ""
    value = match.group(1)
    if value[:1] in {"'", '"'}:
        quote = value[:1]
        closing = value.find(quote, 1)
        trailing = value[closing + 1:].strip() if closing >= 0 else ""
        if closing < 0 or trailing and not trailing.startswith("#"):
            return ""
        return value[1:closing]
    return value.split(" #", 1)[0].rstrip()


def purge_vault_backlinks(
    vault: Path,
    mirror_files: set[Path],
    rewrites: dict[Path, str],
    skip_paths: set[Path],
) -> dict[Path, str]:
    """Remove vault lines that link to deleted vault mirror files."""
    if not mirror_files or not vault.is_dir():
        return {}
    targets: set[str] = set()
    for path in mirror_files:
        relative = path.relative_to(vault).with_suffix("").as_posix()
        targets.update((path.stem, relative, f"{relative}.md"))
    explicit_targets = {
        target for target in targets
        if "/" in target or target.endswith(".md")
    }
    backlink_rewrites: dict[Path, str] = {}
    for path in sorted(candidate for candidate in vault.rglob("*") if candidate.is_file()):
        if path in skip_paths:
            continue
        original_text = rewrites.get(path)
        if original_text is None:
            try:
                original_text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
        list_targets = targets if path.parent.name in ENTITY_BACKLINK_DIRS else explicit_targets
        removable_lines = {f"- [[{target}]]" for target in list_targets}
        if path.parent.name == "decisions":
            removable_lines.update(
                f"See [[{target}]] for the full fact note." for target in targets)
        kept = []
        for line in original_text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped in removable_lines:
                continue
            kept.append(line)
        rewritten = "".join(kept)
        if rewritten != original_text:
            backlink_rewrites[path] = rewritten
    return backlink_rewrites


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
    parser.add_argument("--content-prefix", action="append", default=[],
                        help="Match facts whose content starts with this literal prefix")
    parser.add_argument("--facts", type=Path, default=DEFAULT_ROOT / "facts.json")
    parser.add_argument("--events", type=Path, default=DEFAULT_ROOT / "events.jsonl")
    parser.add_argument("--notes-dir", type=Path, default=DEFAULT_ROOT / "sessions")
    parser.add_argument("--vault", type=Path, default=DEFAULT_ROOT / "vault")
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--apply", action="store_true", help="Rewrite files; otherwise dry-run only")
    args = parser.parse_args(argv)

    if not args.fact_id and not args.pattern and not args.content_prefix:
        parser.error("provide a fact_id, --pattern, or --content-prefix")

    store = resolve_store(args.facts)
    kept_facts, removed_facts = purge_facts(
        args.facts, args.fact_id, args.pattern, args.content_prefix)
    removed_fact_records = [
        fact for fact in store.load_facts()
        if fact_matches(fact, args.fact_id, args.pattern, args.content_prefix)
    ]
    patterns = list(args.pattern)
    for fact in removed_fact_records:
        content = str(fact.get("content", ""))
        if (
            args.fact_id and fact.get("id") == args.fact_id
        ) or matches_text(f"{fact.get('id', '')}\n{content}", args.pattern):
            patterns.append(content)
    event_ids = fact_event_ids(removed_fact_records)
    channel_prefixes = [
        prefix for prefix in args.content_prefix
        if prefix.lstrip().startswith("<channel")
    ]
    other_prefixes = [
        prefix for prefix in args.content_prefix
        if prefix not in channel_prefixes
    ]
    vault_paths = {
        path for path in args.vault.rglob("*") if path.is_file()
    } if args.vault.is_dir() else set()
    removed_ids = {str(fact.get("id")) for fact in removed_fact_records
                   if fact.get("id")}
    vault_mirror_ids = {
        path: mirror_id
        for path in vault_paths
        if path != args.vault / "index.md"
        if (mirror_id := vault_frontmatter_id(path))
    }
    vault_mirror_files = {
        path for path, mirror_id in vault_mirror_ids.items()
        if mirror_id in removed_ids
    }
    kept_events, removed_events = purge_events(
        args.events, event_ids, patterns, other_prefixes, bool(channel_prefixes))
    note_rewrites, removed_note_blocks, removed_note_lines, note_unmatched = purge_text_tree(
        args.notes_dir, patterns, other_prefixes, bool(channel_prefixes))
    vault_rewrites, removed_vault_blocks, removed_vault_lines, vault_unmatched = purge_text_tree(
        args.vault, patterns, other_prefixes, bool(channel_prefixes), set(vault_mirror_ids))
    vault_rewrites.update(purge_vault_backlinks(
        args.vault, vault_mirror_files, vault_rewrites, vault_mirror_files))
    unmatched_openers = dict(note_unmatched)
    for path, count in vault_unmatched.items():
        unmatched_openers[path] = unmatched_openers.get(path, 0) + count
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
        for path in vault_mirror_files:
            path.unlink()
        if kept_insights is not None and removed_insights:
            secure_write_json(insights_path, kept_insights, indent=2, default=str)
        if kept_graph is not None and removed_graph:
            secure_write_json(graph_path, kept_graph, indent=2, default=str)
    elif removed_facts and cache_path.exists():
        cache_note = f"would delete verification cache {cache_path}"

    print(
        f"{'would remove' if not args.apply else 'removed'} "
        f"{removed_facts} fact(s), {removed_events} event(s), "
        f"{removed_note_blocks} note block(s), {removed_vault_blocks} vault block(s), "
        f"{len(vault_mirror_files)} vault mirror file(s), "
        f"{sum(unmatched_openers.values())} unmatched opener(s), "
        f"{removed_note_lines} note line(s), {removed_vault_lines} vault line(s), "
        f"{'all' if removed_vectors < 0 else removed_vectors} vector(s), "
        f"{removed_insights} insight(s), {removed_graph} graph item(s)"
    )
    for path, count in sorted(unmatched_openers.items()):
        print(f"{count} unmatched opener(s): {path}")
    if sidecar_note:
        print(sidecar_note, file=sys.stderr)
    if cache_note:
        print(cache_note, file=sys.stderr)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
