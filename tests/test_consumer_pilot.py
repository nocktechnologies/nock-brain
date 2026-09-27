"""A complete customer journey, using invented notes and separate local stores."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "consumer-brain.py"


def invoke(tmp_path, *args):
    home = tmp_path / "unrelated-home"
    home.mkdir(exist_ok=True)
    environment = {**os.environ, "HOME": str(home), "NOCKBRAIN_STORE": "sqlite",
                   "NOCKBRAIN_SIGNING_KEY": str(home / "missing-key"),
                   "NOCKBRAIN_SIGNING_PUB": str(home / "missing-public-key"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)],
                               cwd=tmp_path, env=environment, capture_output=True,
                               text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def import_notes(tmp_path, store, source):
    return invoke(tmp_path, "propose", "--store", store, "--format", "markdown",
                  "--source", source)["proposal"]


def recalled_context(store):
    event = {"hook_event_name": "UserPromptSubmit",
             "prompt": "What did we decide about workshop report delivery?"}
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "bin" / "consumer-hook.py"),
         "UserPromptSubmit", "--store", str(store)],
        input=json.dumps(event), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr, result.stderr
    return json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_customer_correction_forgetting_and_reimport(tmp_path):
    store = tmp_path / "workshop-brain"
    other = tmp_path / "separate-brain"
    one = invoke(tmp_path, "init", "--store", store)
    two = invoke(tmp_path, "init", "--store", other)
    assert one["store_id"] != two["store_id"]
    assert (store / ".gitignore").read_text() == "*\n"
    status = invoke(tmp_path, "status", "--store", store)
    assert "commands" in status

    notes = tmp_path / "workshop.md"
    notes.write_text("- [DECISION] Ship the Willow workshop report on Friday.\n"
                     "- [DIRECTIVE] Keep report approval with the workshop operator.\n")
    original_source = notes.read_bytes()
    initial = import_notes(tmp_path, store, notes)
    reviewed = invoke(tmp_path, "review", "--store", store, "--proposal", initial)
    assert len(reviewed["review"]["candidates"]) == 2
    assert json.loads((store / "facts.json").read_text()) == []
    invoke(tmp_path, "apply", "--store", store, "--proposal", initial)
    before = json.loads((store / "facts.json").read_text())
    old = next(fact for fact in before if fact["kind"] == "decision")
    unaffected = next(fact for fact in before if fact["kind"] == "directive")

    replacement = tmp_path / "revised.md"
    replacement.write_text("- [DECISION] Ship the Willow workshop report on Monday.\n")
    replacement_digest = import_notes(tmp_path, store, replacement)
    correction = invoke(tmp_path, "correct", "--store", store, "--fact", old["id"],
                        "--replacement-proposal", replacement_digest)["proposal"]
    correction_review = invoke(tmp_path, "review", "--store", store, "--proposal", correction)
    assert correction_review["review"]["action"] == "correct"
    assert json.loads((store / "facts.json").read_text()) == before
    invoke(tmp_path, "apply", "--store", store, "--proposal", correction)
    corrected = json.loads((store / "facts.json").read_text())
    historical = next(fact for fact in corrected if fact["id"] == old["id"])
    assert historical["status"] == "superseded"
    assert {key: historical[key] for key in ("id", "kind", "content", "evidence", "attestation")} == {
        key: old[key] for key in ("id", "kind", "content", "evidence", "attestation")}
    assert next(fact for fact in corrected if fact["id"] == unaffected["id"]) == unaffected
    current = next(fact for fact in corrected if "Monday" in fact["content"])
    recalled = recalled_context(store)
    assert "Monday" in recalled and "Friday" not in recalled

    forgetting = invoke(tmp_path, "forget", "--store", store, "--fact", current["id"])["proposal"]
    forget_review = invoke(tmp_path, "review", "--store", store, "--proposal", forgetting)
    assert forget_review["review"]["action"] == "forget"
    invoke(tmp_path, "apply", "--store", store, "--proposal", forgetting)
    assert invoke(tmp_path, "pending", "--store", store)["proposals"] == []
    assert current["id"] not in {fact["id"] for fact in json.loads((store / "facts.json").read_text())}
    assert b"Monday" not in (store / "revocations.jsonl").read_bytes()
    recalled = recalled_context(store)
    assert "Monday" not in recalled and "Friday" not in recalled

    # Both the corrected old ID and the forgotten replacement stay suppressed.
    replay = import_notes(tmp_path, store, replacement)
    assert invoke(tmp_path, "apply", "--store", store, "--proposal", replay)["added"] == 0
    replay_old = import_notes(tmp_path, store, notes)
    assert invoke(tmp_path, "apply", "--store", store, "--proposal", replay_old)["added"] == 0
    invoke(tmp_path, "discard", "--store", store, "--proposal", replay)
    assert notes.read_bytes() == original_source
    assert json.loads((other / "facts.json").read_text()) == []
    assert not (tmp_path / "unrelated-home" / ".nock-brain").exists()


def test_session_setup_is_explicit_and_does_not_touch_global_settings(tmp_path):
    store = tmp_path / "brain $(touch escaped)"
    transcripts = tmp_path / "selected project"
    transcripts.mkdir()
    invoke(tmp_path, "init", "--store", store)
    global_dir = tmp_path / "unrelated-home" / ".claude"
    global_dir.mkdir()
    original = b'{"unrelated":"settings"}\n'
    (global_dir / "settings.json").write_bytes(original)
    result = invoke(tmp_path, "setup-hooks", "--store", store,
                    "--transcript-root", transcripts)
    settings = Path(result["settings"])
    assert settings == store / "claude-settings.json"
    assert settings.stat().st_mode & 0o077 == 0
    assert shlex.split(result["launch_command"]) == ["claude", "--settings", str(settings)]
    hooks = json.loads(settings.read_text())["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "Stop"}
    assert (global_dir / "settings.json").read_bytes() == original
    assert not (tmp_path / "escaped").exists()
