"""Phase 4 hook wiring: venv interpreter preference + semantic-on marker.

Drives the real hooks/memory-inject.sh via subprocess with HOME pointed at a
tmp dir, mirroring test_stage1_hardening. No model assets are involved: the
marker test proves the flag reaches budget-recall and degrades to flat BM25.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "memory-inject.sh"
PROMPT = "what did we decide about stage one memory recall"


def fact(content: str) -> dict:
    return {"id": "f1", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": content,
            "source_date": "2026-07-01", "evidence": []}


def brain_home(tmp_path: Path) -> Path:
    home = tmp_path
    facts_dir = home / ".nock-brain"
    facts_dir.mkdir()
    (facts_dir / "facts.json").write_text(
        json.dumps([fact("[DECISION] Kevin chose stage one memory recall")]),
        encoding="utf-8",
    )
    return home


def run_hook(home: Path) -> dict:
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"prompt": PROMPT}),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home),
             "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    return json.loads(result.stdout)


def test_hook_prefers_venv_python_when_present(tmp_path):
    home = brain_home(tmp_path)
    venv_bin = home / ".nock-brain" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    marker = home / "venv-python-used"
    shim = venv_bin / "python3"
    # Records each invocation, then defers to the real python3 from PATH
    # (the shim's own directory is never on PATH, so no recursion).
    shim.write_text(
        "#!/bin/bash\n"
        f"echo used >> {marker}\n"
        'exec python3 "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    payload = run_hook(home)
    assert "systemMessage" in payload
    assert "stage one memory recall" in payload["systemMessage"]
    assert marker.exists(), "hook must route python through the venv shim"
    assert len(marker.read_text().splitlines()) >= 3, (
        "prompt parse, classifier, and recall should all use the venv python"
    )


def test_hook_without_venv_uses_plain_python(tmp_path):
    home = brain_home(tmp_path)
    payload = run_hook(home)
    assert "systemMessage" in payload
    assert "stage one memory recall" in payload["systemMessage"]


def test_semantic_marker_reaches_recall_and_degrades_to_bm25(tmp_path):
    home = brain_home(tmp_path)
    (home / ".nock-brain" / "semantic-on").touch()

    payload = run_hook(home)
    assert "systemMessage" in payload, (
        "semantic-on with no model/sidecar must still produce flat recall"
    )
    assert "stage one memory recall" in payload["systemMessage"]
    log = (home / ".nock-brain" / "hook-errors.log").read_text(encoding="utf-8")
    assert "flat BM25" in log, (
        "the marker must reach budget-recall (fallback note proves the "
        "semantic path was attempted)"
    )
