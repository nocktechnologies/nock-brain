"""Regression tests for the hook hot path's Python-version floor.

hooks/memory-inject.sh invokes plain ``python3`` from PATH. On a stock macOS
non-interactive shell that resolves to /usr/bin/python3 — Python 3.9. CI runs
3.11/3.12 only, so 3.10-only syntax that is evaluated at import time (unquoted
PEP 604 unions in signatures, match statements, ...) passes the whole suite in
CI and then crashes the recall hot path on a real Mac. That happened once:
``set[str] | None`` defaults in _facts.py/budget-recall.py broke every recall
until a shell-quoting test tripped over it by accident.

Two layers pin the floor:

- every bin/ module transitively reachable from the hook must carry
  ``from __future__ import annotations`` so annotation syntax is never
  evaluated at import time (enforceable on any interpreter, so CI catches it);
- when /usr/bin/python3 exists (macOS: the interpreter the hook actually
  runs), every reachable module must genuinely import under it.
"""
import ast
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"

# The two scripts hooks/memory-inject.sh invokes directly.
HOOK_ENTRYPOINTS = ["recall-classifier.py", "budget-recall.py"]

# The interpreter the hook gets on a stock Mac. Anything under 3.10 exercises
# the real floor; a newer one still adds an import smoke test at no cost.
STOCK_PYTHON = Path("/usr/bin/python3")


def _local_module_refs(path: Path) -> "set[str]":
    """bin/*.py files referenced by `path`: plain imports of sibling modules,
    plus string literals naming a sibling file — _graph_recall loads
    "export-graph.py" via importlib because a hyphenated name can't be
    imported, and that reference must not escape the closure."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value[:-3]] if node.value.endswith(".py") else []
        else:
            continue
        refs.update(f"{name}.py" for name in names if (BIN / f"{name}.py").exists())
    return refs


def hook_reachable_modules() -> "list[Path]":
    """Transitive closure of bin/ modules reachable from the hook entrypoints,
    including branches behind flags (--graph / NOCKBRAIN_GRAPH_RECALL): the
    hook runs with the user's environment, so gated paths are still hot."""
    seen = []
    queue = list(HOOK_ENTRYPOINTS)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.append(name)
        queue.extend(sorted(_local_module_refs(BIN / name)))
    return sorted(BIN / name for name in seen)


def test_hook_reachable_closure_is_acknowledged():
    """Force a conscious update when the hot path grows: a module newly
    reachable from the hook must be added here — and at that moment, checked
    against the 3.9 floor documented in the README's Development section."""
    assert [p.name for p in hook_reachable_modules()] == [
        "_dense_recall.py",
        "_embed.py",
        "_facts.py",
        "_graph_recall.py",
        "_sign.py",
        "_store.py",
        "budget-recall.py",
        "export-graph.py",
        "recall-classifier.py",
    ]


@pytest.mark.parametrize("path", hook_reachable_modules(), ids=lambda p: p.name)
def test_hook_hot_path_defers_annotation_evaluation(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    has_future_annotations = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    assert has_future_annotations, (
        f"{path.name} is reachable from hooks/memory-inject.sh, which runs the "
        f"stock macOS python3 (3.9): it needs `from __future__ import "
        f"annotations` so its annotations are never evaluated at import time"
    )


@pytest.mark.skipif(
    not STOCK_PYTHON.exists(), reason="no /usr/bin/python3 on this machine"
)
def test_hook_hot_path_imports_under_stock_python3():
    driver = """
import importlib.util, sys
sys.path.insert(0, sys.argv[1])
failures = []
for arg in sys.argv[2:]:
    name = arg.rsplit("/", 1)[-1][:-3].replace("-", "_")
    try:
        spec = importlib.util.spec_from_file_location(name, arg)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        failures.append(f"{arg}: {type(exc).__name__}: {exc}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
"""
    result = subprocess.run(
        [str(STOCK_PYTHON), "-c", driver, str(BIN)]
        + [str(p) for p in hook_reachable_modules()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    version = subprocess.run(
        [str(STOCK_PYTHON), "--version"], capture_output=True, text=True
    ).stdout.strip()
    assert result.returncode == 0, (
        f"hook hot-path modules failed to import under {STOCK_PYTHON} "
        f"({version}):\n{result.stdout}{result.stderr}"
    )
