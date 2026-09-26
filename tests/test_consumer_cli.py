"""Customer CLI flow using only synthetic sources and isolated destinations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "consumer-brain.py"
EXPLORER = ROOT / "bin" / "explore-memory.py"


def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {**os.environ, "HOME": str(home), "NOCKBRAIN_SIGNING_KEY": str(home / "absent-key"),
            "NOCKBRAIN_SIGNING_KEY_PUB": str(home / "absent-pub"),
            "NOCKBRAIN_STORE": "sqlite", "NOCKBRAIN_MACHINE": "fleet-02",
            "PYTHONDONTWRITEBYTECODE": "1"}


def run(tmp_path, *args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=ROOT,
                          env=env(tmp_path), text=True, capture_output=True, timeout=10)


def payload(result, code=0):
    assert result.returncode == code, result.stderr + result.stdout
    assert not result.stderr
    return json.loads(result.stdout)


def test_help_and_bad_arguments_do_not_touch_home(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0 and "usage:" in result.stdout
    assert run(tmp_path, "propose", "--store", "\x1b[31m").returncode != 0
    bad = run(tmp_path, "unknown\x1b[31m")
    assert bad.returncode != 0 and "\x1b" not in bad.stdout + bad.stderr
    assert not (tmp_path / "home" / ".nock-brain").exists()
    assert not (tmp_path / "home" / "absent-key").exists()


def test_explicit_review_apply_and_explorer(tmp_path):
    selected = tmp_path / "notes $(printf unsafe).md"
    selected.write_text("- [DECISION] Friday delivery is the release target.\n"
                        "- [DIRECTIVE] Keep deployment approval with the operator.\n")
    unselected = tmp_path / "unselected.md"
    unselected.write_text("- [DECISION] Never import this source.\n")
    selected_bytes = selected.read_bytes()
    store = tmp_path / "customer brain"
    initialized = payload(run(tmp_path, "init", "--store", store))
    assert initialized["store_id"] and "signing-key" not in initialized
    assert json.loads((store / "facts.json").read_text()) == []
    proposed = payload(run(tmp_path, "propose", "--store", store, "--format", "markdown",
                           "--source", selected))
    assert proposed["candidates"] == 2
    assert len(proposed["proposal"]) == 64
    assert "review --store" in proposed["review_command"]
    assert json.loads((store / "facts.json").read_text()) == []
    reviewed_result = run(tmp_path, "review", "--store", store,
                          "--proposal", proposed["proposal"])
    reviewed = payload(reviewed_result)
    assert reviewed["review"]["stats"]["candidates"] == 2
    assert reviewed["review"]["sources"][0]["sha256"]
    assert all("Never import" not in item["content"] for item in reviewed["review"]["candidates"])
    assert "apply --store" in reviewed["apply_command"]
    assert selected.read_bytes() == selected_bytes
    assert not (store / "brain.db").exists()
    applied = payload(run(tmp_path, "apply", "--store", store,
                          "--proposal", proposed["proposal"]))
    assert applied == {"added": 2, "skipped": 0, "total": 2}
    assert len(json.loads((store / "facts.json").read_text())) == 2
    assert "stale" in payload(run(tmp_path, "apply", "--store", store,
                                  "--proposal", proposed["proposal"]), code=2)["error"]
    proposed_again = payload(run(tmp_path, "propose", "--store", store,
                                  "--format", "markdown", "--source", selected))
    assert payload(run(tmp_path, "apply", "--store", store,
                       "--proposal", proposed_again["proposal"])) == {
                           "added": 0, "skipped": 2, "total": 2}
    assert selected.read_bytes() == selected_bytes
    assert unselected.read_text() == "- [DECISION] Never import this source.\n"

    explorer_env = env(tmp_path)
    explorer_env["NOCKBRAIN_STORE"] = "json"
    process = subprocess.Popen([sys.executable, str(EXPLORER), "--store", str(store)],
                               cwd=ROOT, env=explorer_env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = process.stdout.readline().strip()
        parts = urlsplit(url)
        token = parts.fragment.removeprefix("token=")
        request = Request(f"{parts.scheme}://{parts.netloc}/api/summary",
                          headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=5) as response:
            summary = json.load(response)
        assert summary["counts"]["facts"] == 2
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_safe_output_invalid_paths_and_key_mismatch(tmp_path):
    store = tmp_path / "brain; $(touch escaped)"
    payload(run(tmp_path, "init", "--store", store))
    source = tmp_path / "a; $(touch escaped).md"
    source.write_text("- [DECISION] Use the green button for release.\n")
    proposed = payload(run(tmp_path, "propose", "--store", store,
                           "--format", "markdown", "--source", source))
    assert "'" in proposed["review_command"]
    assert not (ROOT / "escaped").exists()
    review = payload(run(tmp_path, "review", "--store", store,
                         "--proposal", proposed["proposal"]))
    assert "'" in review["apply_command"]
    control = run(tmp_path, "init", "--store", str(tmp_path / "bad\x1b[31m"))
    assert "\x1b" not in control.stdout + control.stderr
    assert not (tmp_path / "bad\x1b[31m").exists()
    other = tmp_path / "other"
    payload(run(tmp_path, "init", "--store", other))
    (store / "signing-key.pub").write_bytes((other / "signing-key.pub").read_bytes())
    assert "keys" in payload(run(tmp_path, "apply", "--store", store,
                                 "--proposal", proposed["proposal"]), code=2)["error"]
