"""S1: signed revocation events. The attestation signs a fact's immutable
core; lifecycle marks are deliberately outside it — which means a superseded
fact could be silently resurrected by editing status back. These tests pin
the fix: supersession itself becomes a signed, append-only event, and
verification detects (a) tampered events, (b) resurrection — a live fact
that a valid revocation says should be dead."""
import json

import pytest

SUP_AT = "2026-08-08T12:00:00+00:00"


def _fact(fid, status="current", **extra):
    f = {
        "id": fid, "kind": "decision", "status": status, "confidence": 0.9,
        "content": f"decision {fid}", "source_date": "2026-08-01", "evidence": [],
    }
    f.update(extra)
    return f


@pytest.fixture()
def key(sign_lib, tmp_path):
    return sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub",
        alg=sign_lib.ALG_HMAC,
    )


# ── event sign/verify roundtrip ──────────────────────────────────────────────
def test_sign_and_verify_roundtrip(revoke_lib, key):
    event = revoke_lib.sign_revocation(
        key, superseded_id="old", superseding_id="new",
        reason="direction changed", superseded_at=SUP_AT,
    )
    assert event["schema"] == "nockbrain-revocation/v1"
    assert event["superseded_id"] == "old"
    assert event["superseding_id"] == "new"
    assert revoke_lib.verify_revocation(event, key) is True


def test_tampered_event_fails_verification(revoke_lib, key):
    event = revoke_lib.sign_revocation(
        key, superseded_id="old", superseding_id="new",
        reason="r", superseded_at=SUP_AT,
    )
    for field, value in (("superseded_id", "other"), ("superseding_id", "x"),
                         ("reason", "edited"), ("superseded_at", "2020-01-01")):
        tampered = dict(event, **{field: value})
        assert revoke_lib.verify_revocation(tampered, key) is False, field


def test_unsigned_or_garbage_event_fails(revoke_lib, key):
    assert revoke_lib.verify_revocation({}, key) is False
    assert revoke_lib.verify_revocation({"schema": "x", "signature": "zz"}, key) is False


# ── append-only sidecar ──────────────────────────────────────────────────────
def test_append_and_load_roundtrip(revoke_lib, key, tmp_path):
    path = tmp_path / "revocations.jsonl"
    e1 = revoke_lib.sign_revocation(key, superseded_id="a", superseding_id="b",
                                    reason="r1", superseded_at=SUP_AT)
    e2 = revoke_lib.sign_revocation(key, superseded_id="c", superseding_id="",
                                    reason="", superseded_at=SUP_AT)
    revoke_lib.append_revocation(path, e1)
    revoke_lib.append_revocation(path, e2)
    loaded = revoke_lib.load_revocations(path)
    assert [e["superseded_id"] for e in loaded] == ["a", "c"]


def test_load_skips_malformed_lines(revoke_lib, key, tmp_path):
    path = tmp_path / "revocations.jsonl"
    revoke_lib.append_revocation(
        path, revoke_lib.sign_revocation(key, superseded_id="a",
                                         superseding_id="", reason="",
                                         superseded_at=SUP_AT))
    with open(path, "a") as f:
        f.write("not json\n")
    assert len(revoke_lib.load_revocations(path)) == 1


def test_load_missing_file_is_empty(revoke_lib, tmp_path):
    assert revoke_lib.load_revocations(tmp_path / "nope.jsonl") == []


# ── the audit: resurrection detection is the point ───────────────────────────
def test_audit_clean_store(revoke_lib, key):
    event = revoke_lib.sign_revocation(key, superseded_id="old",
                                       superseding_id="new", reason="r",
                                       superseded_at=SUP_AT)
    facts = [_fact("old", status="superseded", superseded_by="new"), _fact("new")]
    report = revoke_lib.audit(facts, [event], key)
    assert report["attested"] == 1
    assert report["resurrected"] == []
    assert report["unattested_superseded"] == []
    assert report["invalid_events"] == 0


