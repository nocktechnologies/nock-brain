"""Read explicitly selected customer notes into reviewable, unsigned candidates."""
from __future__ import annotations

import hashlib
import importlib.util
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _consumer_store import (ConsumerError, MAX_CANDIDATES, MAX_SOURCES,
                             SOURCE_LIMIT, canonical_bytes, read_regular, _json)
from _scrub import is_structural_noise, scrub_secrets

TOTAL_SOURCE_LIMIT = 32 * 1024 * 1024


def _sibling(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, BIN_DIR / filename)
    if spec is None or spec.loader is None:
        raise ConsumerError("import adapter unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_extract = _sibling("_consumer_extract_facts", "extract-facts.py")
_ingest = _sibling("_consumer_ingest_jsonl", "ingest-jsonl.py")


def _safe_source(path: Path) -> Path:
    if not path.is_absolute():
        raise ConsumerError("source path must be absolute")
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise ConsumerError("source symlink is denied")
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConsumerError("source path cannot be resolved") from exc
    for candidate in (path, resolved):
        value = str(candidate)
        parts = candidate.parts
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ConsumerError("invalid source path encoding") from exc
        if (len(value) > 1024 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
                or "\\" in value or any(part.casefold() in {".ssh", ".gnupg"} for part in parts)
                or candidate.name.casefold().startswith("signing-key")
                or _ingest._matches_any(value, _ingest.DEFAULT_PATH_DENYLIST)):
            raise ConsumerError("source path is denied")
    return resolved


def _mtime_timestamp(mtime_ns: int) -> str:
    seconds, nanoseconds = divmod(mtime_ns, 1_000_000_000)
    return (datetime.fromtimestamp(seconds, timezone.utc) +
            timedelta(microseconds=nanoseconds // 1000)).isoformat()


def _source_timestamp(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return fallback
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return fallback


def _candidate(manifest: dict, kind: str, content: str, confidence: float,
               actor: str, path: str, sha256: str, line: int, event_id: str,
               timestamp: str) -> dict:
    store_id = manifest["store_id"]
    return {
        "id": "customer-" + hashlib.sha256(canonical_bytes([store_id, kind, content])).hexdigest(),
        "kind": kind, "content": content, "confidence": confidence,
        "scope": "global", "status": "current", "source": "customer:" + store_id,
        "source_file": Path(path).name, "source_date": timestamp[:10],
        "created_at": timestamp, "subject": actor,
        "evidence": [{"store_id": store_id, "key_id": manifest["key_id"],
                      "sha256": sha256, "path": path, "line": line, "event_id": event_id}],
    }


def _append(candidates: list[dict], by_id: dict[str, dict], manifest: dict,
            content: str, actor: str, path: str, sha256: str, line: int,
            event_id: str, timestamp: str, stats: dict) -> None:
    content = content.strip()
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ConsumerError("invalid source content encoding") from exc
    if not content or is_structural_noise(content):
        return
    result = _extract.classify_bullet(content)
    if result is None:
        return
    kind, confidence = result
    if not _extract.authority_fact_allowed(kind, content, actor=actor):
        return
    if len(content) > 1500:
        stats["overlong_skipped"] += 1
        return
    candidate = _candidate(manifest, kind, content, confidence, actor, path,
                           sha256, line, event_id, timestamp)
    existing = by_id.get(candidate["id"])
    if existing is not None:
        evidence = candidate["evidence"][0]
        if evidence not in existing["evidence"]:
            if len(existing["evidence"]) >= 1000:
                raise ConsumerError("too much candidate evidence")
            existing["evidence"].append(evidence)
        return
    if len(candidates) >= MAX_CANDIDATES:
        raise ConsumerError("too many candidates")
    candidates.append(candidate)
    by_id[candidate["id"]] = candidate


def _valid_message(raw: dict) -> list[int]:
    """Validate text surfaces before line_events can coerce malformed shapes."""
    line_type = raw.get("type")
    if line_type not in {"user", "assistant"}:
        return []
    message = raw.get("message")
    if not isinstance(message, dict):
        raise ConsumerError("invalid JSONL message")
    role = message.get("role", line_type)
    if role != line_type:
        raise ConsumerError("JSONL message role mismatch")
    content = message.get("content")
    if isinstance(content, str):
        return [0] if content else []
    if not isinstance(content, list):
        raise ConsumerError("invalid JSONL content")
    text_indices = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise ConsumerError("invalid JSONL content part")
        part_type = part.get("type", "text")
        if not isinstance(part_type, str):
            raise ConsumerError("invalid JSONL content part")
        if part_type == "text":
            if not isinstance(part.get("text"), str):
                raise ConsumerError("invalid JSONL text part")
            if part["text"]:
                text_indices.append(index)
        elif part_type == "tool_use":
            if (not isinstance(part.get("name"), str) or
                    not isinstance(part.get("id", ""), str) or
                    not isinstance(part.get("input", {}), dict)):
                raise ConsumerError("invalid JSONL tool use")
        elif part_type == "tool_result":
            if not isinstance(part.get("tool_use_id", ""), str):
                raise ConsumerError("invalid JSONL tool result")
    return text_indices


def collect_candidates(sources: list[Path], format: str, manifest: dict) -> tuple[list[dict], list[dict], dict]:
    """Capture bounded explicit sources and return deterministic proposal inputs."""
    if format not in {"markdown", "claude-jsonl"}:
        raise ConsumerError("unsupported source format")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ConsumerError("invalid source count")
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("store_id"), str)
            or not isinstance(manifest.get("key_id"), str)):
        raise ConsumerError("invalid customer manifest")
    candidates: list[dict] = []
    by_id: dict[str, dict] = {}
    receipts: list[dict] = []
    stats = {"files": 0, "candidates": 0, "overlong_skipped": 0,
             "secrets_redacted": 0}
    if format == "claude-jsonl":
        stats.update(_ingest.new_stats())
    seen_paths: set[Path] = set()
    seen_files: set[tuple[int, int]] = set()
    total = 0
    for source in sources:
        path = _safe_source(Path(source))
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ConsumerError("unsafe regular file")
            if path in seen_paths or (before.st_dev, before.st_ino) in seen_files:
                raise ConsumerError("duplicate source file")
            if before.st_size > SOURCE_LIMIT or total + before.st_size > TOTAL_SOURCE_LIMIT:
                raise ConsumerError("source exceeds size limit")
            data = read_regular(path, min(SOURCE_LIMIT, TOTAL_SOURCE_LIMIT - total))
            after = path.lstat()
        except OSError as exc:
            raise ConsumerError("cannot read source file") from exc
        if (before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns
                or before.st_ino != after.st_ino or before.st_dev != after.st_dev):
            raise ConsumerError("source changed during read")
        seen_paths.add(path)
        seen_files.add((before.st_dev, before.st_ino))
        total += len(data)
        source_hash = hashlib.sha256(data).hexdigest()
        receipt_path, path_redactions = scrub_secrets(str(path))
        stats["secrets_redacted"] += path_redactions
        if (not receipt_path.startswith("/") or len(receipt_path) > 1024 or
                any(part in {"", ".", ".."} for part in receipt_path[1:].split("/"))):
            raise ConsumerError("unsafe source metadata")
        receipts.append({"path": receipt_path, "sha256": source_hash, "format": format})
        fallback = _mtime_timestamp(before.st_mtime_ns)
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ConsumerError("source is not UTF-8") from exc
        if format == "markdown":
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.startswith("- "):
                    continue
                scrubbed, count = scrub_secrets(line[2:])
                stats["secrets_redacted"] += count
                _append(candidates, by_id, manifest, scrubbed, "user", receipt_path,
                        source_hash, line_number, f"{source_hash}:{line_number}:0", fallback, stats)
        else:
            denied_tool_ids: set[str] = set()
            for line_number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                stats["lines_read"] += 1
                try:
                    raw = _json(line.encode("utf-8"))
                except ConsumerError as exc:
                    raise ConsumerError("invalid JSONL line") from exc
                if not isinstance(raw, dict):
                    raise ConsumerError("JSONL line must be an object")
                if not isinstance(raw.get("type"), str):
                    raise ConsumerError("invalid JSONL event type")
                if "isSidechain" in raw and not isinstance(raw["isSidechain"], bool):
                    raise ConsumerError("invalid JSONL sidechain flag")
                text_indices = _valid_message(raw)
                events = _ingest.line_events(raw, path, line_number, stats,
                                             include_sidechain=False,
                                             denied_tool_use_ids=denied_tool_ids)
                stats["events_written"] += len(events)
                text_events = [event for event in events if event["surface"] == "text"
                               and event["kind"] == "message"]
                if len(text_events) != (0 if raw.get("isSidechain") else len(text_indices)):
                    raise ConsumerError("invalid JSONL text events")
                timestamp = _source_timestamp(raw.get("timestamp"), fallback)
                for part_index, event in zip(text_indices, text_events):
                    _append(candidates, by_id, manifest, event["content"],
                            raw["type"], receipt_path, source_hash, line_number,
                            f"{source_hash}:{line_number}:{part_index}", timestamp, stats)
        stats["files"] += 1
    stats["candidates"] = len(candidates)
    return candidates, receipts, stats
