"""Tests for GRAPH-AUGMENTED RECALL (Graphify merge into budget-recall).

The graph path is ADDITIVE and flag-gated (default OFF). When off, recall is
byte-identical to the flat BM25 path. When on, a fact that shares no query terms
but DOES share a MENTIONS concept (or a SUPPORTS session) with a direct BM25 hit
is surfaced as a graph neighbor — ranked strictly BELOW every flat hit and still
subject to the same confidence / superseded / recency gates and the token budget.

Graph concepts come from export-graph.concepts() verbatim; we never reinvent the
extractor. These fixtures mirror tests/test_budget_recall.py style.
"""
import json
from datetime import datetime


NOW = datetime(2026, 6, 13)


def fact(content, confidence=0.9, status="current", kind="decision",
         source_date="2026-06-01", id=None, session=None, source_file=None):
    f = {
        "content": content,
        "confidence": confidence,
        "status": status,
        "kind": kind,
        "source_date": source_date,
    }
    if id is not None:
        f["id"] = id
    if session is not None:
        f["session"] = session
    if source_file is not None:
        f["source_file"] = source_file
    return f


def _write(tmp_path, facts):
    fp = tmp_path / "facts.json"
    fp.write_text(json.dumps(facts))
    return fp


# A and B share the rare concept 'seatbelt'. The query 'pricing' BM25-matches A
# only; B contains NONE of the query terms and is reachable ONLY via the graph.
def _bridge_fixture(tmp_path):
    a = fact("pricing was locked at the command seatbelt tier",
             id="A", session="s1", source_file="sess-a.jsonl")
    b = fact("the seatbelt rule was finalized in review",
             id="B", session="s2", source_file="sess-b.jsonl")
    return _write(tmp_path, [a, b])


def test_graph_on_surfaces_concept_neighbor_flat_misses(budget_recall, tmp_path):
    # THE proof: graph-on bridges to a fact flat search can't reach.
    fp = _bridge_fixture(tmp_path)
    out = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=True, now=NOW)
    assert "pricing was locked" in out, "direct BM25 hit A must be present"
    assert "seatbelt rule was finalized" in out, (
        "neighbor B must surface purely via the shared MENTIONS concept:seatbelt"
    )


def test_flat_search_misses_the_neighbor(budget_recall, tmp_path):
    # Negative control: flag off -> A present, B absent. B appears IFF graph on.
    fp = _bridge_fixture(tmp_path)
    out = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=False, now=NOW)
    assert "pricing was locked" in out
    assert "seatbelt rule was finalized" not in out


def test_flag_off_byte_identical_to_current(budget_recall, tmp_path):
    # Default (no graph kwarg) == graph_expand=False == frozen golden string.
    facts = [
        fact("pricing was set to 49 dollars for the command tier", id="f1"),
        fact("the pricing plan and pricing tiers were locked", id="f2"),
        fact("an unrelated note about the weather", id="f3"),
    ]
    fp = _write(tmp_path, facts)
    default_out = budget_recall.budget_recall("pricing plan", fp, now=NOW)
    off_out = budget_recall.budget_recall("pricing plan", fp, graph_expand=False, now=NOW)
    assert default_out == off_out
    # Frozen golden: the exact flat-path output for this fixture/query.
    golden = (
        "Memory recall (2 matches, budget 1000 tokens):\n\n"
        "[2026-06-01] [DECISION]\n"
        "the pricing plan and pricing tiers were locked\n\n"
        "[2026-06-01] [DECISION]\n"
        "pricing was set to 49 dollars for the command tier\n\n"
        "[2 item(s), ~46 tokens]"
    )
    assert default_out == golden

    # Stronger: the helper is a pure pass-through when off — returns the SAME
    # list object search() produced (identity preserved, no allocation).
    seeds = budget_recall.search(facts, "pricing plan", now=NOW)
    expanded = budget_recall._maybe_graph_expand(
        facts, seeds, "pricing plan", False, NOW, graph_expand=False
    )
    assert expanded is seeds


