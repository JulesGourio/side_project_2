"""Tests for the Chat VSI engine (server/services/chat_vsi.py) — plan step 2.

Coverage:
  - CitationStreamParser: marker split across chunks, grouped markers, unknown number,
    '[' that is not a marker, no citation, incomplete marker at end of stream
  - equivalence: streaming parse == whole-text parse on the 21 real golden answers
    (tests/fixtures/chat_vsi_raw_answers.json), real chunking and random re-chunkings
  - stream_chat_vsi contract: deltas without markers, sources + citations, one metadata
    event before [DONE], error events (Vector Search, LLM before / during the stream)
  - settings: division -> index, env overrides, unconfigured index
  - inputs: ⟦n⟧ stripped from history, [Date: …] kept out of the search, rewrite fallback
"""

import asyncio
import json
import os
import random
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from server.services import chat_vsi
from server.services.chat_vsi import CitationStreamParser, parse_citations

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'chat_vsi_raw_answers.json')
DOCS = [(f'REF-{i}', {'url': f'https://intraqual/ref={i}'}) for i in range(1, 6)]
_VISIBLE_MARKER = re.compile(r'\[\d+\]')


def _feed_all(chunks, documents=DOCS):
    parser = CitationStreamParser(documents)
    text = ''.join(parser.feed(c) for c in chunks) + parser.flush()
    return text, parser.sources, parser.citations


# ---------------------------------------------------------------------------
# CitationStreamParser
# ---------------------------------------------------------------------------

def test_marker_split_across_chunks():
    text, sources, citations = _feed_all(['Texte [', '1', '] suite.'])
    assert text == 'Texte  suite.'
    assert sources == [{'title': 'REF-1', 'url': 'https://intraqual/ref=1', 'doc_uri': 'https://intraqual/ref=1'}]
    assert citations == [{'n': 1, 'pos': 6}]


def test_no_partial_marker_is_ever_emitted():
    parser = CitationStreamParser(DOCS)
    emitted = [parser.feed(c) for c in ['Voir ', '[', '2', '] et [3', ']', '.']] + [parser.flush()]
    assert all('[' not in e for e in emitted)
    assert ''.join(emitted) == 'Voir  et .'


def test_grouped_markers():
    text, sources, citations = _feed_all(['x [2][5].'])
    assert text == 'x .'
    assert [s['title'] for s in sources] == ['REF-2', 'REF-5']
    assert citations == [{'n': 1, 'pos': 2}, {'n': 2, 'pos': 2}]


def test_sources_numbered_by_first_appearance():
    _, sources, citations = _feed_all(['a [4] b [1] c [4].'])
    assert [s['title'] for s in sources] == ['REF-4', 'REF-1']
    assert [c['n'] for c in citations] == [1, 2, 1]


def test_unknown_document_number_is_dropped():
    text, sources, citations = _feed_all(['a [9] b [0] c'])
    assert text == 'a  b  c'
    assert sources == [] and citations == []


@pytest.mark.parametrize('chunks', [
    ['voir [la doc](https://x) et [Date: 2026] et [^1] fin'],
    ['voir [', 'la doc](https://x) et [', 'Date: 2026] et [', '^1] fin'],
    ['crochets vides [] et [', ']'],
])
def test_bracket_that_is_not_a_marker_is_kept(chunks):
    text, sources, citations = _feed_all(chunks)
    assert text == ''.join(chunks)
    assert sources == [] and citations == []


def test_no_citation():
    text, sources, citations = _feed_all(['Aucun document ', 'ne répond.'])
    assert text == 'Aucun document ne répond.'
    assert sources == [] and citations == []


def test_incomplete_marker_at_end_of_stream_is_plain_text():
    text, _, citations = _feed_all(['fin [12'])
    assert text == 'fin [12'
    assert citations == []


# ---------------------------------------------------------------------------
# Equivalence with the whole-text parse, on the real golden answers
# ---------------------------------------------------------------------------

def _fixture():
    with open(FIXTURE, encoding='utf-8') as f:
        return json.load(f)


def test_fixture_is_the_real_capture():
    data = _fixture()
    assert len(data) == 21
    assert sum(len(re.findall(r'\[\d+\]', ''.join(a['deltas']))) for a in data) > 100


@pytest.mark.parametrize('answer', _fixture(), ids=lambda a: a['question'][:40])
def test_streaming_parse_equals_whole_text_parse(answer):
    documents = [(ref, doc) for ref, doc in answer['documents']]
    raw = ''.join(answer['deltas'])
    expected = parse_citations(raw, documents)

    assert _feed_all(answer['deltas'], documents) == expected          # the real stream chunking
    rng = random.Random(raw)                                            # deterministic per answer
    for _ in range(25):                                                 # random re-chunkings, 1-char chunks included
        cuts = sorted(rng.sample(range(1, len(raw)), k=min(len(raw) - 1, rng.randint(1, 60)))) if len(raw) > 1 else []
        chunks = [raw[i:j] for i, j in zip([0] + cuts, cuts + [len(raw)])]
        assert _feed_all(chunks, documents) == expected


