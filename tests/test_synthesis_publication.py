"""Publication must preserve the prior verified synthesis on every failure."""
import copy
import json
import sys

import pytest


def prepared(synthesize, sign_lib, tmp_path, monkeypatch, *, llm=True):
    key_path = tmp_path / 'signing-key'
    pub_path = tmp_path / 'signing-key.pub'
    key = sign_lib.load_or_create_key(key_path, pub_path, alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv('NOCKBRAIN_SIGNING_KEY', str(key_path))
    monkeypatch.setenv('NOCKBRAIN_SIGNING_PUB', str(pub_path))
    facts = [dict(id=f'f{i}', kind='correction', content=f'Confirm pricing tier before release {i}',
                  source_date=f'2026-06-0{i+1}', status='current', confidence=0.9,
                  evidence=[{'event_id': f'event{i}'}]) for i in range(2)]
    source = tmp_path / 'facts.json'
    source.write_text(json.dumps(sign_lib.sign_facts(facts, key)))
    output = tmp_path / 'insights.json'
    previous = sign_lib.sign_facts([dict(id='prior', kind='insight',
                                      content='Previously verified lesson', evidence=[])], key)
    output.write_text(json.dumps(previous))
    before = output.read_bytes()
    monkeypatch.setattr(synthesize, '_call_claude', lambda *args: 'Confirm pricing before every release.')
    args = ['synthesize.py', '--facts', str(source), '--output', str(output)]
    if llm:
        args.append('--llm')
    monkeypatch.setattr(sys, 'argv', args)
    return source, output, before, key, key_path


def test_scheduled_llm_without_sign_flag_publishes_verified_insights(synthesize, sign_lib, tmp_path, monkeypatch):
    _, output, _, key, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    assert synthesize.main() == 0
    published = json.loads(output.read_text())
    assert published
    assert sign_lib.verify_facts(published, key)['valid'] == len(published)
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['missing_key', 'corrupt_key', 'signing_error', 'unsigned_result', 'dropped_result', 'invalid_source', 'corrupt_source', 'invalid_result', 'write_error'])
def test_failed_synthesis_preserves_prior_artifact(synthesize, sign_lib, tmp_path, monkeypatch, failure):
    source, output, before, _, key_path = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    if failure == 'missing_key':
        key_path.unlink()
    elif failure == 'corrupt_key':
        key_path.write_text('not a key')
    elif failure == 'signing_error':
        def fail(*args, **kwargs):
            raise RuntimeError('signer unavailable')
        monkeypatch.setattr(synthesize, '_sign_insights', fail)
    elif failure == 'unsigned_result':
        monkeypatch.setattr(synthesize, '_sign_insights', lambda items, **kwargs: items)
    elif failure == 'dropped_result':
        monkeypatch.setattr(synthesize, '_sign_insights', lambda items, **kwargs: [])
    elif failure == 'corrupt_source':
        source.write_bytes(b'\xff not JSON')
    elif failure == 'invalid_source':
        data = json.loads(source.read_text())
        data[0]['content'] = 'Changed after signing'
        source.write_text(json.dumps(data))
    elif failure == 'invalid_result':
        monkeypatch.setattr(synthesize, 'synthesize', lambda *args, **kwargs: [dict(id='broken', kind='insight', content='x')])
    elif failure == 'write_error':
        import _store
        def fail(*args, **kwargs):
            raise OSError('disk full before rename')
        monkeypatch.setattr(_store.os, 'replace', fail)
    assert synthesize.main() == 1
    assert output.read_bytes() == before


def test_changed_source_during_synthesis_preserves_prior_artifact(synthesize, sign_lib, tmp_path, monkeypatch):
    source, output, before, _, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    def changed(*args):
        source.write_text(source.read_text() + '\n')
        return 'Confirm pricing before every release.'
    monkeypatch.setattr(synthesize, '_call_claude', changed)
    assert synthesize.main() == 1
    assert output.read_bytes() == before


def test_provenance_is_signature_bound(synthesize, sign_lib, tmp_path, monkeypatch):
    _, output, _, key, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    assert synthesize.main() == 0
    published = json.loads(output.read_text())
    assert published[0]['evidence'][0]['input_ids']
    changed = copy.deepcopy(published)
    changed[0]['evidence'][0]['input_ids'].append('unseen-source')
    assert sign_lib.verify_facts(changed, key)['tampered'] == 1


def test_slow_generation_cannot_replace_newer_output_from_same_source(synthesize, sign_lib, tmp_path, monkeypatch):
    """Two independent loads of the module share the output publication lock."""
    import concurrent.futures
    import importlib.util
    import threading

    source, output, _, key, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    spec = importlib.util.spec_from_file_location('independent_synthesizer', synthesize.__file__)
    newer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(newer)
    started, resume = threading.Event(), threading.Event()

    def slow(inputs, heuristic):
        started.set()
        assert resume.wait(5), 'newer generation did not complete'
        return 'Older generation pricing lesson completed after the newer one.'

    with concurrent.futures.ThreadPoolExecutor() as pool:
        old = pool.submit(synthesize.publish_insights, source, output, synthesizer=slow)
        try:
            assert started.wait(5), 'older generation did not reach its input barrier'
            newer.publish_insights(source, output, synthesizer=lambda *args: 'Newer generation pricing lesson must remain published.')
            published = output.read_bytes()
        finally:
            resume.set()
        with pytest.raises(ValueError, match='stale synthesis generation'):
            old.result(timeout=5)
    assert output.read_bytes() == published
    assert sign_lib.verify_facts(json.loads(published), key)['valid'] == 1


def test_first_publication_also_rejects_stale_generation(synthesize, sign_lib, tmp_path, monkeypatch):
    source, output, _, _, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    output.unlink()
    latest = []

    def competing(inputs, heuristic):
        # A reentrant independent publication finishes while this generation
        # is still producing its content. Generation does not hold the lock.
        synthesize.publish_insights(source, output)
        latest.append(output.read_bytes())
        return 'An older pricing lesson must not overwrite the first publication.'

    with pytest.raises(ValueError, match='stale synthesis generation'):
        synthesize.publish_insights(source, output, synthesizer=competing)
    assert output.read_bytes() == latest[0]


def test_publication_lock_is_shared_by_independent_processes(synthesize, sign_lib, tmp_path, monkeypatch):
    import subprocess
    source, output, _, _, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    driver = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
print("started", flush=True)
worker.publish_insights(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
print("published", flush=True)
'''
    proc = None
    try:
        with synthesize._output_lock(output.resolve()):
            proc = subprocess.Popen([sys.executable, '-c', driver, synthesize.__file__, str(source), str(output)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert proc.stdout.readline() == 'started\n'
            with pytest.raises(subprocess.TimeoutExpired):
                proc.wait(timeout=0.1)
        stdout, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0, stderr
        assert stdout == 'published\n'
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert (output.parent / ('.' + output.name + '.synthesis.lock')).stat().st_mode & 0o777 == 0o600


def test_byte_identical_output_replacement_is_still_a_new_generation(synthesize, sign_lib, tmp_path, monkeypatch):
    source, output, before, _, _ = prepared(synthesize, sign_lib, tmp_path, monkeypatch)
    def competing(inputs, heuristic):
        synthesize.secure_replace_bytes(output, before)
        return 'Pricing lessons from a stale generation must stay unpublished.'
    with pytest.raises(ValueError, match='stale synthesis generation'):
        synthesize.publish_insights(source, output, synthesizer=competing)
    assert output.read_bytes() == before
