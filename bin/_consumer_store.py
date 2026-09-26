"""Strict, explicit-path customer store and reviewed proposal publication."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _sign
from _store import secure_replace_bytes


class ConsumerError(ValueError):
    """A customer boundary or publication precondition failed."""


SCHEMA = "nockbrain-customer/v1"
PROPOSAL_SCHEMA = "nockbrain-customer-proposal/v1"
FACT_LIMIT = 32 * 1024 * 1024
SMALL_LIMIT = 16 * 1024
SOURCE_LIMIT = 8 * 1024 * 1024
MAX_SOURCES = 16
MAX_CANDIDATES = 1000
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEX128 = re.compile(r"[0-9a-f]{128}\Z")
_CANDIDATE_FIELDS = frozenset({
    "id", "kind", "content", "confidence", "scope", "status", "source",
    "source_file", "source_date", "created_at", "subject", "evidence",
})
_EVIDENCE_FIELDS = frozenset({"store_id", "key_id", "sha256", "path", "line", "event_id"})
_ATTESTATION_FIELDS = frozenset({
    "fact_id", "canonical_fact_hash", "source_hash", "alg", "key_id",
    "signature", "parent_fact_ids", "signed_at",
})


def canonical_bytes(value: Any) -> bytes:
    """Canonical ASCII JSON, rejecting nonfinite numbers and unsupported shapes."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConsumerError("invalid JSON value") from exc


def _json(data: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConsumerError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ConsumerError("nonfinite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ConsumerError("nonfinite JSON number")
        return number

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=nonfinite, parse_float=finite_float)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ConsumerError("invalid JSON file") from exc


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ConsumerError("store path must be absolute")
    return path


def _safe_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConsumerError("required directory is missing") from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or
            info.st_uid != os.getuid()):
        raise ConsumerError("unsafe customer directory")


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink, info.st_uid)


def read_regular(path: Path, limit: int) -> bytes:
    """Read one regular file without following its final symlink or accepting drift."""
    path = Path(path)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ConsumerError("invalid read limit")
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ConsumerError("unsafe regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or _fingerprint(opened) != _fingerprint(before):
                raise ConsumerError("file changed during read")
            if opened.st_size > limit:
                raise ConsumerError("file exceeds size limit")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(limit + 1)
            after = os.fstat(fd)
            current = path.lstat()
            if (len(data) > limit or len(data) != opened.st_size or
                    _fingerprint(after) != _fingerprint(opened) or
                    _fingerprint(current) != _fingerprint(opened)):
                raise ConsumerError("file changed during read")
            return data
        finally:
            os.close(fd)
    except ConsumerError:
        raise
    except OSError as exc:
        raise ConsumerError("cannot read regular file") from exc


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _read_owned(path: Path, limit: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConsumerError("required customer file is missing") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise ConsumerError("unsafe customer file")
    data = read_regular(path, limit)
    if _fingerprint(path.lstat()) != _fingerprint(info):
        raise ConsumerError("customer file changed during read")
    return data


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def init_store(path: Path) -> dict:
    path = _absolute(path)
    if not path.parent.is_dir():
        raise ConsumerError("store parent must exist")
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        raise ConsumerError("store destination already exists or cannot be created") from exc
    # An interrupted initialization stays private and incomplete; it is never adopted.
    try:
        _write_new(path / "facts.json", b"[]\n")
        os.mkdir(path / "proposals", 0o700)
        key = _sign.load_or_create_key(path / "signing-key", path / "signing-key.pub")
        manifest = {
            "schema": SCHEMA,
            "store_id": str(uuid.uuid4()),
            "key_id": key.key_id,
            "algorithm": key.alg,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_new(path / "customer.json", canonical_bytes(manifest) + b"\n")
        return manifest
    except Exception as exc:
        raise ConsumerError("customer initialization incomplete") from exc


def _text(value: Any, max_len: int = 1500) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= max_len and
            bool(value.strip()) and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value))


def _content(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 1500:
        return False
    try:
        value.encode("utf-8", errors="strict")
        return True
    except UnicodeError:
        return False


def _safe_path(value: Any) -> bool:
    if not _text(value, 1024):
        return False
    parts = value.split("/")
    if value.startswith("/"):
        parts = parts[1:]
    return (bool(parts) and not any(part in {"", ".", ".."} for part in parts) and
            "\\" not in value)


def _date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


def _captured_keys(private_bytes: bytes, public_bytes: bytes, manifest: dict):
    """Construct both key views from bounded, no-follow captured bytes only."""
    private = _json(private_bytes)
    public = _json(public_bytes)
    algorithm = manifest["algorithm"]
    key_id = manifest["key_id"]
    if algorithm == _sign.ALG_ED25519:
        private_fields = {"alg", "key_id", "private_key"}
        public_fields = {"alg", "key_id", "public_key"}
        material_fields = ((private, "private_key"), (public, "public_key"))
    else:
        private_fields = public_fields = {"alg", "key_id", "secret"}
        material_fields = ((private, "secret"), (public, "secret"))
    if (not isinstance(private, dict) or not isinstance(public, dict) or
            set(private) != private_fields or set(public) != public_fields or
            not isinstance(key_id, str) or
            not re.fullmatch(re.escape(algorithm) + r":[0-9a-f]{16}", key_id) or
            private["alg"] != algorithm or public["alg"] != algorithm or
            private["key_id"] != key_id or public["key_id"] != key_id or
            any(not isinstance(doc[field], str) or
                not _DIGEST.fullmatch(doc[field]) for doc, field in material_fields)):
        raise ConsumerError("invalid customer signing keys")
    if algorithm == _sign.ALG_ED25519 and not _sign._HAVE_CRYPTOGRAPHY:
        raise ConsumerError("Ed25519 customer key requires cryptography")
    signing_key = _sign._load_key_from_doc(private)
    if algorithm == _sign.ALG_ED25519:
        verifier = _sign.SigningKey(
            _sign.ALG_ED25519,
            ed_public=_sign.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public["public_key"])))
    else:
        verifier = _sign.SigningKey(_sign.ALG_HMAC,
                                    hmac_secret=bytes.fromhex(public["secret"]))
    challenge = b"nockbrain-customer-key-match"
    if (signing_key.key_id != key_id or verifier.key_id != key_id or
            not verifier.verify_bytes(challenge, signing_key.sign_bytes(challenge))):
        raise ConsumerError("customer signing keys do not match")
    return signing_key


