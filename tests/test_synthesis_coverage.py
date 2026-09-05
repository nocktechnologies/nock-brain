"""Raw answers survive any summary that is absent, partial or merely related."""
import json


def signed_fixture(synthesize, sign_lib, tmp_path, monkeypatch):
    key_path, pub_path = tmp_path / 'key', tmp_path / 'key.pub'
    key = sign_lib.load_or_create_key(key_path, pub_path, alg=sign_lib.ALG_HMAC)
    monkeypatch.setenv('NOCKBRAIN_SIGNING_KEY', str(key_path))
    monkeypatch.setenv('NOCKBRAIN_SIGNING_PUB', str(pub_path))
    rows = [dict(id=f'f{i}', kind='correction', content=f'pricing tier release {i} approved',
                 source_date=f'2026-06-0{i+1}', status='current', confidence=0.9,
                 evidence=[{'event_id': f'event{i}'}]) for i in range(2)]
    insight = synthesize.synthesize(rows)[0]
    ff, inf = tmp_path / 'facts.json', tmp_path / 'insights.json'
    ff.write_text(json.dumps(sign_lib.sign_facts(rows, key)))
    return rows, insight, ff, inf, key


def test_included_verified_insight_only_suppresses_fully_covered_source(synthesize, sign_lib, budget_recall, tmp_path, monkeypatch):
    rows, insight, ff, inf, key = signed_fixture(synthesize, sign_lib, tmp_path, monkeypatch)
    assert insight['covered_source_ids'] == ['f1']
    inf.write_text(json.dumps(sign_lib.sign_facts([insight], key)))
    selected = budget_recall.select_recall('pricing', ff, budget=400, insights_file=inf)
    assert [f['id'] for f in selected['included']] == [insight['id'], 'f0']


def test_rendered_excerpt_of_verified_insight_cannot_suppress_source(synthesize, sign_lib, budget_recall, tmp_path, monkeypatch):
    rows, insight, ff, inf, key = signed_fixture(synthesize, sign_lib, tmp_path, monkeypatch)
    insight['content'] = ('pricing lesson ' * 40) + rows[1]['content']
    inf.write_text(json.dumps(sign_lib.sign_facts([insight], key)))
    selected = budget_recall.select_recall('pricing', ff, budget=400, insights_file=inf)
    assert {f['id'] for f in selected['included']} == {insight['id'], 'f0', 'f1'}


def test_changed_source_invalidates_old_coverage(synthesize, sign_lib, budget_recall, tmp_path, monkeypatch):
    rows, insight, ff, inf, key = signed_fixture(synthesize, sign_lib, tmp_path, monkeypatch)
    rows[1]['content'] += ' after revision'
    ff.write_text(json.dumps(sign_lib.sign_facts(rows, key)))
    inf.write_text(json.dumps(sign_lib.sign_facts([insight], key)))
    selected = budget_recall.select_recall('pricing', ff, budget=400, insights_file=inf)
    assert {f['id'] for f in selected['included']} == {insight['id'], 'f0', 'f1'}


def test_reserved_raw_source_survives_covered_insight(synthesize, sign_lib, budget_recall, tmp_path, monkeypatch):
    rows, insight, ff, inf, key = signed_fixture(synthesize, sign_lib, tmp_path, monkeypatch)
    inf.write_text(json.dumps(sign_lib.sign_facts([insight], key)))
    monkeypatch.setattr(budget_recall, '_maybe_dense_fuse', lambda facts, seeds, *args: (seeds, frozenset({'f1'})))
    selected = budget_recall.select_recall('pricing', ff, budget=400, insights_file=inf, semantic=True)
    assert {f['id'] for f in selected['included']} == {insight['id'], 'f0', 'f1'}


def test_skipped_insights_do_not_consume_raw_date_diversity_slots(budget_recall, tmp_path):
    common = dict(kind='decision', status='current', confidence=0.9, source_date='2026-06-01')
    source = dict(common, id='short', content='pricing approved')
    # The long other-date raw fact cannot fit. It must not be moved ahead of
    # the short BM25 answer by summaries that were not included themselves.
    other = dict(common, id='long', content='pricing ' + 'unrelated ' * 40, source_date='2026-06-02')
    insights = [dict(common, id=f'ins{i}', kind='insight', content='pricing ' * 100, source_ids=['short'])
                for i in range(5)]
    ff, inf = tmp_path / 'facts.json', tmp_path / 'insights.json'
    ff.write_text(json.dumps([source, other])); inf.write_text(json.dumps(insights))
    baseline = budget_recall.select_recall('pricing', ff, budget=35, max_per_date=4)
    selected = budget_recall.select_recall('pricing', ff, budget=35, max_per_date=4, insights_file=inf)
    assert [f['id'] for f in baseline['included']] == ['short']
    assert [f['id'] for f in selected['included']] == ['short']
