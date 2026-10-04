"""Regression tests for the 2026-10-04 Compare audit (see docs/compare_audit_2026-10.md).

Coverage:
  - diff engine: one-word changes in long paragraphs are no longer filtered
  - truncated diff surfaces a user warning
  - /compare/analyze: mixed file types, server-side hashes for the cache lookup
  - volume path guard (/compare/load, auto-save routes)
  - impact search: cache key, "no changes" in French, REF exclusion, change-list
    budget per candidate, unverifiable "impacted" verdicts, judge cancellation
"""

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.services.impact_queries import changes_to_queries
from server.services.processors._diff_engines import (
    DIFF_TRUNCATED_MARKER,
    is_substantive_keep_modals,
    paragraph_semantic_diff,
    truncate_diff,
)
from server.services.processors.factory import diff_truncation_warnings
from server.services import vector_search
from server.services.vector_search import _changes_block, _exclusion_keys, _judged_doc


# ---------------------------------------------------------------------------
# Diff engine — word-level changes
# ---------------------------------------------------------------------------

_LONG_EN = (
    'The operator shall record the inspection results in the quality register before the part is '
    'released to the next production step and shall keep the record available for the customer '
    'representative during the whole retention period defined by the contract'
)
_LONG_FR = (
    "La valeur mesurée est ≤ 5 unités selon la procédure interne applicable au site et le contrôleur "
    "peut valider le dossier avant expédition au client final après accord du responsable qualité"
)


@pytest.mark.parametrize('old, new', [
    ('shall record', 'shall not record'),   # negation
    ('before the part', 'after the part'),  # sequence reversed
    ('operator', 'inspector'),              # responsibility moved
    ('shall record', 'may record'),         # obligation level
])
def test_single_word_change_in_long_paragraph_is_reported(old, new):
    changed = _LONG_EN.replace(old, new)
    assert is_substantive_keep_modals(_LONG_EN, changed)
    diff, _ = paragraph_semantic_diff(f'{_LONG_EN} [Page 1]', f'{changed} [Page 1]', page_label='Page')
    assert 'MODIFIED' in diff


@pytest.mark.parametrize('old, new', [
    ('peut', 'doit'),        # French modal crossing groups
    ('≤', '≥'),              # limit reversed
    ('interne', 'externe'),
    ('avant', 'après'),
])
def test_single_word_change_in_french_paragraph_is_reported(old, new):
    assert is_substantive_keep_modals(_LONG_FR, _LONG_FR.replace(old, new, 1))


@pytest.mark.parametrize('old, new', [
    ('quality register', 'quality-register'),   # hyphenation
    ('shall record', 'must record'),            # same obligation group
    ('the part', 'a part'),                     # article
    ('operator', 'Operator,'),                  # case + punctuation
    ('retention period', 'retention periods'),  # spelling variant
])
def test_layout_only_differences_stay_filtered(old, new):
    assert not is_substantive_keep_modals(_LONG_EN, _LONG_EN.replace(old, new))


def test_french_same_group_modal_swap_stays_filtered():
    assert not is_substantive_keep_modals(_LONG_FR, _LONG_FR.replace('peut', 'peuvent', 1))


# ---------------------------------------------------------------------------
# Truncated diff
# ---------------------------------------------------------------------------

def test_truncated_diff_produces_a_user_warning(monkeypatch):
    monkeypatch.setattr('server.services.processors._diff_engines._MAX_DIFF_CHARS', 50)
    text = truncate_diff('ADDED: ' + 'x' * 200)
    assert DIFF_TRUNCATED_MARKER in text
    messages = [{'role': 'system', 'content': 'prompt'},
                {'role': 'user', 'content': [{'type': 'text', 'text': text}]}]
    assert len(diff_truncation_warnings(messages)) == 1


def test_untruncated_diff_has_no_warning():
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'ADDED: a line'},
                                              {'type': 'image_url', 'image_url': {'url': 'data:...'}}]}]
    assert diff_truncation_warnings(messages) == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app as fastapi_app
        yield fastapi_app


