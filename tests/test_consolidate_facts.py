"""Tests for the general near-duplicate consolidation tool — cross-date dupes
of durable facts collapse to the highest-confidence canonical via status-only
supersession (superseded_by pointer, content never rewritten)."""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def fact(content, kind="config", source_date="2026-05-01", status="current",
         fid=None, confidence=0.9):
    return {
        "id": fid or f"f{abs(hash((content, source_date, kind))) % 100_000}",
        "kind": kind,
        "status": status,
        "confidence": confidence,
        "content": content,
        "source_date": source_date,
        "evidence": [{"event_id": f"ev-{source_date}"}],
    }


def run_cli(args, cwd=REPO):
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "consolidate-facts.py"), *args],
        cwd=cwd, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


# --- normalization -----------------------------------------------------------

def test_normalize_strips_reference_tokens_but_keeps_numbers(consolidate_facts):
    toks = consolidate_facts.normalize_tokens("Merged PR #123 tracking NOCK-42 and n8382")
    assert "123" not in toks and "42" not in toks and "n8382" not in toks
    # Bare numbers are meaningful: Postgres 14 and Postgres 15 are different claims.
    v14 = consolidate_facts.normalize_tokens("we use Postgres 14")
    v15 = consolidate_facts.normalize_tokens("we use Postgres 15")
    assert "14" in v14 and "15" in v15
    assert v14 != v15


def test_version_bearing_claims_do_not_merge(consolidate_facts):
    rows = [
        fact("we use postgres 14 as the primary datastore", source_date="2026-04-01"),
        fact("we use postgres 15 as the primary datastore", source_date="2026-06-01"),
    ]
    sel = consolidate_facts.select(rows)
    # 8 tokens each, differing in one -> jaccard 7/9 = 0.78 < 0.8: distinct claims.
    assert sel["candidates"] == []


# --- selection ---------------------------------------------------------------

def test_cross_date_near_dups_keep_highest_confidence_canonical(consolidate_facts):
    rows = [
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-10", confidence=0.7, fid="a"),
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-05-02", confidence=0.9, fid="b"),
        fact("we use postgres 14 as the primary datastore now",
             source_date="2026-06-20", confidence=0.8, fid="c"),
    ]
    sel = consolidate_facts.select(rows)
    assert len(sel["clusters"]) == 1
    cluster = sel["clusters"][0]
    assert cluster["canonical"]["id"] == "b"  # highest confidence wins
    assert {f["id"] for f in cluster["supersede"]} == {"a", "c"}
    assert all(c["superseded_by"] == "b" for c in sel["candidates"])


def test_canonical_confidence_tie_breaks_to_most_recent(consolidate_facts):
    rows = [
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-10", fid="old"),
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-06-20", fid="new"),
    ]
    sel = consolidate_facts.select(rows)
    assert sel["clusters"][0]["canonical"]["id"] == "new"


def test_same_date_clusters_skipped_by_default(consolidate_facts):
    rows = [
        fact("We use Postgres 14 as the primary datastore", fid="a"),
        fact("We use Postgres 14 as the primary datastore", fid="b"),
    ]
    # Same-run dupes are extract-time dedup's job; this tool targets cross-date.
    assert consolidate_facts.select(rows)["candidates"] == []
    sel = consolidate_facts.select(rows, include_same_date=True)
    assert len(sel["candidates"]) == 1


def test_correction_is_never_consolidated_even_when_requested(consolidate_facts):
    rows = [
        fact("Kevin corrected the pricing tier for the command plan",
             kind="correction", source_date="2026-04-01"),
        fact("Kevin corrected the pricing tier for the command plan",
             kind="correction", source_date="2026-06-01"),
    ]
    assert consolidate_facts.select(rows)["candidates"] == []
    assert consolidate_facts.select(rows, kinds={"correction"})["candidates"] == []


