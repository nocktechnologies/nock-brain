"""Store-backend contract for the authoritative fact store (E2, phases P1/P2).

One contract, two backends:

- ``JsonStore`` wraps today's ``facts.json`` behavior exactly (load via
  ``_facts.load_facts``, write via ``_store.secure_write_json``) — the default.
- ``SqliteStore`` holds the same facts in a single WAL-mode SQLite database
  (``brain.db``), value-identically: modeled fields become columns, structured
  fields (``evidence``, ``attestation``) are stored as canonical JSON text, and
  every unmodeled or non-scalar field rides an ``extra`` JSON column so any
  fact round-trips losslessly. Attestations are preserved verbatim — they sign
  fact VALUES, not container bytes, so migration needs no re-signing (design:
  docs/specs/2026-07-28-e2-sqlite-store-design.md §2).

Backend selection (``resolve_store``) is deliberately conservative: JSON is
the default and stays authoritative until SQLite is EXPLICITLY selected via
``NOCKBRAIN_STORE=sqlite`` or a ``store-v2`` marker file next to ``brain.db``.
``NOCKBRAIN_STORE=json`` always forces JSON — the reversible kill-switch, same
doctrine as NOCKBRAIN_LIVE_RECALL.

This module is reachable from the recall hook, so it stays Python-3.9
importable and stdlib-only (``sqlite3`` is stdlib; FTS5/sqlite-vec are later
optional accelerators, never required).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

BIN_DIR = Path(__file__).resolve().parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from _facts import filter_valid_facts, load_facts as _load_facts_json
from _store import FILE_MODE, secure_copyfile, secure_mkdir, secure_write_json

DB_FILENAME = "brain.db"
MARKER_FILENAME = "store-v2"
ENV_VAR = "NOCKBRAIN_STORE"
SCHEMA_VERSION = 1

# Modeled scalar columns. Everything else — unknown fields, explicit nulls,
# non-scalar values in a modeled slot — rides the `extra` JSON column, which is
# what makes the round-trip lossless for facts this schema has never heard of.
FACT_COLUMNS = (
    "id", "kind", "content", "status", "confidence", "source", "source_date",
    "valid_at", "invalid_at", "superseded_by", "superseded_at",
    "supersession_reason", "session", "session_anchor", "created_at",
    "last_seen_at",
)
# Structured fields stored as canonical JSON text in their own columns.
JSON_COLUMNS = ("evidence", "attestation")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, kind TEXT, content TEXT, status TEXT, confidence REAL,
  source TEXT, source_date TEXT, valid_at TEXT, invalid_at TEXT,
  superseded_by TEXT, superseded_at TEXT, supersession_reason TEXT,
  session TEXT, session_anchor TEXT, created_at TEXT, last_seen_at TEXT,
  evidence TEXT, attestation TEXT, extra TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS facts_status_kind ON facts(status, kind);
CREATE INDEX IF NOT EXISTS facts_superseded_by ON facts(superseded_by);
-- Ship in the schema, stay unpopulated until P5: insights and embeddings are
-- derived artifacts whose live sources remain insights.json / embeddings.npz.
CREATE TABLE IF NOT EXISTS insights (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS embeddings (
  fact_id TEXT PRIMARY KEY REFERENCES facts(id),
  model_id TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL
);
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fact_to_row(fact: "dict[str, Any]") -> "tuple[Any, ...]":
    """Split a fact into modeled columns + JSON columns + the extra spill.

    Only string/int/float values occupy modeled columns; explicit ``None`` and
    any other type go to ``extra`` so a reload reproduces the original dict
    (keys the fact never had are never fabricated)."""
    columns: "dict[str, Any]" = {}
    extra: "dict[str, Any]" = {}
    for key, value in fact.items():
        if key in JSON_COLUMNS:
            columns[key] = _canonical_json(value)
        elif key in FACT_COLUMNS and isinstance(value, (str, int, float)) \
                and not isinstance(value, bool):
            columns[key] = value
        else:
            extra[key] = value
    row = [columns.get(name) for name in FACT_COLUMNS]
    row.extend(columns.get(name) for name in JSON_COLUMNS)
    row.append(_canonical_json(extra))
    return tuple(row)


def _row_to_fact(row: "tuple[Any, ...]") -> "dict[str, Any]":
    fact: "dict[str, Any]" = {}
    offset = len(FACT_COLUMNS)
    for i, name in enumerate(FACT_COLUMNS):
        if row[i] is not None:
            fact[name] = row[i]
    for j, name in enumerate(JSON_COLUMNS):
        if row[offset + j] is not None:
            fact[name] = json.loads(row[offset + j])
    fact.update(json.loads(row[offset + len(JSON_COLUMNS)]))
    return fact


class JsonStore:
    """Today's facts.json behavior, unchanged, behind the contract."""

    kind = "json"

    def __init__(self, facts_path: Path):
        self.facts_path = Path(facts_path)
        # The verify cache stats this file to decide freshness.
        self.freshness_path = self.facts_path

    def load_facts(self, required_fields: "set[str] | None" = None) -> "list[dict]":
        return _load_facts_json(self.facts_path, required_fields=required_fields)

    def replace_all(self, facts: "list[dict]") -> None:
        secure_write_json(self.facts_path, facts, indent=2, default=str)

    def snapshot(self, dest: Path) -> None:
        secure_copyfile(self.facts_path, Path(dest))

    def export_facts_json(self, dest: Path) -> None:
        dest = Path(dest)
        if dest.resolve() == self.facts_path.resolve():
            return
        secure_copyfile(self.facts_path, dest)

    def describe(self) -> str:
        return f"json:{self.facts_path}"


