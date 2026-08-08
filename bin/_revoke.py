"""Signed revocation events (S1): supersession itself becomes attested.

Fact attestations deliberately sign only the immutable core, so lifecycle
marks (status, superseded_by) stay cheap to write — but that leaves one gap:
nothing stops an editor from flipping a superseded fact's status back to
current without breaking any signature. This module closes it. Every
supersession appends a signed, append-only event whose signature covers
exactly the fields that authorize the revocation:

    {superseded_id, superseding_id, reason, superseded_at}

Verification then has teeth in both directions: a superseded fact without a
valid event is *unattested* (legacy, warn), and a CURRENT fact that a valid
event says is dead is *resurrected* — the attack, and a hard failure.

Events live in ``revocations.jsonl`` next to the facts store (0600,
append-only). Signed under the same store key and algorithm family as fact
attestations, with its own domain separator so a revocation signature can
never be confused with a fact signature.
"""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import json
import os
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
    SigningKey,
    load_or_create_key,
)
from _store import FILE_MODE  # noqa: E402

SCHEMA = "nockbrain-revocation/v1"
REVOCATIONS_FILENAME = "revocations.jsonl"
_DOMAIN = b"nockbrain-revocation-v1\n"
_PAYLOAD_FIELDS = ("superseded_id", "superseding_id", "reason", "superseded_at")


def _canonical_payload(event: "dict[str, Any]") -> bytes:
    body = {field: str(event.get(field, "")) for field in _PAYLOAD_FIELDS}
    return _DOMAIN + json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_revocation(
    key: SigningKey,
    *,
    superseded_id: str,
    superseding_id: str = "",
    reason: str = "",
    superseded_at: "str | None" = None,
) -> "dict[str, Any]":
    """Build and sign one revocation event."""
    event: "dict[str, Any]" = {
        "schema": SCHEMA,
        "superseded_id": str(superseded_id),
        "superseding_id": str(superseding_id),
        "reason": str(reason),
        "superseded_at": superseded_at or datetime.now(timezone.utc).isoformat(),
    }
    event["alg"] = key.alg
    event["key_id"] = key.key_id
    event["signature"] = key.sign_bytes(_canonical_payload(event))
    event["signed_at"] = datetime.now(timezone.utc).isoformat()
    return event


def verify_revocation(event: Any, key: SigningKey) -> bool:
    """True iff the event's signature verifies over its own payload fields."""
    if not isinstance(event, dict):
        return False
    signature = event.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    if event.get("alg") != key.alg:
        return False
    return key.verify_bytes(_canonical_payload(event), signature)


def append_revocation(path: Path, event: "dict[str, Any]") -> None:
    """Append one event to the sidecar (0600); never rewrites prior lines."""
    path = Path(path)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    path.chmod(FILE_MODE)


def load_revocations(path: Path) -> "list[dict[str, Any]]":
    path = Path(path)
    if not path.exists():
        return []
    events: "list[dict[str, Any]]" = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def audit(
    facts: "list[dict[str, Any]]",
    events: "list[dict[str, Any]]",
    key: SigningKey,
) -> "dict[str, Any]":
    """Cross-check facts against revocation events.

    - ``resurrected``: a fact a VALID event revokes, present with a status
      other than superseded — the silent-resurrection attack. Hard failure.
    - ``unattested_superseded``: superseded facts with no valid event —
      legacy marks from before S1; reported, not fatal by default.
    - ``invalid_events``: events whose signature does not verify (tampered
      or wrong key)."""
    valid_ids: "set[str]" = set()
    invalid = 0
    for event in events:
        if verify_revocation(event, key):
            valid_ids.add(str(event.get("superseded_id", "")))
        else:
            invalid += 1
    facts_by_id = {
        str(f.get("id", "")): f for f in facts if isinstance(f, dict)
    }
    resurrected = sorted(
        fid for fid in valid_ids
        if fid in facts_by_id and facts_by_id[fid].get("status") != "superseded"
    )
    unattested = sorted(
        fid for fid, fact in facts_by_id.items()
        if fact.get("status") == "superseded" and fid not in valid_ids
    )
    return {
        "attested": sum(
            1 for event in events if verify_revocation(event, key)
        ),
        "invalid_events": invalid,
        "resurrected": resurrected,
        "unattested_superseded": unattested,
    }


def resolve_signing_key() -> "SigningKey | None":
    """The store's signing key, or None when unavailable (mark-only mode).

    Env overrides mirror the recall path: NOCKBRAIN_SIGNING_KEY /
    NOCKBRAIN_SIGNING_PUB."""
    key_path = Path(os.environ.get("NOCKBRAIN_SIGNING_KEY", DEFAULT_KEY_PATH))
    pub_path = Path(os.environ.get("NOCKBRAIN_SIGNING_PUB", DEFAULT_PUB_PATH))
    try:
        return load_or_create_key(key_path, pub_path, create=False)
    except (FileNotFoundError, RuntimeError, ValueError, KeyError, OSError):
        return None


def record_supersessions(
    facts_path: Path,
    marked: "list[dict[str, Any]]",
) -> "tuple[int, str | None]":
    """Sign+append one event per freshly-marked fact; never blocks marking.

    Returns (events_written, warning). With no signing key available the
    marks stand but the events are skipped with a loud warning — strict
    verification will then report them as unattested."""
    if not marked:
        return 0, None
    key = resolve_signing_key()
    if key is None:
        return 0, (
            "revocation UNSIGNED (no signing key available) — supersession "
            "marks stand but are unattested until backfilled"
        )
    sidecar = Path(facts_path).parent / REVOCATIONS_FILENAME
    written = 0
    for fact in marked:
        event = sign_revocation(
            key,
            superseded_id=str(fact.get("id", "")),
            superseding_id=str(fact.get("superseded_by", "") or ""),
            reason=str(fact.get("supersession_reason", "") or ""),
            superseded_at=str(
                fact.get("superseded_at", "")
            ) or None,
        )
        append_revocation(sidecar, event)
        written += 1
    return written, None
