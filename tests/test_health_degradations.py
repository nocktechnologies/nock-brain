"""Tests for the empty-recall degradation aggregation (Mira's §6 hardening):
a degraded read must land in recall-degradations.jsonl, and nockbrain-health
must flag N recent degradations — stderr nobody watches is still a silent
outage, so the visible failure gets aggregated, not just emitted."""
import json
from datetime import datetime, timedelta, timezone


def _event_line(at, reason="db-missing"):
    return json.dumps({"at": at, "db": "/tmp/brain.db", "reason": reason})


# ── the store records degradations ───────────────────────────────────────────
def test_missing_db_read_records_degradation(storeback, tmp_path):
    store = storeback.SqliteStore(tmp_path / "brain.db")
    assert store.load_facts() == []
    log = tmp_path / "recall-degradations.jsonl"
    events = [json.loads(l) for l in log.read_text().splitlines()]
    assert events and events[0]["reason"] == "db-missing"
    assert events[0]["at"]  # timestamped


def test_corrupt_db_read_records_degradation(storeback, tmp_path):
    (tmp_path / "brain.db").write_bytes(b"not a sqlite database")
    storeback.SqliteStore(tmp_path / "brain.db").load_facts()
    events = [json.loads(l)
              for l in (tmp_path / "recall-degradations.jsonl").read_text().splitlines()]
    assert any(e["reason"].startswith("sqlite-error") for e in events)


def test_healthy_read_records_nothing(storeback, tmp_path):
    store = storeback.SqliteStore(tmp_path / "brain.db")
    store.create()
    store.load_facts()
    assert not (tmp_path / "recall-degradations.jsonl").exists()


# ── health aggregates and flags ──────────────────────────────────────────────
def test_health_flags_recent_degradations(nockbrain_health, tmp_path):
    now = datetime.now(timezone.utc)
    log = tmp_path / "recall-degradations.jsonl"
    log.write_text("\n".join(
        _event_line((now - timedelta(minutes=m)).isoformat()) for m in (5, 10, 15)
    ) + "\n")
    (tmp_path / "facts.json").write_text("[]")

    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json",
        degradations_path=log, degradation_threshold=3,
    )
    d = report["recall_degradations"]
    assert d["recent_24h"] == 3
    assert d["flagged"] is True
    assert "RECALL DEGRADED" in nockbrain_health.render_text(report)


def test_health_ignores_stale_degradations(nockbrain_health, tmp_path):
    log = tmp_path / "recall-degradations.jsonl"
    log.write_text("\n".join(
        _event_line(f"2000-01-0{d}T00:00:00+00:00") for d in (1, 2, 3)
    ) + "\n")
    (tmp_path / "facts.json").write_text("[]")

    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json",
        degradations_path=log, degradation_threshold=3,
    )
    d = report["recall_degradations"]
    assert d["total"] == 3
    assert d["recent_24h"] == 0
    assert d["flagged"] is False
    assert "RECALL DEGRADED" not in nockbrain_health.render_text(report)


def test_health_without_log_is_clean(nockbrain_health, tmp_path):
    (tmp_path / "facts.json").write_text("[]")
    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json",
        degradations_path=tmp_path / "recall-degradations.jsonl",
    )
    assert report["recall_degradations"]["flagged"] is False
    assert report["recall_degradations"]["total"] == 0


# ── F2: contradiction-queue freshness (silence made visible) ─────────────────
def _queue_file(tmp_path, generated_at):
    import pathlib
    review = tmp_path / "review"; review.mkdir(exist_ok=True)
    p = review / "contradiction-candidates.json"
    p.write_text(json.dumps({"generated_at": generated_at, "candidates": []}))
    return p


def test_fresh_queue_not_stale(nockbrain_health, tmp_path):
    now = datetime.now(timezone.utc)
    p = _queue_file(tmp_path, (now - timedelta(hours=5)).isoformat())
    q = nockbrain_health.contradiction_queue_health(p)
    assert q["stale"] is False
    assert 4 < q["age_hours"] < 6


