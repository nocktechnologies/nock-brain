"""Contract tests for claim-authority attestation v2."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
REV_A = "sha256:" + "a" * 64
REV_B = "sha256:" + "b" * 64
BATCH = "sha256:" + "c" * 64


def load_sign():
    path = BIN / "_sign.py"
    spec = importlib.util.spec_from_file_location("claim_attestation_sign", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sign():
    return load_sign()


def make_claim_fact() -> dict:
    return {
        "id": "fact-kevin-approval",
        "memory_id": "018fba20-5f5d-7c52-a714-3fb4f36d153d",
        "revision_id": REV_A,
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
        "promotion_batch_digest": BATCH,
        "parent_revision_ids": [],
        "revokes_revision_ids": [],
        "status": "current",
    }


def make_verifier_receipt_payload(fact: dict) -> dict:
    payload = fact["attestation"]["payload"]
    return {
        "schema": "nock-claim-verifier-receipt/v1",
        "session_id": "provider-session-7",
        "turn_id": "turn-12",
        "fact_id": fact["id"],
        "memory_id": fact["memory_id"],
        "revision_id": fact["revision_id"],
        "evidence_hash": payload["evidence_hash"],
        "promotion_batch_digest": payload["promotion_batch_digest"],
        "verifier_id": "probe:webhook",
        "source_digest": "d" * 64,
        "result": "verified",
        "observed_at": "2026-08-04T15:00:00.000000Z",
    }


def test_v2_roundtrip_binds_complete_claim_payload(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)

    attestation = fact["attestation"]
    assert attestation["schema"] == "nock-claim-attestation/v2"
    assert attestation["payload"] == sign.claim_payload_v2(fact)
    assert attestation["alg"] == key.alg
    assert attestation["key_id"] == key.key_id
    assert attestation["signature"]

    public_key = sign.load_public_key(tmp_path / "key.pub")
    assert sign.verify_fact(fact, public_key) == sign.VALID


@pytest.mark.parametrize(
    ("mode", "replacement"),
    [
        ("missing", None),
        ("empty", ""),
        ("null", None),
        ("number", 7),
        ("list", []),
    ],
)
def test_malformed_v2_signature_is_tampered_not_unsigned(
    sign, tmp_path, mode, replacement
):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    if mode == "missing":
        fact["attestation"].pop("signature")
    else:
        fact["attestation"]["signature"] = replacement

    assert sign.verify_fact(fact, key) == sign.TAMPERED
    report = sign.verify_facts([fact], key)
    assert report["tampered"] == 1
    assert report["unsigned"] == 0
    assert report["statuses"] == [
        {"id": fact["id"], "status": sign.TAMPERED}
    ]


def test_v2_payload_is_canonical_and_does_not_authorize_mutable_status(sign):
    fact = make_claim_fact()
    first = sign.claim_payload_v2(fact)
    reordered = json.loads(json.dumps(fact, sort_keys=True))
    reordered["status"] = "revoked"

    assert sign.claim_payload_v2(reordered) == first
    assert sign.canonical_claim_payload_v2(reordered) == sign.canonical_claim_payload_v2(
        fact
    )


def test_contract_fixture_matches_exact_joined_bytes(sign):
    fixture_path = REPO / "tests" / "nock_memory_conformance_v1.json"
    raw = fixture_path.read_bytes().rstrip(b"\n")
    fixture = json.loads(raw)

    assert sign.canonical_contract_json(fixture) == raw
    assert sign._sha256_hex(raw) == (
        "8a76447f2d7db0af886caf6f59881398ae9ca8d767593b8dddddd1fea68d2343"
    )


@pytest.mark.parametrize("value", [-0.0, 1.0, 1e-7, float("nan"), float("inf")])
def test_contract_canonicalizer_rejects_nonportable_numbers(sign, value):
    with pytest.raises(sign.ClaimAttestationError):
        sign.canonical_contract_json({"confidence": value})


@pytest.mark.parametrize("value", [-0.0, 1.0, 1e-7, float("nan"), float("inf")])
def test_contract_canonicalizer_rejects_nested_nonportable_numbers(sign, value):
    with pytest.raises(sign.ClaimAttestationError):
        sign.canonical_contract_json(
            {"outer": [{"authority": {"confidence": value}}]}
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("memory_id", "018fba20-5f5d-7c52-a714-3fb4f36d153e"),
        ("revision_id", REV_B),
        ("id", "forged-fact-id"),
        ("kind", "directive"),
        ("category", "standing_order"),
        ("content", "Kevin approved an unlimited build."),
        ("evidence", [{"source_id": "forged"}]),
        ("scope", "public"),
        ("confidence", 0.7),
        ("source_time", "2026-08-03T15:00:00.000000Z"),
        ("valid_from", "2026-08-03T15:00:00.000000Z"),
        ("valid_to", "2026-08-05T15:00:00.000000Z"),
        ("verify_before_act", True),
        ("promotion_batch_digest", "sha256:" + "e" * 64),
        ("parent_revision_ids", [REV_B]),
        ("revokes_revision_ids", [REV_B]),
    ],
)
def test_v2_detects_tampering_in_every_authority_field(
    sign, tmp_path, field, replacement
):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    fact[field] = replacement

    assert sign.verify_fact(fact, key) == sign.TAMPERED


def test_v2_detects_tampered_declared_content_or_evidence_hash(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    for field in ("content_hash", "evidence_hash"):
        fact = make_claim_fact()
        if field == "content_hash":
            fact[field] = sign.content_hash_v2(fact)
        else:
            fact[field] = sign.evidence_hash_v2(fact)
        fact = sign.sign_claim_fact_v2(fact, key)
        fact[field] = "sha256:" + "f" * 64
        assert sign.verify_fact(fact, key) == sign.TAMPERED


def test_v2_comutation_reaches_signature_verification(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    old_signature = fact["attestation"]["signature"]
    fact["scope"] = "org"
    fact["confidence"] = 0.72
    fact["attestation"]["payload"] = sign.claim_payload_v2(fact)

    calls = []
    verify_bytes = key.verify_bytes

    def recording_verify(payload, signature):
        calls.append((payload, signature))
        return verify_bytes(payload, signature)

    key.verify_bytes = recording_verify
    assert fact["attestation"]["payload"] == sign.claim_payload_v2(fact)
    assert sign.verify_fact(fact, key) == sign.TAMPERED
    assert calls == [
        (
            sign.CLAIM_ATTESTATION_V2_DOMAIN
            + sign.canonical_claim_payload_v2(fact),
            old_signature,
        )
    ]


def test_v2_status_edit_cannot_retire_or_resurrect_authority(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)

    for status in ("revoked", "superseded", "current", "invented"):
        fact["status"] = status
        assert sign.verify_fact(fact, key) == sign.VALID


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("memory_id", None),
        ("revision_id", "not-a-revision"),
        ("id", ""),
        ("category", ""),
        ("content", ""),
        ("evidence", []),
        ("evidence", [{"score": float("nan")}]),
        ("scope", "unknown"),
        ("confidence", 1.1),
        ("source_time", "2026-08-04"),
        ("valid_from", "not-a-time"),
        ("valid_to", "2026-08-04T14:59:59.000000Z"),
        ("verify_before_act", "false"),
        ("promotion_batch_digest", "not-a-digest"),
        ("parent_revision_ids", ["not-a-revision"]),
        ("revokes_revision_ids", [REV_B, REV_B]),
    ],
)
def test_v2_signing_rejects_invalid_authority_contract(
    sign, tmp_path, field, replacement
):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_claim_fact()
    fact[field] = replacement

    with pytest.raises(sign.ClaimAttestationError):
        sign.sign_claim_fact_v2(fact, key)


def test_v2_signing_rejects_oversized_integer_confidence(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = make_claim_fact()
    fact["confidence"] = 10**400

    with pytest.raises(sign.ClaimAttestationError):
        sign.sign_claim_fact_v2(fact, key)


def test_v2_verification_rejects_oversized_integer_confidence(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    fact["confidence"] = 10**400

    assert sign.verify_fact(fact, key) == sign.TAMPERED


def test_v2_signature_is_domain_separated_from_v1(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    v1 = sign.attest_fact(fact, key)
    fact["attestation"]["signature"] = v1["signature"]

    assert sign.verify_fact(fact, key) == sign.TAMPERED


def test_signed_verifier_receipt_roundtrip_binds_every_field(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    receipt = sign.sign_verifier_receipt(
        make_verifier_receipt_payload(fact), key
    )
    public_key = sign.load_public_key(tmp_path / "key.pub")

    assert sign.verify_verifier_receipt(receipt, public_key) is True
    for field, replacement in (
        ("session_id", "provider-session-8"),
        ("turn_id", "turn-13"),
        ("revision_id", REV_B),
        ("source_digest", "e" * 64),
        ("observed_at", "2026-08-04T15:00:01.000000Z"),
    ):
        tampered = copy.deepcopy(receipt)
        tampered[field] = replacement
        assert sign.verify_verifier_receipt(tampered, public_key) is False


def test_verifier_receipt_signature_is_domain_separated(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    payload = make_verifier_receipt_payload(fact)
    receipt = sign.sign_verifier_receipt(payload, key)
    receipt["signature"] = key.sign_bytes(
        sign.CLAIM_ATTESTATION_V2_DOMAIN
        + sign.canonical_verifier_receipt_payload(payload)
    )

    assert sign.verify_verifier_receipt(receipt, key) is False


def test_legacy_v1_attestation_still_verifies(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = {
        "id": "legacy-fact",
        "kind": "decision",
        "content": "Existing signed memory remains readable.",
        "evidence": [{"event_id": "event-1"}],
    }

    sign.sign_fact(fact, key)
    assert "schema" not in fact["attestation"]
    assert sign.verify_fact(fact, key) == sign.VALID


def test_v2_authority_fields_cannot_use_a_legacy_attestation(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = {
        "id": "legacy-fact",
        "kind": "decision",
        "content": "Existing signed memory remains readable.",
        "evidence": [{"event_id": "event-1"}],
    }
    sign.sign_fact(fact, key)
    fact["verify_before_act"] = True

    assert sign.verify_fact(fact, key) == sign.TAMPERED


def test_v2_envelope_without_schema_is_tampered(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "key", tmp_path / "key.pub")
    fact = sign.sign_claim_fact_v2(make_claim_fact(), key)
    del fact["attestation"]["schema"]

    assert sign.verify_fact(fact, key) == sign.TAMPERED
