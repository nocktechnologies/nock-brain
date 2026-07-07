"""Shared fixtures. The bin/ scripts have hyphenated names and no package
structure, so we load each as a module by path via importlib."""
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str):
    path = BIN / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_signing_key_env(monkeypatch, tmp_path):
    """Keep tests hermetic: budget-recall verifies fact attestations against
    the key at ~/.nock-brain by default, so a developer machine with a real
    signing key would silently change recall-test behavior. Point resolution
    at paths that never exist; verification tests override these env vars."""
    missing = tmp_path / "no-such-signing-key"
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(missing))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(missing) + ".pub")


@pytest.fixture(scope="session")
def classifier():
    return _load("recall-classifier")


@pytest.fixture(scope="session")
def budget_recall():
    return _load("budget-recall")


@pytest.fixture(scope="session")
def facts_lib():
    return _load("_facts")


@pytest.fixture(scope="session")
def brain_check():
    return _load("brain-check")


@pytest.fixture(scope="session")
def brain_think():
    return _load("brain-think")


@pytest.fixture(scope="session")
def extract_facts():
    return _load("extract-facts")


@pytest.fixture(scope="session")
def synthesize():
    return _load("synthesize")


@pytest.fixture(scope="session")
def propose_facts():
    return _load("propose-facts")


@pytest.fixture(scope="session")
def approve_proposals():
    return _load("approve-proposals")


@pytest.fixture(scope="session")
def ingest_jsonl():
    return _load("ingest-jsonl")


@pytest.fixture(scope="session")
def refine_sessions():
    return _load("refine-sessions")


@pytest.fixture(scope="session")
def scrub():
    return _load("_scrub")


@pytest.fixture(scope="session")
def review_promotions():
    return _load("review-promotions")


@pytest.fixture(scope="session")
def export_obsidian():
    return _load("export-obsidian")


@pytest.fixture(scope="session")
def export_graph():
    return _load("export-graph")


@pytest.fixture(scope="session")
def nockbrain_health():
    return _load("nockbrain-health")


@pytest.fixture(scope="session")
def ingest_curated_memory():
    return _load("ingest-curated-memory")


@pytest.fixture(scope="session")
def sign_lib():
    return _load("_sign")
