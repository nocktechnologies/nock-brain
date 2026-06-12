import json
import os
import stat
import subprocess
import sys
import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def run_python(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    merged_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO,
        env=merged_env,
        text=True,
        capture_output=True,
        check=True,
    )


def fact(content="[DIRECTIVE] Kevin chose private store permissions", line=5):
    return {
        "id": "fact-1",
        "kind": "directive",
        "scope": "global",
        "status": "current",
        "confidence": 0.9,
        "content": content,
        "source_file": "session.jsonl",
        "source_date": "2026-06-11",
        "session": "s1",
        "session_anchor": "/tmp/session.jsonl:5",
        "created_at": "2026-06-11T00:00:00Z",
        "last_seen_at": "2026-06-11T00:00:00Z",
        "subject": "user",
        "evidence": [{"event_id": "event-5", "path": "/tmp/session.jsonl", "line": line}],
    }


def test_stage1_store_writers_create_private_files(tmp_path):
    old_umask = os.umask(0o022)
    try:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user",
                "sessionId": "s1",
                "timestamp": "2026-06-11T01:00:00Z",
                "message": {"role": "user", "content": "[DECISION] Kevin chose private stores"},
            }) + "\n",
            encoding="utf-8",
        )
        events = tmp_path / "store" / "events.jsonl"
        run_python(["bin/ingest-jsonl.py", "--output", str(events), str(transcript)])
        assert mode(events) == 0o600
        assert mode(events.parent) == 0o700

        facts = tmp_path / "store" / "facts.json"
        notes = tmp_path / "store" / "sessions"
        run_python([
            "bin/refine-sessions.py",
            "--events", str(events),
            "--facts", str(facts),
            "--notes-dir", str(notes),
        ])
        assert mode(facts) == 0o600
        assert mode(next(notes.glob("*.md"))) == 0o600
        assert mode(notes) == 0o700

        review = tmp_path / "store" / "review"
        run_python(["bin/review-promotions.py", "--facts", str(facts), "--output", str(review)])
        assert mode(review / "promotion-candidates.json") == 0o600
        assert mode(review / "promotion-candidates.md") == 0o600

        graph = tmp_path / "store" / "graph.json"
        run_python(["bin/export-graph.py", "--facts", str(facts), "--output", str(graph)])
        assert mode(graph) == 0o600

        vault = tmp_path / "store" / "vault"
        run_python([
            "bin/export-obsidian.py",
            "--facts", str(facts),
            "--sessions", str(notes),
            "--review", str(review),
            "--vault", str(vault),
        ])
        assert mode(vault / "index.md") == 0o600
        assert mode(next((vault / "facts").glob("*.md"))) == 0o600
        assert mode(next((vault / "sessions").glob("*.md"))) == 0o600

        insights = tmp_path / "store" / "insights.json"
        run_python(["bin/synthesize.py", "--facts", str(facts), "--output", str(insights)])
        assert mode(insights) == 0o600

        transcript_dir = tmp_path / "transcripts"
        transcript_dir.mkdir()
        (transcript_dir / "2026-06-11.md").write_text(
            "## Session 10:00\n- [DECISION] Kevin chose private extracted facts\n",
            encoding="utf-8",
        )
        extracted = tmp_path / "store" / "extracted-facts.json"
        run_python(["bin/extract-facts.py", "--dir", str(transcript_dir), "--output", str(extracted)])
        assert mode(extracted) == 0o600

        generated_fact_id = json.loads(facts.read_text(encoding="utf-8"))[0]["id"]
        run_python(["bin/supersede-fact.py", generated_fact_id, "--facts", str(facts)])
        assert mode(facts) == 0o600
    finally:
        os.umask(old_umask)


def test_installer_hardens_existing_store_tree():
    installer = (REPO / "install.sh").read_text(encoding="utf-8")

    assert 'mkdir -p -m 700 "$FACTS_DIR"' in installer
    assert 'chmod -R go-rwx "$FACTS_DIR"' in installer


def test_secure_write_does_not_chmod_unrelated_existing_parent(tmp_path):
    spec = importlib.util.spec_from_file_location("store", REPO / "bin" / "_store.py")
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)

    store.secure_write_text(existing / "artifact.json", "{}")

    assert mode(existing / "artifact.json") == 0o600
    assert mode(existing) == 0o755


def test_memory_hook_handles_prompt_that_starts_with_dash(tmp_path):
    facts_dir = tmp_path / ".nock-brain"
    facts_dir.mkdir()
    (facts_dir / "facts.json").write_text(
        json.dumps([fact("[DECISION] Kevin chose stage one memory recall")]),
        encoding="utf-8",
    )
    prompt = "- what did we decide about stage one memory recall"

    result = subprocess.run(
        ["bash", str(REPO / "hooks" / "memory-inject.sh")],
        input=json.dumps({"prompt": prompt}),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
    )
    payload = json.loads(result.stdout)

    assert "systemMessage" in payload
    assert "stage one memory recall" in payload["systemMessage"]


def test_memory_hook_uses_printf_and_arg_separator():
    hook = (REPO / "hooks" / "memory-inject.sh").read_text(encoding="utf-8")

    assert 'printf \'%s\' "$INPUT"' in hook
    assert 'printf \'%s\' "$PROMPT"' in hook
    assert '-- "$PROMPT"' in hook
