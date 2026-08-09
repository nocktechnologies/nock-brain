"""S9: actor-tracked edit history + unique-match replace.

Two guarantees under test. (1) ``unique_replace`` is the safe primitive — an
edit is refused unless its target substring occurs exactly once, so an
ambiguous edit is a retryable error, not silent corruption. (2) every content
edit re-signs the fact (content is inside the signed core) and appends a
who-made-it row to an append-only history, so a human can one-click revert.
"""
import hashlib
import json
import sys

import pytest


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fact(fid, content, kind="decision", **extra):
    f = {
        "id": fid, "kind": kind, "status": "current", "confidence": 0.9,
        "content": content, "source_date": "2026-08-01", "evidence": [],
    }
    f.update(extra)
    return f


def _run(module_main, monkeypatch, argv, name="edit-fact.py"):
    monkeypatch.setattr(sys, "argv", [name] + argv)
    try:
        module_main()
    except SystemExit:
        pass


@pytest.fixture()
def key(sign_lib, tmp_path):
    return sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub",
        alg=sign_lib.ALG_HMAC,
    )


def _use_key(monkeypatch, tmp_path):
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))


# ── unique_replace: the safe primitive ───────────────────────────────────────
def test_unique_replace_happy_path(edit_fact):
    assert edit_fact.unique_replace("the sky is green today", "green", "blue") \
        == "the sky is blue today"


def test_unique_replace_refuses_zero_matches(edit_fact):
    with pytest.raises(ValueError) as exc:
        edit_fact.unique_replace("the sky is green", "purple", "blue")
    assert "0 time(s)" in str(exc.value)


def test_unique_replace_refuses_multiple_matches(edit_fact):
    with pytest.raises(ValueError) as exc:
        edit_fact.unique_replace("go go go", "go", "stop")
    # The message must name the count so the caller knows why to retry.
    assert "3 time(s)" in str(exc.value)


# ── CLI edit: changes content, re-signs, records history ─────────────────────
def test_cli_edit_changes_content_and_resigns(edit_fact, sign_lib, key, tmp_path, monkeypatch):
    facts = [_fact("f1", "the sky is green today")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))
    _use_key(monkeypatch, tmp_path)

    _run(edit_fact.main, monkeypatch,
         ["f1", "--replace", "green", "--with", "blue", "--actor", "agent",
          "--facts", str(store)])

    written = json.loads(store.read_text())
    assert written[0]["content"] == "the sky is blue today"
    assert written[0]["id"] == "f1" and written[0]["kind"] == "decision"  # never mutated
    report = sign_lib.verify_facts(written, key)
    assert report["valid"] == 1 and report["tampered"] == 0


def test_cli_edit_appends_history_row(edit_fact, sign_lib, key, tmp_path, monkeypatch):
    facts = [_fact("f1", "the sky is green today")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))
    _use_key(monkeypatch, tmp_path)

    _run(edit_fact.main, monkeypatch,
         ["f1", "--replace", "green", "--with", "blue", "--actor", "agent",
          "--facts", str(store)])

    rows = edit_fact.load_edits(tmp_path / "fact-edits.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["fact_id"] == "f1"
    assert row["actor"] == "agent"
    assert row["old_sha256"] == _sha("the sky is green today")
    assert row["new_sha256"] == _sha("the sky is blue today")
    assert row["old_excerpt"] == "the sky is green today"
    assert row["new_excerpt"] == "the sky is blue today"


def test_cli_edit_refuses_ambiguous_target(edit_fact, sign_lib, key, tmp_path, monkeypatch):
    """A non-unique target leaves the store and history untouched (exit 1)."""
    facts = [_fact("f1", "go go go")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    before = json.dumps(facts)
    store.write_text(before)
    _use_key(monkeypatch, tmp_path)

    _run(edit_fact.main, monkeypatch,
         ["f1", "--replace", "go", "--with", "stop", "--actor", "agent",
          "--facts", str(store)])

    assert store.read_text() == before
    assert edit_fact.load_edits(tmp_path / "fact-edits.jsonl") == []


# ── revert: the human one-click undo ─────────────────────────────────────────
def test_cli_revert_restores_prior_content(edit_fact, sign_lib, key, tmp_path, monkeypatch):
    facts = [_fact("f1", "the sky is green today")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))
    _use_key(monkeypatch, tmp_path)

    _run(edit_fact.main, monkeypatch,
         ["f1", "--replace", "green", "--with", "blue", "--actor", "agent",
          "--facts", str(store)])
    assert json.loads(store.read_text())[0]["content"] == "the sky is blue today"

    _run(edit_fact.main, monkeypatch, ["--revert", "f1", "--facts", str(store)])

    reverted = json.loads(store.read_text())
    assert reverted[0]["content"] == "the sky is green today"  # restored
    report = sign_lib.verify_facts(reverted, key)
    assert report["valid"] == 1 and report["tampered"] == 0  # re-signed

    rows = edit_fact.load_edits(tmp_path / "fact-edits.jsonl")
    assert len(rows) == 2  # append-only: edit + revert
    assert rows[-1]["actor"] == "human"  # a revert is a human change
    assert rows[-1]["new_excerpt"] == "the sky is green today"


# ── no-key path: still edits + records, warns, leaves UNSIGNED ────────────────
def test_cli_edit_without_key_warns_but_records(edit_fact, tmp_path, monkeypatch, capsys):
    # conftest's autouse fixture already points the signing-key env at missing
    # paths, so resolve_signing_key() returns None (mark-only mode).
    facts = [_fact("f1", "the sky is green today")]
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))

    _run(edit_fact.main, monkeypatch,
         ["f1", "--replace", "green", "--with", "blue", "--actor", "human",
          "--facts", str(store)])

    written = json.loads(store.read_text())
    assert written[0]["content"] == "the sky is blue today"  # edit still lands
    assert "attestation" not in written[0]  # UNSIGNED, not stale/TAMPERED
    assert "unsigned" in capsys.readouterr().err.lower()
    rows = edit_fact.load_edits(tmp_path / "fact-edits.jsonl")
    assert len(rows) == 1 and rows[0]["actor"] == "human"