def test_env_var_gates_path(budget_recall, tmp_path, monkeypatch):
    # NOCKBRAIN_GRAPH_RECALL=1 surfaces B; unset/'0' hides it. Mirrors the
    # NOCK_BRAIN_NOW env pattern, resolved at main()-level via _env_truthy.
    fp = _bridge_fixture(tmp_path)

    monkeypatch.setenv("NOCKBRAIN_GRAPH_RECALL", "1")
    assert budget_recall._env_truthy("NOCKBRAIN_GRAPH_RECALL") is True
    on = budget_recall.budget_recall(
        "pricing", fp, budget=1000,
        graph_expand=budget_recall._env_truthy("NOCKBRAIN_GRAPH_RECALL"), now=NOW)
    assert "seatbelt rule was finalized" in on

    monkeypatch.setenv("NOCKBRAIN_GRAPH_RECALL", "0")
    assert budget_recall._env_truthy("NOCKBRAIN_GRAPH_RECALL") is False
    off = budget_recall.budget_recall(
        "pricing", fp, budget=1000,
        graph_expand=budget_recall._env_truthy("NOCKBRAIN_GRAPH_RECALL"), now=NOW)
    assert "seatbelt rule was finalized" not in off

    monkeypatch.delenv("NOCKBRAIN_GRAPH_RECALL", raising=False)
    assert budget_recall._env_truthy("NOCKBRAIN_GRAPH_RECALL") is False


def test_graph_neighbors_rank_below_direct_hits(budget_recall, tmp_path):
    # Several BM25 hits + one concept-neighbor: every flat hit precedes the
    # graph-surfaced fact (GRAPH_WEIGHT band separation holds).
    facts = [
        fact("pricing was set for the command seatbelt tier", id="d1",
             source_file="s.jsonl", session="s1"),
        fact("pricing plan and pricing tiers were locked", id="d2",
             source_file="s.jsonl", session="s1"),
        fact("a third pricing note about pricing discounts", id="d3",
             source_file="s.jsonl", session="s1"),
        # neighbor: shares 'seatbelt' with d1, contains no query term
        fact("the seatbelt rule was finalized in review", id="n1",
             source_file="s.jsonl", session="s9"),
    ]
    fp = _write(tmp_path, facts)
    out = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=True, now=NOW)
    assert "seatbelt rule was finalized" in out, "neighbor surfaced"
    pos_neighbor = out.index("seatbelt rule was finalized")
    for direct in ("pricing was set", "pricing plan and pricing",
                   "third pricing note"):
        assert direct in out
        assert out.index(direct) < pos_neighbor, (
            f"flat hit {direct!r} must precede the graph neighbor"
        )


def test_multi_word_query_surfaces_zero_overlap_neighbor(budget_recall, tmp_path):
    # Regression (2026-07-10): the old hardcoded >=2-shared-terms gate filtered
    # every possible neighbor of a >=3-term query (a fact sharing >=2 terms is
    # already a BM25 seed), silently disabling expansion for natural questions.
    # By default a zero-overlap concept neighbor must surface.
    facts = [
        fact("pricing tier decision locked at the command seatbelt anchor",
             id="seed", source_file="s.jsonl", session="s1"),
        fact("the seatbelt rule was finalized in review", id="neighbor",
             source_file="s.jsonl", session="s2"),
    ]
    fp = _write(tmp_path, facts)

    query = "pricing tier decision"  # 3 signal terms -> long-query path
    assert len(budget_recall._query_terms(query)) >= 3

    on = budget_recall.budget_recall(query, fp, budget=1000,
                                     graph_expand=True, now=NOW)
    assert "pricing tier decision locked" in on
    assert "seatbelt rule was finalized" in on, (
        "zero-overlap neighbor must surface via graph on a multi-word query"
    )

    off = budget_recall.budget_recall(query, fp, budget=1000,
                                      graph_expand=False, now=NOW)
    assert "seatbelt rule was finalized" not in off


def test_min_shared_terms_gate_filters_neighbors_when_set(budget_recall, tmp_path, monkeypatch):
    # NOCKBRAIN_GRAPH_MIN_SHARED_TERMS=1 restores an anti-drift guard for
    # multi-subject queries: a neighbor touching none of the query's signal
    # terms is dropped, while the flat seed is unaffected.
    facts = [
        fact("NockLock pricing uses the shared seatbelt anchor", id="seed",
             source_file="s.jsonl", session="s1"),
        fact("the seatbelt implementation note has no query subject terms", id="neighbor",
             source_file="s.jsonl", session="s2"),
    ]
    fp = _write(tmp_path, facts)
    query = "remind me what NockLock pricing is and who is on the consumer team"

    monkeypatch.setenv("NOCKBRAIN_GRAPH_MIN_SHARED_TERMS", "1")
    gated = budget_recall.budget_recall(query, fp, budget=1000,
                                        graph_expand=True, now=NOW)
    assert "NockLock pricing uses" in gated
    assert "implementation note" not in gated

    monkeypatch.delenv("NOCKBRAIN_GRAPH_MIN_SHARED_TERMS", raising=False)
    ungated = budget_recall.budget_recall(query, fp, budget=1000,
                                          graph_expand=True, now=NOW)
    assert "implementation note" in ungated


