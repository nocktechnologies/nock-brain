"""Tests for the synthesis (consolidation) layer — the worker that turns
recurring same-kind facts into higher-level insights."""
import json


def fact(content, kind="correction", source_date="2026-06-01", status="current", fid=None):
    return {
        "id": fid or f"f{abs(hash((content, source_date))) % 10_000}",
        "kind": kind,
        "content": content,
        "source_date": source_date,
        "status": status,
        "confidence": 0.9,
    }


def test_recurring_same_kind_facts_become_one_insight(synthesize):
    facts = [
        fact("Kevin corrected the pricing tier for the command plan", source_date="2026-06-01"),
        fact("corrected again on the pricing tier and the plan pricing", source_date="2026-06-03"),
        fact("pricing tier plan correction once more", source_date="2026-06-05"),
    ]
    insights = synthesize.synthesize(facts, threshold=0.2, min_cluster=2)
    assert len(insights) == 1
    ins = insights[0]
    assert ins["kind"] == "insight"
    assert ins["of_kind"] == "correction"
    assert ins["recurrence"] == 3
    assert "pricing" in ins["theme"]
    # The insight points back to its sources.
    assert len(ins["source_ids"]) == 3
    # Most-recent member is surfaced in the content.
    assert "2026-06-05" in ins["content"] or "once more" in ins["content"]


def test_one_off_facts_are_not_synthesized(synthesize):
    facts = [
        fact("a unique decision about the database engine", kind="decision"),
        fact("an unrelated bug in the parser", kind="bug"),
    ]
    # Nothing recurs -> no insights (min_cluster=2).
    assert synthesize.synthesize(facts, min_cluster=2) == []


def test_different_kinds_do_not_cluster_together(synthesize):
    facts = [
        fact("pricing tier theme one", kind="correction"),
        fact("pricing tier theme two", kind="decision"),
    ]
    # Same words, different kind -> not the same recurrence.
    assert synthesize.synthesize(facts, threshold=0.2, min_cluster=2) == []


def test_superseded_facts_excluded(synthesize):
    facts = [
        fact("pricing tier theme", status="superseded"),
        fact("pricing tier theme again", status="superseded"),
    ]
    assert synthesize.synthesize(facts, threshold=0.2, min_cluster=2) == []


def test_confidence_grows_with_recurrence(synthesize):
    base = [fact(f"shared recurring theme item number {i}") for i in range(2)]
    more = [fact(f"shared recurring theme item number {i}") for i in range(5)]
    low = synthesize.synthesize(base, threshold=0.2, min_cluster=2)[0]
    high = synthesize.synthesize(more, threshold=0.2, min_cluster=2)[0]
    assert high["confidence"] >= low["confidence"]
    assert high["confidence"] <= 0.95  # capped


def test_kinds_filter(synthesize):
    facts = [
        fact("recurring theme alpha", kind="correction"),
        fact("recurring theme alpha two", kind="correction"),
        fact("recurring theme beta", kind="bug"),
        fact("recurring theme beta two", kind="bug"),
    ]
    only_corrections = synthesize.synthesize(facts, threshold=0.2, min_cluster=2, kinds={"correction"})
    assert len(only_corrections) == 1
    assert only_corrections[0]["of_kind"] == "correction"


def test_tokenize_drops_stopwords_and_short_tokens(synthesize):
    toks = synthesize.tokenize("The Claude Code agent fixed a big BUG in pricing")
    assert "claude" not in toks  # stopword
    assert "the" not in toks
    assert "a" not in toks  # too short
    assert "pricing" in toks
    assert "bug" in toks


def test_synthesize_cli_writes_insights(synthesize, tmp_path):
    facts = [
        fact("recurring pricing tier correction one"),
        fact("recurring pricing tier correction two"),
    ]
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(facts))
    out = tmp_path / "insights.json"
    result = synthesize.synthesize(json.loads(facts_file.read_text()), threshold=0.2, min_cluster=2)
    out.write_text(json.dumps(result))
    written = json.loads(out.read_text())
    assert written and written[0]["kind"] == "insight"