def test_audit_detects_resurrection(revoke_lib, key):
    """The attack S1 exists for: status flipped back to current while a valid
    signed revocation says the fact is dead."""
    event = revoke_lib.sign_revocation(key, superseded_id="old",
                                       superseding_id="new", reason="r",
                                       superseded_at=SUP_AT)
    facts = [_fact("old", status="current"), _fact("new")]
    report = revoke_lib.audit(facts, [event], key)
    assert report["resurrected"] == ["old"]


def test_audit_purged_fact_is_not_resurrection(revoke_lib, key):
    event = revoke_lib.sign_revocation(key, superseded_id="gone",
                                       superseding_id="", reason="purged",
                                       superseded_at=SUP_AT)
    assert revoke_lib.audit([], [event], key)["resurrected"] == []


def test_audit_flags_legacy_unattested_supersession(revoke_lib, key):
    facts = [_fact("old", status="superseded", superseded_by="new")]
    report = revoke_lib.audit(facts, [], key)
    assert report["unattested_superseded"] == ["old"]


def test_audit_counts_invalid_events(revoke_lib, key):
    event = revoke_lib.sign_revocation(key, superseded_id="a",
                                       superseding_id="", reason="",
                                       superseded_at=SUP_AT)
    report = revoke_lib.audit([], [dict(event, reason="edited")], key)
    assert report["invalid_events"] == 1


# ── writers append signed events ─────────────────────────────────────────────
def _run(module_main, monkeypatch, argv, name="tool"):
    import sys
    monkeypatch.setattr(sys, "argv", [name] + argv)
    try:
        module_main()
    except SystemExit:
        pass


def test_supersede_fact_appends_signed_event(revoke_lib, sign_lib, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "supersede_fact", P(__file__).resolve().parent.parent / "bin" / "supersede-fact.py")
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)

    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("old"), _fact("new")]))

    _run(sf.main, monkeypatch,
         ["old", "--by", "new", "--reason", "changed", "--facts", str(store)],
         name="supersede-fact.py")

    events = revoke_lib.load_revocations(tmp_path / "revocations.jsonl")
    assert len(events) == 1
    assert events[0]["superseded_id"] == "old"
    assert events[0]["superseding_id"] == "new"
    assert revoke_lib.verify_revocation(events[0], key) is True


def test_dedup_apply_appends_signed_events(revoke_lib, sign_lib, dedup_facts, tmp_path, monkeypatch):
    import sys
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    dupes = [
        _fact("a", content="every nock for mara must include a surface line"),
        _fact("b", content="every nock for mara must include a surface line."),
    ]
    for f in dupes:
        f["content"] = f.pop("content")
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(dupes))

    monkeypatch.setattr(sys, "argv",
                        ["dedup-facts.py", "--facts", str(store), "--apply"])
    try:
        dedup_facts.main()
    except SystemExit:
        pass

    events = revoke_lib.load_revocations(tmp_path / "revocations.jsonl")
    assert len(events) == 1
    assert revoke_lib.verify_revocation(events[0], key) is True
    assert "dedup" in events[0]["reason"]


def test_supersede_without_key_still_marks_but_warns(revoke_lib, tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "supersede_fact2", P(__file__).resolve().parent.parent / "bin" / "supersede-fact.py")
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    # conftest autouse fixture already points signing-key env at missing paths
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("old")]))

    _run(sf.main, monkeypatch, ["old", "--facts", str(store)],
         name="supersede-fact.py")

    facts = json.loads(store.read_text())
    assert facts[0]["status"] == "superseded"  # marking never blocked
    assert "unsigned" in capsys.readouterr().err.lower()
    assert revoke_lib.load_revocations(tmp_path / "revocations.jsonl") == []