class SqliteStore:
    """The same facts in one WAL-mode SQLite file, value-identically."""

    kind = "sqlite"

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.freshness_path = self.db_path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=2.0)
        # Degrade to the hook's fail-open-to-empty contract instead of hanging.
        con.execute("PRAGMA busy_timeout=2000")
        return con

    def _chmod_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                sidecar.chmod(FILE_MODE)

    def create(self, store_uuid: str = "") -> None:
        secure_mkdir(self.db_path.parent)
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            if store_uuid:
                con.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('store_uuid', ?)",
                    (store_uuid,),
                )
            con.commit()
        finally:
            con.close()
        self._chmod_files()

    def meta(self) -> "dict[str, str]":
        con = self._connect()
        try:
            return dict(con.execute("SELECT key, value FROM meta"))
        finally:
            con.close()

    def set_meta(self, key: str, value: str) -> None:
        con = self._connect()
        try:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
            con.commit()
        finally:
            con.close()

    def load_facts(self, required_fields: "set[str] | None" = None) -> "list[dict]":
        columns = ", ".join(FACT_COLUMNS + JSON_COLUMNS + ("extra",))
        con = self._connect()
        try:
            rows = con.execute(f"SELECT {columns} FROM facts ORDER BY rowid").fetchall()
        finally:
            con.close()
        facts = [_row_to_fact(row) for row in rows]
        return filter_valid_facts(
            facts, source=str(self.db_path), required_fields=required_fields
        )

    def replace_all(self, facts: "list[dict]") -> None:
        placeholders = ", ".join("?" for _ in range(len(FACT_COLUMNS) + len(JSON_COLUMNS) + 1))
        columns = ", ".join(FACT_COLUMNS + JSON_COLUMNS + ("extra",))
        rows = [_fact_to_row(f) for f in facts if isinstance(f, dict)]
        con = self._connect()
        try:
            with con:  # one transaction: readers never see a half-replaced store
                con.execute("DELETE FROM facts")
                con.executemany(f"INSERT INTO facts ({columns}) VALUES ({placeholders})", rows)
        finally:
            con.close()
        self._chmod_files()

    def snapshot(self, dest: Path) -> None:
        dest = Path(dest)
        secure_mkdir(dest.parent)
        con = self._connect()
        try:
            con.execute("VACUUM INTO ?", (str(dest),))
        finally:
            con.close()
        dest.chmod(FILE_MODE)

    def export_facts_json(self, dest: Path) -> None:
        secure_write_json(Path(dest), self.load_facts(), indent=2, default=str)

    def describe(self) -> str:
        return f"sqlite:{self.db_path}"


def resolve_store(facts_path: Path, env: "os._Environ | dict | None" = None):
    """Pick the backend for a store rooted at ``facts_path``'s directory.

    JSON is authoritative by default. SQLite engages only when explicitly
    selected: ``NOCKBRAIN_STORE=sqlite``, or a ``store-v2`` marker file next to
    an existing ``brain.db`` (the deliberate cutover artifact). Setting
    ``NOCKBRAIN_STORE=json`` always forces JSON — instant rollback."""
    env = os.environ if env is None else env
    facts_path = Path(facts_path)
    store_dir = facts_path.parent
    db_path = store_dir / DB_FILENAME
    choice = str(env.get(ENV_VAR, "")).strip().lower()
    if choice == "json":
        return JsonStore(facts_path)
    if choice == "sqlite":
        return SqliteStore(db_path)
    if (store_dir / MARKER_FILENAME).exists() and db_path.exists():
        return SqliteStore(db_path)
    return JsonStore(facts_path)