@pytest.fixture
def client(app, monkeypatch):
    from server.services.user import require_compare
    monkeypatch.setenv('COMPARE_ENABLED', 'true')
    monkeypatch.setenv('COMPARE_ANALYSIS_ENDPOINT', 'test-endpoint')
    monkeypatch.setenv('DATABRICKS_HOST', 'https://example.test')
    monkeypatch.setenv('DATABRICKS_TOKEN', 'token')
    monkeypatch.setenv('COMPARE_VOLUME_PATH', '/Volumes/cat/sch/compare')
    app.dependency_overrides[require_compare] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sse(text: str) -> list:
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith('data: ') and line != 'data: [DONE]']


def test_analyze_rejects_mixed_file_types(client):
    res = client.post('/api/compare/analyze', files={
        'old_file': ('spec_A.pdf', b'%PDF-1.4 old', 'application/pdf'),
        'new_file': ('spec_B.docx', b'PK new', 'application/octet-stream'),
    })
    events = _sse(res.text)
    assert events[0]['type'] == 'error'
    assert 'same type' in events[0]['error']


def test_analyze_cache_lookup_uses_server_side_hashes(client):
    old_bytes, new_bytes = b'%PDF-1.4 old revision', b'%PDF-1.4 new revision'
    cached = {'id': 7, 'analysis_text': 'cached report', 'file_type': 'pdf',
              'processing_method': 'standard', 'processor_version': 'standard', 'volume_session_path': ''}
    with patch('server.routers.compare._get_cached_analysis', new=AsyncMock(return_value=cached)) as lookup:
        res = client.post(
            '/api/compare/analyze',
            files={'old_file': ('a.pdf', old_bytes, 'application/pdf'),
                   'new_file': ('b.pdf', new_bytes, 'application/pdf')},
            data={'old_file_hash': 'stale-client-hash', 'new_file_hash': 'stale-client-hash',
                  'processor_version': 'standard'},
        )
    assert lookup.await_args.args[:2] == (
        hashlib.sha256(old_bytes).hexdigest(), hashlib.sha256(new_bytes).hexdigest(),
    )
    assert _sse(res.text)[0]['cached'] is True


@pytest.mark.parametrize('session_path', [
    '/Volumes/cat/sch/compare/../../other/secret',
    '/Volumes/cat/sch/compare_other/2026-01-01',
    '/Volumes/elsewhere/x',
])
def test_load_rejects_paths_outside_the_volume(client, session_path):
    res = client.get('/api/compare/load', params={'session_path': session_path})
    assert res.status_code == 403


def test_save_result_rejects_paths_outside_the_volume(client):
    res = client.post('/api/compare/save-result', data={
        'session_path': '/Volumes/cat/sch/compare/../../other', 'filename': 'analysis.md', 'content': 'x',
    })
    assert res.status_code == 400


def test_is_within_volume_accepts_session_folders():
    from server.routers.compare import _is_within_volume
    assert _is_within_volume('/Volumes/cat/sch/compare/2026-10-04_101500', '/Volumes/cat/sch/compare')
    assert _is_within_volume('/Volumes/cat/sch/compare', '/Volumes/cat/sch/compare/')


def test_export_excel_route(client):
    res = client.post('/api/compare/export-excel', data={'json_text': '[]', 'filename': 'cmp'})
    assert res.status_code == 200
    assert res.headers['content-disposition'].endswith('cmp.xlsx"')


# ---------------------------------------------------------------------------
# Impact search
# ---------------------------------------------------------------------------

def test_impact_cache_key_depends_on_changes_and_index():
    from server.routers.compare import _impact_cache_version
    cfg = {'impact_index': 'cat.sch.chunks_index_v1', 'impact_endpoint': 'judge',
           'impact_max_queries': 30, 'impact_per_query_results': 40, 'impact_max_candidates': 15}
    base = _impact_cache_version('[{"section": "4.3"}]', cfg)
    assert base == _impact_cache_version('[{"section": "4.3"}]\n', cfg)
    assert base != _impact_cache_version('## 4.3 Torque\n- changed', cfg)
    assert base != _impact_cache_version('[{"section": "4.3"}]', {**cfg, 'impact_index': 'cat.sch.chunks_full_index_v1'})


