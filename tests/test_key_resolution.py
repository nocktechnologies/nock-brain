"""Shared signing-key resolution (N10013 / N10021).

sign-facts, verify-facts, rebuild-store, and recall must resolve the same
key: explicit CLI > NOCKBRAIN_SIGNING_KEY/PUB > store-dir / ~/.nock-brain
defaults. A store signed under key A with recall pointed at key B must be
loud on health, not silently empty.
"""
import json
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_") + "_kr", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sign_cli():
    return _load("sign-facts")


@pytest.fixture(scope="module")
def verify_cli():
    return _load("verify-facts")


@pytest.fixture(scope="module")
def rebuild_store():
    return _load("rebuild-store")


def _fact(fid="f-1", content="ed25519 rollout was approved for signing"):
    return {
        "id": fid,
        "kind": "decision",
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": "2026-07-01",
        "evidence": [{"event_id": f"ev-{fid}", "path": "session.jsonl", "line": 1}],
    }


def test_resolve_key_paths_env_beats_store_dir(sign_lib, tmp_path, monkeypatch):
    env_key = tmp_path / "protected" / "signing-key"
    env_pub = tmp_path / "protected" / "signing-key.pub"
    store = tmp_path / "brain"
    store.mkdir()
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(env_key))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(env_pub))
    key, pub = sign_lib.resolve_key_paths(store_dir=store)
    assert key == env_key
    assert pub == env_pub


def test_resolve_key_paths_explicit_beats_env(sign_lib, tmp_path, monkeypatch):
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "env-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "env-key.pub"))
    explicit_key = tmp_path / "cli-key"
    explicit_pub = tmp_path / "cli-key.pub"
    key, pub = sign_lib.resolve_key_paths(explicit_key, explicit_pub)
    assert key == explicit_key
    assert pub == explicit_pub


def test_sign_facts_without_flags_uses_env_key(
        sign_cli, verify_cli, sign_lib, budget_recall, tmp_path, monkeypatch):
    """The original split-brain: env set, sign-facts ignores it and mints the
    default key. After the shared resolver, sign-facts with no --key uses env
    and recall with the same env accepts the store."""
    key_path = tmp_path / "protected-key"
    pub_path = tmp_path / "protected-key.pub"
    key = sign_lib.load_or_create_key(key_path, pub_path)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(key_path))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(pub_path))

    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([_fact()]), encoding="utf-8")
    rc = sign_cli.run(["--facts", str(facts_file)])  # no --key / --pub
    assert rc == 0
    signed = json.loads(facts_file.read_text(encoding="utf-8"))
    assert signed[0]["attestation"]["key_id"] == key.key_id

    rc = verify_cli.run(["--facts", str(facts_file)])  # no --pub
    assert rc == 0

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    assert "approved for signing" in out


def test_health_warns_when_recall_key_differs_from_store(
        nockbrain_health, sign_lib, budget_recall, tmp_path, monkeypatch, capsys):
    a = tmp_path / "key-a"
    b = tmp_path / "key-b"
    a.mkdir()
    b.mkdir()
    key_a = sign_lib.load_or_create_key(a / "signing-key", a / "signing-key.pub")
    key_b = sign_lib.load_or_create_key(b / "signing-key", b / "signing-key.pub")
    assert key_a.key_id != key_b.key_id

    fact = _fact()
    sign_lib.sign_fact(fact, key_a)
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact]), encoding="utf-8")

    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(b / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(b / "signing-key.pub"))

    report = nockbrain_health.build_report(facts_path=facts_file)
    assert report["signing_key"]["mismatch"] is True
    assert report["signing_key"]["verify_key_id"] == key_b.key_id
    assert key_a.key_id in report["signing_key"]["store_key_ids"]
    text = nockbrain_health.render_text(report)
    assert "SIGNING KEY MISMATCH" in text

    out = budget_recall.budget_recall("ed25519 rollout", facts_file)
    err = capsys.readouterr().err
    assert "approved for signing" not in out
    assert "tampered" in err


def test_resolve_verify_key_pub_only_env_skips_default_private(
        sign_lib, tmp_path, monkeypatch):
    """recall-eval sets SIGNING_PUB and pops SIGNING_KEY. A live default
    private key must not win — that was 196/196 TAMPERED on the fixture."""
    default_dir = tmp_path / "default-brain"
    default_dir.mkdir()
    other = sign_lib.load_or_create_key(
        default_dir / "signing-key", default_dir / "signing-key.pub")
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    fixture = sign_lib.load_or_create_key(
        fixture_dir / "signing-key", fixture_dir / "signing-key.pub")
    assert other.key_id != fixture.key_id

    monkeypatch.setattr(sign_lib, "DEFAULT_KEY_PATH", default_dir / "signing-key")
    monkeypatch.setattr(sign_lib, "DEFAULT_PUB_PATH", default_dir / "signing-key.pub")
    monkeypatch.delenv("NOCKBRAIN_SIGNING_KEY", raising=False)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(fixture_dir / "signing-key.pub"))

    key, err = sign_lib.resolve_verify_key()
    assert err is None
    assert key is not None
    assert key.key_id == fixture.key_id


def test_rebuild_resolve_uses_env_not_store_dir_default(
        rebuild_store, sign_lib, tmp_path, monkeypatch):
    store = tmp_path / "live"
    store.mkdir()
    env_key = tmp_path / "env-signing-key"
    env_pub = tmp_path / "env-signing-key.pub"
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(env_key))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(env_pub))
    key, pub = sign_lib.resolve_key_paths(store_dir=store)
    assert key == env_key
    assert pub == env_pub
    # rebuild() itself uses the same helper; no transcripts so it aborts
    # before signing, but the resolved paths are what sign_and_export would get.
    with pytest.raises(rebuild_store.RebuildError):
        rebuild_store.rebuild(
            store_dir=store,
            source_roots=[tmp_path / "nonexistent"],
            since_days=7,
        )
