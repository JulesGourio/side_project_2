"""Exact code lookup: codes of a question, passages citing them (Lakebase chunk_codes), merged into the turn's passages."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from server.services.chat import engine
from server.services.chat import llm as chat_llm
from server.services.chat.engine import codes
from server.services.chat.turn_log import TurnLog

PIPELINE_CODES = os.path.join(os.path.dirname(__file__), '..', 'utils', 'parsing_pipeline', 'codes.py')


def _pipeline_constants():
    """The pattern and bounds of the parsing pipeline's codes.py, read without importing pyspark."""
    out = {}
    with open(PIPELINE_CODES, encoding='utf-8') as fh:
        for line in fh:
            name = line.split('=')[0].strip()
            if name in ('CODE_PATTERN', 'CODE_MIN_CHARS', 'CODE_MAX_CHARS'):
                out[name] = eval(line.split('=', 1)[1].strip())   # noqa: S307 — a literal of this repository
    return out


def test_the_pipeline_extracts_codes_the_way_the_chat_finds_them():
    found = _pipeline_constants()
    assert found == {'CODE_PATTERN': codes.CODE_PATTERN, 'CODE_MIN_CHARS': codes.CODE_MIN_CHARS,
                     'CODE_MAX_CHARS': codes.CODE_MAX_CHARS}


def test_codes_of_a_question():
    assert codes.codes_in('Dans quel document se trouve SF-1027 ?') == ['SF-1027']
    assert codes.codes_in('sf-1027, AS9102 et Q0102QP_GB (rev 2024-10)') == ['SF-1027', 'AS9102', 'Q0102QP_GB']
    assert codes.codes_in('le suivi post-production de la pièce') == []                  # no digit
    assert codes.codes_in('la norme AS9102 et as9102') == ['AS9102']                     # once
    assert codes.codes_in('QP-1176 cite SF-1027', exclude=['qp-1176']) == ['SF-1027']   # REF lookup covers it


def test_rarest_code_first_and_capped(monkeypatch):
    rows = [{'code': 'AS9102', 'chunk_ids': [f'x{i}' for i in range(20)]}, {'code': 'SF-1027', 'chunk_ids': ['a', 'b']}]
    conn = MagicMock(fetch=AsyncMock(return_value=rows))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(codes, 'get_pool', lambda: pool)
    ids = asyncio.run(codes.chunk_ids_citing(['SF-1027', 'AS9102']))
    assert ids[:2] == ['a', 'b'] and len(ids) == codes.CODE_LOOKUP_K


def test_no_lakebase_no_lookup(monkeypatch):
    monkeypatch.setattr(codes, 'get_pool', lambda: None)
    assert asyncio.run(codes.chunk_ids_citing(['SF-1027'])) == []


def _row(cid, ref):
    return {'chunk_id': cid, 'IDDOC': 1, 'REF': ref, 'division': 'AS', 'url': f'https://intraqual/{ref}',
            'semantic_headers': '', 'chunk_text': 'passage'}


def test_passages_citing_the_code_reach_the_answer(monkeypatch):
    cols = ['chunk_id', 'IDDOC', 'REF', 'division', 'url', 'semantic_headers', 'chunk_text', 'score']
    sent = []

    async def _post(self, url, json=None, headers=None, timeout=None):
        sent.append(json)
        f = __import__('json').loads(json.get('filters_json') or '{}')
        rows = [_row('q1', 'Q0102QP_GB')] if f.get('chunk_id') == ['q1'] else [_row('s1', 'SF-1152'), _row('m1', 'MM-1027')]
        body = {'manifest': {'columns': [{'name': c} for c in cols]},
                'result': {'data_array': [[r.get(c, 0.5) for c in cols] for r in rows]}}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    monkeypatch.setenv('CHAT_INDEX', 'cat.sch.chunks_index')
    log = TurnLog()
    question = 'Dans quel document se trouve SF-1027 ?'
    with (patch.object(chat_llm, 'complete', AsyncMock(return_value=('FR: SF-1027\nEN: SF-1027', 'x'))),
          patch.object(engine.retrieval, 'refs_named_in', lambda t: []),
          patch.object(engine.retrieval, 'chunk_ids_citing', AsyncMock(return_value=['q1']))):
        found = asyncio.run(engine.retrieve_for_turn('https://h', 't', 'ALL', [{'role': 'user', 'content': question}],
                                                     log=log))
    refs = [r['REF'] for r in found['rows']]
    assert 'Q0102QP_GB' in refs[:2]
    assert log.data['named_codes'] == ['SF-1027'] and log.data['code_passages'] == 1
    assert {p['chunk_id']: p['source'] for p in log.passages}['q1'] == 'code_lookup'
    assert 'code_lookup' in log.timings_ms


def test_a_failed_code_lookup_is_a_warning_not_a_failed_turn(monkeypatch):
    log = TurnLog()
    with patch.object(engine.retrieval, 'chunk_ids_citing', AsyncMock(side_effect=OSError('down'))):
        rows = asyncio.run(engine.retrieval._passages_citing('https://h', 't', 'i', 'q SF-1027', ['SF-1027'], {}, log))
    assert rows == [] and log.codes == ['code_lookup_failed']