def test_superseded_noise_kinds_and_short_facts_are_ignored(consolidate_facts):
    rows = [
        # Already superseded: out of scope.
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-01", status="superseded"),
        fact("We use Postgres 14 as the primary datastore", source_date="2026-05-01"),
        # Operational-noise kind: not a durable kind, never clustered.
        fact("pipeline heartbeat completed without new work items",
             kind="status", source_date="2026-03-01"),
        fact("pipeline heartbeat completed without new work items",
             kind="status", source_date="2026-05-01"),
        # Too few normalized tokens to call near-identical.
        fact("Postgres 14", source_date="2026-03-01"),
        fact("Postgres 14", source_date="2026-05-01"),
    ]
    assert consolidate_facts.select(rows)["candidates"] == []


def test_dissimilar_same_kind_facts_do_not_cluster(consolidate_facts):
    rows = [
        fact("we use postgres 14 as the primary datastore", source_date="2026-03-01"),
        fact("deploys run on debian 12 with systemd timers", source_date="2026-05-01"),
    ]
    assert consolidate_facts.select(rows)["candidates"] == []


# --- apply: status-only supersession ------------------------------------------

def test_apply_flips_status_only_and_preserves_signed_core(consolidate_facts, sign_lib, tmp_path):
    key = sign_lib.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    rows = sign_lib.sign_facts([
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-10", confidence=0.7, fid="a"),
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-05-02", confidence=0.9, fid="b"),
    ], key)
    sel = consolidate_facts.select(rows)
    n = consolidate_facts.apply_supersessions(rows, sel["clusters"],
                                              now="2026-07-06T00:00:00+00:00")
    assert n == 1
    loser = next(f for f in rows if f["id"] == "a")
    keeper = next(f for f in rows if f["id"] == "b")
    assert loser["status"] == "superseded"
    assert loser["superseded_by"] == "b"
    assert loser["superseded_at"] == "2026-07-06T00:00:00+00:00"
    assert "near-duplicate" in loser["supersession_reason"]
    assert keeper["status"] == "current"
    # The constraint that makes this tool safe post-PR#33: signatures commit to
    # id+kind+content, so the status-only flip leaves BOTH facts verifying VALID.
    pub = sign_lib.load_public_key(tmp_path / "k.pub")
    assert sign_lib.verify_fact(loser, pub) == sign_lib.VALID
    assert sign_lib.verify_fact(keeper, pub) == sign_lib.VALID


def test_consolidated_losers_drop_from_recall_canonical_stays(consolidate_facts, budget_recall):
    rows = [
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-10", confidence=0.7, fid="a"),
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-05-02", confidence=0.9, fid="b"),
    ]
    sel = consolidate_facts.select(rows)
    consolidate_facts.apply_supersessions(rows, sel["clusters"])
    hits = budget_recall.search(rows, "postgres primary datastore")
    assert [f["id"] for f in hits] == ["b"]
    # The loser stays queryable historically via include_superseded.
    all_hits = budget_recall.search(rows, "postgres primary datastore",
                                    include_superseded=True)
    assert {f["id"] for f in all_hits} == {"a", "b"}


# --- CLI: dry-run default, gated execute --------------------------------------

def store(tmp_path):
    rows = [
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-03-10", confidence=0.7, fid="a"),
        fact("We use Postgres 14 as the primary datastore",
             source_date="2026-05-02", confidence=0.9, fid="b"),
        fact("Kevin corrected the pricing tier for the command plan",
             kind="correction", source_date="2026-04-01", fid="keepme"),
    ]
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(rows, indent=2))
    return facts


def test_cli_dry_run_writes_manifest_and_mutates_nothing(consolidate_facts, tmp_path):
    facts = store(tmp_path)
    before = facts.read_text()
    proc = run_cli(["--facts", str(facts)])
    assert proc.returncode == 0
    assert "DRY-RUN" in proc.stdout
    assert facts.read_text() == before  # byte-identical store
    manifest = json.loads((tmp_path / consolidate_facts.MANIFEST_NAME).read_text())
    assert manifest["candidate_ids"] == ["a"]
    assert manifest["clusters"][0]["canonical"]["id"] == "b"
    assert manifest["params"]["similarity"] == consolidate_facts.DEFAULT_SIMILARITY


