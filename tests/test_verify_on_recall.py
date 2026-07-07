"""Regression tests for attestation verification on the recall hot path
(OWASP F5 residual). The signed fact envelope (bin/_sign.py) existed but
budget-recall never checked it, so a poisoned facts.json was injected into
sessions undetected. These tests pin the closed loop:

- a tampered signed fact is EXCLUDED from recall and counted on stderr;
- an unsigned fact stays recallable by default (much of the store predates
  signing) but is counted, and falls to --strict-verify (fail closed);
- with no signing key on disk, verification is skipped entirely.
"""
import json
import sys

import pytest


def signable_fact(fid, content, kind="decision", source_date="2026-07-01"):
    return {
        "id": fid,
        "kind": kind,
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": source_date,
        "evidence": [{"event_id": f"ev-{fid}", "path": "session.jsonl", "line": 1}],
    }


def write_facts(tmp_path, facts):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


@pytest.fixture()
def signing_key(sign_lib, tmp_path, monkeypatch):
    """A real signing key in tmp, with budget-recall's key resolution pointed
    at it (overriding the conftest no-key isolation)."""
    key_path = tmp_path / "signing-key"
    pub_path = tmp_path / "signing-key.pub"
    key = sign_lib.load_or_create_key(key_path, pub_path)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(key_path))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(pub_path))
    return key


# --- tampered facts (the F5 attack) ------------------------------------------
def test_tampered_fact_excluded_from_recall_and_warned(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    good = signable_fact("f-good", "ed25519 rollout was approved for signing")
    bad = signable_fact("f-bad", "ed25519 rollout budget was zero dollars")
    sign_lib.sign_facts([good, bad], signing_key)
    # Poison the signed store: edit content, leave the attestation intact.
    bad["content"] = "ed25519 rollout budget was one million dollars"
    facts_file = write_facts(tmp_path, [good, bad])

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err

    assert "approved for signing" in out
    assert "million" not in out
    assert "excluded 1 tampered" in err


def test_all_valid_store_recalls_silently(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    facts = [signable_fact("f-1", "ed25519 rollout was approved for signing")]
    sign_lib.sign_facts(facts, signing_key)
    facts_file = write_facts(tmp_path, facts)

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err

    assert "approved for signing" in out
    assert err == ""  # nothing to warn about


# --- unsigned facts: allowed by default, excluded under --strict-verify ------
def test_unsigned_fact_included_by_default_but_counted(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    signed = signable_fact("f-signed", "ed25519 rollout was approved")
    sign_lib.sign_fact(signed, signing_key)
    unsigned = signable_fact("f-unsigned", "ed25519 rollout needs a runbook")
    facts_file = write_facts(tmp_path, [signed, unsigned])

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err

    assert "approved" in out
    assert "runbook" in out  # unsigned stays recallable (backward compat)
    assert "allowed 1 unsigned" in err


def test_strict_verify_excludes_unsigned(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    signed = signable_fact("f-signed", "ed25519 rollout was approved")
    sign_lib.sign_fact(signed, signing_key)
    unsigned = signable_fact("f-unsigned", "ed25519 rollout needs a runbook")
    facts_file = write_facts(tmp_path, [signed, unsigned])

    out = budget_recall.budget_recall("ed25519 rollout", facts_file,
                                      strict_verify=True)
    err = capsys.readouterr().err

    assert "approved" in out
    assert "runbook" not in out  # fail closed
    assert "excluded 1 unsigned" in err


# --- parent-suspect (Merkle ancestry break) ----------------------------------
def test_parent_suspect_kept_by_default_excluded_in_strict(
        budget_recall, sign_lib, signing_key, tmp_path, capsys):
    parent = signable_fact("f-parent", "ed25519 rollout pricing was 29 dollars")
    child = signable_fact("f-child", "ed25519 rollout note derived from pricing")
    child["parent_fact_ids"] = ["f-parent"]
    sign_lib.sign_facts([parent, child], signing_key)
    parent["content"] = "ed25519 rollout pricing was 299 dollars"  # tamper parent
    facts_file = write_facts(tmp_path, [parent, child])

    # Default: tampered parent gone; intact child stays but is counted.
    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert "299" not in out
    assert "derived from pricing" in out
    assert "excluded 1 tampered" in err
    assert "allowed 1 parent-suspect" in err

    # Strict: only VALID facts survive -> child excluded too.
    out = budget_recall.budget_recall("ed25519 rollout", facts_file,
                                      strict_verify=True)
    assert "derived from pricing" not in out


# --- no signing key: verification skipped entirely ---------------------------
def test_no_signing_key_skips_verification(budget_recall, tmp_path, capsys):
    # conftest points key resolution at nonexistent paths; even a fact with a
    # forged attestation (TAMPERED under any key) must pass through untouched.
    forged = signable_fact("f-forged", "ed25519 rollout is fine honestly")
    forged["attestation"] = {
        "fact_id": "f-forged", "canonical_fact_hash": "00", "source_hash": "00",
        "alg": "ed25519", "key_id": "bogus", "signature": "deadbeef",
        "parent_fact_ids": [], "signed_at": "2026-01-01T00:00:00+00:00",
    }
    plain = signable_fact("f-plain", "ed25519 rollout was approved")
    facts_file = write_facts(tmp_path, [forged, plain])

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err

    assert "fine honestly" in out
    assert "approved" in out
    assert err == ""  # no verification ran, no warning


def test_strict_verify_without_key_warns_and_still_recalls(
        budget_recall, tmp_path, capsys):
    plain = signable_fact("f-plain", "ed25519 rollout was approved")
    facts_file = write_facts(tmp_path, [plain])

    out = budget_recall.budget_recall("ed25519 rollout", facts_file,
                                      strict_verify=True)
    err = capsys.readouterr().err

    assert "approved" in out  # no key -> skipped, not fail-everything
    assert "no signing key" in err


# --- CLI wiring ---------------------------------------------------------------
def test_cli_strict_verify_flag(budget_recall, sign_lib, signing_key, tmp_path,
                                capsys, monkeypatch):
    signed = signable_fact("f-signed", "ed25519 rollout was approved")
    sign_lib.sign_fact(signed, signing_key)
    unsigned = signable_fact("f-unsigned", "ed25519 rollout needs a runbook")
    facts_file = write_facts(tmp_path, [signed, unsigned])

    monkeypatch.setattr(sys, "argv", [
        "budget-recall.py", "--strict-verify",
        "--facts", str(facts_file),
        "--insights", str(tmp_path / "no-insights.json"),
        "ed25519", "rollout",
    ])
    budget_recall.main()
    out = capsys.readouterr().out

    assert "approved" in out
    assert "runbook" not in out


def test_cli_strict_verify_via_env(budget_recall, sign_lib, signing_key,
                                   tmp_path, capsys, monkeypatch):
    unsigned = signable_fact("f-unsigned", "ed25519 rollout needs a runbook")
    facts_file = write_facts(tmp_path, [unsigned])

    monkeypatch.setenv("NOCKBRAIN_STRICT_VERIFY", "1")
    monkeypatch.setattr(sys, "argv", [
        "budget-recall.py",
        "--facts", str(facts_file),
        "--insights", str(tmp_path / "no-insights.json"),
        "ed25519", "rollout",
    ])
    budget_recall.main()
    out = capsys.readouterr().out

    assert "runbook" not in out
    assert "No matching facts found." in out
