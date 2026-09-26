"""Synthetic, production-backed preview and child-isolation checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from _explorer_preview import PreviewError, run_preview  # noqa: E402
from _explorer_store import ExplorerStore  # noqa: E402
from _revoke import sign_revocation  # noqa: E402
from _sign import ALG_HMAC, SigningKey, sign_facts  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preview_uses_selected_snapshot(tmp_path, monkeypatch):
    unrelated = tmp_path / "home"
    unrelated.mkdir()
    (unrelated / ".nock-brain").mkdir()
    sentinel = unrelated / ".nock-brain" / "facts.json"
    sentinel.write_text('SECRET_UNRELATED_HOME_STORE')
    monkeypatch.setenv("HOME", str(unrelated))
    for name, value in {
        "NOCKBRAIN_AGENT_SCOPE": "unrelated", "NOCKBRAIN_STORE": "sqlite",
        "NOCKBRAIN_SIGNING_KEY": str(unrelated / "missing-private"),
        "NOCKBRAIN_SIGNING_PUB": str(unrelated / "missing-public"),
        "NOCKBRAIN_SEMANTIC": "1", "NOCKBRAIN_GRAPH_RECALL": "1",
        "NOCKBRAIN_MAX_PER_DATE": "0", "NOCKBRAIN_STRICT_VERIFY": "1",
        "PYTHONPATH": str(unrelated),
    }.items():
        monkeypatch.setenv(name, value)
    # Demo itself rejects explicit SQLite selection; hostile inherited values
    # are set after construction to test the child boundary.
    monkeypatch.setenv("NOCKBRAIN_STORE", "json")
    with ExplorerStore(demo=True) as store:
        monkeypatch.setenv("NOCKBRAIN_STORE", "sqlite")
        result = run_preview(store.snapshot_dir, "what did we decide about Friday delivery", 800)
        assert result["classifier"]["eligible"] is True
        assert result["settings"] == {
            "budget": 800, "semantic": False, "graph": False,
            "max_per_date": 4, "strict_verify": False, "agent_scope": None,
        }
        assert result["items"]
        assert "SECRET_UNRELATED_HOME_STORE" not in json.dumps(result)
        assert not any(p.name.startswith("facts.json.verified-cache") for p in store.snapshot_dir.iterdir())
    assert sentinel.read_text() == "SECRET_UNRELATED_HOME_STORE"


def test_preview_matches_production_selection_and_renderer(monkeypatch):
    for name in list(os.environ):
        if name.startswith("NOCKBRAIN_"):
            monkeypatch.delenv(name)
    recall = _load_script("budget-recall.py")
    classifier = _load_script("recall-classifier.py")
    with ExplorerStore(demo=True) as store:
        monkeypatch.setenv("NOCKBRAIN_STORE", "json")
        monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(store.snapshot_dir / "missing-private"))
        monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(store.snapshot_dir / "signing-key.pub"))
        query = "what did we decide about Friday delivery"
        selected = recall.select_recall(
            query, store.snapshot_dir / "facts.json", 800,
            insights_file=store.snapshot_dir / "insights.json",
            graph_expand=False, max_per_date=4, strict_verify=False,
            semantic=False, agent_scope=None,
        )
        rendered = recall.budget_recall(
            query, store.snapshot_dir / "facts.json", 800,
            insights_file=store.snapshot_dir / "insights.json",
            graph_expand=False, max_per_date=4, strict_verify=False,
            semantic=False, agent_scope=None,
        )
        result = run_preview(store.snapshot_dir, query, 800)
        assert [i["id"] for i in result["items"]] == [i["id"] for i in selected["included"]]
        assert result["rendered"] == rendered
        assert result["tokens_used"] == selected["tokens_used"]
        eligible, reason, categories = classifier.classify(query)
        assert result["classifier"] == {"eligible": eligible, "reason": reason, "categories": categories}


def test_classifier_skip_still_shows_matching_preview():
    with ExplorerStore(demo=True) as store:
        result = run_preview(store.snapshot_dir, "delivery", 800)
        assert result["classifier"]["eligible"] is False
        assert result["matches"] >= 1


@pytest.mark.parametrize("query,budget", [("", 800), ("x" * 2001, 800),
                                           ("delivery", True), ("delivery", 0),
                                           ("delivery", 1501), (42, 800)])
def test_preview_request_validation(query, budget):
    with pytest.raises(PreviewError):
        run_preview(Path("/no-snapshot"), query, budget)


def test_preview_refuses_corrupt_snapshot(tmp_path):
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "facts.json").write_text("not-json")
    with pytest.raises(PreviewError, match="unreadable"):
        run_preview(snap, "what did we decide", 800)
    (snap / "facts.json").write_text("[]")
    (snap / "revocations.jsonl").write_text("not-json\n")
    with pytest.raises(PreviewError, match="unreadable"):
        run_preview(snap, "what did we decide", 800)


def test_missing_and_invalid_key_notices(tmp_path):
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "facts.json").write_text("[]")
    missing = run_preview(snap, "what did we decide", 800)
    assert any("missing" in text for text in missing["notices"])
    (snap / "signing-key.pub").write_text("invalid")
    invalid = run_preview(snap, "what did we decide", 800)
    assert any("invalid" in text for text in invalid["notices"])


def test_selected_source_is_untouched_and_bad_signatures_are_excluded(tmp_path, monkeypatch):
    source = tmp_path / "selected"
    source.mkdir()
    key = SigningKey(ALG_HMAC, hmac_secret=bytes(range(32)))

    def fact(fid, content):
        return {"id": fid, "kind": "decision", "status": "current", "confidence": 0.9,
                "content": content, "source_date": "2026-09-20", "evidence": ["fictional"]}

    facts = [
        fact("good", "Friday delivery uses a bicycle."),
        fact("bad", "Friday delivery uses a hovercraft."),
        fact("revoked", "Friday delivery uses a van."),
    ]
    sign_facts(facts, key)
    facts[1]["content"] = "Friday delivery PRIVATE_TAMPERED_CONTENT"
    facts.append(fact("unsigned", "Friday delivery includes a choice of time windows."))
    (source / "facts.json").write_text(json.dumps(facts))
    (source / "signing-key.pub").write_text(json.dumps({
        "alg": ALG_HMAC, "key_id": key.key_id, "secret": key._hmac_secret.hex(),
    }))
    revocation = sign_revocation(key, superseded_id="revoked", superseding_id="good",
                                 reason="fictional correction")
    (source / "revocations.jsonl").write_text(json.dumps(revocation) + "\n")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    with ExplorerStore(store=source) as store:
        monkeypatch.setenv("TMPDIR", str(source))
        result = run_preview(store.snapshot_dir, "Friday delivery", 800)
        ids = {item["id"] for item in result["items"]}
        assert "good" in ids and "unsigned" in ids
        assert "bad" not in ids and "revoked" not in ids
        assert any("tampered" in notice for notice in result["notices"])
        assert any("revoked" in notice for notice in result["notices"])
        assert any("unsigned" in notice for notice in result["notices"])
        assert "PRIVATE_TAMPERED_CONTENT" not in json.dumps(result)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
    assert after == before


def test_timeout_is_safe(monkeypatch, tmp_path):
    import _explorer_preview

    def timed_out(*_args, **_kwargs):
        raise __import__("subprocess").TimeoutExpired(cmd="worker", timeout=0.01,
                                                       stderr=b"PRIVATE_TOKEN")

    monkeypatch.setattr(_explorer_preview.subprocess, "run", timed_out)
    with pytest.raises(PreviewError, match="timed out") as exc:
        run_preview(tmp_path / "missing", "what did we decide", 800)
    assert "PRIVATE_TOKEN" not in str(exc.value)
