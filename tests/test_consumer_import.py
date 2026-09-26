"""Selected-file customer extraction, using only synthetic inputs."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _consumer_import as ci
import _consumer_store as cs


@pytest.fixture
def manifest(tmp_path):
    return cs.init_store(tmp_path / "customer")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _jsonl(path, *lines):
    return _write(path, "\n".join(json.dumps(line) for line in lines) + "\n")


def _message(actor, text, **extra):
    return {"type": actor, "message": {"role": actor, "content": [{"type": "text", "text": text}]}, **extra}


def test_markdown_tagged_inferred_authority_and_proposal(tmp_path, manifest, monkeypatch):
    monkeypatch.setenv("NOCKBRAIN_MACHINE", "hostile-fleet-name")
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", "/does/not/exist")
    notes = _write(tmp_path / "notes.md", """# Curated notes
- [DECISION] I chose the blue layout.
- User approved the green layout.
- [BUG] A regression caused the save error.
- [MERGE] Merged PR #42.
* [DECISION] This is not a bullet.
- I decided on orange.
""")
    candidates, receipts, stats = ci.collect_candidates([notes], "markdown", manifest)
    assert [item["kind"] for item in candidates] == ["decision", "decision", "bug"]
    assert [item["subject"] for item in candidates] == ["user"] * 3
    assert all(item["source"] == "customer:" + manifest["store_id"] for item in candidates)
    assert receipts == [{"path": str(notes), "sha256": hashlib.sha256(notes.read_bytes()).hexdigest(),
                         "format": "markdown"}]
    assert stats["files"] == 1 and stats["candidates"] == 3
    with cs.ConsumerStore(tmp_path / "customer") as store:
        digest = store.save_proposal(candidates, receipts, stats)
        assert store.read_proposal(digest)["candidates"] == candidates


def test_jsonl_roles_tool_surfaces_sidechains_and_private_pair(tmp_path, manifest):
    path = _jsonl(
        tmp_path / "session.jsonl",
        _message("user", "[DECISION] I selected A.", timestamp="2026-09-25T18:00:00Z"),
        _message("assistant", "[DECISION] I selected B."),
        _message("assistant", "[BUG] A regression caused the crash."),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "id": "private", "input": {"path": "/x/.env"}},
            {"type": "text", "text": "[ARCHITECTURE] Schema changed with the migration."}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "private", "content": "[DECISION] User chose private."}]}},
        _message("user", "[DECISION] I selected hidden.", isSidechain=True),
        {"type": "progress", "data": {"text": "[DECISION] User selected private."}},
    )
    candidates, _, stats = ci.collect_candidates([path], "claude-jsonl", manifest)
    assert [item["kind"] for item in candidates] == ["decision", "bug", "architecture"]
    assert [item["subject"] for item in candidates] == ["user", "assistant", "assistant"]
    assert candidates[0]["source_date"] == "2026-09-25"
    assert stats["sidechain_excluded"] == 1
    assert stats["denied_paths"] == 1 and stats["denied_results"] == 1
    assert all("private" not in item["content"] for item in candidates)


@pytest.mark.parametrize("bad", [
    "{", "[]", "null", "NaN", '{"type":"user","message":{"role":"user","content":[1]}}',
    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":{"x":1}}]}}',
    '{"type":"assistant","message":{"role":"user","content":"[DECISION] I chose A."}}',
    '{"type":"user","message":{"role":"user","content":"[DECISION] I chose A."},"x":1e999}',
    '{"type":"user","type":"assistant"}',
    '{"type":[],"message":{}}',
    '{"type":"user","message":{"role":"user","content":"[DECISION] I chose \\ud800"}}',
])
def test_malformed_jsonl_fails_whole_file(tmp_path, manifest, bad):
    path = _write(tmp_path / "session.jsonl", json.dumps(_message("user", "[DECISION] I chose A.")) + "\n" + bad + "\n")
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([path], "claude-jsonl", manifest)


def test_scrubbed_content_metadata_and_repeatability(tmp_path, manifest):
    path = _write(tmp_path / "notes.md", "- [CONFIG] API key sk-" + "a" * 40 + " changed.\n")
    first = ci.collect_candidates([path], "markdown", manifest)
    assert first == ci.collect_candidates([path], "markdown", manifest)
    candidates, receipts, stats = first
    assert len(candidates) == 1 and stats["secrets_redacted"] >= 1
    assert "sk-" not in candidates[0]["content"]
    assert candidates[0]["evidence"][0]["path"] == receipts[0]["path"]
    assert candidates[0]["evidence"][0]["sha256"] == receipts[0]["sha256"]
    assert "sk-" not in json.dumps(first)


def test_full_content_ids_duplicate_evidence_and_overlong(tmp_path, manifest):
    prefix = "[ARCHITECTURE] The architecture changed " + "several parts " * 19
    first = _write(tmp_path / "one.md", f"- {prefix}A\n- {prefix}A\n- {prefix}B\n- [BUG] A regression caused " + "many errors " * 150 + "\n")
    second = _write(tmp_path / "two.md", f"- {prefix}A\n")
    candidates, receipts, stats = ci.collect_candidates([first, second], "markdown", manifest)
    assert len(candidates) == 2 and candidates[0]["id"] != candidates[1]["id"]
    assert len(candidates[0]["evidence"]) == 3
    assert candidates[0]["source_file"] == "one.md"
    assert stats["overlong_skipped"] == 1 and stats["candidates"] == 2
    assert len(receipts) == 2


def test_multiline_content_and_bad_timestamp_fallback(tmp_path, manifest):
    raw = _message("user", "[DECISION] I chose A.\nThen I chose B.\x00")
    raw["timestamp"] = "2026-99-99T00:00:00Z"
    path = _jsonl(tmp_path / "session.jsonl", raw)
    candidates, _, _ = ci.collect_candidates([path], "claude-jsonl", manifest)
    assert candidates[0]["content"].endswith("B.\x00")
    assert candidates[0]["source_date"] == ci._mtime_timestamp(path.stat().st_mtime_ns)[:10]


def test_jsonl_metadata_is_derived_and_overlong_is_counted(tmp_path, manifest):
    raw = _message("user", "[DECISION] I chose A.")
    raw.update({"uuid": "\x1b[31m", "sessionId": "private-session", "timestamp": "not-a-date"})
    huge = _message("user", "[DECISION] I chose " + "many options " * 150)
    path = _jsonl(tmp_path / "session.jsonl", raw, huge)
    candidates, receipts, stats = ci.collect_candidates([path], "claude-jsonl", manifest)
    assert len(candidates) == 1 and stats["overlong_skipped"] == 1
    assert stats["events_written"] == 2
    assert "private-session" not in json.dumps((candidates, receipts, stats))
    assert "\x1b" not in json.dumps((candidates, receipts, stats))


def test_source_selection_denials_aliases_and_duplicates(tmp_path, manifest):
    good = _write(tmp_path / "good.md", "- [DECISION] I chose A.\n")
    alias = tmp_path / "alias.md"
    alias.symlink_to(good)
    for path in (Path("relative.md"), alias, _write(tmp_path / ".env", "x"),
                 _write(tmp_path / "signing-key.pub", "x"),
                 _write(tmp_path / ".ssh" / "notes.md", "x"),
                 _write(tmp_path / ".gnupg" / "notes.md", "x"),
                 _write(tmp_path / "secret-notes.md", "x")):
        with pytest.raises(cs.ConsumerError):
            ci.collect_candidates([path], "markdown", manifest)
    private = _write(tmp_path / "secret-dir" / "notes.md", "x")
    parent_alias = tmp_path / "shortcut"
    parent_alias.symlink_to(private.parent, target_is_directory=True)
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([parent_alias / "notes.md"], "markdown", manifest)
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([good, good], "markdown", manifest)
    hardlink = tmp_path / "other.md"
    os.link(good, hardlink)
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([good, hardlink], "markdown", manifest)


def test_source_limits_special_files_and_drift(tmp_path, manifest, monkeypatch):
    path = _write(tmp_path / "notes.md", "- [DECISION] I chose A.\n")
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([path] * 17, "markdown", manifest)
    oversized = tmp_path / "huge.md"
    with oversized.open("wb") as handle:
        handle.truncate(cs.SOURCE_LIMIT + 1)
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([oversized], "markdown", manifest)
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([fifo], "markdown", manifest)
    original = ci.read_regular

    def drifting(source, limit):
        data = original(source, limit)
        source.write_text("changed", encoding="utf-8")
        return data

    monkeypatch.setattr(ci, "read_regular", drifting)
    with pytest.raises(cs.ConsumerError, match="changed"):
        ci.collect_candidates([path], "markdown", manifest)


def test_metadata_control_path_rejected(tmp_path, manifest):
    path = _write(tmp_path / "bad\x1b[31m.md", "- [DECISION] I chose A.\n")
    with pytest.raises(cs.ConsumerError):
        ci.collect_candidates([path], "markdown", manifest)


def test_sensitive_basename_is_sanitized_in_receipt_and_basename(tmp_path, manifest):
    sensitive_name = "ghp_" + "Q" * 25 + ".md"
    path = _write(tmp_path / sensitive_name, "- [DECISION] I chose A.\n")
    candidates, receipts, stats = ci.collect_candidates([path], "markdown", manifest)
    assert sensitive_name not in json.dumps((candidates, receipts, stats))
    assert "[REDACTED_SECRET]" in receipts[0]["path"]
    assert candidates[0]["source_file"] == Path(receipts[0]["path"]).name
    assert candidates[0]["evidence"][0]["path"] == receipts[0]["path"]
