"""Phase 1 semantic tier: sidecar build, incremental sync, purge parity.

All tests run the deterministic stub encoder (NOCKBRAIN_EMBED_STUB=1) so CI
never downloads model files. numpy is required (added to CI); machines
without it skip cleanly — the semantic tier is optional by design.
"""
import importlib.util
import json
from pathlib import Path

import pytest

numpy = pytest.importorskip("numpy")

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def embed_mod():
    return _load("_embed")


@pytest.fixture()
def embed_facts():
    return _load("embed-facts")


@pytest.fixture()
def purge_fact():
    return _load("purge-fact")


@pytest.fixture(autouse=True)
def _stub_encoder(monkeypatch):
    monkeypatch.setenv("NOCKBRAIN_EMBED_STUB", "1")


def fact(fact_id: str, content: str) -> dict:
    return {
        "id": fact_id,
        "kind": "decision",
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_date": "2026-07-01",
        "evidence": [],
    }


def write_facts(path: Path, facts: list) -> None:
    path.write_text(json.dumps(facts), encoding="utf-8")


@pytest.fixture()
def store(tmp_path):
    facts_path = tmp_path / "facts.json"
    sidecar = tmp_path / "embeddings.npz"
    write_facts(facts_path, [
        fact("a", "pricing locked at 49 dollars"),
        fact("b", "the deploy pipeline uses railway"),
        fact("c", "kevin prefers voice replies"),
    ])
    return facts_path, sidecar


def test_backfill_creates_sidecar(embed_mod, embed_facts, store, capsys):
    facts_path, sidecar = store
    assert embed_facts.run(["--facts", str(facts_path),
                            "--sidecar", str(sidecar), "--backfill"]) == 0
    out = capsys.readouterr().out
    assert "backfill: 3 vector(s)" in out

    data = embed_mod.load_sidecar(sidecar, expect_model="stub-hash-32")
    assert data is not None
    assert sorted(data["ids"]) == ["a", "b", "c"]
    assert data["mat"].shape == (3, 32)
    # rows are L2-normalized and deterministic per content
    norms = numpy.linalg.norm(data["mat"], axis=1)
    assert numpy.allclose(norms, 1.0, atol=1e-5)
    row_a = data["mat"][data["ids"].index("a")]
    again = embed_mod.StubEncoder().encode(["pricing locked at 49 dollars"])[0]
    assert numpy.allclose(row_a, again)
    # hashes match the content-hash contract
    idx = data["ids"].index("b")
    assert data["hashes"][idx] == embed_mod.content_hash(
        "the deploy pipeline uses railway")


def test_incremental_sync_adds_reembeds_prunes(embed_mod, embed_facts, store,
                                               capsys):
    facts_path, sidecar = store
    embed_facts.run(["--facts", str(facts_path), "--sidecar", str(sidecar),
                     "--backfill"])
    before = embed_mod.load_sidecar(sidecar)
    row_a_before = before["mat"][before["ids"].index("a")]

    # b changes content, c is deleted, d is new; a is untouched
    write_facts(facts_path, [
        fact("a", "pricing locked at 49 dollars"),
        fact("b", "the deploy pipeline moved to fly.io"),
        fact("d", "gitleaks scans run on PR only"),
    ])
    capsys.readouterr()
    assert embed_facts.run(["--facts", str(facts_path),
                            "--sidecar", str(sidecar)]) == 0
    out = capsys.readouterr().out
    assert "embedded 2" in out and "kept 1" in out and "pruned 2" in out

    after = embed_mod.load_sidecar(sidecar)
    assert sorted(after["ids"]) == ["a", "b", "d"]
    assert numpy.allclose(after["mat"][after["ids"].index("a")], row_a_before)
    assert after["hashes"][after["ids"].index("b")] == embed_mod.content_hash(
        "the deploy pipeline moved to fly.io")


def test_sidecar_from_other_model_is_invalidated(embed_mod, tmp_path):
    sidecar = tmp_path / "embeddings.npz"
    mat = numpy.zeros((1, 8), dtype=numpy.float32)
    embed_mod.save_sidecar(sidecar, ["a"], ["deadbeef"], "some-other-model", mat)
    assert embed_mod.load_sidecar(sidecar, expect_model="stub-hash-32") is None
    # a model swap re-embeds everything on the next sync
    facts = [fact("a", "hello world")]
    stats = embed_mod.sync_sidecar(facts, embed_mod.StubEncoder(), sidecar)
    assert stats == {"total": 1, "embedded": 1, "kept": 0, "pruned": 0}


def test_corrupt_sidecar_loads_as_none(embed_mod, tmp_path):
    sidecar = tmp_path / "embeddings.npz"
    sidecar.write_bytes(b"not an npz archive")
    assert embed_mod.load_sidecar(sidecar) is None


def test_purge_removes_vector_with_fact(embed_mod, embed_facts, purge_fact,
                                        store, tmp_path, capsys):
    facts_path, sidecar = store
    embed_facts.run(["--facts", str(facts_path), "--sidecar", str(sidecar),
                     "--backfill"])
    argv = [
        "b",
        "--facts", str(facts_path),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(tmp_path / "sessions"),
        "--vault", str(tmp_path / "vault"),
        "--sidecar", str(sidecar),
    ]
    # dry-run reports but does not rewrite
    assert purge_fact.run(argv) == 0
    assert "would remove 1 fact(s)" in capsys.readouterr().out
    assert sorted(embed_mod.load_sidecar(sidecar)["ids"]) == ["a", "b", "c"]

    assert purge_fact.run(argv + ["--apply"]) == 0
    out = capsys.readouterr().out
    assert "removed 1 fact(s)" in out and "1 vector(s)" in out
    data = embed_mod.load_sidecar(sidecar)
    assert sorted(data["ids"]) == ["a", "c"]
    assert data["mat"].shape == (2, 32)
    remaining = json.loads(facts_path.read_text())
    assert [f["id"] for f in remaining] == ["a", "c"]


def test_purge_deletes_unreadable_sidecar(purge_fact, store, tmp_path, capsys):
    facts_path, sidecar = store
    sidecar.write_bytes(b"garbage")
    argv = [
        "a",
        "--facts", str(facts_path),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(tmp_path / "sessions"),
        "--vault", str(tmp_path / "vault"),
        "--sidecar", str(sidecar),
        "--apply",
    ]
    assert purge_fact.run(argv) == 0
    assert not sidecar.exists(), "fail-safe: unreadable sidecar is deleted"


def test_missing_model_assets_error_cleanly(embed_facts, store, tmp_path,
                                            monkeypatch, capsys):
    monkeypatch.delenv("NOCKBRAIN_EMBED_STUB", raising=False)
    facts_path, sidecar = store
    rc = embed_facts.run(["--facts", str(facts_path),
                          "--sidecar", str(sidecar),
                          "--model-dir", str(tmp_path / "no-model")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no embedding model" in err and "fetch-embed-model" in err
    assert not sidecar.exists()


def test_deps_hint_prefers_installer_venv(embed_mod, tmp_path, monkeypatch):
    # A missing-deps error must point at the interpreter that actually has
    # numpy + tokenizers (the installer venv) when one exists — "pip install"
    # targets whatever `python3` resolves to, which is how the deps went
    # missing in the first place.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert embed_mod._deps_hint() == "pip install numpy tokenizers"

    venv_python = tmp_path / ".nock-brain" / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    assert embed_mod._deps_hint() == f"rerun with {venv_python}"
