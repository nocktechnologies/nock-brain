#!/usr/bin/env python3
"""Consolidate the 2026-05-19 operational-noise bulk in the nock-brain store (N8382).

The May-19 ingest bulk is ~66% of the store and dominated by point-in-time
operational events (boot/status, dispatch, merge, config) with near-zero durable
recall value. The per-source_date diversity cap (N8142) suppresses it per-query;
this removes it at the SOURCE by flipping the noise facts to status=superseded
(recall drops superseded), shrinking the store without deleting history.

DETERMINISTIC selection (all gated source_date startswith 2026-05-19):
  Tier A  — pure operational-noise KINDS: status, dispatch, merge, config.
  Tier B  — near-duplicate extras WITHIN durable kinds (directive, decision,
            bug, architecture, content): group by (kind, normalized first-6-words
            with #N / PR-N / bare-digit tokens stripped); for clusters of >=3,
            keep the highest-confidence fact and flag the rest.
  PRESERVE — every durable fact not caught by Tier B, and NEVER kind=correction
            (highest standing-order value — hard-excluded from all tiers).

SAFETY: default is --dry-run (read-only): writes a manifest of candidate ids for
Kevin/Mira eyeball and mutates NOTHING. --execute is gated and additionally
requires --i-have-reviewed-the-manifest; it backs up facts.json first, flips
status to superseded (+ superseded_at, supersession_reason), and leaves the
ed25519 attestation intact (the signature covers core content, not lifecycle
status — verify with the brain verify CLI after).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

STORE = Path.home() / ".nock-brain" / "facts.json"
MANIFEST = Path.home() / ".nock-brain" / "consolidation-may19-manifest.json"

MAY19 = "2026-05-19"
TIER_A_KINDS = {"status", "dispatch", "merge", "config"}
# durable kinds eligible for Tier B near-dup pruning; correction is NEVER touched
TIER_B_KINDS = {"directive", "decision", "bug", "architecture", "content"}
NEVER_TOUCH = {"correction"}
TIER_B_MIN_CLUSTER = 3

_ID_TOKEN = re.compile(r"(#\d+|\bpr[-\s]?\d+\b|\bnock[-\s]?\d+\b|\bn\d+\b|\b\d+\b)", re.I)
_WS = re.compile(r"\s+")


def _load() -> list[dict]:
    data = json.loads(STORE.read_text())
    return data if isinstance(data, list) else data.get("facts", [])


def _conf(f: dict) -> float:
    try:
        return float(f.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _norm_key(f: dict) -> str:
    text = (f.get("content") or f.get("subject") or "").lower()
    text = _ID_TOKEN.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return " ".join(text.split()[:6])


def select(rows: list[dict]) -> dict:
    may19 = [f for f in rows if str(f.get("source_date", "")).startswith(MAY19)]
    tier_a, tier_b, preserved = [], [], []

    for f in may19:
        kind = f.get("kind")
        if kind in NEVER_TOUCH:
            preserved.append(f)
        elif kind in TIER_A_KINDS:
            tier_a.append(f)
        # durable kinds handled in the Tier B pass below

    # Tier B: near-dup clusters within durable kinds (excluding correction)
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    durable = [f for f in may19 if f.get("kind") in TIER_B_KINDS]
    for f in durable:
        clusters[(f.get("kind"), _norm_key(f))].append(f)
    tier_b_ids = set()
    for members in clusters.values():
        if len(members) >= TIER_B_MIN_CLUSTER:
            members_sorted = sorted(members, key=_conf, reverse=True)
            for loser in members_sorted[1:]:  # keep the highest-confidence one
                tier_b.append(loser)
                tier_b_ids.add(loser.get("id"))
    preserved += [f for f in durable if f.get("id") not in tier_b_ids]

    def entry(f, tier, reason):
        return {
            "id": f.get("id"),
            "kind": f.get("kind"),
            "tier": tier,
            "confidence": _conf(f),
            "reason": reason,
            "snippet": (f.get("content") or f.get("subject") or "")[:120],
        }

    candidates = (
        [entry(f, "A", f"operational-noise kind={f.get('kind')}") for f in tier_a]
        + [entry(f, "B", f"near-dup within kind={f.get('kind')}") for f in tier_b]
    )
    return {
        "may19_total": len(may19),
        "tier_a": tier_a,
        "tier_b": tier_b,
        "preserved": preserved,
        "candidates": candidates,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="apply the supersede flip (gated)")
    ap.add_argument("--i-have-reviewed-the-manifest", action="store_true")
    args = ap.parse_args()

    rows = _load()
    sel = select(rows)
    kinds_a = defaultdict(int)
    for f in sel["tier_a"]:
        kinds_a[f.get("kind")] += 1
    kinds_pres = defaultdict(int)
    for f in sel["preserved"]:
        kinds_pres[f.get("kind")] += 1

    total_archive = len(sel["candidates"])
    print(f"store total: {len(rows)}")
    print(f"May-19 facts: {sel['may19_total']}")
    print(f"Tier A (operational-noise): {len(sel['tier_a'])}  {dict(kinds_a)}")
    print(f"Tier B (near-dup durable):  {len(sel['tier_b'])}")
    print(f"PRESERVE (May-19 durable):  {len(sel['preserved'])}  {dict(kinds_pres)}")
    print(f"ARCHIVE candidates total:   {total_archive}")
    print(f"corrections touched: {sum(1 for c in sel['candidates'] if c['kind']=='correction')} (must be 0)")
    print(f"post-archive store size:    {len(rows) - total_archive}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "store": str(STORE),
        "store_total": len(rows),
        "may19_total": sel["may19_total"],
        "tier_a_count": len(sel["tier_a"]),
        "tier_b_count": len(sel["tier_b"]),
        "preserved_count": len(sel["preserved"]),
        "archive_total": total_archive,
        "post_archive_total": len(rows) - total_archive,
        "candidate_ids": [c["id"] for c in sel["candidates"]],
        "candidates": sel["candidates"],
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"\nmanifest written: {MANIFEST}")

    if not args.execute:
        print("\nDRY-RUN only — nothing mutated. Review the manifest, then re-run with")
        print("  --execute --i-have-reviewed-the-manifest")
        return 0

    # ---- gated execute ----
    if not args.i_have_reviewed_the_manifest:
        print("\nREFUSING --execute without --i-have-reviewed-the-manifest", file=sys.stderr)
        return 2
    if any(c["kind"] == "correction" for c in sel["candidates"]):
        print("REFUSING --execute with correction in candidates", file=sys.stderr)
        return 2
    backup = STORE.with_suffix(f".json.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    shutil.copy2(STORE, backup)
    print(f"backup written: {backup}")
    ids = {c["id"] for c in sel["candidates"]}
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for f in rows:
        if f.get("id") in ids:
            f["status"] = "superseded"
            f["superseded_at"] = now
            f["supersession_reason"] = "N8382 May-19 operational-noise consolidation"
            n += 1
    STORE.write_text(json.dumps(rows, indent=2))
    print(f"superseded {n} facts; store now {len(rows)} rows ({len(rows)-n} current).")
    print("NEXT: run the brain verify CLI to confirm signatures still validate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
