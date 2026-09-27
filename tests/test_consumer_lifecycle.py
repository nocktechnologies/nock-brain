"""Customer lifecycle, replay and recoverability contracts."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _consumer_store as cs
import _revoke
import _sign
from test_consumer_store import candidate, proposal


def seeded(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        old = candidate(manifest, "Retire this policy")
        store.apply_proposal(proposal(store, [old]))
    return path, manifest, old


def test_correction_immutable_signed_core_and_replay(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        prior = store.facts[0]
        replacement = candidate(manifest, "Replacement policy")
        source = proposal(store, [replacement])
        correction = store.propose_correction(old["id"], source)
        assert store.read_proposal(correction)["replacement"] == replacement
        result = store.apply_proposal(correction)
        assert result["replacement_id"] == replacement["id"]
        assert store.facts[0]["attestation"] == prior["attestation"]
        assert store.facts[0]["content"] == prior["content"]
        assert store.facts[0]["status"] == "superseded"
        assert _sign.verify_fact(store.facts[0], store._key) == _sign.VALID
        assert _sign.verify_fact(store.facts[1], store._key) == _sign.VALID
        assert old["id"] in store.revoked_ids
        with pytest.raises(cs.ConsumerError, match="stale"):
            store.apply_proposal(correction)
        replay = proposal(store, [old])
        assert store.apply_proposal(replay)["skipped"] == 1
        assert len(store.facts) == 2
    with cs.ConsumerStore(path) as reopened:
        assert len(reopened.facts) == 2
        assert reopened.facts[0]["status"] == "superseded"


def test_correction_review_survives_discarded_source_and_reopen(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        source = proposal(store, [candidate(manifest, "New policy")])
        lifecycle = store.propose_correction(old["id"], source)
        store.discard_proposal(source)
        assert store.read_proposal(lifecycle)["replacement"]["content"] == "New policy"
    with cs.ConsumerStore(path) as reopened:
        assert reopened.read_proposal(lifecycle)["replacement_proposal"] == source
        reopened.apply_proposal(lifecycle)
        assert reopened.facts[0]["status"] == "superseded"


def test_forget_removes_record_and_every_pending_content_proposal(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        pending = proposal(store, [old, candidate(manifest, "Other")])
        digest = store.propose_forget(old["id"])
        assert len(store.pending_proposals()) == 3
        result = store.apply_proposal(digest)
        assert result["total"] == 0
        assert store.revoked_ids == {old["id"]}
        assert store.pending_proposals() == []
        assert not (path / "proposals" / (pending + ".json")).exists()
        assert old["content"].encode() not in (path / cs.JOURNAL_NAME).read_bytes() if (path / cs.JOURNAL_NAME).exists() else True
        replay = proposal(store, [old])
        assert store.apply_proposal(replay)["skipped"] == 1
        assert store.facts == []
        event = store._events[0]
        assert event["reason"] == "customer-forget"
        assert _revoke.verify_revocation(event, store._key)
    with cs.ConsumerStore(path) as reopened:
        assert reopened.facts == []
        assert reopened.status()["revoked"] == 1


def test_historical_superseded_fact_can_be_forgotten(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        replacement = candidate(manifest, "Current policy")
        store.apply_proposal(store.propose_correction(old["id"], proposal(store, [replacement])))
        store.apply_proposal(store.propose_forget(old["id"]))
        assert [f["id"] for f in store.facts] == [replacement["id"]]
        assert len(store._events) == 2
    with cs.ConsumerStore(path) as reopened:
        assert reopened.facts[0]["id"] == replacement["id"]


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("action", ["correct", "forget"])
def test_recover_after_each_publication_boundary(tmp_path, monkeypatch, boundary, action):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        if action == "correct":
            digest = store.propose_correction(old["id"], proposal(store, [candidate(manifest, "New")]))
        else:
            proposal(store, [candidate(manifest, "Pending private note")])
            digest = store.propose_forget(old["id"])
        original = cs.secure_replace_bytes
        calls = 0

        def fail_at(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == boundary:
                raise OSError("injected publication interruption")
            return original(*args, **kwargs)

        if boundary == 3:
            original_unlink = Path.unlink
            def fail_unlink(self, *args, **kwargs):
                if self.name == cs.JOURNAL_NAME:
                    raise OSError("injected journal cleanup interruption")
                return original_unlink(self, *args, **kwargs)
            monkeypatch.setattr(Path, "unlink", fail_unlink)
        else:
            monkeypatch.setattr(cs, "secure_replace_bytes", fail_at)
        with pytest.raises((cs.ConsumerError, OSError)):
            store.apply_proposal(digest)
    monkeypatch.undo()
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        with pytest.raises(cs.ConsumerError, match="recovery required"):
            store.save_proposal([], [], {"files": 0, "candidates": 0, "overlong_skipped": 0})
        assert store.recover()["recovered"]
        assert not store.recovery_required
        assert old["id"] in store.revoked_ids
        if action == "forget":
            assert store.facts == []
            assert store.pending_proposals() == []
        else:
            assert store.facts[0]["status"] == "superseded"
            assert len(store.facts) == 2


def test_reject_foreign_and_malformed_revocations_and_journal(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_forget(old["id"])
        event = _revoke.sign_revocation(store._key, superseded_id=old["id"], reason="customer-forget")
    rev = path / "revocations.jsonl"
    rev.write_bytes(cs.canonical_bytes(dict(event, extra="unsigned")) + b"\n")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    rev.unlink()
    other_path = tmp_path / "other-store"
    cs.init_store(other_path)
    with cs.ConsumerStore(other_path) as foreign:
        foreign_event = _revoke.sign_revocation(foreign._key, superseded_id=old["id"], reason="customer-forget")
    rev.write_bytes(cs.canonical_bytes(foreign_event) + b"\n")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    rev.unlink()
    with cs.ConsumerStore(path) as store:
        assert store.read_proposal(digest)["fact_id"] == old["id"]
    journal = path / cs.JOURNAL_NAME
    journal.write_bytes(b"{}")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass
    journal.unlink()
    journal.symlink_to(path / "facts.json")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_signed_journal_tampering_is_rejected(tmp_path, monkeypatch):
    path, _manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_forget(old["id"])
        def fail(*_args, **_kwargs):
            raise OSError("injected before revocation")
        monkeypatch.setattr(cs, "secure_replace_bytes", fail)
        with pytest.raises(cs.ConsumerError):
            store.apply_proposal(digest)
    journal = path / cs.JOURNAL_NAME
    data = json.loads(journal.read_bytes())
    data["action"] = "correct"
    journal.write_bytes(cs.canonical_bytes(data))
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_forget_recovery_finishes_interrupted_pending_cleanup(tmp_path, monkeypatch):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        proposal(store, [candidate(manifest, "Pending private note")])
        digest = store.propose_forget(old["id"])
        original_unlink = Path.unlink
        failed = False

        def fail_once(self, *args, **kwargs):
            nonlocal failed
            if self.parent.name == "proposals" and not failed:
                failed = True
                raise OSError("injected cleanup interruption")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_once)
        with pytest.raises(cs.ConsumerError):
            store.apply_proposal(digest)
        journal_bytes = (path / cs.JOURNAL_NAME).read_bytes()
        assert old["content"].encode() not in journal_bytes
    monkeypatch.undo()
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        assert store.recover()["recovered"]
        assert store.pending_proposals() == []


def test_forgotten_signed_record_cannot_be_restored(tmp_path):
    path, _manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        prior = store.facts[0]
        store.apply_proposal(store.propose_forget(old["id"]))
    (path / "facts.json").write_bytes(cs.canonical_bytes([prior]) + b"\n")
    with pytest.raises(cs.ConsumerError):
        with cs.ConsumerStore(path):
            pass


def test_queue_bound_and_discard(tmp_path, monkeypatch):
    path, manifest, _ = seeded(tmp_path)
    monkeypatch.setattr(cs, "MAX_PROPOSALS", 1)
    with cs.ConsumerStore(path) as store:
        store.discard_proposal(store.pending_proposals()[0]["digest"])
        first = proposal(store, [candidate(manifest, "One")])
        with pytest.raises(cs.ConsumerError, match="full"):
            proposal(store, [candidate(manifest, "Two")])
        assert store.discard_proposal(first) == {"discarded": first}
        assert store.pending_proposals() == []


def test_generation_matches_reopen_after_additive_import(tmp_path):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    with cs.ConsumerStore(path) as store:
        store.apply_proposal(proposal(store, [candidate(manifest)]))
        generation = store.generation
    with cs.ConsumerStore(path) as reopened:
        assert reopened.generation == generation


def test_signed_lifecycle_proposal_rejects_changed_replacement(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_correction(old["id"], proposal(store, [candidate(manifest, "First")]))
        file = path / "proposals" / (digest + ".json")
        data = json.loads(file.read_bytes())
        data["replacement"] = candidate(manifest, "Second")
        file.write_bytes(cs.canonical_bytes(data))
        with pytest.raises(cs.ConsumerError):
            store.read_proposal(digest)


def test_hmac_lifecycle_round_trip(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(_sign, "_HAVE_CRYPTOGRAPHY", False)
        path, manifest, old = seeded(tmp_path)
        assert manifest["algorithm"] == _sign.ALG_HMAC
        with cs.ConsumerStore(path) as store:
            digest = store.propose_correction(old["id"], proposal(store, [candidate(manifest, "New")]))
            store.apply_proposal(digest)
            assert len(store._events[0]["signature"]) == 64
    with cs.ConsumerStore(path) as reopened:
        assert reopened.facts[0]["status"] == "superseded"


def test_forget_preserves_unrelated_signed_record_and_rejects_derived_output(tmp_path):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        other = candidate(manifest, "Keep this fact")
        store.apply_proposal(proposal(store, [other]))
        preserved = store.facts[1]
        digest = store.propose_forget(old["id"])
        (path / "insights.json").write_bytes(b"[]")
        with pytest.raises(cs.ConsumerError, match="derived"):
            store.apply_proposal(digest)
        (path / "insights.json").unlink()
        store.apply_proposal(digest)
        assert store.facts == [preserved]
        assert _sign.verify_fact(store.facts[0], store._key) == _sign.VALID


_CRASH_DRIVER = r'''
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import _consumer_store as cs

store_path, operation, target, moment, identity = sys.argv[2:]
real_replace = os.replace
real_unlink = Path.unlink

def exit_replace(source, destination, *args, **kwargs):
    selected = (Path(destination).parent.name == "proposals" if target == "proposal"
                else Path(destination).name == target)
    if selected:
        if moment == "before":
            os._exit(73)
        result = real_replace(source, destination, *args, **kwargs)
        os._exit(73)
    return real_replace(source, destination, *args, **kwargs)

def exit_unlink(self, *args, **kwargs):
    if self.parent.name == "proposals" and self.name.endswith(".json"):
        if moment == "before":
            os._exit(73)
        result = real_unlink(self, *args, **kwargs)
        os._exit(73)
    return real_unlink(self, *args, **kwargs)

if operation == "forget_cleanup":
    Path.unlink = exit_unlink
else:
    os.replace = exit_replace
try:
    with cs.ConsumerStore(Path(store_path)) as store:
        if operation == "forget_proposal":
            store.propose_forget(identity)
        else:
            store.apply_proposal(identity)
except BaseException:
    traceback.print_exc()
    os._exit(74)
os._exit(75)
'''


def _exit_in_subprocess(path, operation, target, moment, identity):
    result = subprocess.run(
        [sys.executable, "-B", "-c", _CRASH_DRIVER,
         str(Path(__file__).resolve().parents[1] / "bin"), str(path),
         operation, target, moment, identity],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 73, result.stderr


@pytest.mark.parametrize("basename", [cs.JOURNAL_NAME, "revocations.jsonl", "facts.json"])
@pytest.mark.parametrize("moment", ["before", "after"])
def test_process_death_at_lifecycle_replace_is_recoverable_and_forget_cleans_copies(
        tmp_path, basename, moment):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_correction(old["id"], proposal(store, [candidate(manifest, "New")]))
    _exit_in_subprocess(path, "correct", basename, moment, digest)
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        assert store.recover()["recovered"]
        assert not store.recovery_required
        store.apply_proposal(store.propose_forget(old["id"]))
        assert old["id"] not in {fact["id"] for fact in store.facts}
    remaining = [str(item.relative_to(path)) for item in path.rglob("*")
                 if item.is_file() and old["content"].encode() in item.read_bytes()]
    assert remaining == []


@pytest.mark.parametrize("moment", ["before", "after"])
def test_process_death_at_proposal_replace_is_usable(tmp_path, moment):
    path, _manifest, old = seeded(tmp_path)
    _exit_in_subprocess(path, "forget_proposal", "proposal", moment, old["id"])
    with cs.ConsumerStore(path) as store:
        if moment == "before":
            assert store.recovery_required
            assert store.recover()["action"] == "cleanup"
        else:
            assert not store.recovery_required
        store.apply_proposal(store.propose_forget(old["id"]))
        assert store.pending_proposals() == []


def test_old_hardlink_publication_artifacts_are_recoverable(tmp_path):
    path, _manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_forget(old["id"])
    proposal_path = path / "proposals" / (digest + ".json")
    os.link(proposal_path, path / "proposals" / ".proposal-abcdefgh")
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        assert store.pending_proposals()
        assert store.recover()["action"] == "cleanup"
        assert not store.recovery_required
        store.apply_proposal(digest)
        assert store.facts == []


def test_old_hardlinked_journal_can_be_recovered(tmp_path, monkeypatch):
    path, _manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        digest = store.propose_forget(old["id"])
        with monkeypatch.context() as patch:
            patch.setattr(cs, "secure_replace_bytes", lambda *_a, **_k: (_ for _ in ()).throw(OSError("stop")))
            with pytest.raises(cs.ConsumerError):
                store.apply_proposal(digest)
    os.link(path / cs.JOURNAL_NAME, path / ".customer-journal-abcdefgh")
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        assert store.recover()["recovered"]
        assert store.facts == []
    assert not (path / ".customer-journal-abcdefgh").exists()


@pytest.mark.parametrize("moment", ["before", "after"])
def test_process_death_during_forget_queue_cleanup_recovers_without_plaintext(tmp_path, moment):
    path, manifest, old = seeded(tmp_path)
    with cs.ConsumerStore(path) as store:
        proposal(store, [candidate(manifest, "Other pending")])
        digest = store.propose_forget(old["id"])
    _exit_in_subprocess(path, "forget_cleanup", "proposal", moment, digest)
    with cs.ConsumerStore(path) as store:
        assert store.recovery_required
        assert store.recover()["recovered"]
        assert store.pending_proposals() == []
        assert store.facts == []
    remaining = [str(item.relative_to(path)) for item in path.rglob("*")
                 if item.is_file() and old["content"].encode() in item.read_bytes()]
    assert remaining == []


def test_revocation_limit_preflight_keeps_store_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "store"
    manifest = cs.init_store(path)
    monkeypatch.setattr(cs, "FACT_LIMIT", 2500)
    stopped = False
    for index in range(12):
        with cs.ConsumerStore(path) as store:
            fact = candidate(manifest, "Short policy " + str(index))
            store.apply_proposal(proposal(store, [fact]))
            digest = store.propose_forget(fact["id"])
            before_facts = (path / "facts.json").read_bytes()
            rev_path = path / "revocations.jsonl"
            before_rev = rev_path.read_bytes() if rev_path.exists() else None
            try:
                store.apply_proposal(digest)
            except cs.ConsumerError as exc:
                assert "revocation stream exceeds size limit" in str(exc)
                assert (path / "facts.json").read_bytes() == before_facts
                assert (rev_path.read_bytes() if rev_path.exists() else None) == before_rev
                assert not (path / cs.JOURNAL_NAME).exists()
                stopped = True
                break
    assert stopped
    with cs.ConsumerStore(path) as reopened:
        assert not reopened.recovery_required
        assert reopened.facts