# ---------------------------------------------------------------------------
# stream_chat_vsi — the event contract
# ---------------------------------------------------------------------------

QUESTION = 'Quelles procédures parlent de qualification CND ?'
MESSAGES = [{'role': 'user', 'content': f'[Date: 2026-10-06]\n\n{QUESTION}'}]
CHUNKS = [
    {'chunk_id': 'c1', 'REF': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'chunk_text': '[Source: QP-1518] passage 1'},
    {'chunk_id': 'c2', 'REF': 'MR-1465', 'url': 'https://intraqual/MR-1465', 'chunk_text': '[Source: MR-1465] passage'},
    {'chunk_id': 'c3', 'REF': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'chunk_text': '[Source: QP-1518] passage 2'},
]


def _llm_stream(*deltas, error=None):
    async def _gen(*args, **kwargs):
        yield ': keepalive\n\n'
        for d in deltas:
            yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": d})}\n\n'
        if error:
            yield f'data: {json.dumps({"type": "error", "error": error, "error_type": "HTTPError", "http_status": 503})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "usage", "input_tokens": 1, "output_tokens": 1})}\n\n'
        yield 'data: [DONE]\n\n'
    return _gen


def _run(messages=MESSAGES, division='ALL', llm=None, fetch=None, rewrite=None):
    """Run stream_chat_vsi with mocked services; returns (events, mocks)."""
    fetch = fetch or AsyncMock(return_value=CHUNKS)
    rewrite = rewrite or AsyncMock(return_value='qualification du personnel CND')
    llm = llm or _llm_stream('QP-1518 est la procédure [', '1', '] de référence.')
    captured = {}

    def _llm_spy(*args, **kwargs):
        captured['prompt'] = args[3]
        return llm(*args, **kwargs)

    async def _collect():
        return [c async for c in chat_vsi.stream_chat_vsi('https://host', 'tok', division, messages)]

    with (patch('server.services.chat_vsi._fetch_chunks', fetch),
          patch('server.services.chat_vsi._complete', rewrite),
          patch('server.services.chat_vsi.stream_analysis', side_effect=_llm_spy)):
        raw = asyncio.run(_collect())
    events = []
    for chunk in raw:
        if chunk.startswith('data: '):
            data = chunk[6:].strip()
            events.append('[DONE]' if data == '[DONE]' else json.loads(data))
    return events, {'fetch': fetch, 'rewrite': rewrite, 'prompt': captured.get('prompt')}


def _types(events):
    return [e if e == '[DONE]' else e['type'] for e in events]


def test_happy_path_contract():
    events, _ = _run()
    deltas = [e['delta'] for e in events if e != '[DONE]' and e['type'] == 'response.output_text.delta']
    assert ''.join(deltas) == 'QP-1518 est la procédure  de référence.'
    assert not any(_VISIBLE_MARKER.search(d) or d.endswith('[') for d in deltas)
    sources = next(e for e in events if e != '[DONE]' and e['type'] == 'sources')
    assert sources['sources'] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'doc_uri': 'https://intraqual/QP-1518'}]
    assert sources['citations'] == [{'n': 1, 'pos': len('QP-1518 est la procédure ')}]
    assert _types(events)[-3:] == ['sources', 'metadata', '[DONE]']


def test_one_metadata_event_before_done_with_trace_id():
    events, _ = _run()
    metadata = [e for e in events if e != '[DONE]' and e['type'] == 'metadata']
    assert len(metadata) == 1
    assert metadata[0]['trace_id'].startswith('vsi-') and len(metadata[0]['trace_id']) > 4
    assert _types(events).index('metadata') == len(events) - 2
    assert metadata[0]['tool_query'] == 'qualification du personnel CND'
    assert metadata[0]['tool_result'] == 'QP-1518, MR-1465'


def test_vector_search_http_error_gives_error_then_done():
    request = httpx.Request('POST', 'https://host/api/2.0/vector-search/indexes/i/query')
    fetch = AsyncMock(side_effect=httpx.HTTPStatusError('boom', request=request,
                                                        response=httpx.Response(403, request=request, text='denied')))
    events, _ = _run(fetch=fetch)
    assert _types(events) == ['error', '[DONE]']
    assert events[0]['error_type'] == 'VectorSearchError' and events[0]['http_status'] == 403


def test_vector_search_timeout_gives_error_then_done():
    events, _ = _run(fetch=AsyncMock(side_effect=httpx.ReadTimeout('slow')))
    assert _types(events) == ['error', '[DONE]']
    assert events[0]['error_type'] == 'TimeoutError'


def test_llm_error_before_stream_gives_error_then_done():
    events, _ = _run(llm=_llm_stream(error='Chat failed. Details: endpoint returned 400'))
    assert _types(events) == ['error', '[DONE]']
    assert 'Chat failed' in events[0]['error']


def test_llm_error_mid_stream_emits_no_partial_marker():
    events, _ = _run(llm=_llm_stream('Début de réponse [', '1', error='Qualibot is tired'))
    assert _types(events) == ['response.output_text.delta', 'error', '[DONE]']
    assert events[0]['delta'] == 'Début de réponse '
    assert '[' not in events[0]['delta']


