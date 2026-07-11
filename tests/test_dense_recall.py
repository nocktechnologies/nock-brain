"""Phase 2 hybrid semantic recall: fusion, reserved slots, fallbacks.

Uses a crafted encoder (fixed vectors per known text) injected via
sys.modules so similarity is controllable — the stub's hash vectors are
deterministic but semantically meaningless. numpy required; machines without
it skip (the tier is optional by design).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

numpy = pytest.importorskip("numpy")

BIN = Path(__file__).resolve().parent.parent / "bin"
NOW_ISO = "2026-07-01"


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
def br():
    return _load("budget-recall")


QUERY = "pricing"
LEX_FACT = "pricing was locked at 49 dollars"
TARGET = "the seatbelt rule was finalized in review"  # zero query overlap
NOISE = "an unrelated note about the weather"

# Crafted vector space (dim 4): query and TARGET are identical, everything
# else orthogonal — a perfect paraphrase pair with zero token overlap.
VECTORS = {
    QUERY: [1.0, 0.0, 0.0, 0.0],
    TARGET: [1.0, 0.0, 0.0, 0.0],
    LEX_FACT: [0.0, 1.0, 0.0, 0.0],
    NOISE: [0.0, 0.0, 1.0, 0.0],
}


class CraftedEncoder:
    model_id = "crafted-test"
    dim = 4

    def encode(self, texts):
        rows = [VECTORS.get(t, [0.0, 0.0, 0.0, 1.0]) for t in texts]
        return numpy.asarray(rows, dtype=numpy.float32)


def fact(fact_id, content, date=NOW_ISO):
    return {"id": fact_id, "kind": "decision", "status": "current",
            "confidence": 0.9, "content": content, "source_date": date,
            "evidence": []}


@pytest.fixture()
def semantic_env(embed_mod, monkeypatch, tmp_path):
    """Wire the crafted encoder + a tmp sidecar into the import path
    budget-recall's semantic gate uses at call time."""
    sidecar = tmp_path / "embeddings.npz"
    monkeypatch.setattr(embed_mod, "get_encoder",
                        lambda model_dir=None: CraftedEncoder())
    monkeypatch.setattr(embed_mod, "DEFAULT_SIDECAR", sidecar)
    monkeypatch.setitem(sys.modules, "_embed", embed_mod)
    sys.modules.pop("_dense_recall", None)  # force fresh import against patch

    def build(facts):
        enc = CraftedEncoder()
        texts = [str(f["content"]) for f in facts]
        embed_mod.save_sidecar(
            sidecar,
            [f["id"] for f in facts],
            [embed_mod.content_hash(f["content"]) for f in facts],
            enc.model_id,
            enc.encode(texts),
        )
        return sidecar

    return build, sidecar


def write_facts(tmp_path, facts):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path


def test_semantic_surfaces_zero_overlap_fact(br, semantic_env, tmp_path):
    build, _ = semantic_env
    facts = [fact("lex", LEX_FACT), fact("tgt", TARGET), fact("n", NOISE)]
    fp = write_facts(tmp_path, facts)
    build(facts)

    off = br.budget_recall(QUERY, fp, budget=1000, semantic=False)
    assert "seatbelt rule" not in off, "BM25 alone cannot reach the target"

    on = br.budget_recall(QUERY, fp, budget=1000, semantic=True)
    assert "pricing was locked" in on
    assert "seatbelt rule was finalized" in on, (
        "dense fusion must surface the zero-overlap paraphrase target"
    )


def test_flag_off_is_pure_passthrough(br, tmp_path):
    facts = [fact("lex", LEX_FACT)]
    seeds = br.search(facts, QUERY, now=br._resolve_now(None))
    fused, reserved = br._maybe_dense_fuse(
        facts, seeds, QUERY, False, br._resolve_now(None), semantic=False)
    assert fused is seeds
    assert reserved == frozenset()


def test_missing_sidecar_falls_back_to_flat(br, semantic_env, tmp_path,
                                            capsys):
    # sidecar never built: semantic output must equal the flat output
    facts = [fact("lex", LEX_FACT), fact("tgt", TARGET)]
    fp = write_facts(tmp_path, facts)
    off = br.budget_recall(QUERY, fp, budget=1000, semantic=False)
    on = br.budget_recall(QUERY, fp, budget=1000, semantic=True)
    assert on == off
    assert "using flat BM25" in capsys.readouterr().err


def test_stale_vector_is_skipped(br, embed_mod, semantic_env, tmp_path):
    build, _ = semantic_env
    facts = [fact("lex", LEX_FACT), fact("tgt", TARGET)]
    fp = write_facts(tmp_path, facts)
    build(facts)
    # target's content changes after embedding -> its vector is stale
    facts[1] = fact("tgt", TARGET + " (edited)")
    fp.write_text(json.dumps(facts), encoding="utf-8")

    on = br.budget_recall(QUERY, fp, budget=1000, semantic=True)
    assert "seatbelt rule" not in on, (
        "a hash-mismatched vector must not surface its fact"
    )


def test_reserved_slot_survives_date_cap_and_budget(br, semantic_env,
                                                    tmp_path):
    build, _ = semantic_env
    # 6 lexical hits share one bulk-import date (cap=4 defers two); the
    # zero-overlap target shares that same date and only dense finds it.
    facts = [fact(f"lex{i}", f"{LEX_FACT} variant {i}", date="2026-05-19")
             for i in range(6)]
    facts.append(fact("tgt", TARGET, date="2026-05-19"))
    fp = write_facts(tmp_path, facts)
    build(facts)

    on = br.budget_recall(QUERY, fp, budget=220, semantic=True)
    assert "seatbelt rule was finalized" in on, (
        "reserved dense slot must survive the date diversity cap AND a "
        "budget that truncates the lexical tail"
    )


def test_insight_lead_cap_applies_only_when_semantic(br, semantic_env,
                                                     tmp_path, monkeypatch):
    build, _ = semantic_env
    facts = [fact("lex", LEX_FACT)]
    fp = write_facts(tmp_path, facts)
    build(facts)
    insights = [
        {"id": f"ins{i}", "kind": "insight", "status": "current",
         "confidence": 0.9, "content": f"pricing insight number {i}",
         "source_date": NOW_ISO, "source_ids": []}
        for i in range(8)
    ]
    ip = tmp_path / "insights.json"
    ip.write_text(json.dumps(insights), encoding="utf-8")

    off = br.budget_recall(QUERY, fp, budget=4000, semantic=False,
                           insights_file=ip)
    assert off.count("pricing insight number") == 8

    on = br.budget_recall(QUERY, fp, budget=4000, semantic=True,
                          insights_file=ip)
    assert on.count("pricing insight number") == 5, (
        "semantic mode caps the insight lead at 5 by default"
    )

    monkeypatch.setenv("NOCKBRAIN_INSIGHT_LEAD", "2")
    on2 = br.budget_recall(QUERY, fp, budget=4000, semantic=True,
                           insights_file=ip)
    assert on2.count("pricing insight number") == 2