def test_graph_expansion_respects_budget(budget_recall, tmp_path):
    # Many concept-neighbors but a tiny budget -> seeds present, truncation
    # message emitted, output never exceeds budget. (graph_expand=True variant
    # of test_budget_recall_truncates_to_budget.)
    seed = fact("pricing decision with the shared seatbelt anchor word " * 3,
                id="seed", source_file="s.jsonl", session="s1")
    neighbors = [
        fact(f"the seatbelt followup note number {i} with extra padding words", id=f"n{i}",
             source_file="s.jsonl", session="s1")
        for i in range(20)
    ]
    fp = _write(tmp_path, [seed] + neighbors)
    out = budget_recall.budget_recall("pricing", fp, budget=80,
                                      graph_expand=True, now=NOW)
    assert "pricing decision" in out, "seed survives the budget"
    assert "truncated by budget" in out


def test_graph_expansion_honors_superseded_and_confidence_filters(budget_recall, tmp_path):
    # A superseded / sub-MIN_CONFIDENCE concept-neighbor is NOT surfaced by the
    # graph path unless include_superseded=True — expansion reuses search()'s gates.
    seed = fact("pricing was locked at the seatbelt tier", id="seed",
                source_file="s.jsonl", session="s1")
    superseded_neighbor = fact("the seatbelt rule is now superseded", id="sup",
                               status="superseded", source_file="s.jsonl", session="s2")
    lowconf_neighbor = fact("the seatbelt rule with low confidence", id="low",
                            confidence=0.5, source_file="s.jsonl", session="s3")
    fp = _write(tmp_path, [seed, superseded_neighbor, lowconf_neighbor])

    out = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=True, now=NOW)
    assert "pricing was locked" in out
    assert "now superseded" not in out, "superseded neighbor filtered by default"
    assert "low confidence" not in out, "sub-MIN_CONFIDENCE neighbor filtered"

    out_super = budget_recall.budget_recall("pricing", fp, budget=1000,
                                            graph_expand=True,
                                            include_superseded=True, now=NOW)
    assert "now superseded" in out_super, (
        "include_superseded=True surfaces the superseded neighbor"
    )


def test_same_session_expansion_toggle(budget_recall, tmp_path, monkeypatch):
    # Two facts sharing ONLY a session (no shared concept): session expansion on
    # bridges them; NOCKBRAIN_GRAPH_SESSION=0 does not.
    seed = fact("pricing rates were adjusted upward", id="seed",
                source_file="s.jsonl", session="shared-sess")
    # neighbor shares the session but no concept and no query term
    neighbor = fact("a goldfish swam quietly through murky aquarium glass",
                    id="nbr", source_file="s.jsonl", session="shared-sess")
    fp = _write(tmp_path, [seed, neighbor])

    on = budget_recall.budget_recall("pricing", fp, budget=1000,
                                     graph_expand=True, now=NOW)
    assert "goldfish swam quietly" in on, "same-session neighbor bridges when on"

    monkeypatch.setenv("NOCKBRAIN_GRAPH_SESSION", "0")
    off = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=True, now=NOW)
    assert "goldfish swam quietly" not in off, (
        "NOCKBRAIN_GRAPH_SESSION=0 disables same-session expansion"
    )


def test_facts_without_id_are_tolerated(budget_recall, tmp_path):
    # graph-on over facts lacking id/session must not crash; degrades to flat
    # (id-less facts can't be graph targets). Mirrors the missing-source_date guard.
    facts = [
        fact("pricing was locked at the command seatbelt tier"),  # no id
        fact("the seatbelt rule was finalized in review"),  # no id
    ]
    fp = _write(tmp_path, facts)
    out = budget_recall.budget_recall("pricing", fp, budget=1000,
                                      graph_expand=True, now=NOW)
    assert "pricing was locked" in out, "flat hit still works"
    assert "seatbelt rule was finalized" not in out, (
        "id-less neighbor can't be a graph target -> degrades to flat, no crash"
    )
