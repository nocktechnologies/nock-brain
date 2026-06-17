"""Tests for the rebuild-and-promote orchestrator (N8070).

The point of N8070 is that the live store never silently rots: one command
builds into staging, HARD-gates on health, signs, exports, then atomically
promotes with timestamped backups. These tests assert the three load-bearing
safety properties:

  1. The health gate ABORTS (non-zero / RebuildError, no swap) when staging has
     a live-secret finding OR is not recall-ready.
  2. ``--dry-run`` performs NO swap -- the live store paths are byte-identical
     before and after.
  3. A successful promote creates timestamped backups BEFORE swapping in staging.

The heavy ingest/refine/health subprocess chain is stubbed where needed so the
tests are fast, deterministic, and NEVER touch the real ~/.nock-brain.
"""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str):
    path = BIN / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def rebuild_store():
    return _load("rebuild-store")


# --- helpers ---------------------------------------------------------------

def _healthy_report(facts=2, findings=0, recall_ready=True):
    return {
        "facts": {"count": facts, "malformed": []},
        "notes": {"count": facts},
        "privacy": {"live_secret_findings": findings, "live_secret_locations": []},
        "recall_ready": recall_ready,
    }


def _seed_live_store(store_dir: Path):
    """Write a minimal pre-existing live store so we can prove backup-before-swap."""
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "facts.json").write_text(
        json.dumps([{"id": "OLD", "content": "old live fact"}]), encoding="utf-8"
    )
    (store_dir / "sessions").mkdir(exist_ok=True)
    (store_dir / "sessions" / "old.md").write_text("# old session\n", encoding="utf-8")
    (store_dir / "review").mkdir(exist_ok=True)
    (store_dir / "review" / "promotion-candidates.json").write_text("[]", encoding="utf-8")
    (store_dir / "vault").mkdir(exist_ok=True)
    (store_dir / "vault" / "index.md").write_text("# old vault\n", encoding="utf-8")
    (store_dir / "graph.json").write_text('{"nodes": []}', encoding="utf-8")


def _seed_staging(staging_dir: Path):
    """Write a fully-built staging store (as if ingest->...->export already ran)."""
    sp = {
        "facts": staging_dir / "facts.json",
        "sessions": staging_dir / "sessions",
        "review": staging_dir / "review",
        "vault": staging_dir / "vault",
        "graph": staging_dir / "graph.json",
    }
    staging_dir.mkdir(parents=True, exist_ok=True)
    sp["facts"].write_text(
        json.dumps([{"id": "NEW", "content": "fresh staged fact"}]), encoding="utf-8"
    )
    sp["sessions"].mkdir(exist_ok=True)
    (sp["sessions"] / "new.md").write_text("# new session\n", encoding="utf-8")
    sp["review"].mkdir(exist_ok=True)
    (sp["review"] / "promotion-candidates.json").write_text("[]", encoding="utf-8")
    sp["vault"].mkdir(exist_ok=True)
    (sp["vault"] / "index.md").write_text("# new vault\n", encoding="utf-8")
    sp["graph"].write_text('{"nodes": [{"id": "fact:NEW"}]}', encoding="utf-8")
    return sp


# --- 1. HARD HEALTH GATE ---------------------------------------------------

def test_health_gate_aborts_on_live_secret_finding(rebuild_store):
    """A staging store with ANY live-secret finding must abort."""
    with pytest.raises(rebuild_store.RebuildError) as exc:
        rebuild_store.health_gate(_healthy_report(findings=1))
    assert "live-secret" in str(exc.value).lower()


def test_health_gate_aborts_when_not_recall_ready(rebuild_store):
    """A staging store that is not recall-ready must abort."""
    with pytest.raises(rebuild_store.RebuildError) as exc:
        rebuild_store.health_gate(_healthy_report(recall_ready=False))
    assert "recall-ready" in str(exc.value).lower()


def test_health_gate_passes_when_clean_and_ready(rebuild_store):
    """Clean + recall-ready must NOT raise."""
    rebuild_store.health_gate(_healthy_report(findings=0, recall_ready=True))