# ── verify-facts CLI: resurrection is a hard failure ─────────────────────────
def test_verify_cli_fails_on_resurrection(revoke_lib, sign_lib, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "verify_facts_cli", P(__file__).resolve().parent.parent / "bin" / "verify-facts.py")
    vf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vf)

    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    facts = [_fact("old", status="superseded", superseded_by="new"), _fact("new")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))
    revoke_lib.append_revocation(
        tmp_path / "revocations.jsonl",
        revoke_lib.sign_revocation(key, superseded_id="old", superseding_id="new",
                                   reason="r", superseded_at=SUP_AT))

    clean = vf.run(["--facts", str(store), "--pub", str(tmp_path / "signing-key.pub")])
    assert clean == 0

    # The attack: peel the sticker — flip status back and re-verify.
    facts[0]["status"] = "current"
    store.write_text(json.dumps(facts))
    resurrected = vf.run(["--facts", str(store), "--pub", str(tmp_path / "signing-key.pub")])
    assert resurrected == 4


def test_verify_cli_strict_revocations_flags_legacy_marks(revoke_lib, sign_lib, tmp_path):
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "verify_facts_cli2", P(__file__).resolve().parent.parent / "bin" / "verify-facts.py")
    vf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vf)

    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    facts = [_fact("old", status="superseded")]
    sign_lib.sign_facts(facts, key)
    store = tmp_path / "facts.json"
    store.write_text(json.dumps(facts))

    default = vf.run(["--facts", str(store), "--pub", str(tmp_path / "signing-key.pub")])
    strict = vf.run(["--facts", str(store), "--pub", str(tmp_path / "signing-key.pub"),
                     "--strict-revocations"])
    assert default == 0   # legacy marks warn, don't fail
    assert strict == 5


# ── gate findings (Mira #47932 + CodeRabbit, converged) ──────────────────────
def test_strict_revocations_without_key_fails_loudly(tmp_path, capsys):
    """Thread 1: --strict-revocations with no loadable key must NOT silently
    pass — a nightly job would believe it is enforcing revocations while
    checking nothing."""
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "verify_facts_cli3", P(__file__).resolve().parent.parent / "bin" / "verify-facts.py")
    vf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vf)

    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("a")]))

    code = vf.run(["--facts", str(store), "--pub", str(tmp_path / "missing.pub"),
                   "--strict-revocations"])
    assert code == 5  # exact code, not just non-zero (CodeRabbit :286)
    assert "revocation" in capsys.readouterr().err.lower()


def test_signed_keyid_blocks_relabel_evasion(revoke_lib, sign_lib, tmp_path):
    """The evasion Mira + CodeRabbit found: key_id/alg were classification
    inputs but NOT signed, so a valid event could be relabeled 'foreign' to
    dodge resurrection detection. Now they are signed — any relabel breaks the
    signature, verifies under NO key -> invalid (exit-4 class), never foreign."""
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    event = revoke_lib.sign_revocation(key, superseded_id="x", superseding_id="",
                                       reason="", superseded_at=SUP_AT)
    assert revoke_lib.verify_revocation(event, key) is True
    for field, value in (("key_id", "hmac-sha256:deadbeefdeadbeef"),
                         ("alg", "ed25519")):
        forged = dict(event, **{field: value})
        assert revoke_lib.verify_revocation(forged, key) is False, field
        report = revoke_lib.audit([], [forged], key)
        assert report["invalid_events"] == 1, field
        assert report["foreign_key_events"] == 0, field


def test_rotation_benign_only_with_retired_key_in_ring(revoke_lib, sign_lib, tmp_path):
    """Rotation is benign ONLY when the retired public key is supplied: then
    the old event verifies (foreign, counted, still trusted for resurrection).
    Without it the event verifies under no key -> invalid, which is correct —
    an unverifiable event is never trusted-benign by its label alone."""
    old_key = sign_lib.load_or_create_key(
        tmp_path / "old-key", tmp_path / "old-key.pub", alg=sign_lib.ALG_HMAC)
    new_key = sign_lib.load_or_create_key(
        tmp_path / "new-key", tmp_path / "new-key.pub", alg=sign_lib.ALG_HMAC)
    old_event = revoke_lib.sign_revocation(
        old_key, superseded_id="x", superseding_id="", reason="",
        superseded_at=SUP_AT)

    without_ring = revoke_lib.audit([], [old_event], new_key)
    assert without_ring["invalid_events"] == 1
    assert without_ring["foreign_key_events"] == 0

    with_ring = revoke_lib.audit([], [old_event], new_key, retired_keys=[old_key])
    assert with_ring["foreign_key_events"] == 1
    assert with_ring["invalid_events"] == 0


