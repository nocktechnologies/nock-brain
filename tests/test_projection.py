"""Tests for projection readback receipts (S4).

A derived/projection write can silently fail or go stale and nothing notices —
the class of failure that froze Mira's memory for three days. Every projection
write becomes a ledger row that is "applied" only after the file reads back with
the intended content hash; a mismatch is "ambiguous" (recorded, never raised),
and nockbrain-health flags any artifact whose latest write is ambiguous."""
import hashlib
import json


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── write_with_receipt: applied on a clean write ─────────────────────────────
def test_clean_write_is_applied(projection, tmp_path):
    artifact = tmp_path / "graph.json"
    receipts = tmp_path / "projection-receipts.jsonl"

    receipt = projection.write_with_receipt(artifact, '{"ok": true}', receipts, kind="text")

    assert receipt["status"] == "applied"
    assert artifact.read_text() == '{"ok": true}'
    assert receipt["bytes"] == len(b'{"ok": true}')
    # the receipt hash is the content hash a reader can independently re-verify.
    assert receipt["sha256"] == hashlib.sha256(b'{"ok": true}').hexdigest()
    assert artifact.stat().st_mode & 0o777 == 0o600  # private, like the store


def test_json_kind_serializes_once_and_applies(projection, tmp_path):
    artifact = tmp_path / "graph.json"
    receipts = tmp_path / "projection-receipts.jsonl"

    receipt = projection.write_with_receipt(
        artifact, {"b": 1, "a": 2}, receipts, kind="json", indent=2, sort_keys=True)

    assert receipt["status"] == "applied"
    assert json.loads(artifact.read_text()) == {"a": 2, "b": 1}


# ── ambiguous when on-disk bytes don't match the intended hash ───────────────
def test_corrupted_write_is_ambiguous(projection, tmp_path, monkeypatch):
    artifact = tmp_path / "graph.json"
    receipts = tmp_path / "projection-receipts.jsonl"
    real = projection.secure_write_text

    def corrupting_write(path, text, *, encoding="utf-8"):
        # the write "succeeds" but lands different bytes than intended — the
        # exact silent drift the readback receipt exists to catch.
        real(path, text + "  <!-- truncated -->", encoding=encoding)

    monkeypatch.setattr(projection, "secure_write_text", corrupting_write)

    receipt = projection.write_with_receipt(artifact, '{"ok": true}', receipts, kind="text")

    assert receipt["status"] == "ambiguous"  # recorded, not raised
    assert _rows(receipts)[-1]["status"] == "ambiguous"


def test_unwritable_target_is_ambiguous_not_raised(projection, tmp_path, monkeypatch):
    receipts = tmp_path / "projection-receipts.jsonl"

    def failing_write(path, text, *, encoding="utf-8"):
        raise OSError("disk full")

    monkeypatch.setattr(projection, "secure_write_text", failing_write)

    receipt = projection.write_with_receipt(tmp_path / "graph.json", "x", receipts, kind="text")

    assert receipt["status"] == "ambiguous"  # a failed write is flagged, never silent


# ── append + load roundtrip, last_status ─────────────────────────────────────
def test_receipts_append_and_load_roundtrip(projection, tmp_path):
    receipts = tmp_path / "projection-receipts.jsonl"

    a = projection.write_with_receipt(tmp_path / "a.json", "aaa", receipts, kind="text")
    b = projection.write_with_receipt(tmp_path / "b.json", "bbb", receipts, kind="text")

    loaded = projection.load_receipts(receipts)
    assert [r["artifact_path"] for r in loaded] == [a["artifact_path"], b["artifact_path"]]
    assert all(r["status"] == "applied" for r in loaded)


def test_load_receipts_missing_and_malformed(projection, tmp_path):
    receipts = tmp_path / "projection-receipts.jsonl"
    assert projection.load_receipts(receipts) == []  # missing -> empty, never raises

    receipts.write_text('{"status": "applied"}\nnot json\n\n')
    assert len(projection.load_receipts(receipts)) == 1  # bad/blank lines skipped


