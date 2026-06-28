#!/usr/bin/env python3
"""Ingest the CURATED auto-memory into the NockBrain fact store (Finding A5).

The Claude Code project keeps a hand-curated, canonical auto-memory at
``~/.claude/projects/-home-nock-Dev-crm-mira/memory/`` — one Markdown file per
durable fact (``feedback_*.md``, ``project_*.md``, ``reference_*.md``) holding
the fleet roster, NockLock/product pricing, ownership, standing corrections,
etc., plus a ``MEMORY.md`` index. nock-brain recall (``budget-recall.py``) reads
``~/.nock-brain/facts.json`` and never saw these files, so recall fired but
could not return the curated roster/pricing. This script extracts each curated
file as ONE high-confidence, SIGNED fact and writes it into the store so recall
surfaces it.

Properties:
  * One fact per curated file (the ``MEMORY.md`` index is skipped — it is a
    table of contents, not a fact).
  * Each fact is signed with the SAME Ed25519/HMAC pipeline every other fact
    uses (``bin/_sign.py``), so claim-guard and ``verify-facts.py`` still pass.
  * Idempotent: re-running first drops every existing ``curated-*`` fact, then
    re-ingests, so the curated slice is always a clean mirror of the directory.
  * STAGING-SAFE: ``--store`` points the read+write at any facts.json copy. It
    never touches a store you do not name. The signing KEY is only READ.

Usage:
    # Stage: copy live -> staging, ingest into staging, verify recall.
    cp ~/.nock-brain/facts.json /tmp/nb-staging-facts.json
    python3 bin/ingest-curated-memory.py --store /tmp/nb-staging-facts.json
    python3 bin/budget-recall.py --facts /tmp/nb-staging-facts.json \
        "remind me what NockLock pricing is and who is on the consumer team"
"""
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

from _sign import (  # noqa: E402
    DEFAULT_KEY_PATH,
    DEFAULT_PUB_PATH,
    load_or_create_key,
    sign_fact,
    verify_fact,
)

DEFAULT_MEMORY_DIR = (
    Path.home()
    / ".claude"
    / "projects"
    / "-home-nock-Dev-crm-mira"
    / "memory"
)
DEFAULT_STORE = Path.home() / ".nock-brain" / "facts.json"

# Provenance tag — distinct so the curated slice is trivially found, re-ingested,
# or purged, and so a fact's origin is obvious in recall output.
CURATED_SOURCE = "curated-memory"
CURATED_ID_PREFIX = "curated-"
CURATED_CONFIDENCE = 0.95  # >= 0.9 per A5: high-confidence canonical truth.

# The index file is a table of contents, not a standalone fact.
SKIP_FILES = {"MEMORY.md"}

