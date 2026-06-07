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


@pytest.fixture(scope="session")
def classifier():
    return _load("recall-classifier")


@pytest.fixture(scope="session")
def budget_recall():
    return _load("budget-recall")


@pytest.fixture(scope="session")
def extract_facts():
    return _load("extract-facts")


@pytest.fixture(scope="session")
def synthesize():
    return _load("synthesize")
