"""POST /compare/analyze: the SSE stream of one analysis, with the run saved on its llm_requests row."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import server.app as app_module
from server.routers.compare import analyze


def _events(body: str) -> list:
    return [line[6:] for line in body.splitlines() if line.startswith('data: ')]


def _post(monkeypatch, llm_chunks, warnings=('scanned PDF',)):
    """Run one analysis with a fake processor and a fake model stream; return (response text, mocks)."""
    monkeypatch.setenv('COMPARE_ANALYSIS_ENDPOINT', 'databricks-test')
    monkeypatch.setenv('CAPS_BYPASS', 'true')
    result = SimpleNamespace(
        messages=[{'role': 'user', 'content': 'diff'}], warnings=list(warnings), image_pairs=[],
        metadata=SimpleNamespace(file_type='docx', method='standard'))
    processor = SimpleNamespace(build_messages=lambda *a: result)

    async def fake_stream(*args, **kwargs):
        for chunk in llm_chunks:
            yield chunk

    mocks = SimpleNamespace(run=AsyncMock(), usage=AsyncMock(), store=AsyncMock(return_value=7), cache=AsyncMock())
    with patch.object(analyze, 'get_credentials', return_value=('https://host', 'tok')), \
            patch.object(analyze, 'get_user_identity', AsyncMock(return_value={'user_id': 'u1', 'workspace_id': None, 'email': None})), \
            patch.object(analyze, 'get_processor', return_value=processor), \
            patch.object(analyze, 'get_cached_analysis', AsyncMock(return_value=None)), \
            patch.object(analyze, 'store_analysis_cache', mocks.cache), \
            patch.object(analyze, 'stream_analysis', fake_stream), \
            patch.object(analyze, 'store_llm_request', mocks.store), \
            patch.object(analyze, 'update_llm_request_run', mocks.run), \
            patch.object(analyze, 'update_llm_request_usage', mocks.usage):
        client = TestClient(app_module.app)
        response = client.post('/api/compare/analyze', files={'old_file': ('a.docx', b'old'),
                                                               'new_file': ('b.docx', b'new')})
    return response, mocks


def _post_clean(monkeypatch, llm_chunks, warnings=()):
    """``_post`` with a processor that reports no extraction warning."""
    return _post(monkeypatch, llm_chunks, warnings)


def test_a_successful_analysis_streams_metadata_warnings_and_text(monkeypatch):
    delta = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'Changed.'}) + '\n\n'
    usage = 'data: ' + json.dumps({'type': 'usage', 'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 8}) + '\n\n'
    response, mocks = _post(monkeypatch, [delta, usage, 'data: [DONE]\n\n'])
    assert response.status_code == 200
    events = _events(response.text)
    types = [json.loads(e)['type'] for e in events if e != '[DONE]']
    assert types == ['metadata', 'warning', 'response.output_text.delta', 'usage'] and events[-1] == '[DONE]'
    assert json.loads(events[0])['llm_request_id'] == 7
    mocks.run.assert_awaited_once()
    saved = mocks.run.await_args.kwargs
    assert mocks.run.await_args.args == (7,) and saved['status'] == 'ok' and 'first_token_ms' in saved
    mocks.usage.assert_any_await(7, input_tokens=5, output_tokens=3, thinking_tokens=0, total_tokens=8,
                                 cost_eur=0.0, http_status=200)


def test_a_model_error_reaches_the_browser_without_its_technical_detail(monkeypatch):
    error = 'data: ' + json.dumps({'type': 'error', 'error': 'Tired', 'detail': 'secret trace'}) + '\n\n'
    response, mocks = _post(monkeypatch, [error, 'data: [DONE]\n\n'])
    assert 'secret trace' not in response.text and 'Tired' in response.text
    assert mocks.run.await_args.kwargs['status'] == 'error'


def test_a_complete_report_is_cached_by_the_server_for_the_next_user(monkeypatch):
    delta = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'Report.'}) + '\n\n'
    _, mocks = _post_clean(monkeypatch, [delta, 'data: [DONE]\n\n'])
    mocks.cache.assert_awaited_once()
    args = mocks.cache.await_args.args
    assert args[3] == 'Report.' and args[2].startswith(analyze.APP_VERSION + ':')


def test_a_cut_report_is_not_cached(monkeypatch):
    delta = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'Partial'}) + '\n\n'
    cut = 'data: ' + json.dumps({'type': 'warning', 'warning': 'output_truncated'}) + '\n\n'
    _, mocks = _post_clean(monkeypatch, [delta, cut, 'data: [DONE]\n\n'])
    mocks.cache.assert_not_awaited()


def test_a_report_with_an_extraction_warning_is_not_cached(monkeypatch):
    delta = 'data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'Report.'}) + '\n\n'
    _, mocks = _post_clean(monkeypatch, [delta, 'data: [DONE]\n\n'], warnings=['scanned PDF'])
    mocks.cache.assert_not_awaited()


def test_the_cache_version_follows_the_prompt_the_model_and_the_method():
    cfg = {'analysis_system_prompt': 'p', 'analysis_endpoint': 'e', 'max_tokens': 1, 'thinking_budget': 0,
           'temperature': 0.0, 'chunk_threshold_chars': 10, 'chunk_size_chars': 5}
    processor = SimpleNamespace(method='standard')
    base = analyze._analysis_cache_version(cfg, processor, '')
    assert analyze._analysis_cache_version({**cfg, 'analysis_system_prompt': 'other'}, processor, '') != base
    assert analyze._analysis_cache_version({**cfg, 'analysis_endpoint': 'other'}, processor, '') != base
    assert analyze._analysis_cache_version(cfg, SimpleNamespace(method='structured'), '') != base
    assert analyze._analysis_cache_version(cfg, processor, '') == base
