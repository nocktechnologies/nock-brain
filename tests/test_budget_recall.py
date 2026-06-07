"""Tests for budget-aware recall — ranking, the confidence/superseded filters,
and the token-budget truncation that keeps recall from flooding the context."""
import json


def fact(content, confidence=0.9, status="current", kind="decision", source_date="2026-06-01"):
    return {
        "content": content,
        "confidence": confidence,
        "status": status,
        "kind": kind,
        "source_date": source_date,
    }


def test_search_ranks_by_term_overlap(budget_recall):
    facts = [
        fact("pricing was set to 49 dollars for the command tier"),
        fact("the pricing plan and pricing tiers were locked"),
        fact("an unrelated note about the weather"),
    ]
    results = budget_recall.search(facts, "pricing plan")
    assert results, "expected matches"
    # The fact containing both 'pricing' and 'plan' outranks the single-term one.
    assert results[0]["content"].startswith("the pricing plan")
    assert all("weather" not in r["content"] for r in results)


def test_bm25_weights_rarer_terms_higher(budget_recall):
    # "pricing" is common across the corpus; "seatbelt" is rare. A doc matching
    # the rare term should outrank docs matching only the common one.
    facts = [
        fact("a seatbelt decision we made once"),
        fact("pricing note one"),
        fact("pricing note two"),
        fact("pricing note three"),
    ]
    results = budget_recall.search(facts, "pricing seatbelt")
    assert results, "expected matches"
    assert "seatbelt" in results[0]["content"], (
        f"rare-term doc should rank first under BM25, got: {results[0]['content']!r}"
    )


def test_bm25_normalizes_for_document_length(budget_recall):
    # Same term frequency (1x "kubernetes"), but the longer doc is penalized.
    facts = [
        fact("kubernetes"),
        fact("kubernetes " + " ".join(f"filler{i}" for i in range(40))),
    ]
    results = budget_recall.search(facts, "kubernetes")
    assert results[0]["content"] == "kubernetes", (
        f"shorter doc should rank first under length normalization, got: {results[0]['content']!r}"
    )


def test_bm25_matches_tokens_not_substrings(budget_recall):
    # "cat" must NOT match the substring inside "category" — the old naive search
    # over-matched; BM25 tokenizes.
    facts = [
        fact("category management and taxonomy notes"),
        fact("the cat sat on the mat"),
    ]
    results = budget_recall.search(facts, "cat")
    assert len(results) == 1, f"expected only the token match, got {len(results)}: {results}"
    assert "cat sat" in results[0]["content"]


def test_search_excludes_low_confidence(budget_recall):
    facts = [fact("pricing locked", confidence=0.5)]  # below MIN_CONFIDENCE (0.7)
    assert budget_recall.search(facts, "pricing") == []


def test_search_superseded_filtered_by_default(budget_recall):
    facts = [fact("pricing locked", status="superseded")]
    assert budget_recall.search(facts, "pricing") == []
    assert len(budget_recall.search(facts, "pricing", include_superseded=True)) == 1


def test_estimate_tokens(budget_recall):
    assert budget_recall.estimate_tokens("a" * 40) == 10  # 4 chars/token


def test_budget_recall_truncates_to_budget(budget_recall, tmp_path):
    facts = [fact("pricing decision number with extra words to add length " * 3) for _ in range(20)]
    fp = tmp_path / "facts.json"
    fp.write_text(json.dumps(facts))
    out = budget_recall.budget_recall("pricing", fp, budget=80)
    assert "truncated by budget" in out


def test_budget_recall_empty_on_no_match(budget_recall, tmp_path):
    fp = tmp_path / "facts.json"
    fp.write_text(json.dumps([fact("pricing locked")]))
    assert budget_recall.budget_recall("nonexistentterm", fp) == ""


def test_budget_recall_missing_file_is_empty(budget_recall, tmp_path):
    assert budget_recall.budget_recall("anything", tmp_path / "nope.json") == ""


def test_insights_surface_first_and_dedup_their_sources(budget_recall, tmp_path):
    f1 = fact("pricing tier correction one")
    f2 = fact("pricing tier correction two")
    f1["id"], f2["id"] = "f1", "f2"
    insight = {
        "id": "ins1",
        "kind": "insight",
        "content": "Recurring correction (seen 2x): pricing tier",
        "confidence": 0.9,
        "status": "current",
        "source_date": "2026-06-05",
        "source_ids": ["f1", "f2"],
    }
    ff = tmp_path / "facts.json"
    ff.write_text(json.dumps([f1, f2]))
    inf = tmp_path / "insights.json"
    inf.write_text(json.dumps([insight]))

    out = budget_recall.budget_recall("pricing", ff, budget=1000, insights_file=inf)
    assert "Recurring correction" in out  # the synthesized insight is surfaced
    # its source facts are deduped out (we show the synthesis, not its raw sources)
    assert "correction one" not in out
    assert "correction two" not in out


def test_recall_works_with_insights_but_no_facts(budget_recall, tmp_path):
    insight = {
        "id": "ins1", "kind": "insight", "content": "Recurring decision: use Postgres",
        "confidence": 0.9, "status": "current", "source_date": "2026-06-05", "source_ids": [],
    }
    inf = tmp_path / "insights.json"
    inf.write_text(json.dumps([insight]))
    out = budget_recall.budget_recall("postgres", tmp_path / "nofacts.json", insights_file=inf)
    assert "Recurring decision" in out
