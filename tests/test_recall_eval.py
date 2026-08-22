"""The committed recall eval must run hermetically against the signed fixture
and reproduce the documented instrument shape. This is the local mirror of the
CI regression gate (bin/recall-eval.py --gate)."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVAL = REPO / "bin" / "recall-eval.py"
FIXTURE = REPO / "tests" / "fixtures" / "recall-eval-store.json"
GOLD = REPO / "docs" / "evals" / "recall-gold-v1.json"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(EVAL), *args],
        capture_output=True, text=True, cwd=str(REPO),
    )


def test_fixture_and_gold_committed():
    assert FIXTURE.exists(), "signed fixture store must be committed"
    facts = json.loads(FIXTURE.read_text())
    assert len(facts) >= 100
    assert all("attestation" in f for f in facts), "every fixture fact is signed"
    gold = json.loads(GOLD.read_text())
    assert len(gold["queries"]) == 36
    gold_ids = set(gold["queries"])
    fixture_ids = {f["id"] for f in facts}
    assert gold_ids <= fixture_ids, "fixture must contain every gold fact"


def test_gate_passes_on_committed_fixture():
    r = _run("--gate", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    res = out["result"]
    # attestation: the v2-authority signing rule must hold for the whole fixture
    assert res["fixture_verified"]["valid"] == res["fixture_verified"]["total"]
    # both metrics are emitted
    assert res["recall"] >= 0.90
    assert 0.0 <= res["companionship"] <= 1.0
    assert res["companionship_measured_on"] > 0


def test_self_test_reproduces_cap_lever():
    """Instrument validity: cap=2 must sit materially below cap>=4 (~8pt)."""
    r = _run("--self-test", "--json")
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)["self_test"]
    assert st["pass"] is True
    assert st["cap2_recall"] < st["cap4_recall"]
    assert st["cap_lever_pts"] >= 0.05