class ConsumerStore:
    """One locked customer JSON store; all paths are explicit and local."""

    def __init__(self, path: Path):
        self._path = _absolute(path)
        self._manifest: dict = {}
        self._facts: list[dict] = []
        self._generation = ""
        self._lock_fd: int | None = None
        self._key = None
        self._captured: dict[str, bytes | None] = {}

    @property
    def path(self) -> Path:
        return self._path

    @property
    def manifest(self) -> dict:
        """A copy of the verified customer identity for callers to inspect."""
        return deepcopy(self._manifest)

    @property
    def facts(self) -> list[dict]:
        """A copy of the verified authoritative records for callers to inspect."""
        return deepcopy(self._facts)

    @property
    def generation(self) -> str:
        return self._generation

    def __enter__(self) -> "ConsumerStore":
        _safe_dir(self.path)
        _safe_dir(self.path / "proposals")
        lock = self.path / ".customer.lock"
        try:
            self._lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(self._lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_mode & 0o077 or info.st_uid != os.getuid() or
                    lock.lstat().st_ino != info.st_ino):
                raise ConsumerError("unsafe customer lock")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            self._load()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _capture(self) -> dict[str, bytes | None]:
        _safe_dir(self.path)
        _safe_dir(self.path / "proposals")
        for forbidden in ("store-v2", "brain.db"):
            if (self.path / forbidden).is_symlink() or (self.path / forbidden).exists():
                raise ConsumerError("SQLite customer store is unsupported")
        result: dict[str, bytes | None] = {}
        for name, limit in (("customer.json", SMALL_LIMIT), ("signing-key", SMALL_LIMIT),
                            ("signing-key.pub", SMALL_LIMIT), ("facts.json", FACT_LIMIT)):
            result[name] = _read_owned(self.path / name, limit)
        for name in ("revocations.jsonl", "purged-ids.jsonl"):
            target = self.path / name
            if target.is_symlink() or target.exists():
                data = _read_owned(target, FACT_LIMIT)
                if data.strip():
                    raise ConsumerError("customer lifecycle sidecar is nonempty")
                result[name] = data
            else:
                result[name] = None
        return result

    def _load(self) -> None:
        self._captured = self._capture()
        manifest = _json(self._captured["customer.json"])
        if (not isinstance(manifest, dict) or set(manifest) !=
                {"schema", "store_id", "key_id", "algorithm", "created_at"} or
                manifest["schema"] != SCHEMA or not _uuid(manifest["store_id"]) or
                not _text(manifest["key_id"], 128) or
                not isinstance(manifest["algorithm"], str) or
                manifest["algorithm"] not in {_sign.ALG_ED25519, _sign.ALG_HMAC} or
                not _timestamp(manifest["created_at"])):
            raise ConsumerError("invalid customer manifest")
        self._manifest = manifest
        try:
            self._key = _captured_keys(self._captured["signing-key"],
                                       self._captured["signing-key.pub"], manifest)
        except ConsumerError:
            raise
        except Exception as exc:
            raise ConsumerError("invalid customer signing keys") from exc
        facts = _json(self._captured["facts.json"])
        if not isinstance(facts, list):
            raise ConsumerError("facts must be an array")
        self._validate_facts(facts, require_attestation=True)
        if self._capture() != self._captured:
            raise ConsumerError("customer store changed during opening")
        self._facts = facts
        self._generation = hashlib.sha256(self._captured["facts.json"]).hexdigest()

    def _validate_facts(self, facts: list, *, require_attestation: bool) -> None:
        if not isinstance(facts, list):
            raise ConsumerError("facts must be an array")
        seen = set()
        for fact in facts:
            self._validate_candidate(fact, signed=require_attestation)
            if fact["id"] in seen:
                raise ConsumerError("duplicate customer fact ID")
            seen.add(fact["id"])
        if require_attestation:
            result = _sign.verify_facts(facts, self._key)
            if result["valid"] != len(facts):
                raise ConsumerError("customer fact signature invalid")

    def _validate_candidate(self, fact: Any, *, signed: bool = False) -> None:
        fields = _CANDIDATE_FIELDS | ({"attestation"} if signed else set())
        if not isinstance(fact, dict) or set(fact) != fields:
            raise ConsumerError("invalid customer fact fields")
        if (not _text(fact["kind"], 64) or not _content(fact["content"]) or
                not _text(fact["subject"], 256) or not _safe_path(fact["source_file"]) or
                not _date(fact["source_date"]) or not _timestamp(fact["created_at"]) or
                fact["scope"] != "global" or fact["status"] != "current" or
                fact["source"] != "customer:" + self._manifest["store_id"] or
                isinstance(fact["confidence"], bool) or
                not isinstance(fact["confidence"], (int, float)) or
                not 0 <= fact["confidence"] <= 1 or
                not math.isfinite(fact["confidence"])):
            raise ConsumerError("invalid customer fact value")
        expected_id = "customer-" + hashlib.sha256(canonical_bytes([
            self._manifest["store_id"], fact["kind"], fact["content"]])).hexdigest()
        if fact["id"] != expected_id:
            raise ConsumerError("customer fact ID mismatch")
        evidence = fact["evidence"]
        if not isinstance(evidence, list) or not evidence or len(evidence) > 1000:
            raise ConsumerError("invalid customer evidence")
        for entry in evidence:
            if (not isinstance(entry, dict) or set(entry) != _EVIDENCE_FIELDS or
                    entry["store_id"] != self._manifest["store_id"] or
                    entry["key_id"] != self._manifest["key_id"] or
                    not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"]) or
                    not _safe_path(entry["path"]) or
                    not isinstance(entry["line"], int) or isinstance(entry["line"], bool) or
                    entry["line"] < 1 or not _text(entry["event_id"], 256)):
                raise ConsumerError("invalid customer evidence")
        if signed:
            att = fact["attestation"]
            signature_pattern = (_HEX128 if self._manifest["algorithm"] == _sign.ALG_ED25519
                                 else _DIGEST)
            if (not isinstance(att, dict) or set(att) != _ATTESTATION_FIELDS or
                    att["fact_id"] != fact["id"] or
                    not isinstance(att["canonical_fact_hash"], str) or
                    not _DIGEST.fullmatch(att["canonical_fact_hash"]) or
                    not isinstance(att["source_hash"], str) or
                    not _DIGEST.fullmatch(att["source_hash"]) or
                    att["key_id"] != self._manifest["key_id"] or
                    att["alg"] != self._manifest["algorithm"] or
                    not isinstance(att["signature"], str) or
                    not signature_pattern.fullmatch(att["signature"]) or
                    att["parent_fact_ids"] != [] or
                    not _timestamp(att["signed_at"])):
                raise ConsumerError("invalid customer attestation")

    @staticmethod
    def _check_receipts(candidates: list, sources: list) -> None:
        anchors = {(source["path"], source["sha256"]) for source in sources}
        for fact in candidates:
            if fact["source_file"] != Path(fact["evidence"][0]["path"]).name:
                raise ConsumerError("candidate source filename mismatch")
            for evidence in fact["evidence"]:
                if (evidence["path"], evidence["sha256"]) not in anchors:
                    raise ConsumerError("candidate evidence has no source receipt")

    def _validate_proposal_inputs(self, candidates: Any, sources: Any, stats: Any) -> None:
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ConsumerError("too many candidates")
        self._validate_facts(candidates, require_attestation=False)
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
            raise ConsumerError("too many source receipts")
        for source in sources:
            if (not isinstance(source, dict) or set(source) != {"path", "sha256", "format"} or
                    not _safe_path(source["path"]) or not isinstance(source["sha256"], str) or
                    not _DIGEST.fullmatch(source["sha256"]) or
                    not isinstance(source["format"], str) or
                    source["format"] not in {"markdown", "claude-jsonl"}):
                raise ConsumerError("invalid source receipt")
        self._check_receipts(candidates, sources)
        if (not isinstance(stats, dict) or not {"files", "candidates", "overlong_skipped"} <= set(stats) or
                any(not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", k) or
                    not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in stats.items()) or
                stats["files"] != len(sources) or stats["candidates"] != len(candidates)):
            raise ConsumerError("invalid proposal statistics")

    def save_proposal(self, candidates: list, sources: list, stats: dict) -> str:
        self._validate_proposal_inputs(candidates, sources, stats)
        proposal = {"schema": PROPOSAL_SCHEMA, "store_id": self._manifest["store_id"],
                    "key_id": self._manifest["key_id"], "generation": self._generation,
                    "candidates": candidates, "sources": sources, "stats": stats}
        data = canonical_bytes(proposal)
        if len(data) > FACT_LIMIT:
            raise ConsumerError("proposal exceeds size limit")
        digest = hashlib.sha256(data).hexdigest()
        destination = self.path / "proposals" / (digest + ".json")
        if destination.exists() or destination.is_symlink():
            if read_regular(destination, FACT_LIMIT) != data:
                raise ConsumerError("proposal changed")
            return digest
        fd, temporary = tempfile.mkstemp(prefix=".proposal-", dir=self.path / "proposals")
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if read_regular(destination, FACT_LIMIT) != data:
                raise ConsumerError("proposal changed")
        finally:
            os.unlink(temporary)
        return digest

    def read_proposal(self, digest: str) -> dict:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ConsumerError("proposal digest must be full SHA-256")
        data = _read_owned(self.path / "proposals" / (digest + ".json"), FACT_LIMIT)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ConsumerError("proposal digest changed")
        proposal = _json(data)
        if (not isinstance(proposal, dict) or set(proposal) !=
                {"schema", "store_id", "key_id", "generation", "candidates", "sources", "stats"} or
                proposal["schema"] != PROPOSAL_SCHEMA or
                proposal["store_id"] != self._manifest["store_id"] or
                proposal["key_id"] != self._manifest["key_id"] or
                not isinstance(proposal["generation"], str) or
                not _DIGEST.fullmatch(proposal["generation"])):
            raise ConsumerError("invalid proposal envelope")
        self._validate_proposal_inputs(proposal["candidates"], proposal["sources"], proposal["stats"])
        return proposal

    def apply_proposal(self, digest: str) -> dict:
        proposal = self.read_proposal(digest)
        if proposal["generation"] != self._generation:
            raise ConsumerError("stale proposal")
        seen = {fact["id"] for fact in self._facts}
        new = []
        skipped = 0
        for candidate in proposal["candidates"]:
            if candidate["id"] in seen:
                skipped += 1
            else:
                new.append(dict(candidate))
                seen.add(candidate["id"])
        if new:
            _sign.sign_facts(new, self._key)
        merged = self._facts + new
        self._validate_facts(merged, require_attestation=True)
        if self._capture() != self._captured:
            raise ConsumerError("customer store changed during publication")
        if new:
            output = canonical_bytes(merged) + b"\n"
            if len(output) > FACT_LIMIT:
                raise ConsumerError("facts exceed size limit")
            try:
                replaced = secure_replace_bytes(
                    self.path / "facts.json", output,
                    before_replace=lambda: self._capture() == self._captured)
                if not replaced:
                    raise ConsumerError("customer store changed during publication")
            except Exception as exc:
                raise ConsumerError("customer publication failed") from exc
            self._facts = merged
            self._captured["facts.json"] = output
            self._generation = hashlib.sha256(output).hexdigest()
        return {"added": len(new), "skipped": skipped, "total": len(merged)}
