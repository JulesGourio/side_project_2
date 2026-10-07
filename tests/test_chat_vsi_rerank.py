"""Chat VSI variant ``rerank`` (server/services/chat_vsi_rerank.py) and the variant switch."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx

from server.services import chat_vsi, chat_vsi_rerank, chat_vsi_variants

ROW = {'chunk_id': 'c1', 'REF': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'chunk_text': 'texte'}
VS_BODY = {'manifest': {'columns': [{'name': k} for k in ROW]}, 'result': {'data_array': [list(ROW.values())]}}


def _fake_post(status=200, body=None, sent=None):
    async def _post(self, url, json=None, headers=None):
        if sent is not None:
            sent.append(json)
        if status != 200:
            return httpx.Response(status, text=body or 'error', request=httpx.Request('POST', url))
        return httpx.Response(200, json=VS_BODY, request=httpx.Request('POST', url))
    return _post


def test_query_asks_the_reranker(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(sent=sent))
    monkeypatch.delenv('CHAT_VSI_RERANK_TOP_K', raising=False)
    monkeypatch.delenv('CHAT_VSI_RERANK_COLUMNS', raising=False)
    rows, reranked = asyncio.run(chat_vsi_rerank.retrieve_reranked('https://h', 't', 'cat.sch.idx', ['q1', 'q2'], 12))
    assert reranked and [r['chunk_id'] for r in rows] == ['c1']
    assert len(sent) == 2
    for p in sent:
        assert p['query_type'] == 'HYBRID' and p['num_results'] == 12
        assert p['reranker'] == {'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': ['chunk_text']}}


def test_reranker_refused_falls_back_to_baseline(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(status=400, body='Invalid parameter: reranker'))
    baseline = AsyncMock(return_value=[ROW])
    with patch.object(chat_vsi, 'retrieve', baseline):
        rows, reranked = asyncio.run(chat_vsi_rerank.retrieve_reranked('https://h', 't', 'idx', ['q'], 12))
    assert not reranked and rows == [ROW]
    baseline.assert_awaited_once()


def test_other_vector_search_error_is_an_error_event(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(status=403, body='forbidden'))
    try:
        asyncio.run(chat_vsi_rerank.retrieve_reranked('https://h', 't', 'idx', ['q'], 12))
    except chat_vsi.ChatVsiError as exc:
        assert exc.http_status == 403
    else:
        raise AssertionError('expected ChatVsiError')


def _llm(*deltas):
    async def _gen(*args, **kwargs):
        for d in deltas:
            yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": d})}\n\n'
        yield 'data: [DONE]\n\n'
    return _gen


def test_stream_contract_same_as_baseline():
    messages = [{'role': 'user', 'content': '[Date: 2026-10-07]\n\nQui qualifie le personnel CND ?'}]
    with (patch.object(chat_vsi_rerank, 'retrieve_reranked', AsyncMock(return_value=([ROW], True))),
          patch.object(chat_vsi_rerank, 'search_query_fr', AsyncMock(return_value='qualification personnel CND')),
          patch.object(chat_vsi_rerank, 'stream_analysis', side_effect=_llm('QP-1518 [', '1] fait foi.'))):
        raw = asyncio.run(_collect(chat_vsi_rerank.stream_chat_vsi_rerank('https://h', 't', 'ALL', messages)))
    events = [json.loads(c[6:]) for c in raw if c.startswith('data: ') and '[DONE]' not in c]
    text = ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta')
    assert text == 'QP-1518  fait foi.'
    src = next(e for e in events if e['type'] == 'sources')
    assert src['sources'][0]['title'] == 'QP-1518' and src['citations'] == [{'n': 1, 'pos': len('QP-1518 ')}]
    meta = next(e for e in events if e['type'] == 'metadata')
    assert meta['tool_name'] == 'vector_search+rerank' and meta['trace_id'].startswith('vsi-rerank-')
    assert raw[-1] == 'data: [DONE]\n\n'


async def _collect(gen):
    return [c async for c in gen]


def test_variant_switch(monkeypatch):
    monkeypatch.delenv('CHAT_VSI_VARIANT', raising=False)
    assert chat_vsi_variants.vsi_variant() == 'baseline'
    monkeypatch.setenv('CHAT_VSI_VARIANT', 'rerank')
    assert chat_vsi_variants.vsi_variant() == 'rerank'
    assert chat_vsi_variants.variant_settings()['reranker'] == 'databricks_reranker'
    monkeypatch.setenv('CHAT_VSI_VARIANT', 'nope')
    assert chat_vsi_variants.vsi_variant() == 'baseline'


def test_cap_per_document_keeps_best_ranked():
    rows = [{'chunk_id': str(i), 'REF': ref} for i, ref in enumerate(['A', 'A', 'B', 'A', 'C', 'B', 'B'])]
    kept = chat_vsi_rerank.cap_per_document(rows, 2)
    assert [(r['chunk_id'], r['REF']) for r in kept] == [('0', 'A'), ('1', 'A'), ('2', 'B'), ('4', 'C'), ('5', 'B')]
    assert chat_vsi_rerank.cap_per_document(rows, 0) == rows


def test_union_merges_reranked_and_raw_search(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_RERANK_MERGE', 'union')
    monkeypatch.setenv('CHAT_VSI_MAX_PASSAGES_PER_DOC', '1')
    reranked = [{'chunk_id': 'r1', 'REF': 'NF-10065', 'url': 'u1', 'chunk_text': 'a'},
                {'chunk_id': 'r2', 'REF': 'NF-10065', 'url': 'u1', 'chunk_text': 'b'}]
    raw = [{'chunk_id': 'b1', 'REF': 'MR-1465', 'url': 'u2', 'chunk_text': 'c'}]
    captured = {}

    def _llm_spy(*args, **kwargs):
        captured['prompt'] = args[3]
        return _llm('ok')(*args, **kwargs)
    messages = [{'role': 'user', 'content': 'q'}]
    with (patch.object(chat_vsi_rerank, 'retrieve_reranked', AsyncMock(return_value=(reranked, True))),
          patch.object(chat_vsi, 'retrieve', AsyncMock(return_value=raw)),
          patch.object(chat_vsi_rerank, 'search_query_fr', AsyncMock(return_value='q fr')),
          patch.object(chat_vsi_rerank, 'stream_analysis', side_effect=_llm_spy)):
        asyncio.run(_collect(chat_vsi_rerank.stream_chat_vsi_rerank('https://h', 't', 'ALL', messages)))
    prompt = captured['prompt'][-1]['content']
    assert 'Document NF-10065' in prompt and 'Document MR-1465' in prompt
    assert '\n\nb\n\n' not in prompt          # second NF-10065 passage dropped by the cap
    s = chat_vsi_rerank.settings()
    assert s['merge'] == 'union' and s['max_passages_per_doc'] == 1


def test_fit_budget_by_size_not_count():
    rows = [{'chunk_id': 'a', 'chunk_text': 'x' * 900}, {'chunk_id': 'b', 'chunk_text': 'x' * 300},
            {'chunk_id': 'c', 'chunk_text': 'x' * 50}, {'chunk_id': 'd', 'chunk_text': 'x' * 100}]
    assert [r['chunk_id'] for r in chat_vsi_rerank.fit_budget(rows, 1000)] == ['a', 'c']
    assert [r['chunk_id'] for r in chat_vsi_rerank.fit_budget(rows, 100)] == ['a']      # best one always kept
    assert chat_vsi_rerank.fit_budget(rows, 0) == rows


def test_instructions_ka_is_the_baseline_prompt_and_v2_replaces_only_the_system_text(monkeypatch):
    from server.services import chat_vsi_prompts
    conv = [{'role': 'user', 'content': 'q'}]
    docs = [('QP-1518', {'url': 'u', 'passages': ['p']})]
    monkeypatch.delenv('CHAT_VSI_INSTRUCTIONS', raising=False)
    assert chat_vsi_prompts.build_prompt('ALL', conv, docs) == chat_vsi.build_prompt('ALL', conv, docs)
    monkeypatch.setenv('CHAT_VSI_INSTRUCTIONS', 'v2')
    v2 = chat_vsi_prompts.build_prompt('AS', conv, docs)
    assert v2[1:] == chat_vsi.build_prompt('AS', conv, docs)[1:]
    system = v2[0]['content']
    assert 'Aerostructures (AS)' in system and 'ONLY from the numbered passages' in system
    assert 'metadata' not in system and 'ARCHIVED' not in system and '[3]' in system
    assert len(system) < len(chat_vsi.load_instructions('AS')) * 0.8
    assert 'NF, FDAQL' in system


def test_refs_named_in_question_use_the_catalog():
    refs = chat_vsi_rerank.refs_named_in('Peux tu résumer le MI-14242 et le QP-1518 ?')
    assert 'MI-14242' in refs and 'QP-1518' in refs
    assert chat_vsi_rerank.refs_named_in('Quel est le processus de qualification peinture ?') == []


def test_ref_lookup_passages_come_first_and_filter_on_ref(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_REF_LOOKUP', 'on')
    monkeypatch.setenv('CHAT_VSI_RERANK_ENABLED', 'false')
    sent = []
    named_row = {'chunk_id': 'n1', 'REF': 'MI-14242', 'url': 'u', 'chunk_text': 'slide 15'}

    async def _post(self, url, json=None, headers=None):
        sent.append(json)
        body = {'manifest': {'columns': [{'name': k} for k in named_row]}, 'result': {'data_array': [list(named_row.values())]}}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    captured = {}

    def _llm_spy(*args, **kwargs):
        captured['prompt'] = args[3]
        return _llm('ok')(*args, **kwargs)
    messages = [{'role': 'user', 'content': 'résume la slide 15 du MI-14242'}]
    with (patch.object(chat_vsi, 'retrieve', AsyncMock(return_value=[ROW])) as raw,
          patch.object(chat_vsi_rerank, 'search_query_fr', AsyncMock(return_value='q fr')),
          patch.object(chat_vsi_rerank, 'stream_analysis', side_effect=_llm_spy)):
        out = asyncio.run(_collect(chat_vsi_rerank.stream_chat_vsi_rerank('https://h', 't', 'ALL', messages)))
    raw.assert_awaited_once()
    assert len(sent) == 1 and 'MI-14242' in json.loads(sent[0]['filters_json'])['REF']
    prompt = captured['prompt'][-1]['content']
    assert prompt.index('Document MI-14242') < prompt.index('Document QP-1518')
    meta = next(json.loads(c[6:]) for c in out if '"metadata"' in c)
    assert meta['tool_name'].startswith('vector_search+ref_lookup(MI-14242')


def test_instructions_v3_is_ka_plus_addendum(monkeypatch):
    from server.services import chat_vsi_prompts
    conv = [{'role': 'user', 'content': 'q'}]
    docs = [('QP-1518', {'url': 'u', 'passages': ['p']})]
    monkeypatch.setenv('CHAT_VSI_INSTRUCTIONS', 'v3')
    system = chat_vsi_prompts.build_prompt('AS', conv, docs)[0]['content']
    assert system.startswith(chat_vsi.load_instructions('AS'))
    assert 'ONLY from the numbered passages' in system and 'NF, FDAQL' in system
    assert system.endswith(chat_vsi.CITATION_RULE)


def test_metadata_carries_generation_usage():
    async def _gen(*args, **kwargs):
        yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": "ok"})}\n\n'
        yield f'data: {json.dumps({"type": "usage", "input_tokens": 12000, "output_tokens": 800, "thinking_tokens": 0, "cost_eur": 0.05})}\n\n'
        yield 'data: [DONE]\n\n'
    with (patch.object(chat_vsi_rerank, 'retrieve_reranked', AsyncMock(return_value=([ROW], True))),
          patch.object(chat_vsi_rerank, 'search_query_fr', AsyncMock(return_value='q')),
          patch.object(chat_vsi_rerank, 'stream_analysis', side_effect=_gen)):
        out = asyncio.run(_collect(chat_vsi_rerank.stream_chat_vsi_rerank('https://h', 't', 'ALL', [{'role': 'user', 'content': 'q'}])))
    meta = next(json.loads(c[6:]) for c in out if '"metadata"' in c)
    assert meta['usage'] == {'input_tokens': 12000, 'output_tokens': 800, 'thinking_tokens': 0, 'cost_eur': 0.05}


def test_claude_5_models_get_no_temperature():
    from server.services.streaming import supports_temperature
    assert supports_temperature('databricks-claude-sonnet-4-6')
    assert not supports_temperature('databricks-claude-sonnet-5-5')


def test_rewrite_reads_reasoning_model_content_and_uses_its_ceiling(monkeypatch):
    sent = []

    async def _post(self, url, json=None, headers=None):
        sent.append(json)
        body = {'choices': [{'message': {'content': [{'type': 'reasoning', 'summary': []},
                                                     {'type': 'text', 'text': 'qualification CND'}]}}]}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    monkeypatch.setenv('CHAT_VSI_REWRITE_MAX_TOKENS', '4000')
    q = asyncio.run(chat_vsi_rerank.search_query_fr('https://h', 't', 'databricks-claude-sonnet-5-5',
                                                    [{'role': 'user', 'content': 'NDT?'}]))
    assert q == 'qualification CND'
    assert sent[0]['max_tokens'] == 4000 and 'temperature' not in sent[0]


def test_answer_ceiling_is_configurable(monkeypatch):
    monkeypatch.delenv('CHAT_VSI_ANSWER_MAX_TOKENS', raising=False)
    assert chat_vsi_rerank.answer_max_tokens() == 2000
    monkeypatch.setenv('CHAT_VSI_ANSWER_MAX_TOKENS', '16000')
    assert chat_vsi_rerank.answer_max_tokens() == 16000 and chat_vsi_rerank.settings()['answer_max_tokens'] == 16000


def test_ref_lookup_reads_refs_from_earlier_turns(monkeypatch):
    monkeypatch.setenv('CHAT_VSI_REF_LOOKUP', 'on')
    monkeypatch.setenv('CHAT_VSI_RERANK_ENABLED', 'false')
    fetched = AsyncMock(return_value=[])
    messages = [{'role': 'user', 'content': 'ordre des sites P&L LEAP ?'},
                {'role': 'assistant', 'content': 'Voir MI-14242 ⟦1⟧.'},
                {'role': 'user', 'content': 'résume la slide 15'}]
    with (patch.object(chat_vsi, 'retrieve', AsyncMock(return_value=[ROW])),
          patch.object(chat_vsi_rerank, 'fetch_named_documents', fetched),
          patch.object(chat_vsi_rerank, 'search_query_fr', AsyncMock(return_value='q')),
          patch.object(chat_vsi_rerank, 'stream_analysis', side_effect=_llm('ok'))):
        asyncio.run(_collect(chat_vsi_rerank.stream_chat_vsi_rerank('https://h', 't', 'ALL', messages)))
    assert 'MI-14242' in fetched.await_args.args[4]


def test_retrieve_documents_follows_the_variant(monkeypatch):
    msgs = [{'role': 'user', 'content': '[Date: 2026-10-07]\n\nqui qualifie ?'}]
    monkeypatch.setenv('CHAT_VSI_VARIANT', 'baseline')
    with (patch.object(chat_vsi, 'search_query_fr', AsyncMock(return_value='qualif')),
          patch.object(chat_vsi, 'retrieve', AsyncMock(return_value=[ROW])) as raw):
        out = asyncio.run(chat_vsi_variants.retrieve_documents('https://h', 't', 'ALL', msgs))
    assert out['rows'] == [ROW] and out['question'] == 'qui qualifie ?'
    assert raw.await_args.args[3] == ['qui qualifie ?', 'qualif']
    monkeypatch.setenv('CHAT_VSI_VARIANT', 'rerank')
    with patch.object(chat_vsi_rerank, 'retrieve_for_turn', AsyncMock(return_value={'rows': []})) as rr:
        asyncio.run(chat_vsi_variants.retrieve_documents('https://h', 't', 'ALL', msgs))
    rr.assert_awaited_once()
