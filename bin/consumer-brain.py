#!/usr/bin/env python3
"""Explicit, reviewed customer-store setup and import commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys

# A selected source may live in this checkout. Imports must not create pyc files.
sys.dont_write_bytecode = True
BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _consumer_import import collect_candidates
from _consumer_store import ConsumerError, ConsumerStore, init_store

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse normally echoes untrusted arguments, including terminal controls.
        raise ValueError("invalid command arguments; use --help")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Explicit customer Brain setup and reviewed imports")
    modes = parser.add_subparsers(dest="mode", parser_class=_Parser)
    for mode in ("init", "propose", "review", "apply"):
        command = modes.add_parser(mode)
        command.add_argument("--store", required=True, metavar="ABSOLUTE_PATH")
        if mode == "propose":
            command.add_argument("--format", required=True, choices=("markdown", "claude-jsonl"))
            command.add_argument("--source", required=True, action="append", metavar="ABSOLUTE_FILE")
        if mode in ("review", "apply"):
            command.add_argument("--proposal", required=True, metavar="SHA256")
    return parser


def _safe_path(value: str) -> Path:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConsumerError("path contains terminal controls")
    return Path(value)


def _command(mode: str, store: str, digest: str) -> str:
    args = ["python3", "bin/consumer-brain.py", mode, "--store", store,
            "--proposal", digest]
    return " ".join(shlex.quote(arg) for arg in args)


def _emit(value: dict, *, pretty: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                     indent=2 if pretty else None))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.mode is None:
            parser.print_help()
            return 0
        store_path = _safe_path(args.store)
        if args.mode == "propose":
            sources = [_safe_path(value) for value in args.source]
        if args.mode in ("review", "apply") and not _DIGEST.fullmatch(args.proposal):
            raise ConsumerError("proposal digest must be full SHA-256")
        if args.mode == "init":
            manifest = init_store(store_path)
            _emit({"store_id": manifest["store_id"], "algorithm": manifest["algorithm"],
                   "store": args.store,
                   "next": "Select explicit notes, then run propose --store PATH --format markdown --source FILE."})
        elif args.mode == "propose":
            with ConsumerStore(store_path) as store:
                candidates, receipts, stats = collect_candidates(sources, args.format, store.manifest)
                digest = store.save_proposal(candidates, receipts, stats)
            _emit({"candidates": len(candidates), "proposal": digest, "stats": stats,
                   "review_command": _command("review", args.store, digest)})
        elif args.mode == "review":
            with ConsumerStore(store_path) as store:
                proposal = store.read_proposal(args.proposal)
            _emit({"proposal": args.proposal, "review": proposal,
                   "apply_command": _command("apply", args.store, args.proposal)},
                  pretty=True)
        else:
            with ConsumerStore(store_path) as store:
                result = store.apply_proposal(args.proposal)
            _emit(result)
        return 0
    except ConsumerError as exc:
        _emit({"error": str(exc)})
        return 2
    except ValueError:
        _emit({"error": "invalid command arguments; use --help"})
        return 2
    except Exception:
        _emit({"error": "customer operation failed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
