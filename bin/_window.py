"""Nonce-bound, watermarked job windows (S3+S8).

RESERVED — not wired into any job yet (operator call, 2026-08-23): the
nightlies already run fail-closed with backups, and no double-run harm has
been observed. Keep the module and its tests; wire it only when a real
double-run shows up. Do not delete as "dead code".

A nightly job that mutates the store must be idempotent and non-interleaving:
run twice on the same inputs and the second run is a no-op, not a double
mutation. This adapts Mira's harness admission-window pattern and Letta's
consolidation watermark into a small dependency-free primitive.

Protocol per job:
    verdict, token = open_window(state, job, inputs_digest)
    if verdict == "run":
        ... do the work ...
        settle(state, job, token, result_summary)
    else:  # "skip" — an identical settled run already exists
        ...

- ``open_window`` records an OPEN window under a fresh nonce and returns
  ("run", token). It returns ("skip", None) ONLY when the SAME job already has
  a SETTLED window whose digest matches — an identical, completed run.
- An OPEN-but-unsettled window (a crash before settle) never blocks: the next
  open re-runs. Fail-open toward doing the work — a missed run is worse than a
  repeated one for memory freshness.
- ``settle`` records the result under the token; a wrong/stale token is
  rejected, so a late writer from a superseded run cannot settle.

State is one small JSON file, last-writer-wins per job (the fleet runs one
distiller at a time by construction). A corrupt state file fails open to
"run" rather than wedging the nightly.
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve).
from __future__ import annotations

import hashlib
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _store import secure_write_json  # noqa: E402


def inputs_digest(paths: "list[Path]") -> str:
    """A stable digest over the exact input files a job consumes.

    Absent files contribute a deterministic marker rather than raising, so a
    job whose input has not been produced yet still gets a well-defined digest
    (and re-runs once it appears, changing the digest)."""
    hasher = hashlib.sha256()
    for path in paths:
        path = Path(path)
        hasher.update(str(path).encode("utf-8"))
        hasher.update(b"\0")
        try:
            hasher.update(path.read_bytes())
        except OSError:
            hasher.update(b"<absent>")
        hasher.update(b"\0")
    return hasher.hexdigest()


def _load(state_path: Path) -> "dict[str, Any]":
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # fail open: a corrupt/missing state never blocks a run


def open_window(state_path: Path, job: str, digest: str) -> "tuple[str, str | None]":
    """Admit one run of ``job`` for ``digest``. Returns ("run", token) to
    proceed, or ("skip", None) when an identical SETTLED run already exists."""
    state = _load(state_path)
    entry = state.get(job)
    if (isinstance(entry, dict) and entry.get("settled")
            and entry.get("digest") == digest):
        return "skip", None
    token = secrets.token_hex(16)
    state[job] = {
        "digest": digest,
        "nonce": token,
        "settled": False,
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    secure_write_json(Path(state_path), state, indent=2)
    return "run", token


def settle(state_path: Path, job: str, token: "str | None", result: str) -> bool:
    """Mark ``job``'s open window settled. True on success; False when the
    token does not match the current open window (a stale/superseded run)."""
    if not token:
        return False
    state = _load(state_path)
    entry = state.get(job)
    if not isinstance(entry, dict) or entry.get("nonce") != token \
            or entry.get("settled"):
        return False
    entry["settled"] = True
    entry["result"] = str(result)
    entry["settled_at"] = datetime.now(timezone.utc).isoformat()
    secure_write_json(Path(state_path), state, indent=2)
    return True
