"""Private local-store write helpers for NockBrain artifacts."""
# Deferred annotations keep this importable on Python 3.9 (stock macOS
# /usr/bin/python3, which non-interactive shells resolve): PEP 604 unions
# in signatures are a def-time TypeError before 3.10.
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_mkdir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed or ".nock-brain" in path.expanduser().parts:
        path.chmod(DIR_MODE)


def secure_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    secure_mkdir(path.parent)
    path.write_text(text, encoding=encoding)
    path.chmod(FILE_MODE)


def secure_write_json(path: Path, value: Any, **json_kwargs: Any) -> None:
    secure_write_text(path, json.dumps(value, **json_kwargs))


def secure_copyfile(src: Path, dst: Path) -> None:
    secure_mkdir(dst.parent)
    shutil.copyfile(src, dst)
    dst.chmod(FILE_MODE)


# --- Verified writers (S4: projection receipts with readback) ---------------
# A derived write is not "applied" until its on-disk bytes have been read back
# and hash-matched against what we intended to write. An unverifiable write is
# reported as state "ambiguous" — a first-class outcome the caller must decide
# on, never an exception and never silently assumed ok (a stale export froze
# recall for 3 days in July without a sound).


def _ambiguous(path: Path, why: str) -> dict[str, Any]:
    return {"path": str(path), "verified": False, "state": "ambiguous", "error": why}


def _verify_readback(path: Path, intended: bytes) -> dict[str, Any]:
    """Read `path` back and compare its bytes against `intended`."""
    try:
        actual = path.read_bytes()
    except Exception as exc:  # noqa: BLE001 — any readback failure is ambiguous
        return _ambiguous(path, f"readback failed: {exc}")
    intended_hash = hashlib.sha256(intended).hexdigest()
    actual_hash = hashlib.sha256(actual).hexdigest()
    if actual_hash != intended_hash:
        return _ambiguous(
            path, f"readback hash mismatch: wrote {intended_hash}, read {actual_hash}"
        )
    return {
        "path": str(path),
        "sha256": actual_hash,
        "bytes": len(actual),
        "verified": True,
    }


def secure_write_text_verified(
    path: Path, text: str, *, encoding: str = "utf-8"
) -> dict[str, Any]:
    """secure_write_text + readback verification. Returns a receipt, never raises."""
    try:
        intended = text.encode(encoding)
        secure_write_text(path, text, encoding=encoding)
    except Exception as exc:  # noqa: BLE001 — disk state is unknown: ambiguous
        return _ambiguous(path, f"write failed: {exc}")
    return _verify_readback(path, intended)


def secure_write_json_verified(path: Path, value: Any, **json_kwargs: Any) -> dict[str, Any]:
    """secure_write_json + readback verification. Returns a receipt, never raises."""
    return secure_write_text_verified(path, json.dumps(value, **json_kwargs))


def secure_copyfile_verified(src: Path, dst: Path) -> dict[str, Any]:
    """secure_copyfile + readback verification. Returns a receipt, never raises."""
    try:
        intended = src.read_bytes()
        secure_copyfile(src, dst)
    except Exception as exc:  # noqa: BLE001 — disk state is unknown: ambiguous
        return _ambiguous(dst, f"copy failed: {exc}")
    return _verify_readback(dst, intended)
