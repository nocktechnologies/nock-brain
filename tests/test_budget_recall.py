"""Tests for budget-aware recall — ranking, the confidence/superseded filters,
the per-kind recency decay + supersession penalty (N8069), and the token-budget
truncation that keeps recall from flooding the context."""
import json
from datetime import datetime


# A fixed reference clock so the recency-decay tests are deterministic and don't
# drift with the wall clock.
NOW = datetime(2026, 6, 13)


def fact(content, confidence=0.9, status="current", kind="decision", source_date="2026-06-01"):
    return {
        "content": content,
        "confidence": confidence,
        "status": status,
        "kind": kind,
        "source_date": source_date,
    }


def test_tokenize_is_none_safe(budget_recall):
    # A fact with content explicitly None must not crash the recall path.
    assert budget_recall._tokenize(None) == []
    assert budget_recall._tokenize("") == []
    assert budget_recall._tokenize("Hello World") == ["hello", "world"]


def test_search_survives_null_content_fact(budget_recall):
    facts = [
        {"content": None, "confidence": 0.9, "status": "current",
         "kind": "decision", "source_date": "2026-06-01"},
        fact("pricing was set to 49 dollars"),
    ]
    # Must not raise; the null-content fact simply never matches.
    results = budget_recall.search(facts, "pricing")
    assert len(results) == 1
    assert "pricing" in results[0]["content"]


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


# --- N8069: recency- and supersession-aware ranking ------------------------

def test_recency_newer_fact_ranks_higher(budget_recall):
    # Two facts identical except source_date. The newer one must rank first
    # once recency decay applies — this is the core audit fix (a stale fact
    # was outranking a current one on identical term match).
    old = fact("pricing was locked at the command tier", source_date="2026-05-01")
    new = fact("pricing was locked at the command tier", source_date="2026-06-12")
    results = budget_recall.search([old, new], "pricing locked", now=NOW)
    assert len(results) == 2
    assert results[0]["source_date"] == "2026-06-12", "newer fact should rank first"


def test_recency_per_kind_half_life_protects_durable_kinds(budget_recall):
    # A durable-kind (decision) old fact must NOT be unfairly decayed below a
    # fast-decaying-kind (status) old fact of the same age and term match.
    # Same content, same age, same confidence — only the kind differs.
    old_decision = fact("the deployment region is us-east", kind="decision",
                         source_date="2026-03-01")
    old_status = fact("the deployment region is us-east", kind="status",
                      source_date="2026-03-01")
    results = budget_recall.search([old_status, old_decision], "deployment region", now=NOW)
    assert len(results) == 2
    # The durable decision outranks the stale status update at equal age.
    assert results[0]["kind"] == "decision", (
        f"durable kind should survive decay better, got {results[0]['kind']!r} first"
    )
    # And the per-kind factors are actually different (not both clamped/equal).
    rf_decision = budget_recall.recency_factor(old_decision, NOW)
    rf_status = budget_recall.recency_factor(old_status, NOW)
    assert rf_decision > rf_status, (
        f"decision half-life should decay slower: {rf_decision} vs {rf_status}"
    )


def test_recency_insight_decays_faster_than_durable_kinds(budget_recall):
    # N8392: synthesized insights inherit a FROZEN (often old) source_date, so a
    # 180-day half-life let stale insights bury recent work (the May-19 dump was
    # 85% of insights and dominated recall). Insights now decay at 45 days — faster
    # than durable decisions/directives — so a same-age insight ranks below a
    # decision and recent raw facts can surface.
    old_insight = fact("the deployment region is us-east", kind="insight",
                       source_date="2026-03-01")
    old_decision = fact("the deployment region is us-east", kind="decision",
                        source_date="2026-03-01")
    results = budget_recall.search([old_insight, old_decision], "deployment region", now=NOW)
    assert results[0]["kind"] == "decision", (
        f"a same-age insight (HL45) should rank below a decision (HL180), got {results[0]['kind']!r}"
    )
    rf_insight = budget_recall.recency_factor(old_insight, NOW)
    rf_decision = budget_recall.recency_factor(old_decision, NOW)
    assert rf_insight < rf_decision, (
        f"insight half-life should decay faster: {rf_insight} vs {rf_decision}"
    )
    # Lock the constant so the regression can't silently revert.
    assert budget_recall.RECENCY_HALF_LIFE_DAYS["insight"] == 45.0


