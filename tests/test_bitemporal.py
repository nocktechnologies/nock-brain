"""Tests for bi-temporal validity (experiment B): facts carry optional
valid_at/invalid_at bounds, and recall stops surfacing a fact as *current* once
its window has closed (it stays in the store, recoverable with include_superseded).
Facts without bounds behave exactly as before — backward compatible."""
from datetime import datetime, timezone

NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
PAST = "2026-06-01T00:00:00+00:00"
FUTURE = "2026-12-01T00:00:00+00:00"


def _fact(fid, content, **extra):
    f = {
        "id": fid, "kind": "decision", "status": "current", "confidence": 0.9,
        "content": content, "source_date": "2026-06-20", "evidence": [],
    }
    f.update(extra)
    return f


# ── the pure helper ─────────────────────────────────────────────────────────
def test_currently_valid_no_bounds_is_always_valid(facts_lib):
    assert facts_lib.fact_currently_valid(_fact("a", "x"), NOW) is True


def test_currently_valid_invalid_at_past_is_closed(facts_lib):
    assert facts_lib.fact_currently_valid(_fact("a", "x", invalid_at=PAST), NOW) is False


def test_currently_valid_valid_at_future_not_yet(facts_lib):
    assert facts_lib.fact_currently_valid(_fact("a", "x", valid_at=FUTURE), NOW) is False


def test_currently_valid_open_window_contains_now(facts_lib):
    f = _fact("a", "x", valid_at=PAST, invalid_at=FUTURE)
    assert facts_lib.fact_currently_valid(f, NOW) is True


def test_currently_valid_lenient_on_garbage_bounds(facts_lib):
    # A malformed timestamp must never break recall — treat as no bound.
    assert facts_lib.fact_currently_valid(_fact("a", "x", invalid_at="not-a-date"), NOW) is True


# ── recall integration ──────────────────────────────────────────────────────
def test_expired_fact_drops_from_default_recall(budget_recall):
    facts = [
        _fact("live", "cloudflare dns migration plan"),
        _fact("dead", "cloudflare dns migration plan", invalid_at=PAST),
    ]
    ids = {f["id"] for f in budget_recall.search(facts, "cloudflare dns migration", now=NOW)}
    assert "live" in ids
    assert "dead" not in ids  # window closed → not current


def test_expired_fact_recoverable_with_include_superseded(budget_recall):
    facts = [_fact("dead", "cloudflare dns migration plan", invalid_at=PAST)]
    ids = {f["id"] for f in budget_recall.search(facts, "cloudflare dns migration",
                                                 include_superseded=True, now=NOW)}
    assert "dead" in ids  # history is recoverable, not deleted


def test_future_fact_excluded_until_valid(budget_recall):
    facts = [_fact("scheduled", "cloudflare dns migration plan", valid_at=FUTURE)]
    ids = {f["id"] for f in budget_recall.search(facts, "cloudflare dns migration", now=NOW)}
    assert "scheduled" not in ids


def test_unbounded_facts_unchanged_regression(budget_recall):
    # No window fields anywhere ⇒ identical behavior to before the feature.
    facts = [_fact("a", "cloudflare dns migration plan"),
             _fact("b", "unrelated kubernetes thing")]
    ids = {f["id"] for f in budget_recall.search(facts, "cloudflare dns migration", now=NOW)}
    assert ids == {"a"}


# ── supersede sets the window ───────────────────────────────────────────────
def test_supersede_sets_invalid_at(tmp_path, monkeypatch):
    """Superseding a fact closes its validity window (invalid_at), so recall
    stops treating it as current — without deleting it from the store."""
    import importlib.util
    import json
    import sys
    from pathlib import Path

    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    spec = importlib.util.spec_from_file_location("supersede_fact", bin_dir / "supersede-fact.py")
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)

    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("old", "we use namecheap email forwarding")]))

    monkeypatch.setattr(sys, "argv",
                        ["supersede-fact.py", "old", "--by", "new",
                         "--reason", "switched to Google", "--facts", str(store)])
    try:
        sf.main()
    except SystemExit:
        pass

    fact = json.loads(store.read_text())[0]
    assert fact["status"] == "superseded"
    assert "invalid_at" in fact and fact["invalid_at"]  # window closed
    assert fact["id"] == "old"  # still in the store, not deleted