def test_rebuild_aborts_and_leaves_live_untouched_on_secret(rebuild_store, tmp_path, monkeypatch):
    """End-to-end: a secret finding aborts with non-zero exit, live store unchanged."""
    store_dir = tmp_path / "live"
    _seed_live_store(store_dir)
    before = {p.name: p.read_bytes() for p in store_dir.iterdir() if p.is_file()}

    staging_dir = tmp_path / "staging"

    # Stub the build to land a staging store + an UNHEALTHY report (secret found).
    def fake_build_staging(staging, transcripts, *, key_path, pub_path, merge_from=None):
        sp = _seed_staging(staging)
        return {
            "health": _healthy_report(findings=2),
            "ingest_stats": {},
            "stage_paths": {**sp, "events": staging / "events.jsonl",
                            "ingest_stats": staging / "ingest-stats.json"},
            "key_path": key_path,
            "pub_path": pub_path,
        }

    monkeypatch.setattr(rebuild_store, "build_staging", fake_build_staging)
    monkeypatch.setattr(rebuild_store, "discover_transcripts", lambda roots, days: [Path("fake.jsonl")])
    # sign/export must never be reached after a gate failure; assert that.
    monkeypatch.setattr(rebuild_store, "sign_and_export",
                        lambda build: pytest.fail("sign_and_export ran despite gate failure"))

    rc = rebuild_store.run([
        "--store-dir", str(store_dir),
        "--staging-dir", str(staging_dir),
    ])
    assert rc == 1  # non-zero exit

    after = {p.name: p.read_bytes() for p in store_dir.iterdir() if p.is_file()}
    assert before == after  # live store byte-identical
    # No backups were created (we never reached promote).
    assert not list(store_dir.glob("*.bak-*"))


# --- 2. DRY RUN: NO SWAP ---------------------------------------------------

def test_dry_run_builds_but_performs_no_swap(rebuild_store, tmp_path, monkeypatch):
    """--dry-run builds + signs + exports into staging but leaves live untouched."""
    store_dir = tmp_path / "live"
    _seed_live_store(store_dir)
    snapshot = {
        p.name: (p.read_bytes() if p.is_file() else None)
        for p in store_dir.rglob("*") if p.is_file()
    }

    staging_dir = tmp_path / "staging"

    def fake_build_staging(staging, transcripts, *, key_path, pub_path, merge_from=None):
        sp = _seed_staging(staging)
        return {
            "health": _healthy_report(),
            "ingest_stats": {},
            "stage_paths": {**sp, "events": staging / "events.jsonl",
                            "ingest_stats": staging / "ingest-stats.json"},
            "key_path": key_path,
            "pub_path": pub_path,
        }

    signed_called = {"v": False}
    monkeypatch.setattr(rebuild_store, "build_staging", fake_build_staging)
    monkeypatch.setattr(rebuild_store, "discover_transcripts", lambda roots, days: [Path("fake.jsonl")])
    monkeypatch.setattr(rebuild_store, "sign_and_export",
                        lambda build: signed_called.__setitem__("v", True))
    # promote MUST NOT run on dry-run.
    monkeypatch.setattr(rebuild_store, "promote",
                        lambda build, sd: pytest.fail("promote ran during --dry-run"))

    rc = rebuild_store.run([
        "--store-dir", str(store_dir),
        "--staging-dir", str(staging_dir),
        "--dry-run",
    ])
    assert rc == 0
    assert signed_called["v"] is True  # dry-run still builds+signs+exports staging

    after = {
        p.name: (p.read_bytes() if p.is_file() else None)
        for p in store_dir.rglob("*") if p.is_file()
    }
    assert snapshot == after  # live store unchanged
    assert not list(store_dir.glob("*.bak-*"))  # no backups on dry-run


# --- 3. SUCCESSFUL PROMOTE: BACKUP BEFORE SWAP -----------------------------

def test_successful_promote_backs_up_before_swap(rebuild_store, tmp_path):
    """promote() copies live -> timestamped .bak, THEN moves staging into place."""
    store_dir = tmp_path / "live"
    _seed_live_store(store_dir)
    old_facts = (store_dir / "facts.json").read_text(encoding="utf-8")

    staging_dir = tmp_path / "staging"
    sp = _seed_staging(staging_dir)
    build = {
        "stage_paths": {**sp, "events": staging_dir / "events.jsonl",
                        "ingest_stats": staging_dir / "ingest-stats.json"},
    }

    result = rebuild_store.promote(build, store_dir)

    # New content is now live.
    assert json.loads((store_dir / "facts.json").read_text())[0]["id"] == "NEW"
    assert (store_dir / "vault" / "index.md").read_text() == "# new vault\n"

    # A timestamped backup of the OLD store exists and holds the OLD content.
    stamp = result["stamp"]
    bak = store_dir / f"facts.json.bak-{stamp}"
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == old_facts
    # Directory artifacts get backed up too.
    assert (store_dir / f"sessions.bak-{stamp}").is_dir()
    assert (store_dir / f"sessions.bak-{stamp}" / "old.md").exists()

    assert "facts.json" in result["promoted"]
    assert any("facts.json.bak-" in b for b in result["backed_up"])