def test_recency_missing_source_date_is_neutral_no_crash(budget_recall):
    # No source_date at all, and the 'unknown' sentinel — both get a neutral
    # 1.0 recency factor and must not crash. Backward compat for pre-N8069 facts.
    no_date = {"content": "pricing locked", "confidence": 0.9,
               "status": "current", "kind": "decision"}  # no source_date key
    unknown_date = fact("pricing locked", source_date="unknown")
    assert budget_recall.recency_factor(no_date, NOW) == 1.0
    assert budget_recall.recency_factor(unknown_date, NOW) == 1.0
    # search() must tolerate both without raising.
    results = budget_recall.search([no_date, unknown_date], "pricing locked", now=NOW)
    assert len(results) == 2


def test_recency_factor_decays_with_age(budget_recall):
    # Same-day / future-dated facts are fully fresh (1.0); older facts decay
    # monotonically toward (but never below) the floor.
    fresh = fact("x", kind="status", source_date="2026-06-13")
    one_half_life = fact("x", kind="status", source_date="2026-05-30")  # 14d = 1 half-life
    older = fact("x", kind="status", source_date="2026-05-01")
    assert budget_recall.recency_factor(fresh, NOW) == 1.0
    assert abs(budget_recall.recency_factor(one_half_life, NOW) - 0.5) < 0.01
    assert budget_recall.recency_factor(older, NOW) < budget_recall.recency_factor(one_half_life, NOW)
    assert budget_recall.recency_factor(older, NOW) >= budget_recall.MIN_RECENCY_FACTOR


def test_now_injectable_via_env(budget_recall, monkeypatch):
    # The scoring path must read an injected 'now' (env), never a bare
    # datetime.now(). With NOW pinned to the fact's own date, no decay applies.
    monkeypatch.setenv("NOCK_BRAIN_NOW", "2026-05-01")
    f = fact("pricing locked", kind="status", source_date="2026-05-01")
    # now=None -> falls back to env -> same day -> factor 1.0
    results = budget_recall.search([f], "pricing locked")
    assert len(results) == 1
    assert budget_recall._resolve_now(None) == datetime(2026, 5, 1)


def test_supersession_factor_is_noop_for_plain_current_facts(budget_recall):
    # In the live schema supersession is a hard filter, so a normal current
    # fact gets the no-op 1.0. (Documented hook — see supersession_factor.)
    assert budget_recall.supersession_factor(fact("anything")) == 1.0


def test_supersession_soft_signal_penalizes_but_keeps_rankable(budget_recall):
    # If a soft signal ever appears (deprecated flag, or a still-current fact
    # that points at a successor), it is down-weighted but still returned.
    plain = fact("the api endpoint is /v2/recall")
    deprecated = {**fact("the api endpoint is /v1/recall"), "deprecated": True}
    results = budget_recall.search([deprecated, plain], "api endpoint recall", now=NOW)
    assert len(results) == 2, "soft-deprecated fact is penalized, not filtered out"
    assert results[0]["content"].endswith("/v2/recall"), "non-deprecated fact wins"
    assert budget_recall.supersession_factor(deprecated) < 1.0


def test_recency_does_not_break_existing_uniform_date_ordering(budget_recall):
    # Regression guard: when all candidates share a source_date, recency is a
    # constant multiplier and BM25 relevance ordering is preserved.
    facts = [
        fact("pricing was set to 49 dollars for the command tier"),
        fact("the pricing plan and pricing tiers were locked"),
        fact("an unrelated note about the weather"),
    ]
    results = budget_recall.search(facts, "pricing plan", now=NOW)
    assert results[0]["content"].startswith("the pricing plan")


# --- N8142: per-batch source_date diversity cap ----------------------------