def test_last_message_must_be_user():
    events, _ = _run(messages=[{'role': 'assistant', 'content': 'Bonjour'}])
    assert _types(events) == ['error', '[DONE]']
    assert events[0]['error_type'] == 'InputError'


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('division,expected', [
    ('ALL', 'dev_landingzone.qualibot.chunks_index_v1'),
    ('AS', 'dev_landingzone.qualibot.chunks_as_index_v1'),
    ('is', 'dev_landingzone.qualibot.chunks_is_index_v1'),
    ('XX', 'dev_landingzone.qualibot.chunks_index_v1'),
    (None, 'dev_landingzone.qualibot.chunks_index_v1'),
])
def test_division_to_index_defaults(division, expected, monkeypatch):
    for d in ('ALL', 'AS', 'IS'):
        monkeypatch.delenv(f'CHAT_VSI_INDEX_{d}', raising=False)
    assert chat_vsi.index_for_division(division) == expected


def test_index_env_override(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_INDEX_AS', 'prod_landingzone.qualibot.chunks_as_index')
    assert chat_vsi.index_for_division('AS') == 'prod_landingzone.qualibot.chunks_as_index'


def test_division_routes_to_its_index(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_INDEX_IS', 'cat.sch.is_index')
    _, mocks = _run(division='IS')
    assert {call.args[2] for call in mocks['fetch'].await_args_list} == {'cat.sch.is_index'}


def test_unconfigured_index_gives_explicit_error(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_INDEX_ALL', '')
    events, mocks = _run()
    assert _types(events) == ['error', '[DONE]']
    assert events[0]['error_type'] == 'ConfigError' and 'CHAT_VSI_INDEX_ALL' in events[0]['error']
    mocks['fetch'].assert_not_awaited()


def test_instructions_loaded_per_division():
    for division in ('ALL', 'AS', 'IS'):
        text = chat_vsi.load_instructions(division)
        assert len(text) > 6000 and '▎ Note' not in text
    assert 'Reference ordering (AS)' in chat_vsi.load_instructions('AS')
    assert 'Reference ordering (AS)' not in chat_vsi.load_instructions('IS')


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def test_stored_markers_are_stripped_from_history():
    messages = [
        {'role': 'user', 'content': 'Première question'},
        {'role': 'assistant', 'content': 'Réponse citant QP-1518⟦1⟧ et MR-1465⟦2⟧.'},
        {'role': 'user', 'content': f'[Date: 2026-10-06]\n\n{QUESTION}'},
    ]
    _, mocks = _run(messages=messages)
    assert all('⟦' not in m['content'] for m in mocks['prompt'])
    transcript = mocks['rewrite'].await_args.args[3][1]['content']
    assert '⟦' not in transcript and 'Réponse citant QP-1518 et MR-1465.' in transcript


def test_date_prefix_kept_out_of_search_but_given_to_the_answer():
    _, mocks = _run()
    assert [call.args[3] for call in mocks['fetch'].await_args_list] == [QUESTION, 'qualification du personnel CND']
    assert '[Date:' not in mocks['rewrite'].await_args.args[3][1]['content']
    assert mocks['prompt'][-1]['content'].endswith(f'[Date: 2026-10-06]\n\n{QUESTION}')
    assert mocks['prompt'][0]['role'] == 'system' and '# How to cite (mandatory)' in mocks['prompt'][0]['content']


def test_documents_numbered_in_prompt():
    _, mocks = _run()
    user_turn = mocks['prompt'][-1]['content']
    assert '[1] Document QP-1518\n[Source: QP-1518] passage 1\n\n[Source: QP-1518] passage 2' in user_turn
    assert '[2] Document MR-1465' in user_turn


def test_rewrite_failure_falls_back_to_question_only():
    events, mocks = _run(rewrite=AsyncMock(side_effect=httpx.ConnectError('down')))
    assert [call.args[3] for call in mocks['fetch'].await_args_list] == [QUESTION]
    assert _types(events)[-1] == '[DONE]' and 'error' not in _types(events)


def test_retrieve_uses_the_real_vector_search_helper(monkeypatch):
    """Contract with vector_search._fetch_chunks itself (only HTTP is faked): the mocks
    above would not notice a change of its signature or return shape."""
    sent = []

    async def _post(self, url, json=None, headers=None):
        sent.append(json)
        body = {'manifest': {'columns': [{'name': 'chunk_id'}, {'name': 'REF'}, {'name': 'url'}, {'name': 'chunk_text'}]},
                'result': {'data_array': [['c1', 'QP-1518', 'https://intraqual/QP-1518', 'texte']]}}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    rows = asyncio.run(chat_vsi.retrieve('https://host', 'tok', 'cat.sch.idx', ['q' * 30000, 'requête'], 5))
    assert [r['chunk_id'] for r in rows] == ['c1']
    assert [len(p['query_text']) for p in sent] == [chat_vsi._MAX_QUERY_CHARS, len('requête')]
    assert all(p['num_results'] == 5 and p['query_type'] == 'HYBRID' for p in sent)