def test_promote_into_empty_store_creates_no_backups(rebuild_store, tmp_path):
    """First-ever promote (no existing live store) swaps in without backups."""
    store_dir = tmp_path / "live"  # does not exist yet
    staging_dir = tmp_path / "staging"
    sp = _seed_staging(staging_dir)
    build = {"stage_paths": {**sp, "events": staging_dir / "events.jsonl",
                             "ingest_stats": staging_dir / "ingest-stats.json"}}

    result = rebuild_store.promote(build, store_dir)

    assert (store_dir / "facts.json").exists()
    assert result["backed_up"] == []
    assert "facts.json" in result["promoted"]


# --- transcript window discovery -------------------------------------------

def test_discover_transcripts_respects_since_window(rebuild_store, tmp_path):
    """Files older than the window are excluded; recent ones kept."""
    import os
    import time

    root = tmp_path / "projects" / "proj"
    root.mkdir(parents=True)
    recent = root / "recent.jsonl"
    old = root / "old.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    old.write_text("{}\n", encoding="utf-8")
    # Age `old` to 30 days ago.
    thirty_days = time.time() - 30 * 86400
    os.utime(old, (thirty_days, thirty_days))

    found = rebuild_store.discover_transcripts([tmp_path / "projects"], since_days=7)
    names = {p.name for p in found}
    assert "recent.jsonl" in names
    assert "old.jsonl" not in names

    # since_days=0 means no window -> both included.
    found_all = rebuild_store.discover_transcripts([tmp_path / "projects"], since_days=0)
    assert {p.name for p in found_all} == {"recent.jsonl", "old.jsonl"}


def test_rebuild_aborts_when_no_transcripts(rebuild_store, tmp_path):
    """An empty source window aborts rather than building an empty store."""
    with pytest.raises(rebuild_store.RebuildError) as exc:
        rebuild_store.rebuild(
            store_dir=tmp_path / "live",
            source_roots=[tmp_path / "nonexistent"],
            since_days=7,
        )
    assert "no transcripts" in str(exc.value).lower()


# --- 4. MERGE MODE: preserve live history + add the recent window (N8142) ---
#
# rebuild-store builds from a time-windowed transcript ingest and promote() does
# a FULL REPLACE of facts.json. Without merge that silently DROPS all history
# older than the window (e.g. the 1732 migrated .memsearch facts) on every run.
# Merge mode seeds staging facts from the existing live store so a scheduled
# refresh ADDS recent facts and never amnesias the store.