def test_retired_key_revocation_still_catches_resurrection(revoke_lib, sign_lib, tmp_path):
    """A genuine old-key revocation is still a real revocation: if its fact is
    flipped live, that is resurrection even though it was signed pre-rotation."""
    old_key = sign_lib.load_or_create_key(
        tmp_path / "old-key", tmp_path / "old-key.pub", alg=sign_lib.ALG_HMAC)
    new_key = sign_lib.load_or_create_key(
        tmp_path / "new-key", tmp_path / "new-key.pub", alg=sign_lib.ALG_HMAC)
    old_event = revoke_lib.sign_revocation(
        old_key, superseded_id="x", superseding_id="", reason="",
        superseded_at=SUP_AT)
    facts = [_fact("x", status="current")]
    report = revoke_lib.audit(facts, [old_event], new_key, retired_keys=[old_key])
    assert report["resurrected"] == ["x"]


def test_tampered_current_key_event_is_tampering(revoke_lib, sign_lib, tmp_path):
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    event = revoke_lib.sign_revocation(key, superseded_id="y", superseding_id="",
                                       reason="", superseded_at=SUP_AT)
    report = revoke_lib.audit([], [dict(event, reason="edited")], key)
    assert report["invalid_events"] == 1
    assert report["foreign_key_events"] == 0


def test_unattributable_garbage_is_invalid_never_foreign(revoke_lib, sign_lib, tmp_path):
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    report = revoke_lib.audit([], [{"schema": "x", "signature": "zz"}], key)
    assert report["invalid_events"] == 1
    assert report["foreign_key_events"] == 0


def test_rotation_verify_cli_needs_retired_pub(revoke_lib, sign_lib, tmp_path):
    """End-to-end: an old-key sidecar exits 0 ONLY when --retired-pub is
    supplied; without it the unverifiable event is invalid -> exit 4 (assert
    the exact code, per CodeRabbit :286/:344)."""
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "verify_facts_cli4", P(__file__).resolve().parent.parent / "bin" / "verify-facts.py")
    vf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vf)

    old_key = sign_lib.load_or_create_key(
        tmp_path / "old-key", tmp_path / "old-key.pub", alg=sign_lib.ALG_HMAC)
    new_key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    facts = [_fact("old", status="superseded", superseded_by="new"), _fact("new")]
    sign_lib.sign_facts(facts, new_key)
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    revoke_lib.append_revocation(
        tmp_path / "revocations.jsonl",
        revoke_lib.sign_revocation(old_key, superseded_id="old",
                                   superseding_id="new", reason="r",
                                   superseded_at=SUP_AT))

    args = ["--facts", str(tmp_path / "facts.json"),
            "--pub", str(tmp_path / "signing-key.pub")]
    assert vf.run(args) == 4  # unverifiable old event = invalid, hard fail
    assert vf.run(args + ["--retired-pub", str(tmp_path / "old-key.pub")]) == 0


# ── backfill one-shot (separate mutation, Mira-gated at run time) ────────────
def _load_backfill():
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "backfill_revocations",
        P(__file__).resolve().parent.parent / "bin" / "backfill-revocations.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_propose_lists_but_writes_nothing(revoke_lib, sign_lib, tmp_path, monkeypatch, capsys):
    bf = _load_backfill()
    sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([
        _fact("old", status="superseded", superseded_by="new"), _fact("new")]))

    _run(bf.main, monkeypatch, ["--facts", str(store)], name="backfill-revocations.py")

    assert "Would mint 1" in capsys.readouterr().out
    assert not (tmp_path / "revocations.jsonl").exists()


