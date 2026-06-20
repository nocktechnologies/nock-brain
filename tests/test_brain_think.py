"""Tests for brain-think — cited synthesis + gap analysis (gbrain steal #2).

brain-think turns raw recall into a briefing packet: the top-ranked facts as
citations built from the structured fact fields (never fabricated), plus the
gap block — what the brain does NOT know. The agent synthesizes prose from the
packet; the tool never composes prose itself (zero-LLM, anti-hallucination).
"""
from datetime import datetime

NOW = datetime(2026, 6, 20)


def fact(content, confidence=0.9, status="current", kind="decision",
         source_date="2026-06-10", _id=None):
    f = {
        "content": content,
        "confidence": confidence,
        "status": status,
        "kind": kind,
        "source_date": source_date,
    }
    if _id is not None:
        f["id"] = _id
    return f


def test_think_returns_cited_evidence(brain_think):
    facts = [
        fact("the command tier pricing was locked at 49 dollars"),
        fact("command tier pricing includes terminal and voice"),
    ]
    r = brain_think.think(facts, "command tier pricing", now=NOW)
    assert r["verdict"] == "exists"
    assert r["citation_count"] >= 1
    c = r["citations"][0]
    assert c["n"] == 1
    assert c["kind"] and c["date"] and c["content"]


def test_citations_are_structured_not_fabricated(brain_think):
    # The citation content + date must come verbatim from a real fact, never
    # an invented string.
    facts = [fact("reddit needs a dev license before oauth",
                  source_date="2026-05-30", _id="fact-7")]
    r = brain_think.think(facts, "reddit dev license oauth", now=NOW)
    c = r["citations"][0]
    assert c["content"] == "reddit needs a dev license before oauth"
    assert c["date"] == "2026-05-30"
    assert c["id"] == "fact-7"


def test_gap_lists_missing_terms(brain_think):
    facts = [fact("the boardroom tab shows the panel debate and judge")]
    r = brain_think.think(facts, "boardroom voting quorum rules", now=NOW)
    assert "boardroom" in r["gap"]["matched_terms"]
    assert "voting" in r["gap"]["missing_terms"]
    assert "quorum" in r["gap"]["missing_terms"]


def test_unknown_topic_has_no_citations_but_flags_gap(brain_think):
    facts = [fact("the command tier pricing was locked at 49 dollars")]
    r = brain_think.think(facts, "submarine sonar calibration", now=NOW)
    assert r["verdict"] == "unknown"
    assert r["citation_count"] == 0
    assert "verify" in r["gap"]["note"].lower()


def test_think_flags_stale_evidence(brain_think):
    facts = [
        fact("boardroom panel design decided", source_date="2026-01-01"),
        fact("boardroom panel uses kimi and deepseek", source_date="2026-01-02"),
    ]
    r = brain_think.think(facts, "boardroom panel", now=NOW, stale_days=60)
    assert r["gap"]["freshness"] == "2026-01-02"
    assert r["gap"]["stale"] is True


def test_think_respects_max_cite(brain_think):
    facts = [fact(f"boardroom note number {i} about the panel") for i in range(10)]
    r = brain_think.think(facts, "boardroom panel", now=NOW, max_cite=3)
    assert r["citation_count"] == 3
