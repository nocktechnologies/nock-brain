"""Tests for detect-contradictions.py (E5b): surface stale-but-live facts by
pairing topic-overlapping facts across time and (opt-in) asking an LLM judge
whether the later one replaces the earlier. Propose-only: the tool never
writes the store — applying is supersede-fact.py's job, behind the human gate.

Counting conventions mirror the 2026-07-27 live measurement: a >=1 day gap
between the stale and superseding fact is required for CONFIRMED; same-date
pairs are at most borderline."""
import json
import sys

import pytest

OLD_RULE = "Updates to Kevin go out as spoken audio clips only, no typed notes."
NEW_RULE = "Updates to Kevin go out as both a spoken audio clip and a typed note."
UNRELATED = "The staging database is rebuilt from the nightly snapshot."

NEAR_DUP_A = "Every nock created for mara must include a surface line in the body."
NEAR_DUP_B = "Every nock created for mara must include a surface line in the body"


def _fact(fid, content, kind="decision", source_date="2026-06-20", **extra):
    f = {
        "id": fid, "kind": kind, "status": "current", "confidence": 0.9,
        "content": content, "source_date": source_date, "evidence": [],
    }
    f.update(extra)
    return f


def _pair_facts(gap_days=10):
    late_day = 1 + gap_days
    return [
        _fact("old", OLD_RULE, kind="decision", source_date="2026-06-01"),
        _fact("new", NEW_RULE, kind="correction", source_date=f"2026-06-{late_day:02d}"),
        _fact("noise", UNRELATED, kind="decision", source_date="2026-06-05"),
    ]


# ── structural candidate pairing ─────────────────────────────────────────────
def test_topic_overlapping_facts_pair_across_kinds(detect_contradictions):
    candidates = detect_contradictions.find_candidates(_pair_facts())
    assert len(candidates) == 1
    c = candidates[0]
    assert c["earlier"]["id"] == "old"
    assert c["later"]["id"] == "new"


def test_unrelated_facts_do_not_pair(detect_contradictions):
    facts = [
        _fact("a", OLD_RULE, source_date="2026-06-01"),
        _fact("b", UNRELATED, source_date="2026-06-10"),
    ]
    assert detect_contradictions.find_candidates(facts) == []


def test_near_duplicates_are_dedups_job_not_contradictions(detect_contradictions):
    facts = [
        _fact("a", NEAR_DUP_A, source_date="2026-06-01"),
        _fact("b", NEAR_DUP_B, source_date="2026-06-10"),
    ]
    assert detect_contradictions.find_candidates(facts) == []


def test_superseded_and_expired_facts_excluded(detect_contradictions):
    facts = _pair_facts()
    facts[0]["status"] = "superseded"
    assert detect_contradictions.find_candidates(facts) == []


def test_date_gap_classification(detect_contradictions):
    with_gap = detect_contradictions.find_candidates(_pair_facts(gap_days=10))[0]
    assert with_gap["date_gap_days"] >= 1

    same_day = [
        _fact("old", OLD_RULE, source_date="2026-06-01"),
        _fact("new", NEW_RULE, source_date="2026-06-01"),
    ]
    candidate = detect_contradictions.find_candidates(same_day)[0]
    assert candidate["date_gap_days"] == 0


def test_max_pairs_cap_keeps_strongest_overlap(detect_contradictions, capsys):
    base = "kevin update delivery rule for channel"
    facts = [
        _fact(f"f{i}", f"{base} variant {i} extra token{i}", source_date=f"2026-06-{i + 1:02d}")
        for i in range(6)
    ]
    candidates = detect_contradictions.find_candidates(facts, max_pairs=3)
    assert len(candidates) == 3
    err = capsys.readouterr().err
    assert "dropped" in err  # no silent caps


# ── classification (structural + judge verdicts) ─────────────────────────────
def test_structural_only_marks_unreviewed(detect_contradictions):
    rows = detect_contradictions.classify(detect_contradictions.find_candidates(_pair_facts()))
    assert rows[0]["classification"] == "unreviewed"
    assert rows[0]["verdict_source"] == "structural"