def test_last_status_tracks_newest_per_artifact(projection, tmp_path):
    artifact = tmp_path / "graph.json"
    receipts = tmp_path / "projection-receipts.jsonl"
    projection.append_receipt(receipts, {
        "at": "2026-08-08T00:00:00+00:00", "artifact_path": str(artifact),
        "sha256": "x", "bytes": 1, "status": "ambiguous"})
    projection.append_receipt(receipts, {
        "at": "2026-08-08T01:00:00+00:00", "artifact_path": str(artifact),
        "sha256": "y", "bytes": 1, "status": "applied"})

    rows = projection.load_receipts(receipts)
    assert projection.last_status(rows, artifact) == "applied"  # newest wins
    assert projection.last_status(rows, tmp_path / "never.json") == ""  # unknown -> ""


# ── health flags the latest-ambiguous artifact ───────────────────────────────
def test_health_flags_ambiguous_projection(projection, nockbrain_health, tmp_path):
    receipts = tmp_path / "projection-receipts.jsonl"
    good = tmp_path / "graph.json"
    stale = tmp_path / "vault" / "index.md"
    projection.append_receipt(receipts, {
        "at": "2026-08-08T00:00:00+00:00", "artifact_path": str(good),
        "sha256": "x", "bytes": 1, "status": "applied"})
    projection.append_receipt(receipts, {
        "at": "2026-08-08T01:00:00+00:00", "artifact_path": str(stale),
        "sha256": "y", "bytes": 1, "status": "ambiguous"})
    (tmp_path / "facts.json").write_text("[]")

    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json", receipts_path=receipts)

    projection_report = report["projection_receipts"]
    assert projection_report["flagged"] is True
    assert projection_report["ambiguous"] == [str(stale)]
    assert "PROJECTION AMBIGUOUS" in nockbrain_health.render_text(report)


def test_health_clean_when_latest_per_artifact_is_applied(projection, nockbrain_health, tmp_path):
    receipts = tmp_path / "projection-receipts.jsonl"
    artifact = tmp_path / "graph.json"
    projection.append_receipt(receipts, {
        "at": "2026-08-08T00:00:00+00:00", "artifact_path": str(artifact),
        "sha256": "x", "bytes": 1, "status": "ambiguous"})
    projection.append_receipt(receipts, {  # newest for the same artifact recovered
        "at": "2026-08-08T02:00:00+00:00", "artifact_path": str(artifact),
        "sha256": "z", "bytes": 1, "status": "applied"})
    (tmp_path / "facts.json").write_text("[]")

    report = nockbrain_health.build_report(
        facts_path=tmp_path / "facts.json", receipts_path=receipts)

    assert report["projection_receipts"]["flagged"] is False
    assert "PROJECTION AMBIGUOUS" not in nockbrain_health.render_text(report)


def test_health_projection_check_is_default_off(nockbrain_health, tmp_path):
    (tmp_path / "facts.json").write_text("[]")
    report = nockbrain_health.build_report(facts_path=tmp_path / "facts.json")
    assert "projection_receipts" not in report  # opt-in only


# ── exporters emit an applied receipt next to the store ──────────────────────
def _fact(**extra):
    fact = {
        "id": "f1", "kind": "note", "status": "current", "confidence": 0.9,
        "content": "recall bug fixed in NockBrain", "source_file": "s.jsonl",
        "source_date": "2026-08-08", "session": "s1",
        "evidence": [{"path": "s.jsonl", "line": 1}],
    }
    fact.update(extra)
    return fact


def test_graph_export_writes_applied_receipt(export_graph, projection, tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([_fact()]))
    out = tmp_path / "graph.json"

    assert export_graph.run(["--facts", str(facts), "--output", str(out)]) == 0

    rows = projection.load_receipts(tmp_path / "projection-receipts.jsonl")
    assert projection.last_status(rows, out) == "applied"


def test_obsidian_export_writes_applied_receipt(export_obsidian, projection, tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([_fact(kind="decision", content="chose sqlite store")]))
    vault = tmp_path / "vault"

    assert export_obsidian.run(["--facts", str(facts), "--vault", str(vault)]) == 0

    rows = projection.load_receipts(tmp_path / "projection-receipts.jsonl")
    assert projection.last_status(rows, vault / "index.md") == "applied"
