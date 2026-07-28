"""Tests for dedup-facts.py (E5a): collapse near-identical extractions of one
real event into a single canonical fact, marking the rest superseded with a
superseded_by link — mark-only, so signatures over the immutable core survive.

The acceptance archetype is the measured 12→1 case: one decision extracted
into a dozen near-identical live facts, each one competing in recall."""
import json
import sys
from pathlib import Path

VARIANTS = [
    "Every nock created for mara must include a surface line in the body.",
    "Every nock created for mara must include a surface line in the body",
    "every nock created for Mara must include a surface line in the body.",
    "Every nock created for mara must include a surface line in its body.",
    "Decision: every nock created for mara must include a surface line in the body.",
    "Every nock created for mara MUST include a surface line in the body.",
]

UNRELATED = "Railway deploys go through the staging environment first."


def _fact(fid, content, kind="decision", confidence=0.8, source_date="2026-06-20", **extra):
    f = {
        "id": fid, "kind": kind, "status": "current", "confidence": confidence,
        "content": content, "source_date": source_date, "evidence": [],
    }
    f.update(extra)
    return f


def _archetype(n=6):
    facts = [
        _fact(f"dup-{i}", VARIANTS[i % len(VARIANTS)], source_date=f"2026-06-{10 + i:02d}")
        for i in range(n)
    ]
    facts.append(_fact("other", UNRELATED))
    return facts


# ── clustering ───────────────────────────────────────────────────────────────
def test_near_identical_same_kind_cluster_together(dedup_facts):
    clusters = dedup_facts.find_clusters(_archetype())
    assert len(clusters) == 1
    ids = {clusters[0]["canonical"]["id"]} | {d["id"] for d in clusters[0]["duplicates"]}
    assert ids == {f"dup-{i}" for i in range(6)}


def test_unrelated_content_not_clustered(dedup_facts):
    clusters = dedup_facts.find_clusters(_archetype())
    for cluster in clusters:
        assert cluster["canonical"]["id"] != "other"
        assert all(d["id"] != "other" for d in cluster["duplicates"])


def test_same_content_different_kind_never_clusters(dedup_facts):
    facts = [
        _fact("a", VARIANTS[0], kind="decision"),
        _fact("b", VARIANTS[1], kind="directive"),
    ]
    assert dedup_facts.find_clusters(facts) == []


def test_superseded_and_window_closed_facts_excluded(dedup_facts):
    facts = [
        _fact("live-1", VARIANTS[0]),
        _fact("live-2", VARIANTS[1]),
        _fact("gone", VARIANTS[2], status="superseded"),
        _fact("expired", VARIANTS[3], invalid_at="2026-01-01T00:00:00+00:00"),
    ]
    clusters = dedup_facts.find_clusters(facts)
    assert len(clusters) == 1
    ids = {clusters[0]["canonical"]["id"]} | {d["id"] for d in clusters[0]["duplicates"]}
    assert ids == {"live-1", "live-2"}


def test_min_similarity_threshold_respected(dedup_facts):
    facts = [
        _fact("a", "the deploy pipeline uses railway with a staging gate"),
        _fact("b", "the deploy pipeline uses railway"),
    ]
    assert dedup_facts.find_clusters(facts, min_similarity=0.99) == []


def test_singletons_produce_no_cluster(dedup_facts):
    assert dedup_facts.find_clusters([_fact("a", VARIANTS[0])]) == []


# ── canonical choice ─────────────────────────────────────────────────────────
def test_canonical_prefers_curated(dedup_facts):
    facts = [
        _fact("dup-1", VARIANTS[0], confidence=0.95),
        _fact("curated-abc", VARIANTS[1], confidence=0.7),
    ]
    cluster = dedup_facts.find_clusters(facts)[0]
    assert cluster["canonical"]["id"] == "curated-abc"


