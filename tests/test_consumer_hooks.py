"""Synthetic customer hook flow and explicit source boundaries."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
from _consumer_store import ConsumerError, ConsumerStore, init_store
from _consumer_hooks import setup_hooks
import _sign

HOOK = BIN / "consumer-hook.py"


def _call(event, store, payload, roots=(), environment=None):
    argv = [sys.executable, str(HOOK), event, "--store", str(store)]
    for root in roots:
        argv.extend(["--transcript-root", str(root)])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if environment:
        env.update(environment)
    return subprocess.run(argv, input=json.dumps(payload), text=True,
                          capture_output=True, env=env, timeout=15)


def _transcript(path):
    row = {"type": "user", "timestamp": "2026-09-26T10:00:00Z",
           "message": {"role": "user", "content": [{"type": "text",
                       "text": "[DECISION] Friday delivery is our release target."}]}}
    path.write_text(json.dumps(row) + "\n")


def _setup(tmp_path):
    store = tmp_path / "customer brain"
    init_store(store)
    root = tmp_path / "transcript root"
    root.mkdir()
    transcript = root / "session.jsonl"
    _transcript(transcript)
    return store, root, transcript


def test_setup_is_private_idempotent_and_quotes_current_interpreter(tmp_path):
    store, root, _ = _setup(tmp_path)
    settings = setup_hooks(store, [root])
    assert settings == store / "claude-settings.json"
    first = settings.read_bytes()
    assert settings.stat().st_mode & 0o777 == 0o600
    assert setup_hooks(store, [root]) == settings
    assert settings.read_bytes() == first
    hooks = json.loads(first)["hooks"]
    recall = hooks["UserPromptSubmit"][0]["hooks"][0]
    stop = hooks["Stop"][0]["hooks"][0]
    assert shlex.split(recall["command"]) == [
        sys.executable, "-B", str(HOOK), "UserPromptSubmit", "--store", str(store)]
    assert shlex.split(stop["command"])[-4:] == [
        "--transcript-root", str(root), "--root-target", str(root.resolve())]
    assert recall["timeout"] == 5 and stop["timeout"] == 10


def test_capture_review_apply_recall_and_repeat_without_private_key(tmp_path):
    store, root, transcript = _setup(tmp_path)
    event = {"hook_event_name": "Stop", "transcript_path": str(transcript),
             "stop_hook_active": False, "last_assistant_message": "Do not import me"}
    source_bytes = transcript.read_bytes()
    capture = _call("Stop", store, event, [root])
    assert capture.returncode == 0 and not capture.stderr
    assert "pending proposal" in json.loads(capture.stdout)["systemMessage"]
    assert "Friday" not in capture.stdout
    assert "hookSpecificOutput" not in json.loads(capture.stdout)
    assert json.loads((store / "facts.json").read_text()) == []
    with ConsumerStore(store) as customer:
        pending = sorted((store / "proposals").glob("*.json"))
        assert len(pending) == 1
        proposal = customer.read_proposal(pending[0].stem)
        assert len(proposal["candidates"]) == 1
        assert "Do not import me" not in json.dumps(proposal)
        customer.apply_proposal(pending[0].stem)
    if _sign._HAVE_CRYPTOGRAPHY:
        assert json.loads((store / "customer.json").read_text())["algorithm"] == _sign.ALG_ED25519
    repeated = _call("Stop", store, event, [root])
    assert repeated.returncode == 0 and json.loads(repeated.stdout) == {}
    assert "pending" not in repeated.stdout + repeated.stderr
    assert len(list((store / "proposals").glob("*.json"))) == 1
    assert transcript.read_bytes() == source_bytes
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    private = store / "signing-key"
    unavailable = store / "signing-key.unavailable"
    private.rename(unavailable)
    recall = _call("UserPromptSubmit", store,
                   {"hook_event_name": "UserPromptSubmit",
                    "prompt": "What did we decide about Friday delivery?"},
                   environment={"NOCKBRAIN_SIGNING_PUB": str(tmp_path / "foreign.pub"),
                                "NOCKBRAIN_STORE": "sqlite", "PYTHONPATH": str(tmp_path)})
    assert recall.returncode == 0
    output = json.loads(recall.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "Friday delivery" in output["additionalContext"]
    assert "untrusted historical context" in output["additionalContext"]
    unavailable.rename(private)
    isolated_home = _call("UserPromptSubmit", store,
                          {"hook_event_name": "UserPromptSubmit",
                           "prompt": "What did we decide about Friday delivery?"},
                          environment={"HOME": str(hostile_home)})
    assert isolated_home.returncode == 0
    assert isinstance(json.loads(isolated_home.stdout), dict)
    assert not (hostile_home / ".nock-brain").exists()
    assert not (store / "facts.json.verified-cache.json").exists()
    assert transcript.read_bytes() == source_bytes


def test_recall_fails_closed_on_tamper_foreign_key_and_revocation(tmp_path):
    store, root, transcript = _setup(tmp_path)
    stop = _call("Stop", store, {"hook_event_name": "Stop",
                 "transcript_path": str(transcript)}, [root])
    assert "pending proposal" in json.loads(stop.stdout)["systemMessage"]
    with ConsumerStore(store) as customer:
        customer.apply_proposal(next((store / "proposals").glob("*.json")).stem)
    event = {"hook_event_name": "UserPromptSubmit",
             "prompt": "What did we decide about Friday delivery?"}
    baseline = (store / "facts.json").read_bytes()
    assert "Friday delivery" in _call("UserPromptSubmit", store, event).stdout

    facts = json.loads(baseline)
    facts[0]["content"] = "[DECISION] Wrong release target."
    (store / "facts.json").write_text(json.dumps(facts))
    assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
    (store / "facts.json").write_bytes(baseline)

    other = tmp_path / "other"
    init_store(other)
    public = store / "signing-key.pub"
    original_public = public.read_bytes()
    public.write_bytes((other / "signing-key.pub").read_bytes())
    assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
    public.write_bytes(original_public)

    revocations = store / "revocations.jsonl"
    revocations.write_text('{"schema":"forged"}\n')
    assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
    revocations.unlink()
    assert "Friday delivery" in _call("UserPromptSubmit", store, event).stdout

    journal = store / ".customer-journal.json"
    journal.write_text("incomplete")
    assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
    journal.unlink()


def test_malformed_transcript_and_timeout_are_bounded(tmp_path, monkeypatch, capsys):
    store, root, transcript = _setup(tmp_path)
    transcript.write_text('{"type":"user"}\n{"type":')
    stop = _call("Stop", store, {"hook_event_name": "Stop",
                 "transcript_path": str(transcript)}, [root])
    assert stop.returncode == 0 and json.loads(stop.stdout) == {}
    assert not list((store / "proposals").glob("*.json"))
    spec = importlib.util.spec_from_file_location("consumer_hook_timeout", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
                        (_ for _ in ()).throw(subprocess.TimeoutExpired("worker", 1)))
    monkeypatch.setattr(sys, "stdin", __import__("io").TextIOWrapper(
        __import__("io").BytesIO(json.dumps({"hook_event_name": "UserPromptSubmit",
            "prompt": "What did we decide about Friday?"}).encode())))
    monkeypatch.setattr(sys, "argv", [str(HOOK), "UserPromptSubmit", "--store", str(store)])
    assert module.main() == 0
    assert capsys.readouterr().out == "{}\n"


def test_invalid_arguments_are_safe_and_fail_open(tmp_path):
    bad = subprocess.run([sys.executable, str(HOOK), "bad\x1b[31m", "--store",
                          str(tmp_path / "secret")], input="{}", text=True,
                         capture_output=True, timeout=5)
    assert bad.returncode == 0 and bad.stdout == "{}\n"
    assert "\x1b" not in bad.stdout + bad.stderr


def test_outside_symlink_and_bad_hook_inputs_fail_open(tmp_path):
    store, root, transcript = _setup(tmp_path)
    outside = tmp_path / "outside.jsonl"
    _transcript(outside)
    alias = root / "alias.jsonl"
    alias.symlink_to(outside)
    for selected in (outside, alias, root / ".." / "outside.jsonl"):
        result = _call("Stop", store, {"hook_event_name": "Stop",
                       "transcript_path": str(selected)}, [root])
        assert result.returncode == 0 and json.loads(result.stdout) == {}
        assert "could not complete" in result.stderr
    bad = _call("UserPromptSubmit", store, {"hook_event_name": "Stop",
                 "prompt": "What did we decide?"})
    assert bad.returncode == 0 and json.loads(bad.stdout) == {}
    assert "could not complete" in bad.stderr
    large = subprocess.run([sys.executable, str(HOOK), "Stop", "--store", str(store),
                            "--transcript-root", str(root)], input="x" * 20000,
                           text=True, capture_output=True, timeout=15)
    assert large.returncode == 0 and json.loads(large.stdout) == {}
    assert not list((store / "proposals").glob("*.json"))
    assert transcript.exists()


def test_setup_rejects_broad_roots_and_unsafe_settings(tmp_path):
    store, root, _ = _setup(tmp_path)
    with pytest.raises(ConsumerError):
        setup_hooks(store, [Path("/")])
    with pytest.raises(ConsumerError):
        setup_hooks(store, [store.parent])
    with pytest.raises(ConsumerError):
        setup_hooks(store, [root / ".."])
    target = store / "claude-settings.json"
    target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ConsumerError):
        setup_hooks(store, [root])


@pytest.mark.parametrize("force_hmac", [False, True])
def test_public_recall_uses_strict_declared_key_document(tmp_path, monkeypatch, force_hmac):
    if force_hmac:
        monkeypatch.setattr(_sign, "_HAVE_CRYPTOGRAPHY", False)
    store, root, transcript = _setup(tmp_path)
    _call("Stop", store, {"hook_event_name": "Stop", "transcript_path": str(transcript)}, [root])
    with ConsumerStore(store) as customer:
        customer.apply_proposal(customer.pending_proposals()[0]["digest"])
    event = {"hook_event_name": "UserPromptSubmit", "prompt": "What did we decide about Friday delivery?"}
    public = store / "signing-key.pub"
    original = public.read_bytes()
    good = json.loads(original)
    malformed_docs = [dict(good, key_id="foreign"), dict(good, unexpected="extra")]
    for document in malformed_docs:
        public.write_text(json.dumps(document))
        failed = _call("UserPromptSubmit", store, event)
        assert json.loads(failed.stdout) == {}
        assert "could not complete" in failed.stderr
        with pytest.raises(ConsumerError):
            with ConsumerStore(store):
                pass
    public.write_bytes(original)
    assert "Friday delivery" in _call("UserPromptSubmit", store, event).stdout


def test_public_recall_rejects_unsupported_store_modes_and_purge_ledger(tmp_path):
    store, root, transcript = _setup(tmp_path)
    _call("Stop", store, {"hook_event_name": "Stop", "transcript_path": str(transcript)}, [root])
    with ConsumerStore(store) as customer:
        customer.apply_proposal(customer.pending_proposals()[0]["digest"])
    event = {"hook_event_name": "UserPromptSubmit", "prompt": "What did we decide about Friday delivery?"}
    for name in ("store-v2", "brain.db", "purged-ids.jsonl"):
        path = store / name
        path.write_bytes(b"unsupported\n")
        path.chmod(0o600)
        assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
        path.unlink()
        path.symlink_to(store / "absent")
        assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
        path.unlink()
    for name in ("facts.json", "signing-key.pub"):
        path = store / name
        path.chmod(0o644)
        assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
        path.chmod(0o600)
    temporary = store / "facts.json.abcdefgh.tmp"
    temporary.write_bytes(b"interrupted publication")
    temporary.chmod(0o600)
    assert json.loads(_call("UserPromptSubmit", store, event).stdout) == {}
    with ConsumerStore(store) as customer:
        assert customer.recovery_required
        customer.recover()
    assert "Friday delivery" in _call("UserPromptSubmit", store, event).stdout


def test_pinned_parent_alias_cannot_change_transcript_boundary(tmp_path):
    store, _, _ = _setup(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for parent in (first, second):
        directory = parent / "transcripts"
        directory.mkdir(parents=True)
        _transcript(directory / "session.jsonl")
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    lexical_root = alias / "transcripts"
    settings = json.loads(setup_hooks(store, [lexical_root]).read_text())
    command = shlex.split(settings["hooks"]["Stop"][0]["hooks"][0]["command"])
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    result = subprocess.run(command, input=json.dumps({
        "hook_event_name": "Stop",
        "transcript_path": str(lexical_root / "session.jsonl")}),
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert "could not complete" in result.stderr
    assert not list((store / "proposals").glob("*.json"))
