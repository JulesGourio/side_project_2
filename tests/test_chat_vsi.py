"""Tests for the chat engine (server/services/chat_vsi.py).

Coverage:
  - CitationStreamParser: marker split across chunks, grouped markers, unknown number,
    '[' that is not a marker, no citation, incomplete marker at end of stream; equivalence
    with the whole-text parse on the 21 real golden answers (tests/fixtures/chat_vsi_raw_answers.json)
  - search: three queries x (reranked 12 + raw 10) merged by rank, division filter, partial
    failures tolerated, reranker refused -> raw only, REF lookup first, title lookup appended,
    one language per document
  - stream_chat_vsi contract: deltas without markers, sources + citations, one metadata event
    before [DONE], error events; prompt: instructions + answering rules + language reminder
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
# The engine — Vector Search faked at the HTTP level
# ---------------------------------------------------------------------------

QUESTION = 'Quelles procédures parlent de qualification CND ?'
MESSAGES = [{'role': 'user', 'content': f'[Date: 2026-10-06]\n\n{QUESTION}'}]
COLS = ['chunk_id', 'IDDOC', 'REF', 'division', 'url', 'semantic_headers', 'chunk_text']


def _row(cid, ref, text='passage', division='AS'):
    return {'chunk_id': cid, 'IDDOC': 1, 'REF': ref, 'division': division, 'url': f'https://intraqual/{ref}',
            'semantic_headers': '', 'chunk_text': text}


def _vs(answer, sent):
    """Fake httpx post: answer(payload) -> list of rows or an int status."""
    async def _post(self, url, json=None, headers=None):
        sent.append(json)
        out = answer(json)
        if isinstance(out, int):
            return httpx.Response(out, text='error' if out != 400 else 'reranker not enabled',
                                  request=httpx.Request('POST', url))
        body = {'manifest': {'columns': [{'name': c} for c in COLS]},
                'result': {'data_array': [[r[c] for c in COLS] for r in out]}}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    return _post


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


DEFAULT_ROWS = [_row('c1', 'QP-1518', 'passage 1'), _row('c2', 'MR-1465'), _row('c3', 'QP-1518', 'passage 2')]


def _run(monkeypatch, messages=MESSAGES, division='ALL', answer=None, rewrite='FR: qualification CND\nEN: NDT qualification',
         llm=None, named=(), titled=(), language=None):
    sent = []
    monkeypatch.setattr(httpx.AsyncClient, 'post', _vs(answer or (lambda p: DEFAULT_ROWS), sent))
    monkeypatch.setattr(chat_vsi.asyncio, 'sleep', AsyncMock())
    monkeypatch.setenv('CHAT_VSI_INDEX', 'cat.sch.chunks_index')
    rewrite_mock = AsyncMock(side_effect=rewrite) if isinstance(rewrite, BaseException) else \
        AsyncMock(return_value=(rewrite, 'databricks-gpt-6-luna'))
    captured = {}

    def _llm_spy(*args, **kwargs):
        captured['prompt'] = args[3]
        captured['chain'] = args[2]
        return (llm or _llm_stream('QP-1518 est la procédure [', '1', '] de référence.'))(*args, **kwargs)

    async def _collect():
        return [c async for c in chat_vsi.stream_chat_vsi('https://host', 'tok', division, messages, language)]

    with (patch.object(chat_vsi.chat_vsi_llm, 'complete', rewrite_mock),
          patch.object(chat_vsi.chat_vsi_llm, 'stream_answer', side_effect=_llm_spy),
          patch.object(chat_vsi, 'refs_named_in', lambda text: list(named) if QUESTION in text else []),
          patch.object(chat_vsi, 'documents_titled', lambda texts, limit: list(titled))):
        raw = asyncio.run(_collect())
    events = []
    for chunk in raw:
        if chunk.startswith('data: '):
            data = chunk[6:].strip()
            events.append('[DONE]' if data == '[DONE]' else json.loads(data))
    return events, {'sent': sent, 'rewrite': rewrite_mock, 'prompt': captured.get('prompt'), 'chain': captured.get('chain')}


def _types(events):
    return [e if e == '[DONE]' else e['type'] for e in events]


def test_happy_path_contract(monkeypatch):
    events, _ = _run(monkeypatch)
    deltas = [e['delta'] for e in events if e != '[DONE]' and e['type'] == 'response.output_text.delta']
    assert ''.join(deltas) == 'QP-1518 est la procédure  de référence.'
    assert not any(_VISIBLE_MARKER.search(d) or d.endswith('[') for d in deltas)
    sources = next(e for e in events if e != '[DONE]' and e['type'] == 'sources')
    assert sources['sources'] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'doc_uri': 'https://intraqual/QP-1518'}]
    assert _types(events)[-3:] == ['sources', 'metadata', '[DONE]']
    meta = next(e for e in events if e != '[DONE]' and e['type'] == 'metadata')
    assert meta['trace_id'].startswith('vsi-') and meta['tool_query'] == 'qualification CND'
    assert meta['tool_result'] == 'QP-1518, MR-1465'


def test_three_queries_reranked_and_raw(monkeypatch):
    _, m = _run(monkeypatch)
    texts = [p['query_text'] for p in m['sent']]
    assert sorted(set(texts)) == sorted([QUESTION, 'qualification CND', 'NDT qualification'])
    reranked = [p for p in m['sent'] if 'reranker' in p]
    raw = [p for p in m['sent'] if 'reranker' not in p]
    assert len(reranked) == 3 and all(p['num_results'] == 12 for p in reranked)
    assert reranked[0]['reranker']['parameters']['columns_to_rerank'] == ['REF', 'semantic_headers', 'chunk_text']
    assert len(raw) == 3 and all(p['num_results'] == 10 and p['query_type'] == 'HYBRID' for p in raw)
    assert all('filters_json' not in p for p in m['sent'])                       # ALL: no division filter


def test_division_is_a_filter_on_the_single_index(monkeypatch):
    _, m = _run(monkeypatch, division='is')
    assert all(json.loads(p['filters_json']) == {'division': ['IS']} for p in m['sent'])
    assert chat_vsi.division_filter('XX') == {} and chat_vsi.division_filter(None) == {}


def test_union_merges_by_best_rank(monkeypatch):
    def answer(p):
        if 'reranker' in p:
            return [_row('r1', 'A-1'), _row('both', 'B-1')]
        return [_row('both', 'B-1'), _row('raw2', 'C-1')]
    found = {}

    async def _go():
        found.update(await chat_vsi.retrieve_for_turn('https://h', 't', 'ALL', [{'role': 'user', 'content': QUESTION}]))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _vs(answer, []))
    with (patch.object(chat_vsi.chat_vsi_llm, 'complete', AsyncMock(return_value=('FR: q', 'x'))),
          patch.object(chat_vsi, 'refs_named_in', lambda t: []), patch.object(chat_vsi, 'documents_titled', lambda t, l: [])):
        asyncio.run(_go())
    assert [r['chunk_id'] for r in found['rows']] == ['r1', 'both', 'raw2']


def test_failing_queries_are_dropped_when_others_answer(monkeypatch):
    events, _ = _run(monkeypatch, answer=lambda p: 503 if 'reranker' in p else DEFAULT_ROWS)
    assert 'error' not in _types(events) and _types(events)[-1] == '[DONE]'


def test_reranker_refused_gives_raw_search(monkeypatch):
    events, m = _run(monkeypatch, answer=lambda p: 400 if 'reranker' in p else DEFAULT_ROWS)
    assert 'error' not in _types(events)
    assert len([p for p in m['sent'] if 'reranker' in p]) >= 1


def test_reranked_only_sends_no_raw_query(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_RAW_TOP_K', '0')
    monkeypatch.setenv('CHAT_VSI_RERANK_TOP_K', '8')
    events, m = _run(monkeypatch)
    assert 'error' not in _types(events)
    assert len(m['sent']) == 3 and all('reranker' in p and p['num_results'] == 8 for p in m['sent'])


def test_raw_search_only_on_the_first_queries(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_RAW_ON', 'fr')
    _, m = _run(monkeypatch)
    raw = [p['query_text'] for p in m['sent'] if 'reranker' not in p]
    assert raw == ['qualification CND'] and len([p for p in m['sent'] if 'reranker' in p]) == 3


def test_reranked_only_with_reranker_refused_falls_back_to_raw(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_RAW_TOP_K', '0')
    events, m = _run(monkeypatch, answer=lambda p: 400 if 'reranker' in p else DEFAULT_ROWS)
    assert 'error' not in _types(events)
    assert [p['num_results'] for p in m['sent'] if 'reranker' not in p] and \
        all(p['num_results'] == 12 for p in m['sent'] if 'reranker' not in p)


def test_cap_keeps_the_best_ranked_search_passages(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_MAX_SEARCH_PASSAGES', '2')
    found = {}

    async def _go():
        found.update(await chat_vsi.retrieve_for_turn('https://h', 't', 'ALL', [{'role': 'user', 'content': QUESTION}]))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _vs(lambda p: [_row('a', 'A-1'), _row('b', 'B-1'), _row('c', 'C-1')], []))
    with (patch.object(chat_vsi.chat_vsi_llm, 'complete', AsyncMock(return_value=('FR: q', 'x'))),
          patch.object(chat_vsi, 'refs_named_in', lambda t: []), patch.object(chat_vsi, 'documents_titled', lambda t, l: [])):
        asyncio.run(_go())
    assert [r['chunk_id'] for r in found['rows']] == ['a', 'b']


def test_vector_search_down_is_an_error_event(monkeypatch):
    events, _ = _run(monkeypatch, answer=lambda p: 403)
    assert _types(events) == ['error', '[DONE]']
    assert events[0]['error_type'] == 'VectorSearchError' and events[0]['http_status'] == 403


def test_named_documents_first_titles_last_one_language(monkeypatch):
    def answer(p):
        f = json.loads(p.get('filters_json') or '{}')
        if f.get('REF') == ['MI-14242']:
            return [_row('n1', 'MI-14242', 'slide 15')]
        if f.get('REF') == ['IQ22-223']:
            return [_row('t1', 'IQ22-223', 'stocker')]
        return [_row('c1', 'Q0102QP_GB'), _row('c2', 'Q0102QP_BG'), _row('c3', 'QP-1518')]
    events, m = _run(monkeypatch, answer=answer, named=['MI-14242'], titled=[('IQ22-223', ['IQ22-223'], 2.0)])
    user_turn = m['prompt'][-1]['content']
    order = [ref for ref in ('MI-14242', 'Q0102QP_GB', 'QP-1518', 'IQ22-223') if f'Document {ref}' in user_turn]
    assert order == ['MI-14242', 'Q0102QP_GB', 'QP-1518', 'IQ22-223']
    assert 'Document Q0102QP_BG' not in user_turn                                   # one language per document
    meta = next(e for e in events if e != '[DONE]' and e['type'] == 'metadata')
    assert 'ref_lookup(MI-14242)' in meta['tool_name'] and 'title_lookup(IQ22-223)' in meta['tool_name']


def test_prompt_rules_and_language(monkeypatch):
    _, m = _run(monkeypatch, language='Spanish')
    system = m['prompt'][0]['content']
    assert system.startswith(chat_vsi.load_instructions('ALL')[:200])
    assert '# Answering rules' in system and '# How to cite (mandatory)' in system and 'quote it exactly' in system
    assert m['prompt'][-1]['content'].endswith('write your whole answer in Spanish, the language of the question above '
                                               '(not the language of the documents).')
    assert '[1] Document QP-1518\npassage 1\n\npassage 2' in m['prompt'][-1]['content']
    assert m['chain'][0] == 'databricks-gpt-6-luna'


def test_generic_language_reminder_without_language(monkeypatch):
    _, m = _run(monkeypatch)
    assert m['prompt'][-1]['content'].endswith(chat_vsi.LANGUAGE_REMINDER)


def test_history_markers_stripped_and_date_kept_out_of_search(monkeypatch):
    messages = [{'role': 'user', 'content': 'Première question'},
                {'role': 'assistant', 'content': 'Réponse citant QP-1518⟦1⟧.'},
                {'role': 'user', 'content': f'[Date: 2026-10-06]\n\n{QUESTION}'}]
    _, m = _run(monkeypatch, messages=messages)
    transcript = m['rewrite'].await_args.args[3][1]['content']
    assert '⟦' not in transcript and '[Date:' not in transcript
    assert all('[Date:' not in p['query_text'] for p in m['sent'])
    assert f'[Date: 2026-10-06]\n\n{QUESTION}' in m['prompt'][-1]['content']


def test_rewrite_failure_searches_with_the_question_only(monkeypatch):
    events, m = _run(monkeypatch, rewrite=RuntimeError('all rewrite models down'))
    assert {p['query_text'] for p in m['sent']} == {QUESTION}
    assert 'error' not in _types(events)


def test_llm_error_mid_stream_emits_no_partial_marker(monkeypatch):
    events, _ = _run(monkeypatch, llm=_llm_stream('Début de réponse [', '1', error='down'))
    assert _types(events) == ['response.output_text.delta', 'error', '[DONE]']
    assert events[0]['delta'] == 'Début de réponse '


def test_last_message_must_be_user(monkeypatch):
    events, _ = _run(monkeypatch, messages=[{'role': 'assistant', 'content': 'Bonjour'}])
    assert _types(events) == ['error', '[DONE]'] and events[0]['error_type'] == 'InputError'


def test_split_bilingual():
    assert chat_vsi.split_bilingual('FR: requête\nEN: query') == ('requête', 'query')
    assert chat_vsi.split_bilingual('juste une requête') == ('juste une requête', '')


def test_instructions_per_division():
    for division in ('ALL', 'AS', 'IS'):
        text = chat_vsi.load_instructions(division)
        assert len(text) > 6000 and '# Answering rules' in text and 'NF, FDAQL' in text
    assert 'Reference ordering (AS)' in chat_vsi.load_instructions('AS')
    assert 'Reference ordering (AS)' not in chat_vsi.load_instructions('IS')


def test_settings_defaults(monkeypatch):
    for name in ('CHAT_VSI_INDEX', 'CHAT_VSI_LLM_ENDPOINT', 'CHAT_VSI_REWRITE_ENDPOINT', 'CHAT_VSI_ANSWER_MAX_TOKENS'):
        monkeypatch.delenv(name, raising=False)
    s = chat_vsi.settings()
    assert s['index'] == 'dev_landingzone.qualibot.chunks_index' and s['llm'] == 'databricks-gpt-6-luna'
    assert s['rewrite_llm'] == 'databricks-gpt-6-luna' and s['answer_max_tokens'] == 8000
    assert s['rerank_top_k'] == 12 and s['raw_top_k'] == 10 and s['max_search_passages'] == 0