def test_backfill_apply_attests_legacy_marks_cleanly(revoke_lib, sign_lib, tmp_path, monkeypatch, capsys):
    bf = _load_backfill()
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    facts = [_fact("old", status="superseded", superseded_by="new",
                   supersession_reason="legacy", superseded_at=SUP_AT),
             _fact("new")]
    store = tmp_path / "facts.json"
    before = json.dumps(facts)
    store.write_text(before)

    _run(bf.main, monkeypatch, ["--facts", str(store), "--apply"],
         name="backfill-revocations.py")

    assert store.read_text() == before
    events = revoke_lib.load_revocations(tmp_path / "revocations.jsonl")
    assert len(events) == 1
    assert revoke_lib.verify_revocation(events[0], key)
    report = revoke_lib.audit(facts, events, key)
    assert report["unattested_superseded"] == []
    assert report["resurrected"] == []


def test_backfill_is_idempotent(revoke_lib, sign_lib, tmp_path, monkeypatch, capsys):
    bf = _load_backfill()
    sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("old", status="superseded")]))

    _run(bf.main, monkeypatch, ["--facts", str(store), "--apply"],
         name="backfill-revocations.py")
    _run(bf.main, monkeypatch, ["--facts", str(store), "--apply"],
         name="backfill-revocations.py")

    events = revoke_lib.load_revocations(tmp_path / "revocations.jsonl")
    assert len(events) == 1
    assert "Nothing to backfill" in capsys.readouterr().out


def test_backfill_fails_on_preexisting_invalid_event(revoke_lib, sign_lib, tmp_path, monkeypatch):
    """CodeRabbit :99 — a tampered/invalid sidecar event (S1 exit-4 class)
    must fail the backfill, not slip through as success."""
    bf = _load_backfill()
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("old", status="superseded", superseded_by="new"),
                                 _fact("new")]))
    # Seed a tampered event so the post-backfill audit sees invalid_events > 0.
    good = revoke_lib.sign_revocation(key, superseded_id="z", superseding_id="",
                                      reason="", superseded_at=SUP_AT)
    revoke_lib.append_revocation(tmp_path / "revocations.jsonl",
                                 dict(good, reason="tampered"))

    import sys as _sys
    monkeypatch.setattr(_sys, "argv",
                        ["backfill-revocations.py", "--facts", str(store), "--apply"])
    with pytest.raises(SystemExit) as exc:
        bf.main()
    assert exc.value.code == 1


def test_backfill_refuses_on_invalid_sidecar_even_with_nothing_to_mint(
        revoke_lib, sign_lib, tmp_path, monkeypatch):
    """CodeRabbit #65: an invalid sidecar event must fail the run even when no
    fact needs backfilling (the early-return path skipped the invalid check)."""
    bf = _load_backfill()
    key = sign_lib.load_or_create_key(
        tmp_path / "signing-key", tmp_path / "signing-key.pub", alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv("NOCKBRAIN_SIGNING_KEY", str(tmp_path / "signing-key"))
    monkeypatch.setenv("NOCKBRAIN_SIGNING_PUB", str(tmp_path / "signing-key.pub"))
    # No superseded facts -> nothing to backfill; but seed a tampered event.
    store = tmp_path / "facts.json"
    store.write_text(json.dumps([_fact("live")]))
    good = revoke_lib.sign_revocation(key, superseded_id="z", superseding_id="",
                                      reason="", superseded_at=SUP_AT)
    revoke_lib.append_revocation(tmp_path / "revocations.jsonl",
                                 dict(good, reason="tampered"))

    import sys as _sys
    monkeypatch.setattr(_sys, "argv",
                        ["backfill-revocations.py", "--facts", str(store), "--apply"])
    with pytest.raises(SystemExit) as exc:
        bf.main()
    assert exc.value.code == 1
