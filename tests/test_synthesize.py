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


# --- opt-in LLM (Haiku-distill) synthesizer -------------------------------
# The heuristic stays the default. An injected synthesizer enriches ONLY the
# human-readable `content`; identity/provenance fields stay heuristic and
# deterministic, so an LLM can never alter an insight's identity or corrupt
# the store. Any synthesizer failure falls back to the heuristic content.

def test_heuristic_default_marks_synthesized_by_heuristic(synthesize):
    facts = [fact("beta recurring theme"), fact("beta recurring theme two")]
    ins = synthesize.synthesize(facts, threshold=0.2, min_cluster=2)[0]
    assert ins["synthesized_by"] == "heuristic"
    assert ins["content"].startswith("Recurring correction")


def test_llm_synthesizer_enriches_only_content_keeps_provenance(synthesize):
    facts = [
        fact("pricing tier correction one", source_date="2026-06-01"),
        fact("pricing tier correction two", source_date="2026-06-03"),
    ]
    base = synthesize.synthesize(facts, threshold=0.2, min_cluster=2)[0]
    seen = []

    def fake(cluster, heuristic_content):
        seen.append(heuristic_content)
        return "Always confirm the command-plan pricing tier with Kevin before publishing."

    enriched = synthesize.synthesize(
        facts, threshold=0.2, min_cluster=2, synthesizer=fake
    )[0]
    # The prose content is the LLM output...
    assert enriched["content"] == "Always confirm the command-plan pricing tier with Kevin before publishing."
    assert enriched["synthesized_by"] == "llm"
    # ...but every identity/provenance field is byte-identical to the heuristic.
    for key in ("id", "of_kind", "recurrence", "confidence", "source_ids", "source_dates", "source_date"):
        assert enriched[key] == base[key], f"{key} drifted under LLM synthesis"
    # The synthesizer received the heuristic content to fall back to.
    assert seen and seen[0] == base["content"]


def test_llm_synthesizer_exception_falls_back_to_heuristic(synthesize):
    facts = [fact("shared recurring theme one"), fact("shared recurring theme two")]
    base = synthesize.synthesize(facts, threshold=0.2, min_cluster=2)[0]

    def boom(cluster, heuristic_content):
        raise RuntimeError("claude unavailable")

    out = synthesize.synthesize(facts, threshold=0.2, min_cluster=2, synthesizer=boom)[0]
    assert out["content"] == base["content"]  # never crashes; keeps heuristic
    assert out["synthesized_by"] == "heuristic"


def test_llm_synthesizer_empty_output_falls_back_to_heuristic(synthesize):
    facts = [fact("alpha recurring theme"), fact("alpha recurring theme again")]
    base = synthesize.synthesize(facts, threshold=0.2, min_cluster=2)[0]
    out = synthesize.synthesize(
        facts, threshold=0.2, min_cluster=2, synthesizer=lambda c, h: "   "
    )[0]
    assert out["content"] == base["content"]
    assert out["synthesized_by"] == "heuristic"


def test_call_claude_swallows_missing_binary(synthesize, monkeypatch):
    def raise_oserror(*a, **k):
        raise OSError("no claude binary")

    monkeypatch.setattr(synthesize.subprocess, "run", raise_oserror)
    assert synthesize._call_claude("hi", "haiku", 5) == ""


def test_make_claude_synthesizer_returns_empty_on_failed_call(synthesize, monkeypatch):
    # When the underlying claude call yields nothing, the synthesizer returns ""
    # (not the heuristic) so synthesize_cluster owns the single fallback path.
    monkeypatch.setattr(synthesize, "_call_claude", lambda *a, **k: "")
    synth = synthesize.make_claude_synthesizer(model="haiku", timeout=5)
    assert synth([fact("x"), fact("y")], "heuristic fallback content") == ""


def test_make_claude_synthesizer_scrubs_secrets_from_prompt(synthesize, monkeypatch):
    # Fact content is re-scrubbed before it is embedded in the LLM prompt, and
    # scrubbing happens BEFORE the 300-char truncation so a secret near the cut
    # cannot leak as a fragment.
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    padding = "x" * 290  # pushes the second secret across the 300-char boundary
    facts = [
        fact(f"deploy failed with GITHUB_TOKEN={token} in the release step"),
        fact(f"{padding} api_key: {'k' * 24} rotated after the incident"),
    ]
    prompts = []

    def capture(prompt, model, timeout):
        prompts.append(prompt)
        return "Rotate credentials before every release."

    monkeypatch.setattr(synthesize, "_call_claude", capture)
    synth = synthesize.make_claude_synthesizer(model="haiku", timeout=5)
    assert synth(facts, "heuristic fallback content") != ""

    prompt = prompts[0]
    assert token not in prompt
    assert "k" * 24 not in prompt
    assert "[REDACTED_SECRET]" in prompt


def test_make_claude_synthesizer_collapses_and_returns_llm_text(synthesize, monkeypatch):
    monkeypatch.setattr(
        synthesize, "_call_claude",
        lambda *a, **k: "  Confirm the pricing tier\nwith Kevin first.  ",
    )
    synth = synthesize.make_claude_synthesizer(model="haiku", timeout=5)
    out = synth([fact("x"), fact("y")], "heuristic fallback content")
    assert out == "Confirm the pricing tier with Kevin first."


def test_llm_top_caps_enrichment_to_strongest_clusters(synthesize):
    # Two disjoint clusters: a strong recurrence (4) and a weaker one (2).
    facts = (
        [fact(f"apple orchard harvest {i}") for i in range(4)]   # recurrence 4
        + [fact(f"zebra desert mirage {i}") for i in range(2)]   # recurrence 2
    )
    calls = []

    def fake(cluster, h):
        calls.append(len(cluster))
        return "Consolidated lesson sentence for this cluster."

    ins = synthesize.synthesize(
        facts, threshold=0.2, min_cluster=2, synthesizer=fake, llm_top=1
    )
    # Only the single strongest cluster was sent to the LLM (bounded budget).
    assert calls == [4]
    llm = [i for i in ins if i["synthesized_by"] == "llm"]
    heur = [i for i in ins if i["synthesized_by"] == "heuristic"]
    assert len(llm) == 1 and llm[0]["recurrence"] == 4
    assert any(i["recurrence"] == 2 for i in heur)  # weaker cluster stayed heuristic


def test_llm_top_none_enriches_every_cluster(synthesize):
    facts = (
        [fact(f"apple orchard harvest {i}") for i in range(3)]
        + [fact(f"zebra desert mirage {i}") for i in range(2)]
    )
    calls = []

    def fake(cluster, h):
        calls.append(len(cluster))
        return "Consolidated lesson sentence for this cluster."

    ins = synthesize.synthesize(
        facts, threshold=0.2, min_cluster=2, synthesizer=fake, llm_top=None
    )
    assert len(calls) == 2  # no cap -> both clusters enriched
    assert all(i["synthesized_by"] == "llm" for i in ins)