def test_judge_yes_with_gap_is_confirmed(detect_contradictions):
    candidates = detect_contradictions.find_candidates(_pair_facts(gap_days=10))
    rows = detect_contradictions.classify(
        candidates, judge=lambda early, late: "VERDICT: yes\nThe later rule replaces the earlier."
    )
    assert rows[0]["classification"] == "confirmed"
    assert rows[0]["verdict_source"] == "llm"


def test_judge_yes_without_gap_is_borderline(detect_contradictions):
    same_day = [
        _fact("old", OLD_RULE, source_date="2026-06-01"),
        _fact("new", NEW_RULE, source_date="2026-06-01"),
    ]
    rows = detect_contradictions.classify(
        detect_contradictions.find_candidates(same_day),
        judge=lambda early, late: "VERDICT: yes",
    )
    assert rows[0]["classification"] == "borderline"


def test_judge_no_drops_candidate(detect_contradictions):
    rows = detect_contradictions.classify(
        detect_contradictions.find_candidates(_pair_facts()),
        judge=lambda early, late: "VERDICT: no",
    )
    assert rows == []


def test_judge_garbage_or_failure_is_borderline(detect_contradictions):
    candidates = detect_contradictions.find_candidates(_pair_facts())
    for raw in ("", "something unparseable", "VERDICT: maybe"):
        rows = detect_contradictions.classify(candidates, judge=lambda e, l, r=raw: r)
        assert rows[0]["classification"] == "borderline"


def test_judge_sees_scrubbed_content_only(detect_contradictions):
    secret = "the deploy uses STRIPE_API_KEY=sk_live_abcdef1234567890abcdef and that is fine"
    facts = [
        _fact("old", secret + " for kevin updates delivery", source_date="2026-06-01"),
        _fact("new", "the deploy stopped using that key for kevin updates delivery",
              kind="correction", source_date="2026-06-10"),
    ]
    seen = []

    def spy_judge(early, late):
        seen.append(early + "\n" + late)
        return "VERDICT: yes"

    detect_contradictions.classify(detect_contradictions.find_candidates(facts), judge=spy_judge)
    assert seen, "judge was never called"
    assert "sk_live_abcdef1234567890abcdef" not in seen[0]


# ── CLI: propose-only ────────────────────────────────────────────────────────
def _run_main(detect_contradictions, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["detect-contradictions.py"] + argv)
    try:
        detect_contradictions.main()
    except SystemExit:
        pass


def test_cli_writes_queue_and_never_touches_store(detect_contradictions, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_pair_facts()))
    before = store.read_bytes()
    queue_dir = tmp_path / "review"

    _run_main(detect_contradictions, monkeypatch,
              ["--facts", str(store), "--queue-dir", str(queue_dir)])

    assert store.read_bytes() == before
    queue = json.loads((queue_dir / "contradiction-candidates.json").read_text())
    assert queue["candidate_count"] == 1
    row = queue["candidates"][0]
    assert row["earlier_id"] == "old"
    assert row["later_id"] == "new"
    assert "supersede-fact.py old --by new" in row["proposed_action"]
    assert (queue_dir / "contradiction-candidates.md").exists()


def test_cli_llm_flag_uses_claude_judge(detect_contradictions, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_pair_facts(gap_days=10)))
    queue_dir = tmp_path / "review"
    monkeypatch.setattr(detect_contradictions, "_call_claude",
                        lambda prompt, model, timeout: "VERDICT: yes\nLater replaces earlier.")

    _run_main(detect_contradictions, monkeypatch,
              ["--facts", str(store), "--queue-dir", str(queue_dir), "--llm"])

    queue = json.loads((queue_dir / "contradiction-candidates.json").read_text())
    assert queue["candidates"][0]["classification"] == "confirmed"
    assert queue["candidates"][0]["verdict_source"] == "llm"


