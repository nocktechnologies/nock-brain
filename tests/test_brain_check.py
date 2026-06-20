"""Tests for brain-check — the exists/probable/unknown signal (gbrain steal).

Before an agent asserts something is absent ("there's no X", "I don't have
access to Y"), it asks the brain. The verdict plus the missing-terms gap-note
are what keep it from a false absence claim — the recurring assert-from-inference
failure mode this tool exists to fix.
"""
from datetime import datetime

# Fixed reference clock so freshness/stale assertions don't drift with wall time.
NOW = datetime(2026, 6, 20)


def fact(content, confidence=0.9, status="current", kind="decision",
         source_date="2026-06-10"):
    return {
        "content": content,
        "confidence": confidence,
        "status": status,
        "kind": kind,
        "source_date": source_date,
    }


def test_known_corroborated_topic_returns_exists(brain_check):
    facts = [
        fact("the command tier pricing was locked at 49 dollars"),
        fact("command tier pricing includes the terminal and voice"),
        fact("an unrelated note about the weather"),
    ]
    r = brain_check.check(facts, "command tier pricing", now=NOW)
    assert r["verdict"] == "exists"
    assert r["missing_terms"] == []
    assert r["hits"] >= 2
    # exists must tell the agent NOT to claim absence.
    assert "absence" in r["advice"].lower()


def test_absent_topic_returns_unknown(brain_check):
    facts = [fact("the command tier pricing was locked at 49 dollars")]
    r = brain_check.check(facts, "submarine sonar calibration", now=NOW)
    assert r["verdict"] == "unknown"
    assert r["hits"] == 0
    assert set(r["missing_terms"]) == {"submarine", "sonar", "calibration"}
    # even on unknown, verify-before-claim still applies.
    assert "verify" in r["advice"].lower()


def test_partial_signal_returns_probable_with_gap_note(brain_check):
    facts = [fact("the boardroom tab will show the panel debate and judge")]
    r = brain_check.check(facts, "boardroom voting quorum rules", now=NOW)
    assert r["verdict"] == "probable"
    # the gap note: brain knows 'boardroom' but not the voting specifics.
    assert "boardroom" in r["matched_terms"]
    assert "voting" in r["missing_terms"]
    assert "quorum" in r["missing_terms"]


def test_missing_terms_are_the_gap_note(brain_check):
    facts = [fact("reddit manual pull until a dev license lands")]
    r = brain_check.check(facts, "reddit oauth app registration", now=NOW)
    assert "reddit" in r["matched_terms"]
    assert "oauth" in r["missing_terms"]


def test_coverage_uses_top_ranked_fact_not_incidental_matches(brain_check):
    # One fact covers the whole query; the rest only share a common term. The
    # verdict + gap-note must reflect the topical fact, not the incidental ones.
    facts = [
        fact("reddit needs a dev license and app registration before oauth"),
        fact("a dev note about something unrelated"),
        fact("another dev item, off topic"),
    ]
    r = brain_check.check(facts, "reddit dev license app registration oauth",
                          now=NOW)
    assert r["verdict"] == "exists"
    assert r["missing_terms"] == []
    assert r["strong_hits"] >= 1


def test_stale_evidence_is_flagged(brain_check):
    facts = [
        fact("the boardroom panel design was decided", source_date="2026-01-01"),
        fact("the boardroom panel uses kimi and deepseek", source_date="2026-01-02"),
    ]
    r = brain_check.check(facts, "boardroom panel", now=NOW, stale_days=60)
    assert r["verdict"] in {"exists", "probable"}
    assert r["freshness"] == "2026-01-02"
    assert r["stale"] is True


def test_fresh_evidence_not_stale(brain_check):
    facts = [
        fact("the boardroom panel design was decided", source_date="2026-06-15"),
        fact("the boardroom panel uses kimi and deepseek", source_date="2026-06-18"),
    ]
    r = brain_check.check(facts, "boardroom panel", now=NOW, stale_days=60)
    assert r["stale"] is False


def test_empty_query_is_unknown(brain_check):
    facts = [fact("anything")]
    r = brain_check.check(facts, "what about the", now=NOW)  # all stopwords
    assert r["verdict"] == "unknown"
    assert r["query_terms"] == []


def test_null_content_does_not_false_match(brain_check):
    # A fact with content explicitly None must not tokenize to "none" and
    # match a query containing the word 'none'.
    facts = [
        {"content": None, "confidence": 0.9, "status": "current",
         "kind": "decision", "source_date": "2026-06-10"},
    ]
    r = brain_check.check(facts, "none", now=NOW)
    assert r["verdict"] == "unknown"
    assert r["hits"] == 0


def test_superseded_facts_do_not_create_exists(brain_check):
    facts = [
        fact("old plan said quux tier", status="superseded"),
        fact("old plan said quux tier again", status="superseded"),
    ]
    r = brain_check.check(facts, "quux tier", now=NOW)
    # superseded facts are hard-filtered by search(); no live signal => unknown.
    assert r["verdict"] == "unknown"
    assert r["hits"] == 0