def test_old_queue_is_stale(nockbrain_health, tmp_path):
    now = datetime.now(timezone.utc)
    p = _queue_file(tmp_path, (now - timedelta(hours=50)).isoformat())
    q = nockbrain_health.contradiction_queue_health(p)
    assert q["stale"] is True


def test_missing_queue_is_stale(nockbrain_health, tmp_path):
    q = nockbrain_health.contradiction_queue_health(
        tmp_path / "review" / "contradiction-candidates.json")
    assert q["exists"] is False and q["stale"] is True


def test_garbage_timestamp_is_stale(nockbrain_health, tmp_path):
    p = _queue_file(tmp_path, "not-a-date")
    assert nockbrain_health.contradiction_queue_health(p)["stale"] is True


def test_report_renders_stale_flag(nockbrain_health, tmp_path):
    (tmp_path / "facts.json").write_text("[]")
    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json",
        degradations_path=tmp_path / "recall-degradations.jsonl",
        contradictions_path=tmp_path / "review" / "contradiction-candidates.json",
    )
    text = nockbrain_health.render_text(report)
    assert "CONTRADICTION QUEUE STALE" in text


def test_future_dated_queue_is_stale_not_fresh(nockbrain_health, tmp_path):
    """B1: a queue stamped LATER than now (host-clock skew in the just-written
    direction) must never read fresh — that's the exact silence F2 surfaces."""
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    p = _queue_file(tmp_path, future)
    q = nockbrain_health.contradiction_queue_health(p)
    assert q["stale"] is True


def test_non_dict_queue_doc_does_not_crash(nockbrain_health, tmp_path):
    """B2: a malformed artifact (JSON list/str/null) must degrade to stale,
    never crash the health checker."""
    review = tmp_path / "review"; review.mkdir(exist_ok=True)
    p = review / "contradiction-candidates.json"
    for payload in ('["a-list"]', '"a-string"', 'null'):
        p.write_text(payload)
        q = nockbrain_health.contradiction_queue_health(p)
        assert q["stale"] is True, payload


# ── verification-cache sidecar (present / fresh / writable) ──────────────────
def test_health_reports_missing_verification_cache(nockbrain_health, tmp_path):
    (tmp_path / "facts.json").write_text("[]")
    report = nockbrain_health.build_report(facts_path=tmp_path / "facts.json")
    cache = report["verification_cache"]
    assert cache["present"] is False
    assert cache["fresh"] is False
    assert cache["writable"] is True
    assert cache["flagged"] is False
    text = nockbrain_health.render_text(report)
    assert "missing (cold start)" in text
    assert "UNWRITABLE" not in text


def test_health_reports_fresh_verification_cache(nockbrain_health, tmp_path):
    import _verify_cache as vc
    facts = tmp_path / "facts.json"
    facts.write_text("[]")
    st = facts.stat()
    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": st.st_mtime_ns, "size": st.st_size},
        "digests": [],
    }))
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["present"] is True
    assert cache["fresh"] is True
    assert cache["writable"] is True
    assert cache["flagged"] is False
    assert "present, fresh, writable" in nockbrain_health.render_text(report)


def test_health_reports_stale_verification_cache_stamp(nockbrain_health, tmp_path):
    import _verify_cache as vc
    facts = tmp_path / "facts.json"
    facts.write_text("[]")
    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": 1, "size": 1},
        "digests": ["ab"],
    }))
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["present"] is True
    assert cache["fresh"] is False
    assert cache["writable"] is True
    assert cache["flagged"] is False  # stale is informational, not an outage
    assert "stale stamp" in nockbrain_health.render_text(report)


def test_health_flags_unwritable_verification_cache(nockbrain_health, tmp_path):
    import os
    import pytest
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([{
        "id": "f-1", "kind": "decision", "status": "current",
        "confidence": 0.9, "content": "x", "source_date": "2026-07-01",
        "evidence": [],
    }]))
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses directory mode")
    os.chmod(tmp_path, 0o555)
    try:
        report = nockbrain_health.build_report(facts_path=facts)
    finally:
        os.chmod(tmp_path, 0o755)
    cache = report["verification_cache"]
    assert cache["writable"] is False
    assert cache["flagged"] is True
    assert report["recall_ready"] is True  # cache flag does not trip the gate
    assert "UNWRITABLE" in nockbrain_health.render_text(report)