def test_canonical_prefers_confidence_then_earliest(dedup_facts):
    facts = [
        _fact("late-high", VARIANTS[0], confidence=0.9, source_date="2026-06-20"),
        _fact("early-high", VARIANTS[1], confidence=0.9, source_date="2026-06-01"),
        _fact("low", VARIANTS[2], confidence=0.7, source_date="2026-05-01"),
    ]
    cluster = dedup_facts.find_clusters(facts)[0]
    assert cluster["canonical"]["id"] == "early-high"


def test_canonical_choice_deterministic_under_input_order(dedup_facts):
    facts = _archetype()
    canonical_a = dedup_facts.find_clusters(facts)[0]["canonical"]["id"]
    canonical_b = dedup_facts.find_clusters(list(reversed(facts)))[0]["canonical"]["id"]
    assert canonical_a == canonical_b


# ── propose mode (default): store untouched, queue written ───────────────────
def _run_main(dedup_facts, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["dedup-facts.py"] + argv)
    try:
        dedup_facts.main()
    except SystemExit:
        pass


def test_propose_mode_writes_queue_and_leaves_store_untouched(dedup_facts, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_archetype()))
    before = store.read_bytes()
    queue_dir = tmp_path / "review"

    _run_main(dedup_facts, monkeypatch, ["--facts", str(store), "--queue-dir", str(queue_dir)])

    assert store.read_bytes() == before  # propose never mutates the store
    queue = json.loads((queue_dir / "dedup-candidates.json").read_text())
    assert len(queue["clusters"]) == 1
    assert len(queue["clusters"][0]["duplicate_ids"]) == 5
    assert (queue_dir / "dedup-candidates.md").exists()


# ── apply mode: mark-only supersession with link + window ────────────────────
def test_apply_marks_duplicates_with_link_and_window(dedup_facts, tmp_path, monkeypatch):
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(_archetype()))

    _run_main(dedup_facts, monkeypatch, ["--facts", str(store), "--apply"])

    facts = {f["id"]: f for f in json.loads(store.read_text())}
    cluster_ids = [f"dup-{i}" for i in range(6)]
    canonical = [fid for fid in cluster_ids if facts[fid]["status"] == "current"]
    duplicates = [fid for fid in cluster_ids if facts[fid]["status"] == "superseded"]
    assert len(canonical) == 1
    assert len(duplicates) == 5
    for fid in duplicates:
        f = facts[fid]
        assert f["superseded_by"] == canonical[0]
        assert "dedup" in f["supersession_reason"]
        assert f.get("invalid_at")  # bi-temporal window closed
        assert f.get("superseded_at")
    # Canonical and unrelated facts untouched.
    assert "invalid_at" not in facts[canonical[0]]
    assert facts["other"]["status"] == "current"


def test_apply_overwrites_future_invalid_at_on_duplicates(dedup_facts, tmp_path, monkeypatch):
    future = "2099-01-01T00:00:00+00:00"
    facts = [
        _fact("a", VARIANTS[0], confidence=0.9),
        _fact("b", VARIANTS[1], confidence=0.5, invalid_at=future),
    ]
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))

    _run_main(dedup_facts, monkeypatch, ["--facts", str(store), "--apply"])

    marked = {f["id"]: f for f in json.loads(store.read_text())}["b"]
    assert marked["status"] == "superseded"
    assert marked["invalid_at"] < future  # closed now, not at the future bound


def test_apply_preserves_attestation_signatures(dedup_facts, sign_lib, tmp_path, monkeypatch):
    """Dedup mutates only lifecycle fields (status/superseded_*/invalid_at),
    never the signed core — so a fully signed store stays fully VALID."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC
    )
    facts = _archetype()
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))

    _run_main(dedup_facts, monkeypatch, ["--facts", str(store), "--apply"])

    after = json.loads(store.read_text())
    result = sign_lib.verify_facts(after, key)
    assert result["tampered"] == 0
    assert result["parent_suspect"] == 0
    assert result["valid"] == result["total"]
