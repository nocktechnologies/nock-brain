"""S3+S8: nonce-bound, watermarked job windows (harness admission + Letta
watermark, adapted). A nightly job digests its exact input set, admits once,
and settles under a nonce — so double-fires are structurally impossible and
an identical re-run is an explicit no-op, while a crash before settle means
the next run simply runs again (fail-open toward doing work)."""
import json

import pytest


@pytest.fixture()
def window():
    # loaded via conftest _load pattern
    import importlib.util
    from pathlib import Path as P
    spec = importlib.util.spec_from_file_location(
        "_window", P(__file__).resolve().parent.parent / "bin" / "_window.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_open_runs(window, tmp_path):
    state = tmp_path / "windows.json"
    verdict, token = window.open_window(state, "nightly-contradictions", "digest-a")
    assert verdict == "run" and token


def test_identical_inputs_after_settle_skip(window, tmp_path):
    state = tmp_path / "windows.json"
    verdict, token = window.open_window(state, "job", "digest-a")
    window.settle(state, "job", token, "result-1")
    verdict2, token2 = window.open_window(state, "job", "digest-a")
    assert verdict2 == "skip" and token2 is None


def test_changed_inputs_run_again(window, tmp_path):
    state = tmp_path / "windows.json"
    _, token = window.open_window(state, "job", "digest-a")
    window.settle(state, "job", token, "r")
    verdict, token2 = window.open_window(state, "job", "digest-B")
    assert verdict == "run" and token2


def test_crash_before_settle_reruns(window, tmp_path):
    state = tmp_path / "windows.json"
    window.open_window(state, "job", "digest-a")   # opened, never settled
    verdict, token = window.open_window(state, "job", "digest-a")
    assert verdict == "run" and token  # unsettled window never blocks work


def test_settle_rejects_wrong_nonce(window, tmp_path):
    state = tmp_path / "windows.json"
    _, token = window.open_window(state, "job", "digest-a")
    assert window.settle(state, "job", "not-the-token", "r") is False
    assert window.settle(state, "job", token, "r") is True
    # after a real settle, the stale token can't settle again
    assert window.settle(state, "job", token, "r2") is False


def test_jobs_are_independent(window, tmp_path):
    state = tmp_path / "windows.json"
    _, t1 = window.open_window(state, "ingest", "d1")
    window.settle(state, "ingest", t1, "r")
    verdict, t2 = window.open_window(state, "contradictions", "d1")
    assert verdict == "run" and t2  # same digest, different job -> runs


def test_corrupt_state_fails_open_to_run(window, tmp_path):
    state = tmp_path / "windows.json"
    state.write_text("not json")
    verdict, token = window.open_window(state, "job", "digest-a")
    assert verdict == "run" and token


def test_inputs_digest_helper_stable_and_sensitive(window, tmp_path):
    a = tmp_path / "a.json"; a.write_text('{"x": 1}')
    d1 = window.inputs_digest([a])
    d2 = window.inputs_digest([a])
    assert d1 == d2
    a.write_text('{"x": 2}')
    assert window.inputs_digest([a]) != d1
    # missing file digests deterministically (absent-marker), never raises
    assert window.inputs_digest([tmp_path / "missing.json"])
