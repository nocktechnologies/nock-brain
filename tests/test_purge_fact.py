"""Tests for hard-deleting sensitive fact material across local stores."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def channel_frame(frame_id, body):
    return (
        '<channel source="plugin:resident-channel">\n'
        f"[BEGIN UNTRUSTED CHANNEL CONTENT #{frame_id}]\n"
        f"{body}\n"
        f"[END UNTRUSTED CHANNEL CONTENT #{frame_id}]\n"
        "</channel>"
    )


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_purge_uses_ingest_channel_frame_regex():
    purge_fact = _load("purge-fact")
    ingest_jsonl = _load("ingest-jsonl")

    assert purge_fact.CHANNEL_FRAME_RE is ingest_jsonl._CHANNEL_FRAME_RE


def test_purge_fact_apply_removes_pattern_from_facts_events_notes_and_vault(tmp_path):
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    (vault / "facts").mkdir(parents=True)

    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-leaky"}],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [{"event_id": "event-keep"}],
        },
    ]))
    events.write_text(
        json.dumps({"id": "event-leaky", "content": "leaked-secret-value"}) + "\n" +
        json.dumps({"id": "event-keep", "content": "safe memory"}) + "\n"
    )
    (notes / "s1.md").write_text("- leaked-secret-value\n- safe memory\n")
    (vault / "facts" / "leaky.md").write_text("leaked-secret-value\n")
    (vault / "facts" / "keep.md").write_text("safe memory\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "removed 1 fact" in result.stdout
    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "leaked-secret-value" not in events.read_text()
    assert "event-keep" in events.read_text()
    assert "leaked-secret-value" not in (notes / "s1.md").read_text()
    assert "safe memory" in (vault / "facts" / "keep.md").read_text()


def test_purge_removes_only_exact_rendered_session_fact_bullets(tmp_path):
    """Source session facts use the refiner's whole bullet, never a prefix."""
    purge_fact = _load("purge-fact")
    refine_sessions = _load("refine-sessions")
    facts = tmp_path / "facts.json"
    notes = tmp_path / "sessions"
    notes.mkdir()
    content = channel_frame(
        "0123456789ab", "[TRUNCATED: original 2000 chars; see session_anchor]"
    )
    removed = {
        "id": "removed", "kind": "directive", "status": "current",
        "confidence": 0.9, "content": content, "source_file": "session.jsonl",
        "source_date": "2026-09-27", "evidence": [{"line": 11}],
    }
    retained = {
        "id": "keep", "kind": "directive", "status": "current",
        "confidence": 0.9, "content": "safe memory", "source_file": "session.jsonl",
        "source_date": "2026-09-27", "evidence": [{"line": 12}],
    }
    facts.write_text(json.dumps([removed, retained]))
    removed_bullet = refine_sessions.render_fact_bullet(removed)
    near_match = removed_bullet.replace("session.jsonl:11", "session.jsonl:99")
    note = notes / "s1.md"
    note.write_text(
        "# Session s1\n\n## Facts\n"
        + removed_bullet + "\n"
        + near_match + "\n"
        + refine_sessions.render_fact_bullet(retained) + "\n"
        + "\n## Evidence Events\n"
        + "- session.jsonl:11 [text/message] <channel source= transcript history\n"
    )

    argv = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(tmp_path / "events.jsonl"), "--notes-dir", str(notes),
        "--vault", str(tmp_path / "vault"), "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    dry_run = subprocess.run(argv, cwd=REPO, text=True, capture_output=True, check=True)
    assert "1 session fact bullet(s)" in dry_run.stdout
    assert note.read_text().count(removed_bullet) == 1

    applied = subprocess.run(
        argv + ["--apply"], cwd=REPO, text=True, capture_output=True, check=True,
    )
    note_text = note.read_text()
    assert "1 session fact bullet(s)" in applied.stdout
    assert removed_bullet not in note_text
    assert near_match in note_text
    assert "[text/message] <channel source= transcript history" in note_text
    assert purge_fact.rendered_session_fact_bullets([removed]) == {removed_bullet}