def test_cli_execute_refused_without_review_ack(tmp_path):
    facts = store(tmp_path)
    before = facts.read_text()
    proc = run_cli(["--facts", str(facts), "--execute"])
    assert proc.returncode == 2
    assert "REFUSING" in proc.stderr
    assert facts.read_text() == before


def test_cli_gated_execute_applies_backs_up_and_prints_signing_rule(tmp_path):
    facts = store(tmp_path)
    original = {f["id"]: f for f in json.loads(facts.read_text())}
    assert run_cli(["--facts", str(facts)]).returncode == 0  # dry-run + review
    proc = run_cli(["--facts", str(facts), "--execute",
                    "--i-have-reviewed-the-manifest"])
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(facts.read_text())
    by_id = {f["id"]: f for f in rows}
    assert by_id["a"]["status"] == "superseded"
    assert by_id["a"]["superseded_by"] == "b"
    assert by_id["b"]["status"] == "current"
    assert by_id["keepme"]["status"] == "current"
    # Content/kind/id/evidence never rewritten — attestations stay verifiable.
    for fid, orig in original.items():
        for field in ("id", "kind", "content", "evidence", "source_date"):
            assert by_id[fid][field] == orig[field]
    assert list(tmp_path.glob("facts.json.bak-*"))  # backup taken first
    # Established ops rule is surfaced in the execute summary.
    assert "sign-facts.py" in proc.stdout and "verify-facts.py" in proc.stdout


def test_cli_execute_refused_without_prior_dry_run_manifest(tmp_path):
    # The review attestation must bind to an actual reviewed artifact: with no
    # dry-run manifest on disk, --execute refuses even with the ack flag.
    facts = store(tmp_path)
    before = facts.read_text()
    proc = run_cli(["--facts", str(facts), "--execute",
                    "--i-have-reviewed-the-manifest"])
    assert proc.returncode == 2
    assert "no reviewable manifest" in proc.stderr
    assert facts.read_text() == before


def test_cli_execute_refused_when_store_drifted_after_review(consolidate_facts, tmp_path):
    # TOCTOU guard: if the store changes between the reviewed dry-run and
    # --execute, the live selection differs from the manifest and execute
    # refuses — and does NOT silently refresh the manifest (a refresh would
    # let an immediate re-run pass without a new review).
    facts = store(tmp_path)
    assert run_cli(["--facts", str(facts)]).returncode == 0
    manifest_path = tmp_path / consolidate_facts.MANIFEST_NAME
    reviewed = manifest_path.read_text()

    rows = json.loads(facts.read_text())
    rows.append(fact("We use Postgres 14 as the primary datastore",
                     source_date="2026-06-30", confidence=0.8, fid="drift"))
    facts.write_text(json.dumps(rows, indent=2))
    before = facts.read_text()

    proc = run_cli(["--facts", str(facts), "--execute",
                    "--i-have-reviewed-the-manifest"])
    assert proc.returncode == 2
    assert "no longer matches" in proc.stderr
    assert facts.read_text() == before
    assert manifest_path.read_text() == reviewed  # manifest untouched by execute


def test_cli_execute_noop_when_nothing_to_consolidate(tmp_path):
    # Clean store: dry-run writes an empty manifest; execute matches it and
    # exits 0 without taking a backup or rewriting anything.
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([fact("a unique architecture decision about "
                                      "the primary datastore engine")]))
    before = facts.read_text()
    assert run_cli(["--facts", str(facts)]).returncode == 0
    proc = run_cli(["--facts", str(facts), "--execute",
                    "--i-have-reviewed-the-manifest"])
    assert proc.returncode == 0
    assert "nothing to consolidate" in proc.stdout
    assert facts.read_text() == before
    assert not list(tmp_path.glob("facts.json.bak-*"))


def test_cli_help_documents_post_execute_signing_rule():
    proc = run_cli(["--help"])
    assert proc.returncode == 0
    assert "sign-facts.py" in proc.stdout
    assert "verify-facts.py" in proc.stdout
    assert "dry-run" in proc.stdout.lower()
