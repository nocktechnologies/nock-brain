"""Tests for gbrain-style fleet scoping (steal #3a).

A `source` field marks the owning agent; recall can be scoped to a set of
sources. The default (no scope) must be byte-for-byte the prior behavior, so
every existing caller is unaffected. Backfill is idempotent.
"""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 6, 20)


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), REPO / "bin" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fact(content, source=None, confidence=0.9, status="current",
         kind="decision", source_date="2026-06-10"):
    f = {"content": content, "confidence": confidence, "status": status,
         "kind": kind, "source_date": source_date}
    if source is not None:
        f["source"] = source
    return f


# ---- fact_source ---------------------------------------------------------

def test_fact_source_defaults_to_mira():
    facts = _load("_facts")
    assert facts.fact_source({"content": "x"}) == "mira"
    assert facts.fact_source({"content": "x", "source": ""}) == "mira"
    assert facts.fact_source({"content": "x", "source": "   "}) == "mira"
    assert facts.fact_source({"content": "x", "source": None}) == "mira"
    assert facts.fact_source({"content": "x", "source": "mar"}) == "mar"
    assert facts.fact_source({"content": "x", "source": "  mar  "}) == "mar"


def test_search_accepts_a_bare_string_source(budget_recall):
    # A caller slip — sources="mira" — must not shatter into characters.
    facts = [fact("pricing in mira", source="mira"),
             fact("pricing in mar", source="mar")]
    results = budget_recall.search(facts, "pricing", sources="mira")
    assert len(results) == 1
    assert results[0]["source"] == "mira"


# ---- scoped search -------------------------------------------------------

def test_search_without_sources_is_unchanged(budget_recall):
    facts = [fact("pricing locked at 49", source="mira"),
             fact("pricing tiers reviewed", source="mar")]
    # No scope => both sources' matches returned (prior behavior).
    results = budget_recall.search(facts, "pricing")
    assert len(results) == 2


def test_search_filters_to_one_source(budget_recall):
    facts = [fact("pricing locked at 49", source="mira"),
             fact("pricing tiers reviewed", source="mar")]
    results = budget_recall.search(facts, "pricing", sources={"mar"})
    assert len(results) == 1
    assert results[0]["source"] == "mar"


def test_search_scope_includes_shared_plus_own(budget_recall):
    facts = [fact("pricing in mira brain", source="mira"),
             fact("pricing in shared brain", source="shared"),
             fact("pricing in mar brain", source="mar")]
    results = budget_recall.search(facts, "pricing", sources={"mira", "shared"})
    got = {r["source"] for r in results}
    assert got == {"mira", "shared"}


def test_search_scope_treats_missing_source_as_default(budget_recall):
    # A pre-source fact (no source field) must scope as "mira".
    facts = [fact("pricing legacy fact")]  # no source
    assert len(budget_recall.search(facts, "pricing", sources={"mira"})) == 1
    assert len(budget_recall.search(facts, "pricing", sources={"mar"})) == 0


# ---- backfill ------------------------------------------------------------

def test_backfill_stamps_only_missing():
    bf = _load("backfill-source")
    facts = [fact("a"), fact("b", source="mar"), fact("c", source="   ")]
    stamped = bf.backfill(facts, "mira")
    assert stamped == 2
    assert facts[0]["source"] == "mira"
    assert facts[1]["source"] == "mar"   # preserved
    assert facts[2]["source"] == "mira"  # whitespace-only treated as missing


def test_backfill_is_idempotent():
    bf = _load("backfill-source")
    facts = [fact("a"), fact("b")]
    assert bf.backfill(facts, "mira") == 2
    assert bf.backfill(facts, "mira") == 0   # second run is a no-op


def test_backfill_rejects_blank_source(tmp_path):
    # A whitespace --source would write a non-stamping value (breaks idempotency).
    import sys
    import pytest
    bf = _load("backfill-source")
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([fact("a")]), encoding="utf-8")
    argv = sys.argv
    sys.argv = ["backfill-source.py", "--facts", str(store), "--source", "   "]
    try:
        with pytest.raises(SystemExit) as exc:
            bf.main()
        assert exc.value.code == 2
    finally:
        sys.argv = argv
    after = json.loads(store.read_text(encoding="utf-8"))
    assert "source" not in after[0]   # store untouched


def test_backfill_dry_run_does_not_write(tmp_path):
    bf = _load("backfill-source")
    store = tmp_path / "facts.json"
    original = [fact("a"), fact("b", source="mar")]
    store.write_text(json.dumps(original), encoding="utf-8")
    import sys
    argv = sys.argv
    sys.argv = ["backfill-source.py", "--facts", str(store), "--dry-run"]
    try:
        bf.main()
    finally:
        sys.argv = argv
    after = json.loads(store.read_text(encoding="utf-8"))
    assert "source" not in after[0]   # untouched on dry-run
