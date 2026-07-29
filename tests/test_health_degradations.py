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
