#!/usr/bin/env python3
"""Build or update the vector sidecar for the fact store.

Phase 1 of the semantic-recall spec. Default is an incremental sync: embed
facts missing from the sidecar, re-embed facts whose content hash changed,
prune vectors whose fact no longer exists. --backfill re-embeds everything.

The sidecar is derived data. A model swap invalidates it wholesale (the
loader rejects a sidecar built by a different model) — with the raw static
encoder a full re-embed of a few thousand facts takes seconds, so recovery
is always "run this tool again".

Run with the interpreter that has numpy + tokenizers — canonically the venv
that `install.sh --semantic` creates. Bare `python3` is PATH/alias-dependent
(Homebrew builds ship without tokenizers) and often lacks them.

Usage:
    ~/.nock-brain/venv/bin/python3 embed-facts.py   # canonical
    python3 embed-facts.py                 # incremental sync
    python3 embed-facts.py --backfill      # re-embed the whole store
    python3 embed-facts.py --facts ~/.nock-brain/facts.json
    NOCKBRAIN_EMBED_STUB=1 python3 embed-facts.py   # test/CI stub encoder
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _embed import (
    DEFAULT_SIDECAR,
    EmbedUnavailable,
    get_encoder,
    sync_sidecar,
)
from _facts import load_facts

DEFAULT_FACTS = Path.home() / ".nock-brain" / "facts.json"


def run(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Embed facts into the vector sidecar")
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--sidecar", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--model-dir", type=Path, default=None,
                        help="Embedding model directory "
                             "(default ~/.nock-brain/model, "
                             "or NOCKBRAIN_EMBED_MODEL_DIR)")
    parser.add_argument("--backfill", action="store_true",
                        help="Re-embed every fact, ignoring the existing "
                             "sidecar")
    parser.add_argument("--batch", type=int, default=256)
    args = parser.parse_args(argv)

    if not args.facts.exists():
        print(f"no fact store at {args.facts}", file=sys.stderr)
        return 1

    try:
        encoder = get_encoder(args.model_dir)
    except EmbedUnavailable as exc:
        print(f"embed-facts: {exc}", file=sys.stderr)
        return 1

    facts = load_facts(args.facts)
    try:
        stats = sync_sidecar(facts, encoder, args.sidecar,
                             full=args.backfill, batch_size=max(1, args.batch))
    except EmbedUnavailable as exc:
        print(f"embed-facts: {exc}", file=sys.stderr)
        return 1

    mode = "backfill" if args.backfill else "sync"
    print(
        f"{mode}: {stats['total']} vector(s) in {args.sidecar} "
        f"(embedded {stats['embedded']}, kept {stats['kept']}, "
        f"pruned {stats['pruned']}) model={encoder.model_id} dim={encoder.dim}"
    )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
