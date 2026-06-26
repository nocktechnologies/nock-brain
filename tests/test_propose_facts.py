"""Tests for the gated propose→approve fact loop.

The whole point of the experiment: automated extraction must PROPOSE new facts
to a review queue and leave the live store byte-identical until a deliberate,
reversible approval releases them. These tests pin that gate.
"""
import json


def _transcript(tmp_path):
    # Transcripts live in their own dir, distinct from the queue location —
    # mirrors real usage (--dir is the transcript dir, --queue is ~/.nock-brain)
    # and keeps the queue's .md render out of the re-scanned transcript glob.
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    md = tdir / "2026-06-02.md"
    md.write_text(
        "## Session 11:00\n"
        "- [DECISION] Kevin chose Cloudflare for forgeoperations DNS\n"
        "- [BUG] Found and fixed a parser bug in the JSONL ingest\n"
        "- Claude Code wrote compaction checkpoint\n"  # operational noise, skipped
    )
    return tdir


def _seed_store(path):
    path.write_text(json.dumps([{
        "id": "existing1", "kind": "decision", "status": "current",
        "confidence": 0.9, "content": "old fact", "source_date": "2026-06-01", "evidence": [],
    }]))


def test_propose_does_not_touch_live_store(propose_facts, tmp_path):
    tdir = _transcript(tmp_path)
    facts_path = tmp_path / "facts.json"
    _seed_store(facts_path)
    before = facts_path.read_bytes()
    queue = tmp_path / "proposed.json"

    rc = propose_facts.run(["--dir", str(tdir), "--facts", str(facts_path), "--queue", str(queue)])

    assert rc == 0
    # The live store is byte-for-byte unchanged — extraction proposed, never wrote.
    assert facts_path.read_bytes() == before
    proposals = json.loads(queue.read_text())
    assert len(proposals) >= 1
    assert all(p["status"] == "proposed" for p in proposals)
    assert all("proposed_at" in p and "approve" in p["actions"] for p in proposals)
    assert (tmp_path / "proposed.md").exists()


def test_propose_skips_already_queued(propose_facts, tmp_path):
    tdir = _transcript(tmp_path)
    facts_path = tmp_path / "facts.json"
    _seed_store(facts_path)
    queue = tmp_path / "proposed.json"

    propose_facts.run(["--dir", str(tdir), "--facts", str(facts_path), "--queue", str(queue)])
    n1 = len(json.loads(queue.read_text()))
    # A second run must not re-propose facts already sitting in the queue.
    propose_facts.run(["--dir", str(tdir), "--facts", str(facts_path), "--queue", str(queue)])
    n2 = len(json.loads(queue.read_text()))
    assert n1 >= 1
    assert n2 == n1


def test_approve_all_releases_into_store(propose_facts, approve_proposals, tmp_path):
    tdir = _transcript(tmp_path)
    facts_path = tmp_path / "facts.json"  # intentionally absent until release
    queue = tmp_path / "proposed.json"
    propose_facts.run(["--dir", str(tdir), "--facts", str(facts_path), "--queue", str(queue)])
    proposed = json.loads(queue.read_text())
    assert proposed

    rc = approve_proposals.run(["--facts", str(facts_path), "--queue", str(queue), "--approve-all"])

    assert rc == 0
    live = json.loads(facts_path.read_text())
    assert len(live) == len(proposed)
    # Released facts are current and stripped of proposal-only metadata.
    assert all(f["status"] == "current" for f in live)
    assert all("proposed_at" not in f and "actions" not in f for f in live)
    assert json.loads(queue.read_text()) == []  # queue drained


def test_reject_drops_without_writing(propose_facts, approve_proposals, tmp_path):
    tdir = _transcript(tmp_path)
    facts_path = tmp_path / "facts.json"
    queue = tmp_path / "proposed.json"
    propose_facts.run(["--dir", str(tdir), "--facts", str(facts_path), "--queue", str(queue)])
    proposed = json.loads(queue.read_text())
    victim = proposed[0]["id"]

    rc = approve_proposals.run(["--facts", str(facts_path), "--queue", str(queue), "--reject", victim])

    assert rc == 0
    live = json.loads(facts_path.read_text()) if facts_path.exists() else []
    assert victim not in {f["id"] for f in live}  # never written
    assert victim not in {p["id"] for p in json.loads(queue.read_text())}  # dropped from queue