def test_history_written_before_store_so_crash_preserves_revert(edit_fact, sign_lib, tmp_path, monkeypatch):
    """Mira #48110 durability: if the store write crashes, the edit-history row
    must already exist so --revert still works. Prove ordering by making
    replace_all raise and asserting the history row is on disk."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("f1", "the value is alpha")]))
    edits = tmp_path / "fact-edits.jsonl"  # derived next to the store

    # Make the store write blow up AFTER history should already be written.
    import _storeback
    real = _storeback.JsonStore.replace_all
    def boom(self, facts):
        raise OSError("disk full mid-write")
    monkeypatch.setattr(_storeback.JsonStore, "replace_all", boom)

    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    try:
        edit_fact.main()
    except OSError:
        pass  # the crash we injected

    # The revert trail survived the crash.
    assert edits.exists()
    rows = [json.loads(l) for l in edits.read_text().splitlines()]
    assert rows and rows[-1]["fact_id"] == "f1"
    assert rows[-1]["old_excerpt"] == "the value is alpha"


def test_history_file_created_0600(edit_fact, sign_lib, tmp_path, monkeypatch):
    """CR:100 — the history sidecar (holds content excerpts) must be 0600 from
    creation, never briefly umask-readable."""
    import stat
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("f1", "value alpha here")]))
    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    edit_fact.main()
    edits = tmp_path / "fact-edits.jsonl"
    assert edits.exists()
    assert stat.S_IMODE(edits.stat().st_mode) == 0o600


def test_edit_refuses_when_fact_is_a_parent(edit_fact, sign_lib, tmp_path, monkeypatch):
    """CR:133 — editing a parent would make children parent-suspect; refuse
    until child re-signing exists (0 parent-linked facts today)."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    parent = _fact("p1", "parent content alpha")
    child = _fact("c1", "child content")
    child["attestation"] = {"parent_fact_ids": ["p1"]}
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([parent, child]))
    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "p1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    with pytest.raises(SystemExit) as exc:
        edit_fact.main()
    assert exc.value.code == 1
    # store untouched
    assert json.loads(store.read_text())[0]["content"] == "parent content alpha"


