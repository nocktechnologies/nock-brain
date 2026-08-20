"""The external applier for memory-promotion batches: additive-only contract,
chain checking, idempotence via the applied-batches state file."""
import json

import pytest


def _batch(seq, facts, parent=None, status="built"):
    return {
        "batch_seq": seq,
        "batch_digest": f"digest-{seq}",
        "parent_batch_digest": parent,
        "status": status,
        "payload": {"facts": facts},
    }


def test_apply_batch_is_additive_only(apply_promotion_batch):
    store = [{"id": "aaa", "content": "existing"}]
    out = apply_promotion_batch.apply_batch(
        store, _batch(1, [{"id": "bbb", "content": "new"}]), "mac-kevin"
    )
    assert [f["id"] for f in out] == ["aaa", "bbb"]
    assert out[1]["machine"] == "mac-kevin"
    assert out[1]["applied_at"]
    assert store == [{"id": "aaa", "content": "existing"}]  # input untouched


def test_apply_batch_refuses_id_collision(apply_promotion_batch):
    store = [{"id": "aaa", "content": "existing"}]
    with pytest.raises(apply_promotion_batch.ApplyError, match="additive-only"):
        apply_promotion_batch.apply_batch(
            store, _batch(1, [{"id": "aaa", "content": "overwrite attempt"}]), "mac-kevin"
        )


def test_check_chain_rejects_broken_parent(apply_promotion_batch):
    ok = [_batch(1, []), _batch(2, [], parent="digest-1")]
    apply_promotion_batch.check_chain(ok)  # no raise
    broken = [_batch(1, []), _batch(2, [], parent="digest-WRONG")]
    with pytest.raises(apply_promotion_batch.ApplyError, match="chain"):
        apply_promotion_batch.check_chain(broken)


def test_run_applies_signs_and_records_state(apply_promotion_batch, tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "facts.json").write_text(json.dumps([{"id": "old", "content": "x"}]))
    monkeypatch.setenv("NOCKCC_API_KEY", "k")
    monkeypatch.setenv("NOCKBRAIN_MACHINE", "mac-kevin")

    batches = [
        _batch(1, [{"id": "new1", "content": "promoted fact"}]),
        _batch(2, [], status="rolled_back", parent="digest-1"),
    ]
    monkeypatch.setattr(apply_promotion_batch, "fetch_batches", lambda *a: batches)

    calls = []
    monkeypatch.setattr(
        apply_promotion_batch.subprocess, "run",
        lambda cmd, **k: (calls.append(cmd[1]), type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1],
    )

    code = apply_promotion_batch.run(["--agent", "mira-nockos", "--store-dir", str(store_dir)])
    assert code == 0
    facts = json.loads((store_dir / "facts.json").read_text())
    assert [f["id"] for f in facts] == ["old", "new1"]
    assert any("sign-facts.py" in c for c in calls) and any("verify-facts.py" in c for c in calls)
    state = json.loads((store_dir / "applied-batches.json").read_text())
    assert state == {"1": "digest-1"}  # rolled-back batch is a no-op
    assert list(store_dir.glob("facts.json.bak-preapply-*"))

    # Idempotent: second run applies nothing.
    code = apply_promotion_batch.run(["--agent", "mira-nockos", "--store-dir", str(store_dir)])
    assert code == 0
    assert [f["id"] for f in json.loads((store_dir / "facts.json").read_text())] == ["old", "new1"]


def test_run_aborts_before_write_when_verify_fails(apply_promotion_batch, tmp_path, monkeypatch):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "facts.json").write_text(json.dumps([]))
    monkeypatch.setenv("NOCKCC_API_KEY", "k")
    monkeypatch.setenv("NOCKBRAIN_MACHINE", "mac-kevin")
    monkeypatch.setattr(
        apply_promotion_batch, "fetch_batches",
        lambda *a: [_batch(1, [{"id": "n", "content": "y"}])],
    )
    monkeypatch.setattr(
        apply_promotion_batch.subprocess, "run",
        lambda cmd, **k: type("P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})(),
    )
    code = apply_promotion_batch.run(["--agent", "mira-nockos", "--store-dir", str(store_dir)])
    assert code == 1
    assert not (store_dir / "applied-batches.json").exists()  # never recorded applied