def test_health_reports_uncreated_parent_as_cold_start(nockbrain_health, tmp_path):
    """A store path whose parent directory has not been created is a cold
    start, not an outage: writable is False (nowhere to persist) but
    flagged is False, so health prints 'missing (cold start)' and not
    UNWRITABLE."""
    facts = tmp_path / "never-created" / "facts.json"
    assert not facts.parent.exists()
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["present"] is False
    assert cache["fresh"] is False
    assert cache["writable"] is False
    assert cache["flagged"] is False
    text = nockbrain_health.render_text(report)
    assert "missing (cold start)" in text
    assert "UNWRITABLE" not in text


def test_health_reports_oversized_verification_cache(
        nockbrain_health, tmp_path, monkeypatch):
    """A sidecar larger than MAX_SIDECAR_BYTES is refused unread: present
    but not fresh because it is oversized, not because the stamp moved."""
    import _verify_cache as vc
    monkeypatch.setattr(vc, "MAX_SIDECAR_BYTES", 64)
    facts = tmp_path / "facts.json"
    facts.write_text("[]")
    st = facts.stat()
    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    sidecar.write_text(json.dumps({
        "version": 2, "alg": "ed25519", "key_id": "k",
        "store": {"mtime_ns": st.st_mtime_ns, "size": st.st_size},
        "digests": ["deadbeef"],
    }))
    assert sidecar.stat().st_size > 64
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["present"] is True
    assert cache["fresh"] is False
    assert cache["flagged"] is False
    text = nockbrain_health.render_text(report)
    assert "oversized" in text
    assert "stale stamp" not in text
    assert "missing (cold start)" not in text
    assert cache["reason"] == "oversized"


def test_health_reports_unreadable_verification_cache(nockbrain_health, tmp_path):
    """A corrupt sidecar is present but not fresh because it fails to parse,
    not because the stamp moved. Must not collapse into a cold start."""
    facts = tmp_path / "facts.json"
    facts.write_text("[]")
    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    sidecar.write_text("{not-json")
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["present"] is True
    assert cache["fresh"] is False
    assert cache["flagged"] is False
    text = nockbrain_health.render_text(report)
    assert "unreadable" in text
    assert "stale stamp" not in text
    assert "missing (cold start)" not in text
    assert cache["reason"] == "unreadable"


def test_health_reports_rejected_sidecar_for_foreign_key(
        nockbrain_health, sign_lib, tmp_path, monkeypatch):
    """N10031: health must not call a foreign-key_id sidecar fresh."""
    import _verify_cache as vc
    key = sign_lib.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "k"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "k.pub"))
    facts = tmp_path / "facts.json"
    facts.write_text("[]")
    st = facts.stat()
    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    sidecar.write_text(json.dumps({
        "version": vc.CACHE_VERSION, "alg": key.alg, "key_id": "foreign",
        "store": {"mtime_ns": st.st_mtime_ns, "size": st.st_size},
        "digests": ["ab"],
    }))
    report = nockbrain_health.build_report(facts_path=facts)
    cache = report["verification_cache"]
    assert cache["fresh"] is False
    assert cache["reason"] == "rejected"
    text = nockbrain_health.render_text(report)
    assert "rejected" in text
    assert "stale stamp" not in text


def test_max_age_validation_rejects_nonfinite(nockbrain_health, tmp_path, capsys):
    import pytest
    (tmp_path / "facts.json").write_text("[]")
    for bad in ("inf", "nan", "0", "-5"):
        with pytest.raises(SystemExit):
            nockbrain_health.run(["--facts", str(tmp_path / "facts.json"),
                                  "--contradictions-max-age-h", bad])