class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess (stdout + returncode)."""

    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_merge_facts_preserves_live_and_adds_recent(rebuild_store):
    """Union: every live id survives, new ids are added, recent wins on collision."""
    live = [{"id": "OLD1", "content": "old one"}, {"id": "OLD2", "content": "old two"}]
    recent = [{"id": "OLD2", "content": "old two UPDATED"}, {"id": "NEW1", "content": "new one"}]
    merged = rebuild_store.merge_facts(live, recent)
    by_id = {f.get("id"): f for f in merged}
    assert {"OLD1", "OLD2", "NEW1"} <= set(by_id)          # every live id survives + new added
    assert by_id["OLD2"]["content"] == "old two UPDATED"   # recent wins on id collision
    assert len(merged) == 3                                 # no history dropped
    assert len(merged) >= len({f["id"] for f in live})      # never shrink below live


def test_merge_facts_dedups_idless_by_content(rebuild_store):
    """Facts without an id dedup on content so a re-extract does not double them."""
    live = [{"content": "no id fact"}]
    recent = [{"content": "no id fact"}, {"content": "brand new"}]
    merged = rebuild_store.merge_facts(live, recent)
    assert sorted(f["content"] for f in merged) == ["brand new", "no id fact"]


def test_build_staging_merges_live_history(rebuild_store, tmp_path, monkeypatch):
    """build_staging(merge_from=...) seeds staging facts from the live store, so a
    full-replace promote can never drop migrated history."""
    live_facts = tmp_path / "live-facts.json"
    live_facts.write_text(
        json.dumps([{"id": "HIST", "content": "historical fact"}]), encoding="utf-8"
    )
    staging = tmp_path / "staging"

    def fake_run_cli(script, args):
        if script == "ingest-jsonl.py":
            return _FakeProc('{"stats": {}}')
        if script == "refine-sessions.py":
            fp = Path(args[args.index("--facts") + 1])
            fp.write_text(
                json.dumps([{"id": "RECENT", "content": "recent fact"}]), encoding="utf-8"
            )
            return _FakeProc("")
        if script == "review-promotions.py":
            return _FakeProc("")
        if script == "nockbrain-health.py":
            return _FakeProc(json.dumps(_healthy_report()))
        return _FakeProc("")

    monkeypatch.setattr(rebuild_store, "_run_cli", fake_run_cli)

    rebuild_store.build_staging(
        staging,
        [Path("x.jsonl")],
        key_path=tmp_path / "k",
        pub_path=tmp_path / "k.pub",
        merge_from=live_facts,
    )
    staged = json.loads((staging / "facts.json").read_text(encoding="utf-8"))
    assert {f.get("id") for f in staged} == {"HIST", "RECENT"}  # history preserved + recent added


def test_rebuild_never_shrinks_below_live_when_merging(rebuild_store, tmp_path, monkeypatch):
    """Anti-amnesia gate: a merge rebuild whose staging would drop below the live
    fact count ABORTS (non-zero) and leaves the live store untouched."""
    store_dir = tmp_path / "live"
    _seed_live_store(store_dir)  # live: 1 fact (id OLD)

    def fake_build_staging(staging, transcripts, *, key_path, pub_path, merge_from=None):
        sp = _seed_staging(staging)
        sp["facts"].write_text("[]", encoding="utf-8")  # 0 facts -> would shrink
        return {
            "health": _healthy_report(),
            "ingest_stats": {},
            "stage_paths": {**sp, "events": staging / "events.jsonl",
                            "ingest_stats": staging / "ingest-stats.json"},
            "key_path": key_path,
            "pub_path": pub_path,
        }

    monkeypatch.setattr(rebuild_store, "build_staging", fake_build_staging)
    monkeypatch.setattr(rebuild_store, "discover_transcripts", lambda roots, days: [Path("x.jsonl")])
    monkeypatch.setattr(rebuild_store, "sign_and_export",
                        lambda build: pytest.fail("sign_and_export ran despite shrink gate"))

    before = {p.name: p.read_bytes() for p in store_dir.iterdir() if p.is_file()}
    with pytest.raises(rebuild_store.RebuildError) as exc:
        rebuild_store.rebuild(
            store_dir=store_dir,
            staging_dir=tmp_path / "staging",
            since_days=7,
        )
    assert "shrink" in str(exc.value).lower()
    after = {p.name: p.read_bytes() for p in store_dir.iterdir() if p.is_file()}
    assert before == after  # live store byte-identical


def test_replace_flag_allows_intentional_shrink(rebuild_store, tmp_path, monkeypatch):
    """--replace (merge=False) opts out of merge + the shrink gate for an
    intentional from-scratch rebuild."""
    store_dir = tmp_path / "live"
    _seed_live_store(store_dir)  # live: 1 fact

    def fake_build_staging(staging, transcripts, *, key_path, pub_path, merge_from=None):
        # When merge is off, merge_from must be None (no live seed requested).
        assert merge_from is None
        sp = _seed_staging(staging)  # 1 fact (id NEW)
        return {
            "health": _healthy_report(),
            "ingest_stats": {},
            "stage_paths": {**sp, "events": staging / "events.jsonl",
                            "ingest_stats": staging / "ingest-stats.json"},
            "key_path": key_path,
            "pub_path": pub_path,
        }

    monkeypatch.setattr(rebuild_store, "build_staging", fake_build_staging)
    monkeypatch.setattr(rebuild_store, "discover_transcripts", lambda roots, days: [Path("x.jsonl")])
    monkeypatch.setattr(rebuild_store, "sign_and_export", lambda build: None)

    result = rebuild_store.rebuild(
        store_dir=store_dir,
        staging_dir=tmp_path / "staging",
        since_days=7,
        merge=False,
    )
    assert result["promote"] is not None  # promoted, no shrink-abort
