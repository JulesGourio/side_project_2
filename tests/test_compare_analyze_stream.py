"""POST /compare/analyze: the SSE stream of one analysis, with the run saved on its llm_requests row."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import server.app as app_module
from server.routers.compare import analyze


def _events(body: str) -> list:
    return [line[6:] for line in body.splitlines() if line.startswith('data: ')]


def _post(monkeypatch, llm_chunks):
    """Run one analysis with a fake processor and a fake model stream; return (response text, mocks)."""
    monkeypatch.setenv('COMPARE_ANALYSIS_ENDPOINT', 'databricks-test')
    monkeypatch.setenv('CAPS_BYPASS', 'true')
    result = SimpleNamespace(
        messages=[{'role': 'user', 'content': 'diff'}], warnings=['scanned PDF'], image_pairs=[],
        metadata=SimpleNamespace(file_type='docx', method='standard'))
    processor = SimpleNamespace(build_messages=lambda *a: result)

    async def fake_stream(*args, **kwargs):
        for chunk in llm_chunks:
            yield chunk

    mocks = SimpleNamespace(run=AsyncMock(), usage=AsyncMock(), store=AsyncMock(return_value=7))
    with patch.object(analyze, 'get_credentials', return_value=('https://host', 'tok')), \
            patch.object(analyze, 'get_processor', return_value=processor), \
            patch.object(analyze, '_get_cached_analysis', AsyncMock(return_value=None)), \
            patch.object(analyze, 'stream_analysis', fake_stream), \
            patch.object(analyze, 'store_llm_request', mocks.store), \
            patch.object(analyze, 'update_llm_request_run', mocks.run), \
            patch.object(analyze, 'update_llm_request_usage', mocks.usage):
        client = TestClient(app_module.app)
        response = client.post('/api/compare/analyze', files={'old_file': ('a.docx', b'old'),
                                                               'new_file': ('b.docx', b'new')})
    return response, mocks


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