def test_revert_refuses_when_current_changed_out_of_band(edit_fact, sign_lib, tmp_path, monkeypatch):
    """CR:192 — revert must not clobber content that changed since the last
    recorded edit (out-of-band write)."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("f1", "value alpha here")]))
    import sys
    # edit normally
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    edit_fact.main()
    # now mutate the store OUT OF BAND
    facts = json.loads(store.read_text())
    facts[0]["content"] = "value gamma here"
    store.write_text(json.dumps(facts))
    # revert must refuse
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "--revert", "f1", "--facts", str(store)])
    with pytest.raises(SystemExit) as exc:
        edit_fact.main()
    assert exc.value.code == 1
    assert json.loads(store.read_text())[0]["content"] == "value gamma here"  # untouched


def test_store_lock_serializes_writers(edit_fact, tmp_path):
    """CR:234 — the lock is exclusive: a second acquire blocks until release."""
    import _storeback  # noqa: F401 - ensure importable
    store = tmp_path / "facts.json"
    store.write_text("[]")
    lock_a = edit_fact._StoreLock(store)
    lock_b = edit_fact._StoreLock(store)
    import fcntl, os
    with lock_a:
        # non-blocking acquire on B must fail while A holds it
        fd = os.open(str(tmp_path / ".edit-fact.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_edit_refuses_v2_claim_fact(edit_fact, sign_lib, tmp_path, monkeypatch):
    """nock-review HIGH: legacy re-sign of a v2-claim fact yields TAMPERED.
    Refuse instead."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    v2 = _fact("c1", "claim content alpha")
    v2["attestation"] = {"schema": "nock-claim-attestation/v2", "signature": "x"}
    v2["revision_id"] = "rev-1"
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([v2]))
    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "c1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    with pytest.raises(SystemExit) as exc:
        edit_fact.main()
    assert exc.value.code == 1
    assert json.loads(store.read_text())[0]["content"] == "claim content alpha"


def test_edit_refuses_when_load_drops_malformed_sibling(edit_fact, sign_lib, tmp_path, monkeypatch):
    """nock-review MEDIUM: writing back after a lossy load would delete a
    malformed sibling. Refuse."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    good = _fact("f1", "value alpha here")
    malformed = {"id": "f2", "content": "no required fields"}  # missing kind/status/...
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([good, malformed]))
    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    with pytest.raises(SystemExit) as exc:
        edit_fact.main()
    assert exc.value.code == 1
    # both facts still on disk (nothing dropped)
    assert len(json.loads(store.read_text())) == 2


def test_double_revert_refused(edit_fact, sign_lib, tmp_path, monkeypatch):
    """nock-review MEDIUM: a second consecutive revert re-applies the undone
    edit. Refuse."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("f1", "value alpha here")]))
    import sys
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    edit_fact.main()  # content -> beta
    monkeypatch.setattr(sys, "argv", ["edit-fact.py", "--revert", "f1", "--facts", str(store)])
    edit_fact.main()  # back to alpha
    assert json.loads(store.read_text())[0]["content"] == "value alpha here"
    # second revert must refuse (would re-apply beta)
    monkeypatch.setattr(sys, "argv", ["edit-fact.py", "--revert", "f1", "--facts", str(store)])
    with pytest.raises(SystemExit) as exc:
        edit_fact.main()
    assert exc.value.code == 1
    assert json.loads(store.read_text())[0]["content"] == "value alpha here"  # still alpha


def test_failed_revert_is_retryable(edit_fact, sign_lib, tmp_path, monkeypatch):
    """CR:289/363 — a revert whose store write fails must be retryable: the
    revert log is written AFTER the store, so a crash leaves the store
    unchanged and the retry runs cleanly (not stranded by its own log row)."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("f1", "value alpha here")]))
    import sys
    # edit alpha -> beta
    monkeypatch.setattr(sys, "argv",
                        ["edit-fact.py", "f1", "--replace", "alpha", "--with", "beta",
                         "--actor", "agent", "--facts", str(store)])
    edit_fact.main()

    # First revert: store write BLOWS UP after content is prepared.
    import _storeback
    def boom(self, facts):
        raise OSError("disk full mid-revert")
    monkeypatch.setattr(_storeback.JsonStore, "replace_all", boom)
    monkeypatch.setattr(sys, "argv", ["edit-fact.py", "--revert", "f1", "--facts", str(store)])
    try:
        edit_fact.main()
    except OSError:
        pass
    # store still has beta (crash), and NO revert row was appended
    assert json.loads(store.read_text())[0]["content"] == "value beta here"
    edits = tmp_path / "fact-edits.jsonl"
    rows = [json.loads(l) for l in edits.read_text().splitlines()]
    assert not any(r.get("op") == "revert" for r in rows)

    # Retry with a working store write -> revert lands.
    monkeypatch.undo()  # restore real replace_all
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    monkeypatch.setattr(sys, "argv", ["edit-fact.py", "--revert", "f1", "--facts", str(store)])
    edit_fact.main()
    assert json.loads(store.read_text())[0]["content"] == "value alpha here"
