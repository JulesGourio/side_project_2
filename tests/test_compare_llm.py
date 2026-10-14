"""Compare LLM calls: endpoint chain, fallback to the other Luna model, user message when everything fails."""

import asyncio
import json

import httpx
import pytest

from server.services.chat import llm as chat_llm
from server.services.compare import llm as compare_llm
from server.services.compare.llm import (
    CONTINUE_PROMPT, GPT_5_6_LUNA, GPT_6_LUNA, TIRED_MESSAGE, CompareLlmUnavailable, LlmFailure,
    compare_chain, complete_chain, stream_analysis,
)
from server.services.compare.summarize import summarize_text
from server.services.infra import vector_search
from server.services.infra.vector_search import _judge, run_impact_search

CLAUDE = 'databricks-claude-sonnet-4-6'
HOST = 'https://example.test'


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    chat_llm.reset_health()
    monkeypatch.setattr(chat_llm, 'ROUND_WAITS_S', (0.0, 0.0, 0.0))
    monkeypatch.delenv('COMPARE_LLM_FALLBACK_ENDPOINTS', raising=False)
    yield
    chat_llm.reset_health()


class FakeService:
    """Serving endpoints by name: each plan entry returns one httpx.Response; every call is recorded."""

    def __init__(self, plan, monkeypatch):
        self.plan = plan
        self.calls = []
        monkeypatch.setattr(compare_llm, 'http_client', self.http_client)

    def http_client(self, timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        endpoint = request.url.path.split('/serving-endpoints/')[1].split('/invocations')[0]
        payload = json.loads(request.content)
        self.calls.append((endpoint, payload))
        return self.plan[endpoint](payload)

    def endpoints(self):
        return [name for name, _ in self.calls]


def _completion(text):
    return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                                     'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})


def _status(code):
    return lambda payload: httpx.Response(code, text='upstream said no')


def _ok(text):
    return lambda payload: _completion(text)


def _sse_body(*deltas, error=False):
    lines = [f'data: {json.dumps({"object": "chat.completion.chunk", "choices": [{"delta": {"content": d}, "finish_reason": None}]})}\n\n'
             for d in deltas]
    if error:
        lines.append('data: {"error": {"message": "connection reset", "code": 500}}\n\n')
    lines.append('data: {"object": "chat.completion.chunk", "choices": [], '
                 '"usage": {"prompt_tokens": 3, "completion_tokens": 2}}\n\n')
    lines.append('data: [DONE]\n\n')
    return httpx.Response(200, content=''.join(lines).encode(), headers={'content-type': 'text/event-stream'})


def _sse(*deltas, error=False):
    return lambda payload: _sse_body(*deltas, error=error)


def _text(completion):
    return completion['choices'][0]['message']['content']


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [chunk async for chunk in agen]


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith('data: ') and '[DONE]' not in chunk:
            out.append(json.loads(chunk[6:]))
    return out


# --- The chain ---

@pytest.mark.parametrize('primary, expected', [
    (CLAUDE, [CLAUDE, GPT_5_6_LUNA]),
    (GPT_6_LUNA, [GPT_6_LUNA, GPT_5_6_LUNA]),
    (GPT_5_6_LUNA, [GPT_5_6_LUNA, GPT_6_LUNA]),
])
def test_default_chain_ends_with_the_other_luna_model(primary, expected):
    assert compare_chain(primary) == expected


def test_configured_fallbacks_replace_the_default(monkeypatch):
    monkeypatch.setenv('COMPARE_LLM_FALLBACK_ENDPOINTS', 'x-model, y-model')
    assert compare_chain(CLAUDE) == [CLAUDE, 'x-model', 'y-model']


# --- Non-streamed calls ---

def test_a_failing_primary_falls_back_to_the_next_endpoint(monkeypatch):
    service = FakeService({'primary': _status(500), 'fallback': _ok('answer')}, monkeypatch)
    completion, used = _run(complete_chain(HOST, 't', ['primary', 'fallback'], [{'role': 'user', 'content': 'q'}],
                                           100, 5.0))
    assert (_text(completion), used) == ('answer', 'fallback')
    assert service.endpoints() == ['primary', 'fallback']


def test_an_empty_answer_is_a_failure_not_a_success(monkeypatch):
    service = FakeService({'primary': _ok(''), 'fallback': _ok('answer')}, monkeypatch)
    _, used = _run(complete_chain(HOST, 't', ['primary', 'fallback'], [], 100, 5.0))
    assert used == 'fallback'
    assert service.endpoints() == ['primary', 'fallback']


def test_a_refused_request_is_not_retried_on_the_same_endpoint(monkeypatch):
    service = FakeService({'primary': _status(400), 'fallback': _status(500)}, monkeypatch)
    with pytest.raises(CompareLlmUnavailable):
        _run(complete_chain(HOST, 't', ['primary', 'fallback'], [], 100, 5.0))
    assert service.endpoints().count('primary') == 1
    assert service.endpoints().count('fallback') == compare_llm.rounds()


def test_every_round_is_tried_before_the_user_message(monkeypatch):
    service = FakeService({'primary': _status(500), 'fallback': _status(503)}, monkeypatch)
    with pytest.raises(CompareLlmUnavailable) as info:
        _run(complete_chain(HOST, 't', ['primary', 'fallback'], [], 100, 5.0))
    assert len(service.calls) == 2 * compare_llm.rounds()
    assert 'every endpoint failed' in info.value.detail


def test_an_accept_rejection_moves_on_to_the_next_endpoint(monkeypatch):
    service = FakeService({'primary': _ok('not json'), 'fallback': _ok('{"a": 1}')}, monkeypatch)

    def accept(completion):
        text = _text(completion)
        if not text.startswith('{'):
            raise LlmFailure('empty', 'not a JSON object')
        return json.loads(text)

    value, used = _run(complete_chain(HOST, 't', ['primary', 'fallback'], [], 100, 5.0, accept=accept))
    assert (value, used) == ({'a': 1}, 'fallback')


def test_summary_falls_back_to_the_other_luna_model(monkeypatch):
    FakeService({GPT_5_6_LUNA: _status(500), GPT_6_LUNA: _ok('résumé du document')}, monkeypatch)
    result = _run(summarize_text(HOST, 't', GPT_5_6_LUNA, 'text of the document', max_chars=1000, max_tokens=100))
    assert result['summary'] == 'résumé du document'
    assert result['endpoint'] == GPT_6_LUNA


def test_summary_with_no_text_from_any_endpoint_is_an_error(monkeypatch):
    FakeService({GPT_5_6_LUNA: _ok(''), GPT_6_LUNA: _ok('')}, monkeypatch)
    with pytest.raises(CompareLlmUnavailable):
        _run(summarize_text(HOST, 't', GPT_5_6_LUNA, 'text', max_chars=1000, max_tokens=100))


def test_judge_falls_back_and_returns_the_judgment(monkeypatch):
    judgment = '{"impacted": false, "confidence": "high", "reason": "r", "passages": []}'
    FakeService({GPT_5_6_LUNA: _status(429), GPT_6_LUNA: _ok(judgment)}, monkeypatch)
    cand = {'ref': 'PR-1', 'title': 't', 'division': 'AS', 'doc_date': '',
            'passages': [{'header': 'H', 'change_ids': ['C1'], 'text': 'apply 35 Nm'}]}
    res = _run(_judge(HOST, 't', GPT_5_6_LUNA, 'CHANGES', cand, 100))
    assert res['judgment']['impacted'] is False
    assert res['endpoint'] == GPT_6_LUNA


def test_judge_that_every_endpoint_fails_is_a_user_message_with_the_cause(monkeypatch):
    service = FakeService({GPT_5_6_LUNA: _status(500), GPT_6_LUNA: _status(500)}, monkeypatch)
    monkeypatch.setattr(vector_search, '_changes_block', lambda *a, **k: 'CHANGES')

    async def fake_fetch(host, token, index_name, queries, num_results):
        chunks = [{'chunk_id': 'D1-0', 'IDDOC': '1', 'REF': 'PR-100', 'division': '', 'url': '',
                   'semantic_headers': '', 'chunk_text': 'apply 35 Nm', 'score': 1.0, '_change_ids': ['C1']}]
        return {'chunks': chunks, 'queries_failed': 0}

    monkeypatch.setattr(vector_search, '_fetch_chunks_multi', fake_fetch)

    async def collect():
        stream = run_impact_search(
            host=HOST, token='t', index_name='i', llm_endpoint=GPT_5_6_LUNA,
            extracted={'changes': [{'id': 'C1', 'text': 'x'}], 'source': 'structured',
                       'queries': [{'text': 'q', 'change_ids': ['C1']}]},
            num_results=5, max_candidates=1, max_changes_chars=1000, max_tokens=100,
            archive_before='', exclude_names=[],
        )
        return [e async for e in stream]

    events = _run(collect())
    doc = next(e['document'] for e in events if e['type'] == 'document')
    assert doc['status'] == 'error'
    assert doc['reason'] == TIRED_MESSAGE
    assert doc['error_type'] == 'CompareLlmUnavailable'
    assert 'every endpoint failed' in doc['error_detail']
    assert len(service.calls) == 2 * compare_llm.rounds()


# --- Streamed analysis ---

def test_analysis_falls_back_and_streams_the_answer(monkeypatch):
    service = FakeService({CLAUDE: _status(429), GPT_5_6_LUNA: _sse('Hello ', 'world.')}, monkeypatch)
    events = _events(_run(_collect(stream_analysis(HOST, 't', CLAUDE, [{'role': 'user', 'content': 'q'}],
                                                    100, 4000, 0.0))))
    deltas = [e['delta'] for e in events if e['type'] == 'response.output_text.delta']
    assert deltas == ['Hello ', 'world.']
    assert not [e for e in events if e['type'] == 'error']
    assert [e['type'] for e in events if e['type'] == 'usage']
    claude_payload, fallback_payload = service.calls[0][1], service.calls[1][1]
    assert 'thinking' in claude_payload and 'temperature' in claude_payload
    assert 'thinking' not in fallback_payload and 'temperature' not in fallback_payload


def test_analysis_that_every_endpoint_fails_sends_the_user_message(monkeypatch):
    service = FakeService({CLAUDE: _status(503), GPT_5_6_LUNA: _status(503)}, monkeypatch)
    chunks = _run(_collect(stream_analysis(HOST, 't', CLAUDE, [], 100, 0, 0.0)))
    events = _events(chunks)
    assert chunks[-1] == 'data: [DONE]\n\n'
    assert not [e for e in events if e['type'] == 'response.output_text.delta']
    error = next(e for e in events if e['type'] == 'error')
    assert error['error'] == TIRED_MESSAGE
    assert error['detail'] and 'every endpoint failed' in error['detail']
    assert len(service.calls) == 2 * compare_llm.rounds()


def test_a_cut_analysis_continues_on_the_fallback(monkeypatch):
    service = FakeService({CLAUDE: _sse('Part one. ', error=True), GPT_5_6_LUNA: _sse('two.')}, monkeypatch)
    events = _events(_run(_collect(stream_analysis(HOST, 't', CLAUDE, [{'role': 'user', 'content': 'q'}],
                                                    100, 0, 0.0))))
    deltas = [e['delta'] for e in events if e['type'] == 'response.output_text.delta']
    assert deltas == ['Part one. ', 'two.']
    assert not [e for e in events if e['type'] == 'error']
    continuation = service.calls[1][1]['messages']
    assert continuation[-2] == {'role': 'assistant', 'content': 'Part one. '}
    assert continuation[-1] == {'role': 'user', 'content': CONTINUE_PROMPT}


def test_error_event_detail_is_dropped_before_the_browser(monkeypatch):
    from server.routers.compare.analyze import _public_error_chunk
    event = {'type': 'error', 'error': TIRED_MESSAGE, 'error_type': 'LLMUnavailable', 'detail': 'every endpoint failed'}
    public = json.loads(_public_error_chunk(event)[6:])
    assert 'detail' not in public and public['error'] == TIRED_MESSAGE
