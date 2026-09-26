"""Hermetic contract tests for the optional Memory Explorer snapshot."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from _explorer_store import ExplorerError, ExplorerStore  # noqa: E402
from _sign import ALG_HMAC, SigningKey, sign_facts  # noqa: E402
from _revoke import sign_revocation  # noqa: E402
import _explorer_store as explorer_module  # noqa: E402


def fact(fid, content=None, **extra):
    result = {
        "id": fid, "kind": "decision", "status": "current", "confidence": 0.8,
        "content": content or f"Synthetic decision {fid}",
        "source_date": "2026-09-01", "evidence": ["synthetic-anchor"],
    }
    result.update(extra)
    return result


def make_store(tmp_path, facts=None, *, insights=None, key=True, revocations=None):
    root = tmp_path / "selected"
    root.mkdir()
    if facts is not None:
        (root / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    if insights is not None:
        (root / "insights.json").write_text(json.dumps(insights), encoding="utf-8")
    if revocations is not None:
        (root / "revocations.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in revocations), encoding="utf-8"
        )
    if key:
        secret = bytes(range(32))
        signing_key = SigningKey(ALG_HMAC, hmac_secret=secret)
        (root / "signing-key.pub").write_text(json.dumps({
            "alg": ALG_HMAC, "key_id": signing_key.key_id, "secret": secret.hex()
        }), encoding="utf-8")
        return root, signing_key
    return root, None


def tree_state(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
            for p in root.rglob("*") if p.is_file()}


def test_demo_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    with ExplorerStore(demo=True) as store:
        summary = store.summary()
        assert summary["mode"] == "demo"
        assert summary["state"] == "readable"
        assert summary["verification"]["state"] == "available"
        assert store.list_records()["items"]
        assert store.list_records(query="what did we decide about delivery")["items"] or store.list_records(query="delivery")["items"]
        scratch = store.snapshot_dir
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in scratch.iterdir() if p.is_file())
    assert not scratch.exists()
    assert not (tmp_path / "unrelated-home").exists()


def test_search_filters_details_and_duplicate_handles(tmp_path):
    root, key = make_store(tmp_path, [])
    records = [fact("duplicate", "Delivery by courier."), fact("duplicate", "Delivery by bicycle.", status="superseded"),
               fact("inactive", "Old schedule", invalid_at="2020-01-01")]
    sign_facts(records, key)
    (root / "facts.json").write_text(json.dumps(records))
    before = tree_state(root)
    with ExplorerStore(root) as store:
        listed = store.list_records(lifecycle="all")
        assert listed["total"] == 3
        assert [item["handle"] for item in listed["items"]] == ["facts:0", "facts:1", "facts:2"]
        assert store.list_records(query="bicycle", lifecycle="all")["items"][0]["handle"] == "facts:1"
        assert store.list_records(kind="decision", lifecycle="superseded")["total"] == 1
        assert store.list_records(lifecycle="current")["total"] == 1
        assert store.detail("facts:1")["content"] == "Delivery by bicycle."
        assert store.detail("facts:1")["details"]["evidence"] == ["synthetic-anchor"]
        assert store.detail("facts:99") is None
        assert any("duplicate" in notice.lower() for notice in store.summary()["notices"])
    assert tree_state(root) == before


@pytest.mark.parametrize("data,state", [(None, "missing"), ("[]", "empty"), ("{", "unreadable"), ("{}", "unreadable")])
def test_store_states(tmp_path, data, state):
    root = tmp_path / "selected"
    root.mkdir()
    if data is not None:
        (root / "facts.json").write_text(data)
    with ExplorerStore(root) as store:
        assert store.summary()["state"] == state
        assert store.summary()["files"]["facts.json"]["state"] == state
        if state == "unreadable":
            assert store.summary()["notices"]


def test_missing_invalid_key_and_signature_states(tmp_path):
    root, key = make_store(tmp_path, [])
    records = [fact("signed"), fact("tampered"), fact("unsigned")]
    sign_facts(records[:2], key)
    records[1]["content"] = "Changed after signing"
    (root / "facts.json").write_text(json.dumps(records))
    with ExplorerStore(root) as store:
        assert [item["verification"] for item in store.list_records()["items"]] == ["valid", "tampered", "unsigned"]
    (root / "signing-key.pub").unlink()
    with ExplorerStore(root) as store:
        assert store.summary()["verification"]["state"] == "missing"
        assert [item["verification"] for item in store.list_records()["items"]] == ["unavailable", "unavailable", "unsigned"]
    (root / "signing-key.pub").write_text("broken")
    with ExplorerStore(root) as store:
        assert store.summary()["verification"]["state"] == "invalid"
        assert [item["verification"] for item in store.list_records()["items"]] == ["unavailable", "unavailable", "unsigned"]


def test_revoked_and_links(tmp_path):
    root, key = make_store(tmp_path, [])
    records = [fact("old", status="current", superseded_by="new"), fact("new")]
    sign_facts(records, key)
    event = sign_revocation(key, superseded_id="old", superseding_id="new")
    (root / "facts.json").write_text(json.dumps(records))
    (root / "revocations.jsonl").write_text(json.dumps(event) + "\n")
    with ExplorerStore(root) as store:
        old = store.detail("facts:0")
        assert old["verification"] == "valid"
        assert old["lifecycle"] == "revoked"
        assert old["links"] == [{"id": "new", "handle": "facts:1", "relation": "superseded_by"}]
        assert store.list_records()["total"] == 1


def test_malformed_records_are_visible_and_do_not_crash(tmp_path):
    root, key = make_store(tmp_path, [fact("ok"), {"id": ["bad"]}, "bad"])
    with ExplorerStore(root) as store:
        listed = store.list_records(lifecycle="all")
        assert listed["total"] == 3
        assert [item["verification"] for item in listed["items"]] == ["unsigned", "invalid", "invalid"]
        assert store.summary()["notices"]


def test_private_key_never_read_and_special_inputs_refused(tmp_path):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    (root / "signing-key").symlink_to(tmp_path / "must-not-read")
    with ExplorerStore(root) as store:
        assert store.summary()["verification"]["state"] == "missing"
    (root / "facts.json").unlink()
    (root / "facts.json").symlink_to(tmp_path / "outside")
    with pytest.raises(ExplorerError):
        ExplorerStore(root)
    (root / "facts.json").unlink()
    os.mkfifo(root / "facts.json")
    with pytest.raises(ExplorerError):
        ExplorerStore(root)


def test_bounds_and_refresh_preserves_source(tmp_path):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    before = tree_state(root)
    with ExplorerStore(root) as store:
        assert store.refresh()["snapshot_id"] != ""
        with pytest.raises(ExplorerError):
            store.list_records(offset=-1)
        with pytest.raises(ExplorerError):
            store.list_records(limit=101)
        with pytest.raises(ExplorerError):
            store.list_records(lifecycle="strange")
    assert tree_state(root) == before


def test_explicit_key_rejects_private_path_and_inode_alias(tmp_path):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    private = root / "signing-key"
    private.write_text("PRIVATE SENTINEL")
    alias = tmp_path / "same-inode"
    os.link(private, alias)
    for selected in (private, alias):
        with pytest.raises(ExplorerError, match="private signing key"):
            ExplorerStore(root, verify_key=selected)


def test_bad_key_document_is_never_an_api_detail(tmp_path):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    (root / "signing-key.pub").write_text(json.dumps({"alg": "ed25519", "private_key": "SECRET SENTINEL"}))
    with ExplorerStore(root) as store:
        assert store.summary()["verification"]["state"] == "invalid"
        assert "SECRET SENTINEL" not in json.dumps(store.summary())
        assert "SECRET SENTINEL" not in json.dumps(store.list_records())
        assert "SECRET SENTINEL" not in json.dumps(store.detail("facts:0"))


def test_default_public_key_hardlink_to_private_is_refused(tmp_path):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    private = root / "signing-key"
    private.write_text("PRIVATE SENTINEL")
    os.link(private, root / "signing-key.pub")
    with pytest.raises(ExplorerError, match="private signing key"):
        ExplorerStore(root)


def test_public_key_swapped_to_private_inode_before_read_is_refused(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("one")], key=True)
    private = root / "signing-key"
    private.write_text("PRIVATE SENTINEL")
    original = explorer_module._read_one

    def swap_key(path, before, private_path=None):
        if path.name == "signing-key.pub":
            path.unlink()
            os.link(private, path)
        return original(path, before, private_path)

    monkeypatch.setattr(explorer_module, "_read_one", swap_key)
    with pytest.raises(ExplorerError, match="private signing key"):
        ExplorerStore(root)


@pytest.mark.parametrize("number", ["NaN", "1e400"])
def test_nonfinite_json_is_unreadable_not_healthy_empty(tmp_path, number):
    root, _ = make_store(tmp_path, key=False)
    (root / "facts.json").write_text('[{"id":"x","confidence":' + number + '}]')
    with ExplorerStore(root) as store:
        assert store.summary()["state"] == "unreadable"
        assert store.summary()["files"]["facts.json"]["state"] == "unreadable"
        assert store.summary()["notices"]


def test_oversize_input_fails_closed(tmp_path):
    root, _ = make_store(tmp_path, [], key=False)
    with (root / "facts.json").open("wb") as stream:
        stream.truncate(explorer_module.FILE_LIMIT + 1)
    with pytest.raises(ExplorerError, match="32 MiB"):
        ExplorerStore(root)


def test_capture_retries_once_on_generation_drift(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("old")], key=False)
    original = explorer_module._read_one
    calls = 0

    def replace_once(path, before, private_path=None):
        nonlocal calls
        data = original(path, before, private_path)
        if path.name == "facts.json":
            calls += 1
            if calls == 1:
                replacement = path.with_suffix(".next")
                replacement.write_text(json.dumps([fact("new")]))
                os.replace(replacement, path)
        return data

    monkeypatch.setattr(explorer_module, "_read_one", replace_once)
    with ExplorerStore(root) as store:
        assert store.list_records()["items"][0]["id"] == "new"
    assert calls == 2


def test_repeated_capture_drift_keeps_previous_generation(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("stable")], key=False)
    with ExplorerStore(root) as store:
        previous = store.summary()
        original = explorer_module._read_one

        def replace_every_time(path, before, private_path=None):
            data = original(path, before, private_path)
            if path.name == "facts.json":
                replacement = path.with_suffix(".next")
                replacement.write_text(json.dumps([fact("moving"), fact("another")]))
                os.replace(replacement, path)
            return data

        monkeypatch.setattr(explorer_module, "_read_one", replace_every_time)
        with pytest.raises(ExplorerError, match="stable snapshot"):
            store.refresh()
        assert store.summary() == previous
        assert store.snapshot_dir.exists()


def test_hostile_tmp_environment_never_places_scratch_in_source(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    before = tree_state(root)
    for variable in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(variable, str(root))
    with ExplorerStore(root) as store:
        assert not store.snapshot_dir.is_relative_to(root)
        assert store.list_records()["total"] == 1
    assert tree_state(root) == before


def test_scratch_base_falls_back_outside_selected_source(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    monkeypatch.setattr(explorer_module, "_TEMP_BASES", (root, alternate))
    before = tree_state(root)
    with ExplorerStore(root) as store:
        assert store.snapshot_dir.is_relative_to(alternate)
    assert tree_state(root) == before


def test_no_safe_scratch_base_fails_before_writing_source(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.setattr(explorer_module, "_TEMP_BASES", (root, nested))
    before = tree_state(root)
    with pytest.raises(ExplorerError, match="safe temporary directory"):
        ExplorerStore(root)
    assert tree_state(root) == before


def test_sqlite_marker_created_midcapture_refuses_stale_json(tmp_path, monkeypatch):
    root, _ = make_store(tmp_path, [fact("one")], key=False)
    with ExplorerStore(root) as store:
        previous = store.summary()
        original = explorer_module._read_one

        def add_marker(path, before, private_path=None):
            data = original(path, before, private_path)
            if path.name == "facts.json":
                (root / "store-v2").write_text("")
            return data

        monkeypatch.setattr(explorer_module, "_read_one", add_marker)
        with pytest.raises(ExplorerError, match="SQLite"):
            store.refresh()
        assert store.summary() == previous