def test_diversity_cap_defers_overflow_same_date_to_tail(budget_recall):
    # 10 facts all dated the same day (a single bulk import); the cap keeps the
    # first N in score order at the front and defers the rest to the tail.
    same = [{"content": f"item {i}", "source_date": "2026-05-19"} for i in range(10)]
    capped = budget_recall._apply_date_diversity_cap(same, max_per_date=4)
    assert len(capped) == 10, "no fact is dropped — only reordered"
    # The first 4 are the original head (score order preserved); the rest are
    # deferred but still present.
    assert [f["content"] for f in capped[:4]] == [f"item {i}" for i in range(4)]


def test_diversity_cap_interleaves_other_dates_to_front(budget_recall):
    # A few May-19 facts followed by other-date facts. After the cap, the
    # over-cap May-19 facts are pushed behind the other-date facts.
    results = (
        [{"content": f"may19 {i}", "source_date": "2026-05-19"} for i in range(6)]
        + [{"content": "june fact", "source_date": "2026-06-20"}]
    )
    capped = budget_recall._apply_date_diversity_cap(results, max_per_date=4)
    front = [f["content"] for f in capped[:5]]
    assert "june fact" in front, "an other-date fact must reach the front, not be buried"
    # Exactly 4 May-19 facts sit ahead of the deferred tail.
    may_before_june = []
    for f in capped:
        if f["content"] == "june fact":
            break
        may_before_june.append(f)
    assert len(may_before_june) == 4


def test_diversity_cap_disabled_when_zero_or_negative(budget_recall):
    results = [{"content": f"x{i}", "source_date": "2026-05-19"} for i in range(10)]
    assert budget_recall._apply_date_diversity_cap(results, max_per_date=0) == results
    assert budget_recall._apply_date_diversity_cap(results, max_per_date=-1) == results


def test_diversity_cap_noop_when_under_cap(budget_recall):
    results = [{"content": "a", "source_date": "2026-05-19"}]
    assert budget_recall._apply_date_diversity_cap(results, max_per_date=4) == results


def test_diversity_cap_handles_missing_source_date(budget_recall):
    # Facts with no source_date all key to 'unknown' — they are capped together
    # and the path must not crash on the missing key.
    results = [{"content": f"x{i}"} for i in range(8)]
    capped = budget_recall._apply_date_diversity_cap(results, max_per_date=3)
    assert len(capped) == 8

def test_resolve_max_per_date_precedence(budget_recall, monkeypatch):
    monkeypatch.delenv("NOCKBRAIN_MAX_PER_DATE", raising=False)
    assert budget_recall._resolve_max_per_date(None) == budget_recall.DEFAULT_MAX_PER_DATE
    assert budget_recall._resolve_max_per_date(2) == 2  # explicit wins
    monkeypatch.setenv("NOCKBRAIN_MAX_PER_DATE", "7")
    assert budget_recall._resolve_max_per_date(None) == 7
    assert budget_recall._resolve_max_per_date(2) == 2  # explicit still wins over env
    monkeypatch.setenv("NOCKBRAIN_MAX_PER_DATE", "notanint")
    assert budget_recall._resolve_max_per_date(None) == budget_recall.DEFAULT_MAX_PER_DATE


def test_budget_recall_applies_diversity_cap_end_to_end(budget_recall, tmp_path):
    # 30 May-19 facts that all match the query plus a couple of other-date
    # matches. With the cap on, the other-date facts must surface in the output
    # rather than being buried behind 30 identical-date hits.
    facts = (
        [fact("pricing decision detail", source_date="2026-05-19") for _ in range(30)]
        + [fact("pricing decision fresh june note", source_date="2026-06-24")]
    )
    fp = tmp_path / "facts.json"
    fp.write_text(json.dumps(facts))
    out = budget_recall.budget_recall("pricing decision", fp, budget=200, now=NOW,
                                      max_per_date=4)
    assert "2026-06-24" in out, "other-date fact should surface under the cap"
    # At the SAME constrained budget with the cap disabled, May-19 dominates and
    # the june fact is buried below the truncation line.
    out_uncapped = budget_recall.budget_recall("pricing decision", fp, budget=200,
                                               now=NOW, max_per_date=0)
    assert "2026-06-24" not in out_uncapped, "uncapped: june fact buried by May-19"
    assert out_uncapped.count("2026-05-19") > out.count("2026-05-19")
