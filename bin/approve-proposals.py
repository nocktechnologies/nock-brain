#!/usr/bin/env python3
"""Release reviewed fact proposals into the live store — the gate.

propose-facts.py writes new facts to a review queue (proposed-facts.json) and
NEVER touches the live store. This script is the deliberate, reversible release
step: approved proposals are stripped of their proposal metadata, set
status="current", and merged into facts.json; rejected ones are dropped from the
queue; the rest stay pending.

Nothing here re-signs the store — signing stays a separate pass (sign-facts.py /
rebuild-store.py), exactly as it is for extract-facts.py writes.

Usage:
    python3 approve-proposals.py --list                      # show pending
    python3 approve-proposals.py --approve <id> [<id> ...]   # release specific
    python3 approve-proposals.py --approve-all               # release all pending
    python3 approve-proposals.py --reject <id> [<id> ...]    # drop from queue
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import load_facts
from _store import secure_write_json, secure_write_text


def _load_module(name: str):
    """Load a hyphenated sibling script as a module (cached, no package structure)."""
    mod_name = name.replace("-", "_")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, BIN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sync_markdown(queue_path: Path, remaining: list[dict]) -> None:
    """Keep the human-readable .md review file in step with the JSON queue:
    re-render it from what's still pending, or remove it once the queue drains."""
    md_path = queue_path.with_suffix(".md")
    if remaining:
        render = _load_module("propose-facts").render_markdown
        secure_write_text(md_path, render(remaining), encoding="utf-8")
    elif md_path.exists():
        md_path.unlink()

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"
DEFAULT_QUEUE = Path.home() / ".nock-brain" / "proposed-facts.json"
PROPOSAL_ONLY_FIELDS = ("proposed_at", "actions")


def _to_current(proposal: dict) -> dict:
    """Strip proposal-only metadata and mark the fact current for the live store."""
    fact = {k: v for k, v in proposal.items() if k not in PROPOSAL_ONLY_FIELDS}
    fact["status"] = "current"
    return fact


def run(argv: list[str] | None = None) -> int:
    """Release approved proposals into the live store and re-sync the pending queue."""
    parser = argparse.ArgumentParser(description="Release reviewed fact proposals into the live store")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--approve", nargs="*", default=[], metavar="ID")
    parser.add_argument("--approve-all", action="store_true")
    parser.add_argument("--reject", nargs="*", default=[], metavar="ID")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    queued = load_facts(args.queue) if args.queue.exists() else []

    if args.list or not (args.approve or args.approve_all or args.reject):
        print(f"{len(queued)} proposal(s) pending in {args.queue}:")
        for p in queued:
            content = str(p.get("content", "")).strip().replace("\n", " ")[:90]
            print(f"  {p.get('id', '')} [{p.get('kind', '?')}] {content}")
        if not (args.approve or args.approve_all or args.reject):
            return 0

    approve_ids = set(args.approve)
    reject_ids = set(args.reject)

    # Surface typos / stale IDs instead of silently ignoring them.
    queued_ids = {p.get("id") for p in queued}
    unknown = (approve_ids | reject_ids) - queued_ids
    if unknown:
        print(f"Warning: ID(s) not in queue, ignored: {', '.join(sorted(unknown))}", file=sys.stderr)

    to_release, remaining = [], []
    for p in queued:
        pid = p.get("id")
        if pid in reject_ids:
            continue  # dropped from the queue, never written
        if args.approve_all or pid in approve_ids:
            to_release.append(_to_current(p))
        else:
            remaining.append(p)

    if to_release:
        live = load_facts(args.facts) if args.facts.exists() else []
        live_ids = {f.get("id") for f in live}
        merged = live + [f for f in to_release if f.get("id") not in live_ids]
        secure_write_json(args.facts, merged, indent=2, default=str)

    # Rewrite the queue (and its markdown view) only if something changed.
    if len(remaining) != len(queued):
        secure_write_json(args.queue, remaining, indent=2, default=str)
        _sync_markdown(args.queue, remaining)

    print(f"Released {len(to_release)} into {args.facts}; "
          f"rejected {len(reject_ids)}; {len(remaining)} still pending.")
    if to_release:
        print("Reminder: run sign-facts.py to re-sign the store after release.")
    return 0


def main() -> int:
    """CLI entry point."""
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
