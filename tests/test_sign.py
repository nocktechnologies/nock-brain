"""Tests for signed, tamper-evident fact provenance (N8068).

Critical security tests: sign->verify roundtrip, tamper detection, unsigned
handling, missing-key grace, Merkle ancestry (parent-suspect), and the
HMAC-SHA256 fallback when cryptography is unavailable.
"""
import copy
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def load_module(name: str, mod_name: str | None = None):
    """Load a bin/ script by path (hyphenated names, no package)."""
    path = BIN / f"{name}.py"
    spec = importlib.util.spec_from_file_location(mod_name or name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sign():
    return load_module("_sign")


@pytest.fixture()
def sign_cli():
    return load_module("sign-facts")


@pytest.fixture()
def verify_cli():
    return load_module("verify-facts")


def make_fact(fid="fact-1", content="Kevin chose Ed25519 signing", kind="decision"):
    return {
        "id": fid,
        "kind": kind,
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": "2026-06-13",
        "evidence": [{"event_id": "event-1", "path": "session.jsonl", "line": 42}],
    }


# --- key management ----------------------------------------------------------
def test_key_auto_generated_with_secure_perms(sign, tmp_path):
    key_path = tmp_path / ".nock-brain" / "signing-key"
    pub_path = tmp_path / ".nock-brain" / "signing-key.pub"
    key = sign.load_or_create_key(key_path, pub_path)

    assert key_path.exists() and pub_path.exists()
    # Private key must be 0600 and dir 0700 (secure_write_text / secure_mkdir).
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert (key_path.parent.stat().st_mode & 0o777) == 0o700
    # key_id is recorded and stable on reload.
    reloaded = sign.load_or_create_key(key_path, pub_path)
    assert reloaded.key_id == key.key_id


def test_private_key_never_in_serialized_fact(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)
    blob = json.dumps(fact)
    # Whatever the secret material is, it must not appear in the fact.
    if key.alg == sign.ALG_HMAC:
        assert key._hmac_secret.hex() not in blob
    else:
        priv = key._ed_private.private_bytes(
            encoding=__import__("cryptography").hazmat.primitives.serialization.Encoding.Raw,
            format=__import__("cryptography").hazmat.primitives.serialization.PrivateFormat.Raw,
            encryption_algorithm=__import__("cryptography").hazmat.primitives.serialization.NoEncryption(),
        )
        assert priv.hex() not in blob


# --- roundtrip ---------------------------------------------------------------
def test_sign_verify_roundtrip_valid(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)

    assert "attestation" in fact
    att = fact["attestation"]
    assert att["alg"] in (sign.ALG_ED25519, sign.ALG_HMAC)
    assert att["key_id"] == key.key_id
    assert att["signature"]

    pub = sign.load_public_key(tmp_path / "k.pub")
    assert sign.verify_fact(fact, pub) == sign.VALID


def test_canonicalization_stable_across_json_roundtrip(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)
    # Serialize and reload (key order / whitespace may differ) -> still valid.
    reloaded = json.loads(json.dumps(fact, sort_keys=False, indent=4))
    pub = sign.load_public_key(tmp_path / "k.pub")
    assert sign.verify_fact(reloaded, pub) == sign.VALID


# --- tamper detection (THE critical test) ------------------------------------
def test_tamper_content_detected(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(content="Kevin approved a $0 spend"), key)
    pub = sign.load_public_key(tmp_path / "k.pub")
    assert sign.verify_fact(fact, pub) == sign.VALID

    # The F5 attack: poison the injected claim, leave the attestation intact.
    fact["content"] = "Kevin approved a $1,000,000 spend"
    assert sign.verify_fact(fact, pub) == sign.TAMPERED


def test_tamper_kind_detected(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(kind="observation"), key)
    pub = sign.load_public_key(tmp_path / "k.pub")
    fact["kind"] = "decision"  # promote an observation to a decision
    assert sign.verify_fact(fact, pub) == sign.TAMPERED


def test_tamper_source_anchor_detected(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)
    pub = sign.load_public_key(tmp_path / "k.pub")
    fact["evidence"] = [{"event_id": "forged", "path": "evil.jsonl", "line": 1}]
    assert sign.verify_fact(fact, pub) == sign.TAMPERED


def test_tamper_signature_byte_detected(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)
    pub = sign.load_public_key(tmp_path / "k.pub")
    sig = fact["attestation"]["signature"]
    # Flip the last hex nibble.
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    fact["attestation"]["signature"] = flipped
    assert sign.verify_fact(fact, pub) == sign.TAMPERED


# --- unsigned ----------------------------------------------------------------
def test_unsigned_fact_reports_unsigned_not_crash(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    pub = sign.load_public_key(tmp_path / "k.pub")
    plain = make_fact()  # no attestation
    assert sign.verify_fact(plain, pub) == sign.UNSIGNED


def test_unsigned_facts_still_load_via_facts_module(tmp_path):
    """Backward-compat: the existing load path must still accept unsigned
    facts (it knows nothing about attestations)."""
    facts_mod = load_module("_facts")
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([make_fact("a"), make_fact("b")]))
    loaded = facts_mod.load_facts(facts_file)
    assert [f["id"] for f in loaded] == ["a", "b"]


# --- missing key -------------------------------------------------------------
def test_missing_key_graceful_verify(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    fact = sign.sign_fact(make_fact(), key)
    # Verifying a signed fact with NO key cannot affirm it -> tampered/unverifiable,
    # but must not crash.
    assert sign.verify_fact(fact, None) == sign.TAMPERED
    # Unsigned fact with no key -> unsigned (no crash).
    assert sign.verify_fact(make_fact(), None) == sign.UNSIGNED


def test_load_public_key_missing_raises(sign, tmp_path):
    with pytest.raises(FileNotFoundError):
        sign.load_public_key(tmp_path / "does-not-exist.pub")


def test_load_or_create_no_create_raises(sign, tmp_path):
    with pytest.raises(FileNotFoundError):
        sign.load_or_create_key(tmp_path / "nope", tmp_path / "nope.pub", create=False)


# --- Merkle ancestry ---------------------------------------------------------
def test_merkle_parent_edit_makes_child_parent_suspect(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    pub = sign.load_public_key(tmp_path / "k.pub")

    parent = make_fact("parent-1", content="Original pricing: $29/mo")
    child = make_fact("child-1", content="Derived: terminal tier is $29/mo")
    child["parent_fact_ids"] = ["parent-1"]

    facts = [parent, child]
    sign.sign_facts(facts, key)

    # Clean store: both valid.
    report = sign.verify_facts(facts, pub)
    assert report["valid"] == 2
    assert report["tampered"] == 0
    assert report["parent_suspect"] == 0

    # Alter the parent's content -> parent TAMPERED, child PARENT_SUSPECT.
    facts[0]["content"] = "Forged pricing: $299/mo"
    report = sign.verify_facts(facts, pub)
    statuses = {s["id"]: s["status"] for s in report["statuses"]}
    assert statuses["parent-1"] == sign.TAMPERED
    assert statuses["child-1"] == sign.PARENT_SUSPECT
    assert report["parent_suspect"] == 1
    assert report["tampered"] == 1


def test_merkle_parent_revoked_makes_child_parent_suspect(sign, tmp_path):
    """A revoked (removed) parent breaks the child's ancestry commitment."""
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    pub = sign.load_public_key(tmp_path / "k.pub")

    parent = make_fact("parent-1", content="Original pricing: $29/mo")
    child = make_fact("child-1", content="Derived: terminal tier is $29/mo")
    child["parent_fact_ids"] = ["parent-1"]
    facts = [parent, child]
    sign.sign_facts(facts, key)

    # Remove the parent from the store (revocation) -> child parent-suspect.
    only_child = [copy.deepcopy(child)]
    report = sign.verify_facts(only_child, pub)
    statuses = {s["id"]: s["status"] for s in report["statuses"]}
    assert statuses["child-1"] == sign.PARENT_SUSPECT


def test_merkle_child_intact_when_parent_intact(sign, tmp_path):
    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    pub = sign.load_public_key(tmp_path / "k.pub")
    parent = make_fact("parent-1")
    child = make_fact("child-1")
    child["parent_fact_ids"] = ["parent-1"]
    facts = [parent, child]
    sign.sign_facts(facts, key)
    assert sign.verify_facts(facts, pub)["valid"] == 2


# --- HMAC fallback (cryptography unavailable) --------------------------------
def test_hmac_fallback_when_cryptography_absent(tmp_path, monkeypatch):
    """When cryptography cannot be imported, _sign must fall back to HMAC-SHA256
    and still sign + verify + detect tampering."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("simulated: cryptography unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Force a fresh import of _sign under the patched import machinery.
    monkeypatch.delitem(sys.modules, "_sign", raising=False)
    sign = load_module("_sign", mod_name="_sign")

    assert sign._HAVE_CRYPTOGRAPHY is False

    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    assert key.alg == sign.ALG_HMAC

    fact = sign.sign_fact(make_fact(), key)
    assert fact["attestation"]["alg"] == sign.ALG_HMAC

    pub = sign.load_public_key(tmp_path / "k.pub")
    assert sign.verify_fact(fact, pub) == sign.VALID

    # Tamper detection on the HMAC path too.
    fact["content"] = "poisoned under HMAC"
    assert sign.verify_fact(fact, pub) == sign.TAMPERED


def test_hmac_key_id_does_not_leak_secret(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "_sign", raising=False)
    sign = load_module("_sign", mod_name="_sign")

    key = sign.load_or_create_key(tmp_path / "k", tmp_path / "k.pub")
    assert key._hmac_secret.hex() not in key.key_id


# --- CLI smoke tests ---------------------------------------------------------
def test_sign_then_verify_cli_roundtrip(sign_cli, verify_cli, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([make_fact("a"), make_fact("b")]))
    key_path = tmp_path / "key"
    pub_path = tmp_path / "key.pub"

    rc = sign_cli.run(["--facts", str(facts_file), "--key", str(key_path), "--pub", str(pub_path)])
    assert rc == 0

    signed = json.loads(facts_file.read_text())
    assert all("attestation" in f for f in signed)

    rc = verify_cli.run(["--facts", str(facts_file), "--pub", str(pub_path)])
    assert rc == 0  # all valid


def test_verify_cli_exits_nonzero_on_tamper(sign_cli, verify_cli, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([make_fact("a")]))
    key_path = tmp_path / "key"
    pub_path = tmp_path / "key.pub"
    sign_cli.run(["--facts", str(facts_file), "--key", str(key_path), "--pub", str(pub_path)])

    # Hand-edit the signed store.
    signed = json.loads(facts_file.read_text())
    signed[0]["content"] = "tampered via CLI"
    facts_file.write_text(json.dumps(signed))

    rc = verify_cli.run(["--facts", str(facts_file), "--pub", str(pub_path)])
    assert rc == 2  # tamper -> non-zero exit


def test_verify_cli_strict_fails_on_unsigned(verify_cli, sign_cli, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([make_fact("a")]))  # unsigned
    pub_path = tmp_path / "key.pub"
    # Generate a key (no signing) so a verifying key exists.
    key_path = tmp_path / "key"
    sign_cli.run(["--facts", str(facts_file), "--key", str(key_path), "--pub", str(pub_path)])
    # Now make a fresh unsigned store and verify --strict.
    unsigned_file = tmp_path / "unsigned.json"
    unsigned_file.write_text(json.dumps([make_fact("a")]))
    rc = verify_cli.run(["--facts", str(unsigned_file), "--pub", str(pub_path), "--strict"])
    assert rc == 3
