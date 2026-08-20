"""Regression tests for the v2 claim-authority signing fix (N9851).

Root cause: ``_sign.sign_facts`` legacy-signed every fact, so facts carrying v2
claim authority verified ``TAMPERED`` and dropped out of recall. The fix routes
``sign_facts`` per fact, and ``bin/resign-v2-authority-facts.py`` repairs a store
already written with the bad legacy signatures.

These tests use synthetic facts and a per-test key on ``tmp_path`` — they never
touch the live store or the live signing key.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def _load(module_name: str, file_name: str):
    path = BIN / file_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sign():
    return _load("n9851_sign", "_sign.py")


@pytest.fixture()
def resign():
    return _load("n9851_resign", "resign-v2-authority-facts.py")


def make_v2_claim_fact() -> dict:
    """A well-formed fact carrying the full v2 claim-authority contract."""
    return {
        "id": "fact-kevin-approval",
        "memory_id": "018fba20-5f5d-7c52-a714-3fb4f36d153d",
        "revision_id": "sha256:" + "a" * 64,
        "kind": "decision",
        "category": "kevin_decision",
        "content": "Kevin approved the bounded memory build.",
        "evidence": [
            {
                "source_type": "owner_inbound",
                "source_id": "44123",
                "digest": "d" * 64,
                "digest_kind": "attested",
                "source_created_at": "2026-08-04T15:00:00.000000Z",
                "scope": "private",
            }
        ],
        "scope": "private",
        "confidence": 0.8,
        "source_time": "2026-08-04T15:00:00.000000Z",
        "valid_from": "2026-08-04T15:00:00.000000Z",
        "valid_to": None,
        "verify_before_act": False,
        "promotion_batch_digest": "sha256:" + "c" * 64,
        "parent_revision_ids": [],
        "revokes_revision_ids": [],
        "status": "current",
    }


def make_plain_fact() -> dict:
    """A plain, non-v2 fact — legacy signing is correct for it."""
    return {
        "id": "legacy-fact-1",
        "kind": "decision",
        "content": "Existing signed memory remains readable.",
        "evidence": [{"event_id": "event-1"}],
    }


# (a) A v2-authority fact through sign_facts() verifies VALID.
#     This FAILS on main (sign_facts legacy-signs it -> TAMPERED) and passes
#     once sign_facts routes v2 facts to sign_claim_fact_v2.
def test_sign_facts_routes_v2_authority_fact_to_valid(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_v2_claim_fact()

    sign.sign_facts([fact], key)

    assert fact["attestation"]["schema"] == sign.CLAIM_ATTESTATION_V2_SCHEMA
    assert sign.verify_fact(fact, key) == sign.VALID


# (a2) A malformed v2-authority fact aborts sign_facts fail-closed. It carries
#      v2 authority (so it routes to sign_claim_fact_v2) but is structurally
#      invalid for claim_payload_v2, so the bulk signer raises rather than
#      minting a fact that would verify TAMPERED.
def test_sign_facts_raises_on_malformed_v2_authority_fact(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_v2_claim_fact()
    # Non-canonical source_time keeps the v2 authority routing but breaks
    # claim_payload_v2.
    fact["source_time"] = "2026-08-04"

    with pytest.raises(sign.ClaimAttestationError):
        sign.sign_facts([fact], key)


# (b) A legacy-signed v2 fact, fed to the corrective script's core function,
#     comes out VALID with the v2 schema.
def test_corrective_resign_repairs_legacy_signed_v2_fact(sign, resign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_v2_claim_fact()

    # Reproduce the bug: legacy-sign a v2-authority fact.
    sign.sign_fact(fact, key)
    assert "schema" not in fact["attestation"]
    assert sign.verify_fact(fact, key) == sign.TAMPERED
    # Snapshot the complete fact before repair; only the attestation may change.
    before = copy.deepcopy(fact)

    summary = resign.resign_wrongly_signed_facts([fact], key)

    assert summary["resigned"] == 1
    assert summary["already_v2"] == 0
    assert summary["cannot_resign"] == 0
    assert summary["records"][0]["before"] == sign.TAMPERED
    assert summary["records"][0]["after"] == sign.VALID
    # Independently re-verify with the sign module.
    assert fact["attestation"]["schema"] == sign.CLAIM_ATTESTATION_V2_SCHEMA
    assert sign.verify_fact(fact, key) == sign.VALID
    # The repair touches ONLY the attestation — every other field of the fact
    # (content, evidence, revision ids, authority fields) is byte-for-byte the
    # same as before the corrective re-sign.
    assert fact["attestation"] != before["attestation"]
    after_core = {k: v for k, v in fact.items() if k != "attestation"}
    before_core = {k: v for k, v in before.items() if k != "attestation"}
    assert after_core == before_core


# (c) A plain, non-v2 fact still round-trips VALID through sign_facts() with the
#     legacy envelope (unchanged behavior).
def test_sign_facts_leaves_plain_fact_on_legacy_scheme(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_plain_fact()

    sign.sign_facts([fact], key)

    assert "schema" not in fact["attestation"]
    assert sign.verify_fact(fact, key) == sign.VALID


# A v2 fact with a malformed authority contract cannot be re-signed: it lands in
# the cannot-resign bucket (with a reason) and is left EXACTLY as-is, never
# half-written. This bucket is the load-bearing output of the supervised apply.
def test_corrective_resign_reports_malformed_fact_and_leaves_it_untouched(
    sign, resign, tmp_path
):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_v2_claim_fact()
    # A non-canonical source_time is a valid v2 authority field (so the fact still
    # routes as a v2 claim) but breaks claim_payload_v2 -> ClaimAttestationError.
    fact["source_time"] = "2026-08-04"
    sign.sign_fact(fact, key)
    legacy_attestation = dict(fact["attestation"])

    summary = resign.resign_wrongly_signed_facts([fact], key)

    assert summary["cannot_resign"] == 1
    assert summary["resigned"] == 0
    assert summary["already_v2"] == 0
    record = summary["records"][0]
    assert record["action"] == "cannot-resign"
    assert record["reason"]
    # The fact is left exactly as it was — still the legacy envelope, not a
    # half-written v2 attestation.
    assert fact["attestation"] == legacy_attestation
    assert "schema" not in fact["attestation"]


# Safety: the corrective run is idempotent — a second pass changes nothing.
def test_corrective_resign_is_idempotent(sign, resign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    legacy_v2 = make_v2_claim_fact()
    sign.sign_fact(legacy_v2, key)
    plain = make_plain_fact()
    sign.sign_fact(plain, key)

    facts = [legacy_v2, plain]
    first = resign.resign_wrongly_signed_facts(facts, key)
    assert first["resigned"] == 1
    assert first["legacy_untouched"] == 1

    second = resign.resign_wrongly_signed_facts(facts, key)
    assert second["resigned"] == 0
    assert second["already_v2"] == 1
    assert second["legacy_untouched"] == 1
    assert sign.verify_fact(legacy_v2, key) == sign.VALID
    assert sign.verify_fact(plain, key) == sign.VALID


# Load-only: a corrective re-sign against an absent signing key must abort — it
# must NEVER mint a fresh key and sign the store with it. The CLI exits non-zero
# and writes no key/pub file.
def test_resign_cli_refuses_to_mint_a_missing_signing_key(resign, tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text("[]", encoding="utf-8")
    key = tmp_path / "absent-key"
    pub = tmp_path / "absent-key.pub"

    rc = resign.run(
        ["--facts", str(facts), "--key", str(key), "--pub", str(pub), "--apply"]
    )

    assert rc == 1
    assert not key.exists()
    assert not pub.exists()