def test_purge_regenerates_review_and_vault_from_clean_sources(tmp_path):
    """Review and vault copies are regenerated, not hand-edited, after purge."""
    refine_sessions = _load("refine-sessions")
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    review = tmp_path / "review"
    vault = tmp_path / "vault"
    notes.mkdir()
    review.mkdir()
    vault.mkdir()
    content = '<channel source="plugin:resident-channel"> stale wrapper'
    removed = {
        "id": "removed", "kind": "directive", "status": "current",
        "confidence": 0.9, "content": content, "source_file": "session.jsonl",
        "source_date": "2026-09-27", "evidence": [{"line": 11}],
    }
    kept = {
        "id": "keep", "kind": "directive", "status": "current",
        "confidence": 0.9, "content": "Kevin kept safe memory", "source_file": "session.jsonl",
        "source_date": "2026-09-27", "evidence": [{"line": 12}],
    }
    facts.write_text(json.dumps([removed, kept]))
    events.write_text("")
    (notes / "s1.md").write_text(
        "# Session s1\n\n## Facts\n"
        + refine_sessions.render_fact_bullet(removed) + "\n"
        + refine_sessions.render_fact_bullet(kept) + "\n\n## Evidence Events\n"
    )
    old_review = "# Stale review\n" + content + "\n"
    (review / "promotion-candidates.md").write_text(old_review)
    (review / "contradiction-candidates.md").write_text(old_review)
    index = vault / "index.md"
    index.write_text("# Existing index\n")

    purge = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(events), "--notes-dir", str(notes), "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"), "--apply",
    ]
    subprocess.run(purge, cwd=REPO, text=True, capture_output=True, check=True)

    assert content not in (notes / "s1.md").read_text()
    assert (review / "promotion-candidates.md").read_text() == old_review
    assert (review / "contradiction-candidates.md").read_text() == old_review
    assert index.read_text() == "# Existing index\n"

    subprocess.run(
        [sys.executable, str(REPO / "bin" / "review-promotions.py"),
         "--facts", str(facts), "--output", str(review)],
        cwd=REPO, text=True, capture_output=True, check=True,
    )
    subprocess.run(
        [sys.executable, str(REPO / "bin" / "detect-contradictions.py"),
         "--facts", str(facts), "--queue-dir", str(review)],
        cwd=REPO, text=True, capture_output=True, check=True,
    )
    subprocess.run(
        [sys.executable, str(REPO / "bin" / "export-obsidian.py"),
         "--facts", str(facts), "--sessions", str(notes), "--review", str(review),
         "--vault", str(vault)],
        cwd=REPO, text=True, capture_output=True, check=True,
    )

    for path in [
        review / "promotion-candidates.json", review / "promotion-candidates.md",
        review / "contradiction-candidates.json", review / "contradiction-candidates.md",
        vault / "sessions" / "s1.md", vault / "review" / "promotion-candidates.md",
        vault / "review" / "contradiction-candidates.md",
    ]:
        assert content not in path.read_text()


def test_purge_content_prefix_only_matches_lstripped_fact_content(tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {
            "id": "wrapper", "kind": "decision", "status": "current",
            "confidence": 0.9,
            "content": " \n<channel source=\"resident-channel\">receipt",
            "source_date": "2026-09-27", "evidence": [],
        },
        {
            "id": "mention", "kind": "decision", "status": "current",
            "confidence": 0.9,
            "content": "A note that mentions <channel source= as an example.",
            "source_date": "2026-09-27", "evidence": [],
        },
        {
            "id": "<channel source=not-content", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": "A safe fact with a marker-like id.",
            "source_date": "2026-09-27", "evidence": [],
        },
    ]))
    command = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(tmp_path / "sessions"),
        "--vault", str(tmp_path / "vault"),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]

    dry_run = subprocess.run(command, cwd=REPO, text=True, capture_output=True,
                             check=True)
    assert "would remove 1 fact" in dry_run.stdout
    assert [fact["id"] for fact in json.loads(facts.read_text())] == [
        "wrapper", "mention", "<channel source=not-content",
    ]

    subprocess.run(command + ["--apply"], cwd=REPO, text=True,
                   capture_output=True, check=True)
    assert [fact["id"] for fact in json.loads(facts.read_text())] == [
        "mention", "<channel source=not-content",
    ]


