"""S4 — projection receipts with verified readback.

A derived write (export) is not "applied" until it has been read back and
hash-verified. An unverifiable write is reported as "ambiguous" — a
first-class state, never silently assumed ok. A stale/failed export caused a
3-day silent memory freeze in July; these tests make that failure mode loud.
"""
import hashlib
import json
from pathlib import Path


def fact(content="[DIRECTIVE] Kevin wants stable memory rules in AGENTS.md", line=5):
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


# --- verified writers (bin/_store.py) --------------------------------------


def test_verified_json_write_returns_sha256_receipt(store_lib, tmp_path):
    path = tmp_path / "out.json"

    receipt = store_lib.secure_write_json_verified(path, {"a": 1}, indent=2)

    on_disk = path.read_bytes()
    assert receipt == {
        "path": str(path),
        "sha256": hashlib.sha256(on_disk).hexdigest(),
        "bytes": len(on_disk),
        "verified": True,
    }
    assert json.loads(on_disk) == {"a": 1}


def test_verified_text_write_returns_sha256_receipt(store_lib, tmp_path):
    path = tmp_path / "out.txt"
    text = "hello receipts\n"

    receipt = store_lib.secure_write_text_verified(path, text)

    assert receipt["verified"] is True
    assert receipt["path"] == str(path)
    assert receipt["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert receipt["bytes"] == len(text.encode("utf-8"))


def test_corrupted_readback_reports_ambiguous_not_raise(store_lib, tmp_path, monkeypatch):
    path = tmp_path / "out.json"
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"corrupted bytes")

    receipt = store_lib.secure_write_json_verified(path, {"a": 1})

    assert receipt["verified"] is False
    assert receipt["state"] == "ambiguous"
    assert receipt["path"] == str(path)
    assert receipt["error"]


def test_failing_readback_reports_ambiguous_not_raise(store_lib, tmp_path, monkeypatch):
    path = tmp_path / "out.txt"

    def boom(self):
        raise OSError("disk went away")

    monkeypatch.setattr(Path, "read_bytes", boom)

    receipt = store_lib.secure_write_text_verified(path, "payload")

    assert receipt["verified"] is False
    assert receipt["state"] == "ambiguous"
    assert "disk went away" in receipt["error"]


# --- export-graph --receipt ------------------------------------------------


def test_graph_export_receipt_all_verified_exit_zero(export_graph, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    out = tmp_path / "graph.json"
    receipt_path = tmp_path / "receipt.json"

    code = export_graph.run([
        "--facts", str(facts_file),
        "--output", str(out),
        "--receipt", str(receipt_path),
    ])

    assert code == 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["all_verified"] is True
    assert receipt["generated_at"]
    assert len(receipt["artifacts"]) == 1
    artifact = receipt["artifacts"][0]
    assert artifact["path"] == str(out)
    assert artifact["verified"] is True
    assert artifact["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_graph_export_without_receipt_byte_identical(export_graph, tmp_path, monkeypatch):
    facts_file = tmp_path / "facts.json"
    facts = [fact()]
    facts_file.write_text(json.dumps(facts))
    out = tmp_path / "graph.json"

    # Prove the verified-writer path is never engaged without --receipt.
    def forbidden(*args, **kwargs):
        raise AssertionError("verified writer must not run without --receipt")

    monkeypatch.setattr(export_graph, "secure_write_text_verified", forbidden)

    code = export_graph.run(["--facts", str(facts_file), "--output", str(out)])

    assert code == 0
    expected = json.dumps(
        export_graph.graph_from_facts(facts), indent=2, ensure_ascii=False
    ).encode("utf-8")
    assert out.read_bytes() == expected


def test_forced_ambiguous_graph_export_exits_nonzero(export_graph, tmp_path, monkeypatch):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    out = tmp_path / "graph.json"
    receipt_path = tmp_path / "receipt.json"

    def ambiguous_writer(path, text, **kwargs):
        return {
            "path": str(path),
            "verified": False,
            "state": "ambiguous",
            "error": "readback hash mismatch",
        }

    monkeypatch.setattr(export_graph, "secure_write_text_verified", ambiguous_writer)

    code = export_graph.run([
        "--facts", str(facts_file),
        "--output", str(out),
        "--receipt", str(receipt_path),
    ])

    assert code != 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["all_verified"] is False
    assert receipt["artifacts"][0]["state"] == "ambiguous"


# --- export-obsidian --receipt ---------------------------------------------


def test_obsidian_export_receipt_covers_all_artifacts(export_obsidian, tmp_path):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact("[DECISION] Kevin chose NockBrain v2", line=9)]))
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "s1.md").write_text("# Session s1\n")
    vault = tmp_path / "vault"
    receipt_path = tmp_path / "receipt.json"

    code = export_obsidian.run([
        "--facts", str(facts_file),
        "--sessions", str(sessions_dir),
        "--vault", str(vault),
        "--receipt", str(receipt_path),
    ])

    assert code == 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["all_verified"] is True
    written = sorted(str(p) for p in vault.rglob("*") if p.is_file())
    receipted = sorted(a["path"] for a in receipt["artifacts"])
    assert receipted == written
    for artifact in receipt["artifacts"]:
        on_disk = Path(artifact["path"]).read_bytes()
        assert artifact["sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_obsidian_export_without_receipt_byte_identical(export_obsidian, tmp_path, monkeypatch):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    vault = tmp_path / "vault"

    def forbidden(*args, **kwargs):
        raise AssertionError("verified writer must not run without --receipt")

    monkeypatch.setattr(export_obsidian, "secure_write_text_verified", forbidden)

    code = export_obsidian.run(["--facts", str(facts_file), "--vault", str(vault)])

    assert code == 0
    assert (vault / "index.md").exists()


def test_forced_ambiguous_obsidian_export_exits_nonzero(export_obsidian, tmp_path, monkeypatch):
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps([fact()]))
    vault = tmp_path / "vault"
    receipt_path = tmp_path / "receipt.json"

    def ambiguous_writer(path, text, **kwargs):
        return {
            "path": str(path),
            "verified": False,
            "state": "ambiguous",
            "error": "readback hash mismatch",
        }

    monkeypatch.setattr(export_obsidian, "secure_write_text_verified", ambiguous_writer)

    code = export_obsidian.run([
        "--facts", str(facts_file),
        "--vault", str(vault),
        "--receipt", str(receipt_path),
    ])

    assert code != 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["all_verified"] is False
