"""Chat VSI resilience: fallback models, retries, continuation, load limit (chat_vsi_llm.py),
Vector Search retries (chat_vsi.py) and language detection helpers (translation_bridge.py)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from server.services import chat_vsi, chat_vsi_llm, translation_bridge

PRIMARY, BACKUP = 'databricks-gpt-6-luna', 'databricks-gpt-5-6-luna'
MESSAGES = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'q'}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    chat_vsi_llm.reset_health()
    monkeypatch.setattr(chat_vsi_llm, '_ROUND_WAITS_S', (0.0, 0.0, 0.0))
    for name in ('CHAT_VSI_LLM_FALLBACK_ENDPOINTS', 'CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS', 'CHAT_VSI_LLM_ROUNDS',
                 'CHAT_VSI_FIRST_TOKEN_TIMEOUT_S', 'CHAT_VSI_MAX_CONCURRENT_ANSWERS'):
        monkeypatch.delenv(name, raising=False)
    yield
    chat_vsi_llm.reset_health()


def _sse(*texts, usage=(100, 20)):
    lines = [f'data: {json.dumps({"object": "chat.completion.chunk", "choices": [{"delta": {"content": t}}]})}\n\n'
             for t in texts]
    lines.append(f'data: {json.dumps({"object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})}\n\n')
    return ''.join(lines) + 'data: [DONE]\n\n'


def _transport(monkeypatch, routes, calls=None):
    """routes: endpoint -> httpx.Response factory (called with the request)."""
    def handler(request):
        endpoint = request.url.path.split('/')[2]
        if calls is not None:
            calls.append((endpoint, json.loads(request.content)))
        return routes[endpoint](request)
    monkeypatch.setattr(chat_vsi_llm, '_client',
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def _events(endpoints, messages=MESSAGES, max_tokens=2000):
    async def _go():
        return [c async for c in chat_vsi_llm.stream_answer('https://h', 't', endpoints, messages, max_tokens)]
    out = []
    for chunk in asyncio.run(_go()):
        if chunk.startswith('data: '):
            data = chunk[6:].strip()
            out.append('[DONE]' if data == '[DONE]' else json.loads(data))
    return out


def _text(events):
    return ''.join(e['delta'] for e in events if e != '[DONE]' and e['type'] == 'response.output_text.delta')


def _of(events, kind):
    return [e for e in events if e != '[DONE]' and e['type'] == kind]


def test_rate_limited_primary_falls_back_and_cools_down(monkeypatch):
    calls = []
    _transport(monkeypatch, {PRIMARY: lambda r: httpx.Response(429, text='quota'),
                             BACKUP: lambda r: httpx.Response(200, text=_sse('Bon', 'jour'))}, calls)
    events = _events([PRIMARY, BACKUP])
    assert _text(events) == 'Bonjour' and not _of(events, 'error') and events[-1] == '[DONE]'
    llm = _of(events, 'llm')[0]
    assert llm['endpoint'] == BACKUP and llm['fallback'] and llm['attempts'][0]['outcome'] == 'rate_limit'
    assert _of(events, 'usage')[0]['input_tokens'] == 100
    # The next turn skips the cooling primary.
    calls.clear()
    _events([PRIMARY, BACKUP])
    assert [c[0] for c in calls] == [BACKUP]


def test_reasoning_fallback_gets_a_larger_ceiling_and_no_temperature(monkeypatch):
    calls = []
    _transport(monkeypatch, {'databricks-claude-sonnet-4-6': lambda r: httpx.Response(503, text='busy'),
                             BACKUP: lambda r: httpx.Response(200, text=_sse('ok'))}, calls)
    _events(['databricks-claude-sonnet-4-6', BACKUP], max_tokens=2000)
    assert calls[0][1]['max_tokens'] == 2000 and calls[0][1]['temperature'] == 0.0
    assert calls[-1][1]['max_tokens'] == 6000 and 'temperature' not in calls[-1][1]


def test_everything_down_ends_with_one_plain_error(monkeypatch):
    calls = []
    monkeypatch.setenv('CHAT_VSI_LLM_ROUNDS', '3')
    _transport(monkeypatch, {PRIMARY: lambda r: httpx.Response(404, text='no such endpoint'),
                             BACKUP: lambda r: httpx.Response(500, text='boom')}, calls)
    events = _events([PRIMARY, BACKUP])
    errors = _of(events, 'error')
    assert len(errors) == 1 and errors[0]['error_type'] == 'LLMUnavailable' and events[-1] == '[DONE]'
    # 404 is not retried in later rounds, 500 is.
    assert [c[0] for c in calls].count(PRIMARY) == 1 and [c[0] for c in calls].count(BACKUP) == 3


def test_empty_answer_moves_to_the_backup(monkeypatch):
    _transport(monkeypatch, {PRIMARY: lambda r: httpx.Response(200, text=_sse()),
                             BACKUP: lambda r: httpx.Response(200, text=_sse('réponse'))})
    events = _events([PRIMARY, BACKUP])
    assert _text(events) == 'réponse' and _of(events, 'llm')[0]['attempts'][0]['outcome'] == 'empty'


def test_mid_stream_error_object_counts_as_a_failure(monkeypatch):
    body = (f'data: {json.dumps({"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Début "}}]})}\n\n'
            f'data: {json.dumps({"error": {"code": "429", "message": "rate limit"}})}\n\n')
    calls = []
    _transport(monkeypatch, {PRIMARY: lambda r: httpx.Response(200, text=body),
                             BACKUP: lambda r: httpx.Response(200, text=_sse('et fin.'))}, calls)
    events = _events([PRIMARY, BACKUP])
    assert _text(events) == 'Début et fin.'
    continued = calls[-1][1]['messages']
    assert continued[-2] == {'role': 'assistant', 'content': 'Début '}
    assert continued[-1]['content'] == chat_vsi_llm.CONTINUE_PROMPT
    assert _of(events, 'llm')[0]['attempts'][-1]['outcome'] == 'continued'


def test_cut_answer_is_continued_by_the_only_endpoint(monkeypatch):
    seen = []

    async def _once(host, token, endpoint, messages, max_tokens):
        seen.append(messages)
        if len(seen) == 1:
            yield 'text', 'La procédure QP-1518 '
            raise chat_vsi_llm.LlmFailure('network', 'connection reset')
        yield 'text', 'fait foi. [1]'
    monkeypatch.setattr(chat_vsi_llm, 'stream_once', _once)
    monkeypatch.setenv('CHAT_VSI_LLM_ROUNDS', '2')
    events = _events([PRIMARY])
    assert _text(events) == 'La procédure QP-1518 fait foi. [1]' and not _of(events, 'error')
    assert seen[1][-2]['content'] == 'La procédure QP-1518 '


def test_answer_that_cannot_be_finished_says_so(monkeypatch):
    async def _once(host, token, endpoint, messages, max_tokens):
        if len(messages) == len(MESSAGES):
            yield 'text', 'Début'
        raise chat_vsi_llm.LlmFailure('server', 'down', 503)
    monkeypatch.setattr(chat_vsi_llm, 'stream_once', _once)
    events = _events([PRIMARY, BACKUP])
    assert _text(events).startswith('Début') and 'interrupted' in _text(events) and not _of(events, 'error')


class _Silent(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(5)
        yield b''


def test_no_first_token_in_time_moves_on(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_FIRST_TOKEN_TIMEOUT_S', '0.2')
    _transport(monkeypatch, {PRIMARY: lambda r: httpx.Response(200, stream=_Silent()),
                             BACKUP: lambda r: httpx.Response(200, text=_sse('vite'))})
    events = _events([PRIMARY, BACKUP])
    assert _text(events) == 'vite' and _of(events, 'llm')[0]['attempts'][0]['outcome'] == 'timeout'


def test_concurrency_limit_queues_answers(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_MAX_CONCURRENT_ANSWERS', '1')
    running, peak = [0], [0]

    async def _once(host, token, endpoint, messages, max_tokens):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.05)
        running[0] -= 1
        yield 'text', 'ok'
    monkeypatch.setattr(chat_vsi_llm, 'stream_once', _once)

    async def _one():
        return [c async for c in chat_vsi_llm.stream_answer('h', 't', [PRIMARY], MESSAGES, 100)]

    async def _all():
        return await asyncio.gather(*(_one() for _ in range(3)))
    results = asyncio.run(_all())
    assert peak[0] == 1 and all(any('"ok"' in c for c in r) for r in results)


def test_chains(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_LLM_FALLBACK_ENDPOINTS', f'{BACKUP}, {PRIMARY}')
    assert chat_vsi_llm.answer_chain(PRIMARY) == [PRIMARY, BACKUP]
    assert chat_vsi_llm.rewrite_chain('databricks-claude-sonnet-4-6', PRIMARY) == [
        'databricks-claude-sonnet-4-6', PRIMARY, BACKUP]
    monkeypatch.setenv('CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS', BACKUP)
    assert chat_vsi_llm.rewrite_chain('databricks-claude-sonnet-4-6', PRIMARY) == ['databricks-claude-sonnet-4-6', BACKUP]


def test_rewrite_falls_back_when_the_first_model_returns_nothing(monkeypatch):
    sent = []

    async def _post(self, url, json=None, headers=None):
        sent.append(url)
        content = '' if PRIMARY in url else 'FR: qualification CND\nEN: NDT qualification'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    monkeypatch.setenv('CHAT_VSI_REWRITE_ENDPOINT', PRIMARY)
    monkeypatch.setenv('CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS', BACKUP)
    q = asyncio.run(chat_vsi.rewrite_queries('https://h', 't', [{'role': 'user', 'content': 'NDT?'}]))
    assert q == ('qualification CND', 'NDT qualification') and BACKUP in sent[-1]


# --- Vector Search -----------------------------------------------------------

def test_vector_search_query_retried_on_503(monkeypatch):
    statuses = [503, 200]

    async def _post(self, url, json=None, headers=None):
        return httpx.Response(statuses.pop(0), json={}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    monkeypatch.setattr(chat_vsi.asyncio, 'sleep', AsyncMock())
    resp = asyncio.run(chat_vsi._post_query('https://h', 't', 'idx', {'query_text': 'q'}))
    assert resp.status_code == 200 and not statuses


# --- Language ------------------------------------------------------------------

def test_clean_for_language_drops_codes_markers_and_quotes():
    cleaned = translation_bridge.clean_for_language('Que dit le QP-1518 [2] et MI_14242_GB sur https://x.y/z ?')
    assert 'QP' not in cleaned and '1518' not in cleaned and 'http' not in cleaned and 'Que dit le' in cleaned
    answer = 'The answer is here.\n> Le texte cité en français\n| Titre | Valeur |\nIt says « une phrase citée ».'
    cleaned = translation_bridge.clean_for_language(answer, answer=True)
    assert 'français' not in cleaned and 'Titre' not in cleaned and 'citée' not in cleaned


def test_bare_code_is_not_guessed():
    assert translation_bridge._fast_lang_guess('QP-1518') is None


def test_answer_language():
    ctx = translation_bridge.TranslationContext
    assert translation_bridge.answer_language(ctx('cs', 'Czech', True), 'q') == 'English'
    assert translation_bridge.answer_language(ctx('fr', 'fr', False), 'q') == 'French'
    assert translation_bridge.answer_language(ctx('unknown', 'x', False), 'q') is None
    with patch.object(translation_bridge, '_fast_lang_guess', return_value='es'):
        assert translation_bridge.answer_language(None, '¿Qué procedimiento?') == 'Spanish'


def test_translation_falls_back_to_the_other_model(monkeypatch):
    tried = []

    async def _endpoint(endpoint, *args, **kwargs):
        tried.append(endpoint)
        if endpoint == translation_bridge.TRANSLATE_ENDPOINT:
            raise httpx.ConnectError('down')
        return 'translated'
    monkeypatch.setattr(translation_bridge, '_call_endpoint', _endpoint)
    monkeypatch.setattr(translation_bridge, 'TRANSLATE_FALLBACK_ENDPOINTS', [PRIMARY])
    assert asyncio.run(translation_bridge._call_llm('s', 'u', 'h', 't', 100)) == 'translated'
    assert tried == [translation_bridge.TRANSLATE_ENDPOINT, PRIMARY]


def test_truncated_translation_keeps_the_original(monkeypatch):
    monkeypatch.setattr(translation_bridge, '_call_llm', AsyncMock(return_value='Corto.'))
    ctx = translation_bridge.TranslationContext('es', 'Spanish', True)
    answer = 'A long English answer. ' * 20
    assert asyncio.run(translation_bridge.translate_answer_back(answer, ctx, 'h', 't')) == answer


def test_slot_is_freed_when_the_caller_stops_at_done(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_MAX_CONCURRENT_ANSWERS', '1')

    async def _once(host, token, endpoint, messages, max_tokens):
        yield 'text', 'ok'
    monkeypatch.setattr(chat_vsi_llm, 'stream_once', _once)

    async def _go():
        async for chunk in chat_vsi_llm.stream_answer('h', 't', [PRIMARY], MESSAGES, 100):
            if chunk.startswith('data: [DONE]'):
                break
        return chat_vsi_llm._semaphore().locked()
    assert asyncio.run(_go()) is False


def test_no_claude_model_in_the_chatbot_config():
    """Decision 2026-10-08: the chatbot (answer, fallback, rewrite, translation) runs on Luna models only."""
    import json as _json
    import os as _os
    import yaml
    root = _os.path.join(_os.path.dirname(__file__), '..')
    assert 'claude' not in chat_vsi._DEFAULT_LLM_ENDPOINT
    env = {e['name']: str(e.get('value', '')) for e in yaml.safe_load(open(_os.path.join(root, 'app.yaml')))['env']}
    targets = _json.load(open(_os.path.join(root, 'utils', 'deploy', 'target_env.json')))
    for values in [env] + [v for k, v in targets.items() if isinstance(v, dict)]:
        for name, value in values.items():
            if name.startswith(('CHAT_VSI_LLM', 'CHAT_VSI_REWRITE_ENDPOINT', 'CHAT_VSI_REWRITE_FALLBACK', 'CHAT_TRANSLATE')):
                assert 'claude' not in str(value).lower(), f'{name}={value}'