def test_purge_content_prefix_removes_multiline_wrapper_copies(tmp_path):
    """Prefix purges must remove whole wrapper copies without event evidence."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    wrapper = channel_frame("a1b2c3d4e5f6", "Historical wrapper text")
    facts.write_text(json.dumps([
        {
            "id": "wrapper", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": wrapper,
            "source_date": "2026-09-27", "evidence": [],
        },
        {
            "id": "keep", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": "Safe memory",
            "source_date": "2026-09-27", "evidence": [],
        },
    ]))
    events.write_text(
        json.dumps({"id": "historical-wrapper", "content": wrapper}) + "\n"
        + json.dumps({"id": "keep", "content": "Safe event"}) + "\n"
    )
    (notes / "session.md").write_text(
        "Session notes\n"
        + wrapper + "\n"
        + "This sentence mentions <channel source= without being a wrapper.\n"
    )
    (vault / "wrapper.md").write_text(wrapper + "\n")

    subprocess.run(
        [
            sys.executable, str(REPO / "bin" / "purge-fact.py"),
            "--content-prefix", "<channel source=",
            "--facts", str(facts), "--events", str(events),
            "--notes-dir", str(notes), "--vault", str(vault),
            "--sidecar", str(tmp_path / "embeddings.npz"), "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "Historical wrapper text" not in events.read_text()
    assert "Safe event" in events.read_text()
    assert "Historical wrapper text" not in (notes / "session.md").read_text()
    assert "This sentence mentions <channel source=" in (notes / "session.md").read_text()
    assert "Historical wrapper text" not in (vault / "wrapper.md").read_text()


def test_purge_removes_vault_fact_mirrors_by_frontmatter_id(tmp_path):
    """Truncated per-fact vault mirrors are selected by their frontmatter id."""
    facts = tmp_path / "facts.json"
    vault = tmp_path / "vault"
    facts_dir = vault / "facts"
    facts_dir.mkdir(parents=True)
    (vault / "agents").mkdir()
    (vault / "decisions").mkdir()
    truncated_wrapper = (
        '<channel source="plugin:resident-channel">\n'
        "[BEGIN UNTRUSTED CHANNEL CONTENT #0123456789ab]\n"
        "[TRUNCATED: original 2000 chars; see session_anchor]\n"
    )
    facts.write_text(json.dumps([
        {
            "id": "removed", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": truncated_wrapper,
            "source_date": "2026-09-27", "evidence": [],
        },
        {
            "id": "keep", "kind": "decision", "status": "current",
            "confidence": 0.9, "content": "safe memory",
            "source_date": "2026-09-27", "evidence": [],
        },
    ]))
    removed_mirror = facts_dir / "2026-09-27-removed.md"
    removed_mirror.write_text(
        "\ufeff---\nid: \"removed\" # mirrored fact id\n---\n\n" + truncated_wrapper,
        encoding="utf-8",
    )
    removed_decision_mirror = vault / "decisions" / "2026-09-27-decision-removed.md"
    removed_decision_mirror.write_text(
        "---\nid: removed\n---\n\n" + truncated_wrapper,
    )
    kept_mirror = facts_dir / "2026-09-27-keep.md"
    kept_mirror_text = "---\nid: keep\n---\n\n" + truncated_wrapper
    kept_mirror.write_text(kept_mirror_text)
    filename_only_mirror = facts_dir / "2026-09-27-removed-copy.md"
    filename_only_text = "---\nid: other\n---\n\n" + truncated_wrapper
    filename_only_mirror.write_text(filename_only_text)
    kept_decision_mirror = vault / "decisions" / "2026-09-27-keep.md"
    kept_decision_text = "---\nid: keep\n---\n\nA retained decision mirror.\n"
    kept_decision_mirror.write_text(kept_decision_text)
    index = vault / "index.md"
    index_text = "---\nid: removed\n---\n\nVault index must remain.\n"
    index.write_text(index_text)
    invalid_utf8_mirror = facts_dir / "2026-09-27-invalid.md"
    invalid_utf8_mirror.write_bytes(b"---\nid: removed\n---\n\xff")
    agent_note = vault / "agents" / "mira.md"
    agent_note.write_text(
        "## Mentioned in\n"
        "- [[2026-09-27-removed]]\n"
        "- [[facts/2026-09-27-removed]]\n"
        "- [[decisions/2026-09-27-decision-removed]]\n"
        "- [[2026-09-27-keep]]\n"
    )
    decision_note = vault / "decisions" / "removed.md"
    decision_note.write_text(
        "See [[2026-09-27-removed]] for the full fact note.\n"
        "Keep this decision-note context.\n"
    )
    prose_note = vault / "notes.md"
    prose_text = "Important context; see [[facts/2026-09-27-removed]] for evidence.\n"
    prose_note.write_text(prose_text)
    (vault / "review").mkdir()
    unrelated_note = vault / "review" / "unrelated.md"
    unrelated_text = (
        "- [[2026-09-27-removed]]\n"
        "- [[decisions/2026-09-27-decision-removed]]\n"
    )
    unrelated_note.write_text(unrelated_text)

    argv = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(tmp_path / "sessions"), "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    dry_run = subprocess.run(
        argv, cwd=REPO, text=True, capture_output=True, check=True,
    )

    assert "would remove 1 fact" in dry_run.stdout
    assert "2 vault mirror file(s)" in dry_run.stdout
    assert "0 unmatched opener(s)" in dry_run.stdout
    assert removed_mirror.exists(), "dry-run must not delete the vault mirror"

    applied = subprocess.run(
        argv + ["--apply"], cwd=REPO, text=True, capture_output=True, check=True,
    )

    assert not removed_mirror.exists()
    assert not removed_decision_mirror.exists()
    assert kept_mirror.exists()
    assert kept_mirror.read_text() == kept_mirror_text
    assert filename_only_mirror.read_text() == filename_only_text
    assert kept_decision_mirror.read_text() == kept_decision_text
    assert index.read_text() == index_text
    assert invalid_utf8_mirror.exists()
    assert "2026-09-27-removed" not in agent_note.read_text()
    assert "2026-09-27-decision-removed" not in agent_note.read_text()
    assert "2026-09-27-keep" in agent_note.read_text()
    assert "2026-09-27-removed" not in decision_note.read_text()
    assert "Keep this decision-note context." in decision_note.read_text()
    assert prose_note.read_text() == prose_text
    assert unrelated_note.read_text() == "- [[2026-09-27-removed]]\n"
    assert "2 vault mirror file(s)" in applied.stdout


def test_purge_content_prefix_removes_complete_single_line_note_wrapper(tmp_path):
    facts = tmp_path / "facts.json"
    notes = tmp_path / "sessions"
    notes.mkdir()
    facts.write_text("[]")
    wrapper = channel_frame("0123456789ab", "single-line wrapper").replace("\n", " ")
    note = notes / "session.md"
    note.write_text(f"Before {wrapper} after\n")

    command = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(notes), "--vault", str(tmp_path / "vault"),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    dry_run = subprocess.run(
        command, cwd=REPO, text=True, capture_output=True, check=True,
    )
    assert "1 note block" in dry_run.stdout
    assert note.read_text() == f"Before {wrapper} after\n"

    result = subprocess.run(
        command + ["--apply"], cwd=REPO, text=True, capture_output=True, check=True,
    )

    assert note.read_text() == "Before  after\n"
    assert "1 note block" in result.stdout


def test_purge_keeps_unclosed_opener_and_prose_before_later_complete_frame(tmp_path):
    facts = tmp_path / "facts.json"
    notes = tmp_path / "sessions"
    notes.mkdir()
    facts.write_text("[]")
    note = notes / "session.md"
    note.write_text(
        '<channel source="plugin:resident-channel">\n'
        "Legitimate prose must survive.\n"
        + channel_frame("abcdef123456", "remove only this block")
        + "\nTrailing prose must survive.\n"
    )

    command = [
        sys.executable, str(REPO / "bin" / "purge-fact.py"),
        "--content-prefix", "<channel source=", "--facts", str(facts),
        "--events", str(tmp_path / "events.jsonl"),
        "--notes-dir", str(notes), "--vault", str(tmp_path / "vault"),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    dry_run = subprocess.run(
        command, cwd=REPO, text=True, capture_output=True, check=True,
    )
    assert "1 unmatched opener" in dry_run.stdout
    assert str(note) in dry_run.stdout
    assert "remove only this block" in note.read_text()

    result = subprocess.run(
        command + ["--apply"], cwd=REPO, text=True, capture_output=True, check=True,
    )

    text = note.read_text()
    assert '<channel source="plugin:resident-channel">' in text
    assert "Legitimate prose must survive." in text
    assert "remove only this block" not in text
    assert "Trailing prose must survive." in text
    assert "1 unmatched opener" in result.stdout
    assert str(note) in result.stdout


def test_purge_does_not_cross_close_channel_frames_with_different_ids(tmp_path):
    facts = tmp_path / "facts.json"
    notes = tmp_path / "sessions"
    notes.mkdir()
    facts.write_text("[]")
    note = notes / "session.md"
    note.write_text(
        '<channel source="plugin:resident-channel">\n'
        "[BEGIN UNTRUSTED CHANNEL CONTENT #111111111111]\n"
        "Unclosed first frame.\n"
        "[END UNTRUSTED CHANNEL CONTENT #222222222222]\n"
        "</channel>\n"
        + channel_frame("222222222222", "remove only second frame")
        + "\n"
    )

    subprocess.run(
        [
            sys.executable, str(REPO / "bin" / "purge-fact.py"),
            "--content-prefix", "<channel source=", "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(notes), "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"), "--apply",
        ],
        cwd=REPO, text=True, capture_output=True, check=True,
    )

    text = note.read_text()
    assert "Unclosed first frame." in text
    assert "remove only second frame" not in text


def test_purge_keeps_mid_sentence_channel_source_mention(tmp_path):
    facts = tmp_path / "facts.json"
    notes = tmp_path / "sessions"
    notes.mkdir()
    facts.write_text("[]")
    note = notes / "session.md"
    mention = "This prose mentions '<channel source=' mid-sentence.\n"
    note.write_text(mention)

    result = subprocess.run(
        [
            sys.executable, str(REPO / "bin" / "purge-fact.py"),
            "--content-prefix", "<channel source=", "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(notes), "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"), "--apply",
        ],
        cwd=REPO, text=True, capture_output=True, check=True,
    )

    assert note.read_text() == mention
    assert "0 note block" in result.stdout


def test_purge_apply_unlinks_verified_cache_sidecar(tmp_path):
    """Issue #52: purge-fact must remove facts.json.verified-cache.json.
    Digests are opaque, so the whole sidecar goes; dry-run leaves it."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    sidecar = tmp_path / "embeddings.npz"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    cache = facts.with_name(facts.name + ".verified-cache.json")
    cache.write_text('{"version": 2, "digests": []}', encoding="utf-8")
    cache.chmod(0o600)

    argv = [
        sys.executable,
        str(REPO / "bin" / "purge-fact.py"),
        "--pattern", "leaked-secret-value",
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(sidecar),
    ]
    dry = subprocess.run(
        argv,
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert cache.exists(), "dry-run must not unlink the verification cache"
    assert "would delete verification cache" in dry.stderr

    applied = subprocess.run(
        argv + ["--apply"],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert not cache.exists(), "apply must unlink the verification cache sidecar"
    assert "deleted verification cache" in applied.stderr


def test_purge_without_matches_leaves_verified_cache(tmp_path):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    cache = facts.with_name(facts.name + ".verified-cache.json")
    cache.write_text("{}", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "no-such-secret",
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert cache.exists(), "a no-op purge must not drop the verification cache"


def test_stale_cache_save_during_purge_cannot_revive_purged_digest(
        tmp_path, monkeypatch):
    """A concurrent recall that loaded the OLD store can save() after the
    sidecar is unlinked but before facts.json is rewritten. Its stamp still
    matches, so save() would recreate a digest for the fact being purged.
    Rewrite the store first so the stamp has moved before that save can land.
    """
    purge_fact = _load("purge-fact")
    vc = sys.modules["_verify_cache"]

    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")

    class _DummyKey:
        key_id = "k"
        alg = "ed25519"

    cache = vc.cache_path_for(facts)
    purged_digest = "digest-of-purged-leaky-fact"
    cache.write_text(json.dumps({
        "version": vc.CACHE_VERSION,
        "alg": "ed25519",
        "key_id": "k",
        "store": vc._store_sig(facts),
        "digests": [purged_digest],
    }), encoding="utf-8")
    cache.chmod(0o600)

    stale = vc.load_for_store(facts, _DummyKey())
    assert stale is not None
    assert stale.hit(purged_digest)
    # A concurrent recall that proved any signature this run is dirty;
    # without that, save() is a no-op and cannot recreate the sidecar.
    stale.add("digest-verified-this-recall")

    real_unlink = purge_fact.unlink_for_store

    def unlink_then_concurrent_save(store_path):
        result = real_unlink(store_path)
        stale.save()
        return result

    monkeypatch.setattr(purge_fact, "unlink_for_store", unlink_then_concurrent_save)

    assert purge_fact.run([
        "leaky",
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"),
        "--apply",
    ]) == 0
    stale.save()

    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert not cache.exists(), (
        "a stale cache save must not recreate the sidecar with a purged digest"
    )


def test_purge_apply_sweeps_stale_cache_tmp_without_sidecar(tmp_path):
    """An interrupted cache write can leave `{sidecar}.*.tmp` with no sidecar.
    Applied matching purges must still call unlink_for_store so the sweep runs.
    """
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")

    sidecar = facts.with_name(facts.name + ".verified-cache.json")
    leftover = tmp_path / (sidecar.name + ".interrupted.tmp")
    leftover.write_text("stale tmp from interrupted cache write", encoding="utf-8")
    # _sweep_stale_tmps leaves files younger than STALE_TMP_AGE_SEC (60s)
    # as a concurrent-writer guard; age this leftover past that cutoff.
    old = time.time() - 90
    os.utime(leftover, (old, old))
    assert not sidecar.exists()

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert not leftover.exists(), (
        "applied matching purge must sweep leftover cache tmp files"
    )
    assert [fact["id"] for fact in json.loads(facts.read_text())] == ["keep"]
    assert "removed 1 fact" in result.stdout
    assert "verification cache" not in result.stderr


def test_purge_apply_scrubs_insights_and_graph(tmp_path):
    """N10019: derived views must not keep injecting purged content."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "leaky",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin removed leaked-secret-value from memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    (tmp_path / "insights.json").write_text(json.dumps([
        {
            "id": "ins-leaky",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "content": "recurring leaked-secret-value",
            "source_date": "2026-06-12",
            "source_ids": ["leaky"],
        },
        {
            "id": "ins-keep",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "content": "recurring safe memory",
            "source_date": "2026-06-12",
            "source_ids": ["keep"],
        },
    ]))
    (tmp_path / "graph.json").write_text(json.dumps({
        "nodes": [
            {"id": "fact:leaky", "type": "fact", "label": "leaked-secret-value"},
            {"id": "fact:keep", "type": "fact", "label": "safe memory"},
        ],
        "edges": [
            {"id": "e1", "source": "fact:leaky", "target": "concept:secret"},
            {"id": "e2", "source": "fact:keep", "target": "concept:safe"},
        ],
    }))

    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "leaked-secret-value",
            "--facts", str(facts),
            "--events", str(events),
            "--notes-dir", str(notes),
            "--vault", str(vault),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )

    insights = json.loads((tmp_path / "insights.json").read_text())
    assert [item["id"] for item in insights] == ["ins-keep"]
    graph = json.loads((tmp_path / "graph.json").read_text())
    assert [node["id"] for node in graph["nodes"]] == ["fact:keep"]
    assert [edge["id"] for edge in graph["edges"]] == ["e2"]
    tombstones = (tmp_path / "purged-ids.jsonl").read_text()
    assert "leaky" in tombstones


def test_purge_zero_match_does_not_rewrite_store(tmp_path):
    """N10028: a no-op apply must not drop loader-skipped malformed records."""
    facts = tmp_path / "facts.json"
    raw = json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
        "not-an-object",
    ])
    facts.write_text(raw)
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", "no-such-secret",
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert facts.read_text() == raw


def test_purge_pattern_does_not_match_signature_hex(tmp_path):
    """N10028: pattern match must not search attestation signature hex."""
    unique_sig = "cafebabe" * 8
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
            "attestation": {"signature": unique_sig},
        },
    ]))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "vault").mkdir()
    subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "purge-fact.py"),
            "--pattern", unique_sig,
            "--facts", str(facts),
            "--events", str(tmp_path / "events.jsonl"),
            "--notes-dir", str(tmp_path / "sessions"),
            "--vault", str(tmp_path / "vault"),
            "--sidecar", str(tmp_path / "embeddings.npz"),
            "--apply",
        ],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(facts.read_text())[0]["id"] == "keep"



def test_purge_sweeps_contaminated_insight_and_reports_counts(tmp_path):
    """N10052 residual: contaminated clusters quote a leaked judge-prompt fact
    verbatim in content's "Most recent:" excerpt while citing already-absent
    facts — and the summary line printed no insight count, so whether the
    match fired was invisible in receipts. Locks in: the content match sweeps
    the contaminated shape (keyword-only themes never match), counts are
    reported, and a zero-fact-match apply still leaves facts.json alone."""
    facts = tmp_path / "facts.json"
    events = tmp_path / "events.jsonl"
    notes = tmp_path / "sessions"
    vault = tmp_path / "vault"
    notes.mkdir()
    vault.mkdir()
    facts.write_text(json.dumps([
        {
            "id": "keep",
            "kind": "decision",
            "status": "current",
            "confidence": 0.9,
            "content": "Kevin kept safe memory",
            "source_date": "2026-06-12",
            "evidence": [],
        },
    ]))
    events.write_text("")
    template = "Two memory facts from the same project, EARLIER then LATER."
    (tmp_path / "insights.json").write_text(json.dumps([
        {
            # the real N10052 shape: mostly-genuine cluster whose latest
            # member was a leaked prompt fact, quoted verbatim in content
            "id": "ins-contaminated",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "theme": "directive, recurring, lesson, sentence, clean",
            "content": f"Recurring directive (seen 33x): directive, recurring. "
                       f"Most recent: {template}",
            "source_date": "2026-08-01",
            "source_ids": ["already-purged-fact"],
        },
        {
            # keyword-coincidence theme, clean content: must survive
            "id": "ins-keep",
            "kind": "insight",
            "status": "current",
            "confidence": 0.9,
            "theme": "memory, facts, project, earlier, later",
            "content": "recurring safe memory",
            "source_date": "2026-06-12",
            "source_ids": ["keep"],
        },
    ]))

    argv = [
        sys.executable,
        str(REPO / "bin" / "purge-fact.py"),
        "--pattern", template,
        "--facts", str(facts),
        "--events", str(events),
        "--notes-dir", str(notes),
        "--vault", str(vault),
        "--sidecar", str(tmp_path / "embeddings.npz"),
    ]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    dry = subprocess.run(argv, cwd=REPO, env=env, text=True,
                         capture_output=True, check=True)
    assert "1 insight(s)" in dry.stdout
    # dry-run must not touch the file
    assert len(json.loads((tmp_path / "insights.json").read_text())) == 2

    wet = subprocess.run(argv + ["--apply"], cwd=REPO, env=env, text=True,
                         capture_output=True, check=True)
    assert "1 insight(s)" in wet.stdout
    insights = json.loads((tmp_path / "insights.json").read_text())
    assert [item["id"] for item in insights] == ["ins-keep"]
    # zero fact matches: the fact store must be untouched (N10028)
    assert [f["id"] for f in json.loads(facts.read_text())] == ["keep"]
