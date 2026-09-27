"""Tests for hard-deleting sensitive fact material across local stores."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_purge_fact_apply_removes_pattern_from_facts_events_notes_and_vault(tmp_path):
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    (vault / "facts").mkdir(parents=True)

    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-leaky"}],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-keep"}],
        },
    ]))
    events.write_text(
        json.dumps({"id": "event-leaky", "content": "leaked-secret-value"}) + "\n" +
        json.dumps({"id": "event-keep", "content": "safe memory"}) + "\n"
    )
    (notes / "s1.md").write_text("- leaked-secret-value\n- safe memory\n")
    (vault / "facts" / "leaky.md").write_text("leaked-secret-value\n")
    (vault / "facts" / "keep.md").write_text("safe memory\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "removed 1 fact" in result.stdout
    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "leaked-secret-value" not in events.read_text()
    assert "event-keep" in events.read_text()
    assert "leaked-secret-value" not in (notes / "s1.md").read_text()
    assert "safe memory" in (vault / "facts" / "keep.md").read_text()


def test_purge_apply_unlinks_verified_cache_sidecar(tmp_path):
    """Issue #52: purge-fact must remove facts.json.verified-cache.json.
    Digests are opaque, so the whole sidecar goes; dry-run leaves it."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    sidecar = tmp_path / "embeddings.npz"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    cache = facts.with_name(facts.name + ".verified-cache.json")
    cache.write_text('{"version": 2, "digests": []}', encoding="utf-8")
    cache.chmod(0o600)

    argv = [
        sys.executable,
        str(REPO / "bin" / "purge-fact.py"),
        "--pattern", "leaked-secret-value",
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(sidecar),
    ]
    dry = subprocess.run(
        argv,
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert cache.exists(), "dry-run must not unlink the verification cache"
    assert "would delete verification cache" in dry.stderr

    applied = subprocess.run(
        argv + ["--apply"],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert not cache.exists(), "apply must unlink the verification cache sidecar"
    assert "deleted verification cache" in applied.stderr


def test_purge_without_matches_leaves_verified_cache(tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    cache = facts.with_name(facts.name + ".verified-cache.json")
    cache.write_text("{}", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "no-such-secret",
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert cache.exists(), "a no-op purge must not drop the verification cache"


def test_stale_cache_save_during_purge_cannot_revive_purged_digest(
        tmp_path, monkeypatch):
    """A concurrent recall that loaded the OLD store can save() after the
    sidecar is unlinked but before facts.json is rewritten. Its stamp still
    matches, so save() would recreate a digest for the fact being purged.
    Rewrite the store first so the stamp has moved before that save can land.
    """
    purge_fact = _load("purge-fact")
    vc = sys.modules["_verify_cache"]

    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")

    class _DummyKey:
        key_id = "k"
        alg = "ed25519"

    cache = vc.cache_path_for(facts)
    purged_digest = "digest-of-purged-leaky-fact"
    cache.write_text(json.dumps({
        "version": vc.CACHE_VERSION,
        "alg": "ed25519",
        "key_id": "k",
        "store": vc._store_sig(facts),
        "digests": [purged_digest],
    }), encoding="utf-8")
    cache.chmod(0o600)

    stale = vc.load_for_store(facts, _DummyKey())
    assert stale is not None
    assert stale.hit(purged_digest)
    # A concurrent recall that proved any signature this run is dirty;
    # without that, save() is a no-op and cannot recreate the sidecar.
    stale.add("digest-verified-this-recall")

    real_unlink = purge_fact.unlink_for_store

    def unlink_then_concurrent_save(store_path):
        result = real_unlink(store_path)
        stale.save()
        return result

    monkeypatch.setattr(purge_fact, "unlink_for_store", unlink_then_concurrent_save)

    assert purge_fact.run([
        "leaky",
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"),
        "--apply",
    ]) == 0
    stale.save()

    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert not cache.exists(), (
        "a stale cache save must not recreate the sidecar with a purged digest"
    )


def test_purge_apply_sweeps_stale_cache_tmp_without_sidecar(tmp_path):
    """An interrupted cache write can leave `{sidecar}.*.tmp` with no sidecar.
    Applied matching purges must still call unlink_for_store so the sweep runs.
    """
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")

    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    leftover = tmp_path / (sidecar.name + ".interrupted.tmp")
    leftover.write_text("stale tmp from interrupted cache write", encoding="utf-8")
    # _sweep_stale_tmps leaves files younger than STALE_TMP_AGE_SEC (60s)
    # as a concurrent-writer guard; age this leftover past that cutoff.
    old = time.time() - 90
    os.utime(leftover, (old, old))
    assert not sidecar.exists()

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert not leftover.exists(), (
        "applied matching purge must sweep leftover cache tmp files"
    )
    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "removed 1 fact" in result.stdout
    assert "verification cache" not in result.stderr


def test_purge_apply_scrubs_insights_and_graph(tmp_path):
    """N10019: derived views must not keep injecting purged content."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    (tmp_path / "insights.json").write_text(json.dumps([
        {
            "id": "ins-leaky",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "content": "recurring leaked-secret-value",
            "source_date": "2026-06-12",
            "source_ids": ["leaky"],
        },
        {
            "id": "ins-keep",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "content": "recurring safe memory",
            "source_date": "2026-06-12",
            "source_ids": ["keep"],
        },
    ]))
    (tmp_path / "graph.json").write_text(json.dumps({
        "nodes": [
            {"id": "fact:leaky", "type": "fact", "label": "leaked-secret-value"},
            {"id": "fact:keep", "type": "fact", "label": "safe memory"},
        ],
        "edges": [
            {"id": "e1", "source": "fact:leaky", "target": "concept:secret"},
            {"id": "e2", "source": "fact:keep", "target": "concept:safe"},
        ],
    }))

    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    insights = json.loads((tmp_path / "insights.json").read_text())
    assert [item["id"] for item in insights] == ["ins-keep"]
    graph = json.loads((tmp_path / "graph.json").read_text())
    assert [node["id"] for node in graph["nodes"]] == ["fact:keep"]
    assert [edge["id"] for edge in graph["edges"]] == ["e2"]
    tombstones = (tmp_path / "purged-ids.jsonl").read_text()
    assert "leaky" in tombstones


def test_purge_zero_match_does_not_rewrite_store(tmp_path):
    """N10028: a no-op apply must not drop loader-skipped malformed records."""
    facts = tmp_path / "facts.json"
    raw = json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        "not-an-object",
    ])
    facts.write_text(raw)
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "no-such-secret",
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert facts.read_text() == raw


def test_purge_pattern_does_not_match_signature_hex(tmp_path):
    """N10028: pattern match must not search attestation signature hex."""
    unique_sig = "cafebabe" * 8
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
            "attestation": {"signature": unique_sig},
        },
    ]))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "vault").mkdir()
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", unique_sig,
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(facts.read_text())[0]["id"] == "keep"



def test_purge_sweeps_contaminated_insight_and_reports_counts(tmp_path):
    """N10052 residual: contaminated clusters quote a leaked judge-prompt fact
    verbatim in content's "Most recent:" excerpt while citing already-absent
    facts — and the summary line printed no insight count, so whether the
    match fired was invisible in receipts. Locks in: the content match sweeps
    the contaminated shape (keyword-only themes never match), counts are
    reported, and a zero-fact-match apply still leaves facts.json alone."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    template = "Two memory facts from the same project, EARLIER then LATER."
    (tmp_path / "insights.json").write_text(json.dumps([
        {
            # the real N10052 shape: mostly-genuine cluster whose latest
            # member was a leaked prompt fact, quoted verbatim in content
            "id": "ins-contaminated",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "theme": "directive, recurring, lesson, sentence, clean",
            "content": f"Recurring directive (seen 33x): directive, recurring. "
                       f"Most recent: {template}",
            "source_date": "2026-08-01",
            "source_ids": ["already-purged-fact"],
        },
        {
            # keyword-coincidence theme, clean content: must survive
            "id": "ins-keep",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "theme": "memory, facts, project, earlier, later",
            "content": "recurring safe memory",
            "source_date": "2026-06-12",
            "source_ids": ["keep"],
        },
    ]))

    argv = [
        sys.executable,
        str(REPO / "bin" / "purge-fact.py"),
        "--pattern", template,
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    dry = subprocess.run(argv, cwd=REPO, env=env, text=True,
                         capture_output=True, check=True)
    assert "1 insight(s)" in dry.stdout
    # dry-run must not touch the file
    assert len(json.loads((tmp_path / "insights.json").read_text())) == 2

    wet = subprocess.run(argv + ["--apply"], cwd=REPO, env=env, text=True,
                         capture_output=True, check=True)
    assert "1 insight(s)" in wet.stdout
    insights = json.loads((tmp_path / "insights.json").read_text())
    assert [item["id"] for item in insights] == ["ins-keep"]
    # zero fact matches: the fact store must be untouched (N10028)
    assert [f["id"] for f in json.loads(facts.read_text())] == ["keep"]
