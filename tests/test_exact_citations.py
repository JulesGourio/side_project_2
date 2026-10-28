"""'Which documents cite SF-1027?': the complete list from doc_references, not from the similarity search.

Coverage:
  - the codes a passage cites (its own document and its source line left out)
  - doc_references: division filter, most recent first, not loaded vs cited by none, Lakebase load
  - the question: citing words + a code, the block given to the answer model (small list, language versions, cut list)
  - a turn: the citing documents' passages are fetched and the block reaches the prompt
  - the CSV download of the full list
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils', 'parsing_pipeline'))

import code_normalization as cn  # noqa: E402
from server.services.chat.engine import references  # noqa: E402
from server.services.infra import doc_references  # noqa: E402

from tests.test_chat_engine import _dated, _run  # noqa: E402


@pytest.fixture(autouse=True)
def _references(monkeypatch):
    saved = doc_references._live
    doc_references._live = {}
    dates = {'Q0102QP_GB': '2025-11-14', 'Q0102QP_FR': '2025-11-14', 'MI-14047': '2024-02-01', 'QP-2163_IS': '2023-05-05'}
    monkeypatch.setattr(doc_references, 'document_info',
                        lambda ref: {'title': f'Title of {ref}', 'doc_date': dates[ref]} if ref in dates else {})
    yield
    doc_references._live = saved


def _load(*rows):
    doc_references.set_references([{'cited_code': c, 'citing_ref': r, 'division': d, 'passages': n}
                                   for c, r, d, n in rows])


# --- The codes a passage cites ---

def test_a_passage_cites_the_codes_of_its_body_not_its_own():
    text = '[Source: Q0102QP_GB | Title: FAI | Date de diffusion: 2025-11-14]\n\nawareness: SF-1027, see also QP-1518.'
    assert cn.cited_codes(text, 'Q0102QP_GB') == ['SF1027', 'QP1518']
    own = '[Source: SF-1027_BG | Title: FAI training]\n\nSF-1027 is sent to the managers, see DGL-1056.'
    assert cn.cited_codes(own, 'SF-1027_BG') == ['DGL1056']


# --- doc_references ---

def test_the_citing_documents_are_filtered_by_division_most_recent_first():
    _load(('SF1027', 'MI-14047', 'AS', 1), ('SF1027', 'Q0102QP_GB', 'AS', 2), ('SF1027', 'QP-2163_IS', 'IS', 1))
    assert [d['ref'] for d in doc_references.citing_documents('SF1027')] == ['Q0102QP_GB', 'MI-14047', 'QP-2163_IS']
    assert [d['ref'] for d in doc_references.citing_documents('SF1027', 'AS')] == ['Q0102QP_GB', 'MI-14047']
    assert doc_references.citing_documents('SF1027', 'AS')[0]['title'] == 'Title of Q0102QP_GB'


def test_a_shared_document_is_in_every_division():
    _load(('SF1027', 'MI-14047', 'GROUP', 1))
    assert [d['ref'] for d in doc_references.citing_documents('SF1027', 'IS')] == ['MI-14047']


def _pool(rows=None, exc=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=rows or [])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def test_the_lakebase_table_is_loaded_and_a_failed_read_keeps_it():
    rows = [{'cited_code': 'SF1027', 'citing_ref': 'Q0102QP_GB', 'division': 'AS', 'passages': 2}]
    assert asyncio.run(doc_references.refresh_from_lakebase(_pool(rows))) == 1
    assert asyncio.run(doc_references.refresh_from_lakebase(_pool(exc=RuntimeError('down')))) == 0
    assert asyncio.run(doc_references.refresh_from_lakebase(_pool([]))) == 0
    assert doc_references.loaded() and doc_references.citing_documents('SF1027')[0]['ref'] == 'Q0102QP_GB'


# --- The question and the block ---

@pytest.mark.parametrize('question, asked', [
    ('Dans quel document SF-1027 est cité ?', [('SF-1027', 'SF1027')]),
    ('Quels documents font référence à sf 1027 ou à SF1027 ?', [('sf 1027', 'SF1027')]),
    ('Which documents mention QP-1518?', [('QP-1518', 'QP1518')]),
    ('Que dit le formulaire SF-1027 ?', []),
    ('Où est cité le contrôle visuel ?', []),
])
def test_a_question_asks_for_the_citing_documents_with_a_code_and_a_citing_word(question, asked):
    assert references.cited_codes_asked(question) == asked


def test_references_not_loaded_are_never_read_as_cited_by_none():
    assert references.exact_citations([('SF-1027', 'SF1027')], 'ALL') == {}


def test_the_block_lists_every_document_and_counts_language_versions_once():
    _load(('SF1027', 'Q0102QP_GB', 'AS', 2), ('SF1027', 'Q0102QP_FR', 'AS', 1), ('SF1027', 'MI-14047', 'AS', 1))
    block = references.exact_citations_block(references.exact_citations([('SF-1027', 'SF1027')], 'ALL'), 'ALL')
    assert 'Code SF-1027: cited by 2 documents:' in block
    assert '- Q0102QP_GB "Title of Q0102QP_GB" (division AS, published 2025-11-14, 2 passages citing it; also cited ' \
           'in Q0102QP_FR)' in block
    assert '- MI-14047 ' in block
    assert block.startswith('Exact citation index')


def test_a_code_cited_by_no_document_is_said_so():
    _load(('QP1518', 'MI-14047', 'AS', 1))
    block = references.exact_citations_block(references.exact_citations([('SF-1027', 'SF1027')], 'AS'), 'AS')
    assert 'Code SF-1027: cited by no document of the base.' in block


def test_a_long_list_is_cut_with_the_breakdown_and_the_download_link():
    _load(*[('DGL1056', f'MI-{n}', 'AS' if n % 3 else 'IS', 1) for n in range(10000, 10045)])
    block = references.exact_citations_block(references.exact_citations([('DGL 1056', 'DGL1056')], 'ALL'), 'ALL')
    assert 'Code DGL 1056: cited by 45 documents (AS: 30, IS: 15). The 30 most recently published:' in block
    assert block.count('\n- MI-') == 30
    assert block.endswith('Full list (download link): /api/chat/citations/DGL1056.csv?division=ALL')


# --- A turn ---

def test_the_citing_documents_reach_the_prompt(monkeypatch):
    _load(('SF1027', 'Q0102QP_GB', 'AS', 2))
    question = 'Dans quel document SF-1027 est cité ?'

    def answer(payload):
        import json
        if (json.loads(payload.get('filters_json') or '{}')).get('REF') == ['Q0102QP_GB']:
            return [_dated('g1', 'Q0102QP_GB', '2025-11-14', 'awareness: SF-1027.')]
        return [_dated('s1', 'SF-1027_BG', '2026-07-02', 'SF-1027 FAI training')]

    _, m = _run(monkeypatch, messages=[{'role': 'user', 'content': question}], answer=answer)
    user_turn = m['prompt'][-1]['content']
    assert 'Document Q0102QP_GB' in user_turn
    assert 'Code SF-1027: cited by 1 document:\n- Q0102QP_GB' in user_turn
    log = m['log']
    assert log.data['exact_citations'] == {'SF1027': 1}
    assert 'exact_citations' in log.prompts
    assert {p['chunk_id']: p['source'] for p in log.passages}['g1'] == 'citation_lookup'


def test_a_question_that_asks_no_citation_gets_no_block(monkeypatch):
    _load(('SF1027', 'Q0102QP_GB', 'AS', 2))
    _, m = _run(monkeypatch, messages=[{'role': 'user', 'content': 'Que dit le formulaire SF-1027 ?'}])
    assert 'Exact citation index' not in m['prompt'][-1]['content']
    assert 'exact_citations' not in m['log'].data


# --- The CSV download ---

def test_the_full_list_downloads_as_csv():
    from server.routers.chat import citations
    _load(('SF1027', 'Q0102QP_GB', 'AS', 2), ('SF1027', 'QP-2163_IS', 'IS', 1))
    resp = asyncio.run(citations.citations_csv('SF-1027', division='as'))
    body = resp.body.decode('utf-8')
    assert body.startswith('﻿ref;title;division;revision;doc_date;passages')
    assert 'Q0102QP_GB;Title of Q0102QP_GB;AS;;2025-11-14;2' in body and 'QP-2163_IS' not in body
    assert 'documents_citing_SF1027.csv' in resp.headers['content-disposition']
