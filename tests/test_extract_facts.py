"""Tests for fact extraction — the hardest, least-tested part: turning transcript
bullets into structured facts, classifying tagged + inferred lines, skipping
operational noise, and deduping near-identical content."""


def test_classify_bullet_tagged(extract_facts):
    res = extract_facts.classify_bullet("[DECISION] we chose Postgres for the store")
    assert res == ("decision", 0.9)


def test_classify_bullet_inferred_natural_language(extract_facts):
    # No tag — must still be caught by the inferred patterns.
    res = extract_facts.classify_bullet("Kevin corrected the pricing tier last night")
    assert res is not None
    assert res[0] == "correction"


def test_classify_bullet_merge_pattern(extract_facts):
    res = extract_facts.classify_bullet("merged PR #42 to main after review")
    assert res is not None
    assert res[0] == "merge"


def test_classify_bullet_skips_operational_noise(extract_facts):
    assert extract_facts.classify_bullet("Claude Code wrote compaction checkpoint") is None
    assert extract_facts.classify_bullet("HEARTBEAT_OK at 14:00") is None
    assert extract_facts.classify_bullet("Claude Code sent via Telegram an update") is None


def test_make_id_deterministic(extract_facts):
    a = extract_facts.make_id("same content", "2026-06-01")
    b = extract_facts.make_id("same content", "2026-06-01")
    assert a == b
    assert len(a) == 12


def test_extract_metadata_pr_numbers(extract_facts):
    meta = extract_facts.extract_metadata("merged PR #42 and later PR #43")
    assert meta.get("pr_numbers") == [42, 43]


def test_parse_file_extracts_and_skips(extract_facts, tmp_path):
    md = tmp_path / "2026-06-01.md"
    md.write_text(
        "## Session 10:00\n"
        "- [DECISION] we chose Seatbelt over the preload shim\n"
        "- Kevin corrected the pricing tier\n"
        "- Claude Code wrote compaction checkpoint\n"
        "- HEARTBEAT_OK\n"
    )
    facts = extract_facts.parse_file(md)
    kinds = {f["kind"] for f in facts}
    contents = " ".join(f["content"] for f in facts)

    assert "decision" in kinds  # tagged line extracted
    assert "correction" in kinds  # inferred line extracted
    assert "compaction checkpoint" not in contents  # noise skipped
    assert "HEARTBEAT" not in contents  # noise skipped
    assert all(f["session"] == "Session 10:00" for f in facts)
    assert all(f["source_date"] == "2026-06-01" for f in facts)


def test_parse_file_respects_since(extract_facts, tmp_path):
    md = tmp_path / "2026-05-01.md"
    md.write_text("## Session 10:00\n- [DECISION] old decision\n")
    assert extract_facts.parse_file(md, since_date="2026-06-01") == []


def test_dedupe_collapses_near_identical(extract_facts):
    facts = [
        {"content": "Claude Code merged the PR to main", "kind": "merge"},
        {"content": "merged the PR to main", "kind": "merge"},
    ]
    out = extract_facts.dedupe(facts)
    assert len(out) == 1