@pytest.mark.parametrize('text', [
    '**Aucun changement significatif détecté.**',
    'Aucune modification significative détectée.',
    '**No significant changes detected.**',
])
def test_no_changes_phrase_recognised_in_document_language(text):
    assert changes_to_queries(text)['queries'] == []


def test_exclusion_does_not_swallow_a_shorter_ref():
    refs = ['GO-131', 'GO-1316_GB', 'GO-13160']
    assert _exclusion_keys(['GO-1316_FR rev B.pdf'], refs) == frozenset({'GO1316'})


def _changes(n, size=400):
    return [{'id': f'C{i}', 'criticality': 'high', 'text': f'change {i} ' + 'x' * size, 'searched': True}
            for i in range(1, n + 1)]


def test_changes_block_unchanged_when_within_budget():
    block = _changes_block(_changes(3, size=10), max_chars=1000)
    assert block.splitlines() == [f'C{i} [high]: change {i} ' + 'x' * 10 for i in (1, 2, 3)]


def test_changes_block_keeps_the_changes_that_retrieved_the_candidate():
    block = _changes_block(_changes(20), max_chars=2000, priority_ids=('C19', 'C20'))
    lines = block.splitlines()
    assert any(line.startswith('C19 ') for line in lines)
    assert any(line.startswith('C20 ') for line in lines)
    assert all(len(line) > 400 for line in lines[:-1])  # whole changes only
    assert 'omitted' in lines[-1]
    assert len(block) <= 2000 + len(lines[-1]) + 1


def _cand():
    return {'iddoc': '1', 'ref': 'PR-1', 'title': 't', 'division': 'd', 'url': '', 'doc_date': '',
            'change_ids': ['C1'], 'max_score': 0.5, 'variants': [],
            'passages': [{'chunk_id': 'a-0', 'header': 'H', 'page': '', 'text': 'apply 35 Nm', 'change_ids': ['C1']}]}


def test_impacted_without_any_valid_passage_is_to_check():
    doc = _judged_doc(_cand(), {'impacted': True, 'confidence': 'high', 'reason': 'r',
                                'passages': [{'passage': 9, 'changes': ['C1'], 'quote': 'x'}]}, '')
    assert doc['status'] == 'check'


def test_passage_number_given_as_float_is_accepted():
    doc = _judged_doc(_cand(), {'impacted': True, 'confidence': 'high', 'reason': 'r',
                                'passages': [{'passage': 1.0, 'changes': 'C1', 'quote': '35 Nm'}]}, '')
    assert doc['status'] == 'impacted'
    assert doc['passages'][0]['highlight'] == [6, 11]


def test_judge_calls_are_cancelled_when_the_consumer_stops(monkeypatch):
    started, cancelled = [], []

    async def fake_fetch(host, token, index_name, queries, num_results):
        chunks = [{'chunk_id': f'D{i}-0', 'IDDOC': str(i), 'REF': f'PR-{i}00', 'division': '', 'url': '',
                   'semantic_headers': '', 'chunk_text': 'text', 'score': 1.0 - i / 10, '_change_ids': ['C1']}
                  for i in range(4)]
        return {'chunks': chunks, 'queries_failed': 0}

    async def slow_judge(host, token, endpoint, changes_text, cand, max_tokens):
        started.append(cand['ref'])
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(cand['ref'])
            raise

    monkeypatch.setattr(vector_search, '_fetch_chunks_multi', fake_fetch)
    monkeypatch.setattr(vector_search, '_judge', slow_judge)

    async def run():
        stream = vector_search.run_impact_search(
            host='h', token='t', index_name='i', llm_endpoint='e',
            extracted={'changes': _changes(1, size=5), 'source': 'structured',
                       'queries': [{'text': 'q', 'change_ids': ['C1']}]},
            num_results=5, max_candidates=4, max_changes_chars=1000, max_tokens=100,
            archive_before='', exclude_names=[],
        )
        assert (await stream.__anext__())['type'] == 'plan'
        waiter = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)       # judges are now running
        waiter.cancel()                 # the client went away
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert len(started) == 4
    assert sorted(cancelled) == sorted(started)