# Map the curated `type` (frontmatter metadata.type) to a nock-brain `kind`. We
# pick DURABLE kinds (long half-life in budget-recall's RECENCY_HALF_LIFE_DAYS)
# so canonical truths do not decay out of recall over months. The original type
# is preserved on `curated_type` for traceability.
TYPE_TO_KIND = {
    "feedback": "correction",   # standing corrections/directives — 180d half-life
    "project": "architecture",  # product/system canon — 180d half-life
    "reference": "architecture",
}
DEFAULT_KIND = "architecture"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a curated Markdown file into (frontmatter dict, body).

    Frontmatter is a leading ``---`` ... ``---`` block. We parse the few fields
    we need (name, description, metadata.type) with a tiny line parser rather
    than pulling in PyYAML — the curated files are simple and we control them.
    A file without frontmatter returns ({}, whole-text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_fm, body = parts[1], parts[2]

    fm: dict[str, Any] = {}
    in_metadata = False
    for line in raw_fm.splitlines():
        if not line.strip():
            continue
        indented = line[0] in (" ", "\t")
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "metadata" and not value:
            in_metadata = True
            continue
        if in_metadata and indented:
            fm[f"metadata.{key}"] = value
            continue
        in_metadata = False
        fm[key] = value
    return fm, body.strip()


def _stable_id(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()  # nosec B324 - id only
    return f"{CURATED_ID_PREFIX}{digest[:12]}"


def _file_source_date(path: Path) -> str:
    """Use the file's mtime as source_date (YYYY-MM-DD). Recent + per-file
    varied, so all curated facts do NOT collide on a single date (which the
    recall diversity-cap would otherwise lump together) while staying fresh
    enough that recency decay never buries them."""
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%d")


def build_fact(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    name = fm.get("name") or path.stem
    description = fm.get("description", "").strip()
    curated_type = fm.get("metadata.type", "").strip().lower()
    kind = TYPE_TO_KIND.get(curated_type, DEFAULT_KIND)

    # Content leads with the name + description (the high-signal summary recall
    # excerpts from), then the full body for depth.
    header = f"[CURATED MEMORY: {name}]"
    if description:
        header += f" {description}"
    content = f"{header}\n\n{body}".strip()

    now = _now_iso()
    abs_path = str(path.resolve())
    return {
        "id": _stable_id(name),
        "kind": kind,
        "scope": "global",
        "status": "current",
        "confidence": CURATED_CONFIDENCE,
        "content": content,
        "source": CURATED_SOURCE,
        "source_file": path.name,
        "source_date": _file_source_date(path),
        "session": CURATED_SOURCE,
        "session_anchor": f"{abs_path}:1",
        "created_at": now,
        "last_seen_at": now,
        "subject": curated_type or "memory",
        "curated_type": curated_type or "memory",
        "curated_name": name,
        "evidence": [{"path": abs_path, "line": 1}],
    }


def collect_curated_facts(memory_dir: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(memory_dir.glob("*.md")):
        if path.name in SKIP_FILES:
            continue
        fact = build_fact(path)
        fid = fact["id"]
        if fid in seen_ids:
            # Two curated files resolving to the same id (duplicate `name:`):
            # keep the first, warn, so we never silently drop content.
            print(
                f"WARN: id collision {fid} between {seen_ids[fid]} and "
                f"{path.name}; keeping the first",
                file=sys.stderr,
            )
            continue
        seen_ids[fid] = path.name
        facts.append(fact)
    return facts


def ingest(
    memory_dir: Path,
    store_path: Path,
    *,
    key_path: Path = DEFAULT_KEY_PATH,
    pub_path: Path = DEFAULT_PUB_PATH,
) -> dict[str, Any]:
    if not store_path.exists():
        raise SystemExit(f"store not found: {store_path}")
    existing = json.loads(store_path.read_text(encoding="utf-8"))
    if not isinstance(existing, list):
        raise SystemExit(f"store is not a JSON list of facts: {store_path}")

    # Drop any prior curated slice so re-runs are a clean mirror (idempotent).
    before = len(existing)
    kept = [
        f
        for f in existing
        if not (isinstance(f, dict) and str(f.get("id", "")).startswith(CURATED_ID_PREFIX))
    ]
    removed = before - len(kept)

    curated = collect_curated_facts(memory_dir)

    # Sign each curated fact with the live signing key (READ only). Curated facts
    # have no parents, so an empty facts_by_id is correct.
    key = load_or_create_key(key_path, pub_path, create=False)
    for fact in curated:
        sign_fact(fact, key, facts_by_id={})

    merged = kept + curated

    # Write back with the same pretty-print the store already uses.
    store_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Self-verify the slice we just wrote (proves signatures are valid).
    facts_by_id = {f.get("id"): f for f in merged}
    statuses = {verify_fact(f, key, facts_by_id=facts_by_id) for f in curated}
    return {
        "store": str(store_path),
        "removed_prior_curated": removed,
        "ingested": len(curated),
        "total_after": len(merged),
        "verify_statuses": sorted(statuses),
        "key_id": key.key_id,
        "alg": key.alg,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    ap.add_argument("--store", type=Path, default=DEFAULT_STORE,
                    help="facts.json to read+write (use a staging copy to be safe)")
    ap.add_argument("--key-path", type=Path, default=DEFAULT_KEY_PATH)
    ap.add_argument("--pub-path", type=Path, default=DEFAULT_PUB_PATH)
    args = ap.parse_args()

    if not args.memory_dir.exists():
        raise SystemExit(f"curated memory dir not found: {args.memory_dir}")

    result = ingest(
        args.memory_dir, args.store,
        key_path=args.key_path, pub_path=args.pub_path,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