def test_cli_llm_failure_degrades_to_borderline_not_crash(detect_contradictions, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_pair_facts()))
    queue_dir = tmp_path / "review"
    monkeypatch.setattr(detect_contradictions, "_call_claude",
                        lambda prompt, model, timeout: "")  # claude missing/errored

    _run_main(detect_contradictions, monkeypatch,
              ["--facts", str(store), "--queue-dir", str(queue_dir), "--llm"])

    queue = json.loads((queue_dir / "contradiction-candidates.json").read_text())
    assert queue["candidates"][0]["classification"] == "borderline"


# ── F2: make the nightly finishable + its silence visible ────────────────────
def test_llm_top_bounds_judge_calls(detect_contradictions):
    """--llm over a big store meant hundreds of sequential claude -p calls —
    the nightly could never finish and died silently. llm_top judges only the
    strongest N; the rest stay structural (unreviewed), never dropped."""
    base = "kevin update delivery rule for channel"
    facts = [
        _fact(f"f{i}", f"{base} variant {i} extra token{i}",
              source_date=f"2026-06-{i + 1:02d}")
        for i in range(6)
    ]
    candidates = detect_contradictions.find_candidates(facts)
    assert len(candidates) > 3
    calls = []

    def spy(early, late):
        calls.append(1)
        return "VERDICT: yes"

    rows = detect_contradictions.classify(candidates, judge=spy, llm_top=2)
    assert len(calls) == 2                       # judged exactly top-2
    judged = [r for r in rows if r["verdict_source"] == "llm"]
    structural = [r for r in rows if r["verdict_source"] == "structural"]
    assert len(judged) == 2
    assert len(structural) == len(candidates) - 2  # rest kept, not dropped
    assert all(r["classification"] == "unreviewed" for r in structural)


def test_llm_top_none_judges_everything(detect_contradictions):
    candidates = detect_contradictions.find_candidates(_pair_facts())
    calls = []
    rows = detect_contradictions.classify(
        candidates, judge=lambda e, l: (calls.append(1) or "VERDICT: yes"))
    assert len(calls) == len(candidates)


def test_cli_llm_top_wired(detect_contradictions, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    base = "kevin update delivery rule for channel"
    facts = [
        _fact(f"f{i}", f"{base} variant {i} extra token{i}",
              source_date=f"2026-06-{i + 1:02d}")
        for i in range(6)
    ]
    store.write_text(json.dumps(facts))
    queue_dir = tmp_path / "review"
    calls = []
    monkeypatch.setattr(detect_contradictions, "_call_claude",
                        lambda p, m, t: (calls.append(1) or "VERDICT: yes"))

    _run_main(detect_contradictions, monkeypatch,
              ["--facts", str(store), "--queue-dir", str(queue_dir),
               "--llm", "--llm-top", "3"])

    assert len(calls) == 3
    queue = json.loads((queue_dir / "contradiction-candidates.json").read_text())
    assert queue["llm_top"] == 3


def test_negative_llm_top_is_an_error(detect_contradictions, tmp_path, monkeypatch):
    import sys
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_pair_facts()))
    monkeypatch.setattr(sys, "argv",
                        ["detect-contradictions.py", "--facts", str(store),
                         "--llm", "--llm-top", "-1"])
    with pytest.raises(SystemExit):
        detect_contradictions.main()


def test_queue_doc_records_max_pairs_truncation(detect_contradictions, tmp_path, monkeypatch):
    """The artifact must tell the whole truth: pairs dropped by --max-pairs are
    recorded in the doc, not just whispered to stderr."""
    base = "kevin update delivery rule for channel"
    facts = [_fact(f"f{i}", f"{base} variant {i} extra token{i}",
                   source_date=f"2026-06-{i + 1:02d}") for i in range(6)]
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))
    queue_dir = tmp_path / "review"

    _run_main(detect_contradictions, monkeypatch,
              ["--facts", str(store), "--queue-dir", str(queue_dir),
               "--max-pairs", "3"])

    queue = json.loads((queue_dir / "contradiction-candidates.json").read_text())
    assert queue["max_pairs"] == 3
    assert queue["dropped_pairs"] > 0
