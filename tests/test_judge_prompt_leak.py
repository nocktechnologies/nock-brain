"""N10052: the nightly LLM judges must never leak their own prompts into the store.

The consolidation judges (synthesize --llm, detect-contradictions --llm) run
Haiku via ``claude -p``. Without --no-session-persistence those one-shot calls
persist transcripts under ~/.claude/projects — the exact default source root
rebuild-store scans — so the judge's own prompt template re-entered the
candidate stream nightly and was minted as 0.75-0.9-confidence "facts"
(47 found in the 2026-08-30 curate, 39 still current).

Two independent layers are locked in here:
  1. SOURCE: _call_claude never persists a session (nothing to ingest).
  2. SINK: is_structural_noise drops any candidate embedding a registered
     judge-prompt marker — even [TAG]-prefixed, so a leaked injected-insight
     rendering ("[INSIGHT] Recurring directive (seen 33x): ...") cannot
     re-mint either.
"""


def event(content, surface="text", actor="user", line=7):
    return {
        "id": f"event-{line}",
        "source": {
            "adapter": "claude-jsonl",
            "path": "/Users/kevin/.claude/projects/demo/session.jsonl",
            "line": line,
            "session_id": "s1",
            "timestamp": "2026-08-29T05:00:00Z",
        },
        "actor": actor,
        "surface": surface,
        "kind": "message",
        "content": content,
        "metadata": {},
        "privacy": {"scrubbed": False, "excluded": False, "policy_version": "v1"},
    }


# Realistic payloads: the live prompts embed real fact text, whose trigger
# words are what made classify_bullet mint them ('bug' 0.7, 'decision' 0.8 —
# the mislabeled-kinds spread Mira reported on N10052).
SYNTH_PROMPT = (
    "These notes are the same recurring lesson from past work sessions. "
    "Write ONE clear, specific sentence (max 45 words) stating the durable, "
    "reusable lesson - what to do or avoid next time. Output only the "
    "sentence, no preamble or quotes.\n\n"
    "- decided to keep the BM25 floor as the recall baseline\n"
    "- fixed the sidecar cache bug in promotion"
)

CONTRA_PROMPT = (
    "Two memory facts from the same project, EARLIER then LATER.\n"
    "Does the LATER one contradict or replace the EARLIER one, such that "
    "the earlier fact is now stale and should be marked superseded?\n"
    "Answer on the first line exactly `VERDICT: yes`, `VERDICT: no`, or "
    "`VERDICT: unclear`, then one short reason sentence.\n\n"
    "EARLIER: Kevin decided the store stays JSON.\n\n"
    "LATER: Kevin decided the store moves to SQLite."
)

LEAKED_INSIGHT_RENDER = (
    "[INSIGHT] Recurring directive (seen 33x, 2026-07-14..2026-08-29): "
    "Two memory facts from the same project, EARLIER then LATER. "
    "Most recent: Kevin decided the earlier fact is now stale and superseded."
)


def test_synthesize_judge_prompt_mints_no_fact(refine_sessions):
    assert refine_sessions.fact_from_event(event(SYNTH_PROMPT)) is None


def test_contradiction_judge_prompt_mints_no_fact(refine_sessions):
    assert refine_sessions.fact_from_event(event(CONTRA_PROMPT)) is None


def test_leaked_insight_rendering_mints_no_fact(refine_sessions):
    # A leaked insight arrives [TAG]-prefixed via the inject hook; the marker
    # check must outrank the genuine-[TAG] escape hatch.
    assert refine_sessions.fact_from_event(event(LEAKED_INSIGHT_RENDER)) is None


def test_genuine_fact_mentioning_memory_facts_still_mints(refine_sessions):
    # Guard precision: ordinary prose about memory facts is not the template.
    fact = refine_sessions.fact_from_event(
        event("[DECISION] Kevin decided the two memory facts about the store "
              "stay separate rather than being merged.")
    )
    assert fact is not None


def test_call_claude_disables_session_persistence(synthesize, monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(synthesize.subprocess, "run", fake_run)
    out = synthesize._call_claude("prompt text", "claude-haiku-4-5-20251001", 5)
    assert out == "ok"
    assert "--no-session-persistence" in seen["argv"]


def test_judge_prompts_carry_registered_markers(synthesize, detect_contradictions, scrub, monkeypatch):
    # The live prompts must embed the exact marker strings the sink checks,
    # so a reworded template cannot drift out from under the guard.
    captured = {}

    def capture(prompt, model, timeout):
        captured["p"] = prompt
        return ""

    monkeypatch.setattr(synthesize, "_call_claude", capture)
    synthesize.make_claude_synthesizer()([{"content": "note"}], "h")
    assert any(m in captured["p"] for m in scrub.JUDGE_PROMPT_MARKERS)

    monkeypatch.setattr(detect_contradictions, "_call_claude", capture)
    detect_contradictions.make_claude_judge()("early fact", "late fact")
    assert any(m in captured["p"] for m in scrub.JUDGE_PROMPT_MARKERS)
